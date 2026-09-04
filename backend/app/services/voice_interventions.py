"""Gemini-authored, policy-gated speech delivery through Agora's Speak API."""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from google import genai
from sqlalchemy.orm import Session

from app.integrations.agora_conversational_ai import (
    ACTIVE_AGENT_SESSIONS,
    AGORA_AGENT,
    SpeakRequest,
)
from app.models import (
    Action,
    Claim,
    Conflict,
    Decision,
    Fact,
    Hypothesis,
    Intervention as InterventionRow,
)
from app.services.event_store import persist_transcript_chunk
from app.services.incident_state_stream import INCIDENT_STATE_BROADCASTER
from app.services.intervention_policy import (
    INTERVENTION_POLICY,
    InterventionCandidate,
    human_recently_active,
)
from app.services.observability import log_event
from app.services.provider_metrics import PROVIDER_REQUESTS
from app.services.transcript_stream import TRANSCRIPT_BROADCASTER


NO_PROVIDER_RETRIES = {"retry_options": {"attempts": 1}}
HIGH_PRIORITY_DEFAULTS = {"CONFLICT_DETECTED", "STALE_CRITICAL_CLAIM"}
SUMMARY_PATTERNS = (
    # Accept natural qualifiers such as "a concise live incident summary"
    # without treating every incidental mention of "summary" as a command.
    re.compile(r"\bgive (?:me|us) (?:a )?(?:[\w-]+\s+){0,5}summary\b", re.I),
    re.compile(r"\bsummar(?:y|ize|ise) (?:the )?(?:incident|status|situation)\b", re.I),
    re.compile(r"\bwhat(?:'s| is) (?:the )?current status\b", re.I),
)


def is_summary_request(text: str) -> bool:
    return any(pattern.search(text) for pattern in SUMMARY_PATTERNS)


def active_agent_for_channel(channel: str) -> tuple[str, dict[str, str]] | None:
    return next(
        (
            (agent_id, session)
            for agent_id, session in reversed(ACTIVE_AGENT_SESSIONS.items())
            if session.get("channel") == channel
        ),
        None,
    )


def _configured_high_priority_triggers() -> set[str]:
    raw = os.getenv(
        "INTERVENTION_HIGH_PRIORITY_TRIGGERS",
        ",".join(sorted(HIGH_PRIORITY_DEFAULTS)),
    )
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def speak_priority_for(trigger_type: str) -> str:
    is_high = trigger_type.upper() in _configured_high_priority_triggers()
    high_mode = os.getenv("INTERVENTION_HIGH_PRIORITY_MODE", "interrupt").lower()
    return "INTERRUPT" if is_high and high_mode == "interrupt" else "APPEND"


def _state_payload(db: Session) -> dict[str, Any]:
    return {
        "facts": [
            {"description": row.description, "status": row.status, "source": row.source}
            for row in db.query(Fact).order_by(Fact.timestamp.desc()).limit(20).all()
        ],
        "hypotheses": [
            {"description": row.description, "status": str(row.status), "confidence": row.confidence}
            for row in db.query(Hypothesis).limit(20).all()
        ],
        "claims": [
            {"id": row.id, "text": row.text, "status": row.status, "speaker": row.speaker}
            for row in db.query(Claim).order_by(Claim.timestamp.desc()).limit(30).all()
        ],
        "actions": [
            {"id": row.id, "task": row.task, "owner": row.owner, "status": str(row.status), "priority": row.priority}
            for row in db.query(Action).order_by(Action.created_at.desc()).limit(20).all()
        ],
        "decisions": [
            {"id": row.id, "recommendation": row.recommendation, "status": row.execution_status, "approved_by": row.approved_by}
            for row in db.query(Decision).limit(20).all()
        ],
        "conflicts": [
            {"topic": row.topic, "status": row.status, "verification": row.recommended_verification}
            for row in db.query(Conflict).filter(Conflict.status == "UNRESOLVED").limit(20).all()
        ],
    }


def _limit_tts_bytes(text: str, limit: int = 500) -> str:
    encoded = " ".join(text.replace("```", "").split()).encode("utf-8")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore").rsplit(" ", 1)[0].rstrip(".,;:") + "."


async def generate_intervention_text(
    trigger_type: str,
    db: Session,
    *,
    trigger_context: dict[str, Any] | None = None,
) -> str:
    """Generate concise speech from a fresh snapshot of current Incident State."""
    client = genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options=NO_PROVIDER_RETRIES,
    )
    prompt = {
        "trigger": trigger_type,
        "trigger_context": trigger_context or {},
        "incident_state": _state_payload(db),
    }
    PROVIDER_REQUESTS.record("gemini")
    response = await client.aio.models.generate_content(
        model=os.getenv(
            "GEMINI_INTERVENTION_MODEL",
            os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-3.5-flash-lite"),
        ),
        contents=json.dumps(prompt, default=str),
        config={
            "system_instruction": (
                "You are the ResQVoice Incident Co-pilot speaking in a live incident room. "
                "Using only the supplied Incident State and trigger context, write one concise, "
                "actionable spoken intervention. Do not use markdown, labels, or preamble. "
                "Do not invent facts. Keep it under 70 words."
            ),
            "temperature": 0.2,
        },
    )
    text = _limit_tts_bytes((response.text or "").strip())
    if not text:
        raise RuntimeError("Gemini returned an empty voice intervention")
    log_event("INTERVENTION_TEXT_GENERATED", trigger=trigger_type, text=text)
    return text


def _recent_trigger_exists(
    trigger_type: str, db: Session, related_claim_ids: list[str]
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=INTERVENTION_POLICY.cooldown_seconds
    )
    rows = (
        db.query(InterventionRow)
        .filter(InterventionRow.trigger_type == trigger_type)
        .filter(InterventionRow.created_at >= cutoff.replace(tzinfo=None))
        .filter(InterventionRow.status.in_(["PENDING", "DEFERRED", "SPOKEN"]))
        .all()
    )
    if not rows:
        return False
    related = set(related_claim_ids)
    return not related or any(related.intersection(row.related_claim_ids or []) for row in rows)


async def enqueue_generated_intervention(
    trigger_type: str,
    severity: str,
    db: Session,
    *,
    channel: str = "incident-room",
    trigger_context: dict[str, Any] | None = None,
    related_claim_ids: list[str] | None = None,
    bypass_severity_threshold: bool = False,
) -> Any | None:
    related = related_claim_ids or []
    if _recent_trigger_exists(trigger_type, db, related):
        log_event("INTERVENTION_DISMISSED", trigger=trigger_type, reason="pre_generation_cooldown")
        return None
    agent = active_agent_for_channel(channel)
    if agent is None:
        log_event("INTERVENTION_DEFERRED", trigger=trigger_type, reason="no_active_agent")
        return None
    agent_id, _ = agent
    text = await generate_intervention_text(
        trigger_type, db, trigger_context=trigger_context
    )
    priority = speak_priority_for(trigger_type)
    return INTERVENTION_POLICY.admit(
        InterventionCandidate(
            trigger_type=trigger_type,
            severity=severity,
            confidence=0.9,
            message=text,
            related_claim_ids=related,
            agent_id=agent_id,
            bypass_severity_threshold=bypass_severity_threshold,
        ),
        db,
        human_speaking=human_recently_active() and priority != "INTERRUPT",
    )


def _speaking_event(event_type: str, row: InterventionRow, **extra: Any) -> None:
    INCIDENT_STATE_BROADCASTER.publish({
        "type": event_type,
        "intervention_id": row.id,
        "trigger_type": row.trigger_type,
        "message": row.message,
        **extra,
    })


async def _emit_stop_after(row: InterventionRow, duration_seconds: float) -> None:
    await asyncio.sleep(duration_seconds)
    _speaking_event(
        "speaking_stopped", row, success=True, completion="estimated_timeout"
    )


async def speak_to_channel(agent_id: str, row: InterventionRow, db: Session) -> dict[str, Any]:
    """Send one admitted intervention and expose the attempt to logs and UI."""
    priority = speak_priority_for(row.trigger_type)
    request = SpeakRequest(
        text=row.message,
        priority=priority,
        interruptable=True,
    )
    log_event(
        "SPEAK_ATTEMPT_STARTED", agent_id=agent_id, intervention_id=row.id,
        trigger=row.trigger_type, priority=priority, text=row.message,
    )
    try:
        response = await AGORA_AGENT.speak(agent_id, request)
        log_event(
            "SPEAK_ATTEMPT_SUCCEEDED", agent_id=agent_id,
            intervention_id=row.id, trigger=row.trigger_type, text=row.message,
            raw_response=response,
        )
        _speaking_event(
            "speaking_started", row, priority=priority, success=True,
            response=response,
        )
        session = ACTIVE_AGENT_SESSIONS.get(agent_id, {})
        timestamp = datetime.now(timezone.utc).isoformat()
        event = persist_transcript_chunk(
            db, "ResQVoice AI", "AI Incident Co-Pilot", row.message, timestamp,
            speaker_uid=session.get("agent_uid", "1000"),
            participant_type="ai_agent", origin="AI_TTS",
        )
        TRANSCRIPT_BROADCASTER.publish({
            "id": event.id, "speaker": "ResQVoice AI",
            "role": "AI Incident Co-Pilot", "text": row.message,
            "timestamp": timestamp,
        })
        INTERVENTION_POLICY.mark_spoken(row.id, db)
        # REST reports when broadcast starts but has no completion callback.
        # The browser combines this guard timeout with actual RTC audio level.
        estimated = min(30.0, max(2.0, len(row.message.split()) / 2.2 + 1.5))
        asyncio.create_task(_emit_stop_after(row, estimated))
        return response
    except Exception as exc:
        log_event(
            "SPEAK_ATTEMPT_FAILED", agent_id=agent_id,
            intervention_id=row.id, trigger=row.trigger_type, text=row.message,
            error_type=type(exc).__name__, message=str(exc)[:500],
        )
        _speaking_event(
            "speaking_stopped", row, success=False,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        raise
