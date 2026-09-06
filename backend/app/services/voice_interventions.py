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
    EventLog,
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
    re.compile(r"^\s*(?:please\s+)?(?:a\s+)?(?:brief|quick|current)?\s*summary\s*[.!?]*\s*$", re.I),
    re.compile(r"\b(?:can|could) you (?:give|provide|share) (?:me|us) (?:a )?(?:[\w-]+\s+){0,5}summary\b", re.I),
    re.compile(r"\b(?:tell|update) me (?:on )?(?:the )?(?:incident|status|situation)\b", re.I),
)


def is_summary_request(text: str) -> bool:
    return any(pattern.search(text) for pattern in SUMMARY_PATTERNS)


def active_agent_for_channel(
    channel: str, db: Session | None = None,
) -> tuple[str, dict[str, str]] | None:
    active = next(
        (
            (agent_id, session)
            for agent_id, session in reversed(ACTIVE_AGENT_SESSIONS.items())
            if session.get("channel") == channel
        ),
        None,
    )
    if active is not None or db is None:
        return active

    # Render can restart the FastAPI process while Agora's cloud agent remains
    # alive. Rehydrate the process-local registry from the durable session event
    # so queued speech does not dead-end after a deployment or cold start.
    events = (
        db.query(EventLog)
        .filter(EventLog.event_type.in_([
            "AGORA_AGENT_SESSION_STARTED", "AGORA_AGENT_SESSION_STOPPED",
        ]))
        .order_by(EventLog.id.desc())
        .limit(100)
        .all()
    )
    for event in events:
        payload = event.payload or {}
        if payload.get("channel") != channel:
            continue
        if event.event_type == "AGORA_AGENT_SESSION_STOPPED":
            break
        agent_id = str(payload.get("agent_id") or "")
        if agent_id:
            session = {
                "channel": channel,
                "agent_uid": str(payload.get("agent_uid") or "1000"),
                "speaker_uid": str(payload.get("speaker_uid") or ""),
                "speaker_name": str(payload.get("speaker_name") or ""),
                "speaker_role": str(payload.get("speaker_role") or ""),
            }
            ACTIVE_AGENT_SESSIONS[agent_id] = session
            log_event("AGORA_AGENT_SESSION_RECOVERED", agent_id=agent_id, channel=channel)
            return agent_id, session

    # Compatibility recovery for sessions created before durable session events
    # were introduced. Only a still-live queued intervention is eligible.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    queued = (
        db.query(InterventionRow)
        .filter(InterventionRow.agent_id.isnot(None))
        .filter(InterventionRow.status.in_(["PENDING", "DEFERRED"]))
        .filter(InterventionRow.expires_at > now)
        .order_by(InterventionRow.created_at.desc())
        .first()
    )
    if queued and queued.agent_id:
        session = {"channel": channel, "agent_uid": "1000"}
        ACTIVE_AGENT_SESSIONS[queued.agent_id] = session
        log_event(
            "AGORA_AGENT_SESSION_RECOVERED",
            agent_id=queued.agent_id,
            channel=channel,
            source="queued_intervention",
        )
        return queued.agent_id, session
    return None


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


def _is_transient_gemini_error(exc: Exception) -> bool:
    """Retry provider saturation/rate-limit failures, but never credentials errors."""
    code = getattr(exc, "code", None)
    if code in {429, 500, 502, 503, 504}:
        return True
    message = str(exc).upper()
    return any(marker in message for marker in (
        "429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
        "HIGH DEMAND", "RATE LIMIT", "TIMEOUT",
    ))


def _fallback_intervention_text(trigger_type: str, db: Session) -> str:
    """A state-derived, spoken-safe response when Gemini is temporarily unavailable."""
    conflicts = (
        db.query(Conflict).filter(Conflict.status == "UNRESOLVED").limit(1).all()
    )
    if trigger_type == "SUMMARY_REQUEST":
        if conflicts:
            conflict = conflicts[0]
            return _limit_tts_bytes(
                f"Current incident status: {conflict.topic} is unresolved. "
                f"Next, {conflict.recommended_verification}"
            )
        claims = db.query(Claim).order_by(Claim.timestamp.desc()).limit(1).all()
        if claims:
            return _limit_tts_bytes(
                f"Current incident status: the latest reported claim is: {claims[0].text}"
            )
        return "I do not have incident claims to summarize yet."
    if conflicts:
        return _limit_tts_bytes(
            f"Attention: {conflicts[0].topic} remains unresolved. "
            f"{conflicts[0].recommended_verification}"
        )
    return "I could not generate the requested intervention right now. Please try again shortly."


def _agent_next_step_fallback(trigger_context: dict[str, Any]) -> str | None:
    remediation = trigger_context.get("remediation") or {}
    diagnosis = str(trigger_context.get("diagnosis") or "").strip()
    next_step = str(trigger_context.get("next_question") or "").strip()
    if remediation.get("status") == "SIMULATED_EXECUTED":
        return _limit_tts_bytes(
            f"I found the issue: {diagnosis} I restarted only the affected component and verified recovery. {next_step}"
        )
    tool = trigger_context.get("tool_call") or {}
    if tool:
        tool_name = str(tool.get("tool") or "monitoring").replace("_", " ")
        return _limit_tts_bytes(f"I checked {tool_name} and updated the response plan. {next_step}")
    return None


async def generate_intervention_text(
    trigger_type: str,
    db: Session,
    *,
    trigger_context: dict[str, Any] | None = None,
) -> str:
    """Generate concise speech from a fresh snapshot of current Incident State."""
    context = trigger_context or {}
    client = genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options=NO_PROVIDER_RETRIES,
    )
    prompt = {
        "trigger": trigger_type,
        "trigger_context": context,
        "incident_state": _state_payload(db),
    }
    model = os.getenv(
        "GEMINI_INTERVENTION_MODEL",
        os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-3.5-flash-lite"),
    )
    for attempt in range(3):
        try:
            PROVIDER_REQUESTS.record("gemini")
            response = await client.aio.models.generate_content(
                model=model,
                contents=json.dumps(prompt, default=str),
                config={
                    "system_instruction": (
                        "You are ResQVoice, a capable teammate speaking naturally during a live incident call. "
                        "Use short first-person sentences and conversational language. Say what you checked, "
                        "what you found, and what you already did before asking for any human input. Only use "
                        "the supplied state and context. No markdown, labels, formal preamble, or robotic wording. "
                        "Keep it to two sentences and under 45 words."
                    ),
                    "temperature": 0.2,
                },
            )
            text = _limit_tts_bytes((response.text or "").strip())
            if not text:
                raise RuntimeError("Gemini returned an empty voice intervention")
            log_event("INTERVENTION_TEXT_GENERATED", trigger=trigger_type, text=text)
            return text
        except Exception as exc:
            if not _is_transient_gemini_error(exc) or attempt == 2:
                if _is_transient_gemini_error(exc):
                    fallback = (
                        _agent_next_step_fallback(context)
                        if trigger_type == "AGENT_NEXT_STEP"
                        else None
                    ) or _fallback_intervention_text(trigger_type, db)
                    log_event(
                        "INTERVENTION_TEXT_FALLBACK", trigger=trigger_type,
                        error_type=type(exc).__name__, message=str(exc)[:300], text=fallback,
                    )
                    return fallback
                raise
            delay_seconds = 1.0 * (2 ** attempt)
            log_event(
                "INTERVENTION_TEXT_RETRY", trigger=trigger_type, attempt=attempt + 1,
                delay_seconds=delay_seconds, error_type=type(exc).__name__,
            )
            await asyncio.sleep(delay_seconds)


def _recent_trigger_exists(
    trigger_type: str, db: Session, related_claim_ids: list[str]
) -> bool:
    cooldown = INTERVENTION_POLICY.cooldown_seconds
    if trigger_type == "AGENT_NEXT_STEP":
        try:
            cooldown = max(5, int(os.getenv("AGENT_CONVERSATION_COOLDOWN_SECONDS", "20")))
        except ValueError:
            cooldown = 20
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown)
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
    agent = active_agent_for_channel(channel, db)
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
