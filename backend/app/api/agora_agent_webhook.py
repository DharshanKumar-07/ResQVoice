"""Agora turn interception and OpenAI-compatible custom-LLM gateway."""
from __future__ import annotations

import json
import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.integrations.agora_conversational_ai import ACTIVE_AGENT_SESSIONS, AGORA_AGENT, SpeakRequest
from app.models import Action, Claim, Conflict, Decision, EventLog, Fact, Hypothesis, Intervention as InterventionRow, Unknown
from app.schemas import ClaimSnapshot, Participant
from app.services.contradiction_radar import detect_conflicts
from app.services.event_store import append_event, persist_transcript_chunk, project_extracted_data
from app.services.incident_state_stream import INCIDENT_STATE_BROADCASTER
from app.services.intervention_policy import INTERVENTION_POLICY, InterventionCandidate, note_human_activity
from app.services.observability import log_event
from app.services.participant_registry import register_participant, resolve_participant
from app.services.safety_layer import recommend_action
from app.services.silence_signal import scan_for_unresolved
from app.services.transcript_stream import TRANSCRIPT_BROADCASTER
from app.services.transcript_ingestion import TRANSCRIPT_GUARD, TranscriptInput
from app.services.voice_interventions import (
    enqueue_generated_intervention,
    is_summary_request,
    speak_priority_for,
    speak_to_channel,
)

router = APIRouter(prefix="/api/agora-agent", tags=["Agora Conversational AI"])


@router.get("/health")
def agora_callback_health() -> dict[str, str]:
    """Unauthenticated tunnel preflight; contains no incident or credential data."""
    return {"status": "ok", "service": "resqvoice-agora-callback"}


class AgoraEvent(BaseModel):
    event_type: Literal["utterance", "transcript", "silence", "tool_call", "tts_completed"]
    agent_id: str | None = None
    channel: str = "incident-room"
    incident_id: str = "INC-001"
    speaker_uid: str | int | None = None
    speaker: str = "Agora participant"
    role: str = "Incident responder"
    text: str = ""
    timestamp: str | None = None
    duration_seconds: float = 0
    silence_duration_seconds: float = 0
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    utterance_id: str | None = None
    sequence: int | None = None
    is_final: bool = True


class Intervention(BaseModel):
    action: Literal["speak", "ask_clarifying_question", "remain_silent", "tool_response"]
    text: str | None = None
    priority: Literal["INTERRUPT", "APPEND", "IGNORE"] = "IGNORE"
    reasons: list[str] = Field(default_factory=list)
    mutations: dict[str, list[str]] = Field(default_factory=dict)
    tool_result: Any | None = None


class ChatMessage(BaseModel):
    role: str
    content: Any = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "resqvoice-incident-brain"
    messages: list[ChatMessage]
    stream: bool = True
    user: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


def _authorize(authorization: str | None) -> None:
    expected = os.getenv("AGORA_WEBHOOK_SECRET", "").strip()
    if not expected:  # local/mock development
        return
    if authorization != f"Bearer {expected}":
        log_event("AGORA_AUTH_REJECTED", reason="invalid_bearer")
        raise HTTPException(status_code=401, detail="Invalid Agora middleware token")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        ).strip()
    return str(content or "").strip()


def _transcript_payload(event: EventLog, speaker: str, role: str, text: str, timestamp: str) -> dict[str, Any]:
    return {"id": event.id, "speaker": speaker, "role": role, "text": text, "timestamp": timestamp}


def _identity_for(event: AgoraEvent, db: Session) -> Participant:
    speaker_uid = event.speaker_uid
    if speaker_uid is None:
        # Agora's OpenAI-compatible request body does not consistently include
        # the RTC speaker UID. The start call records the human UID so callbacks
        # can still resolve the participant registered by the browser.
        session = next(
            (
                metadata for metadata in reversed(ACTIVE_AGENT_SESSIONS.values())
                if metadata.get("channel") == event.channel
            ),
            None,
        )
        if session:
            speaker_uid = session.get("speaker_uid") or None
    identity = resolve_participant(event.channel, speaker_uid, db)
    # Explicit event identity is accepted only for direct normalized event calls;
    # the OpenAI endpoint resolves identity from the registered UID.
    if event.speaker != "Agora participant" and event.speaker:
        identity.display_name = event.speaker
    if event.role != "Incident responder" and event.role:
        identity.role = event.role
    return identity


def _persist_ai_transcript(db: Session, text: str, timestamp: str, agent_uid: str = "1000") -> EventLog:
    event = persist_transcript_chunk(
        db, "ResQVoice AI", "AI Incident Co-Pilot", text, timestamp,
        speaker_uid=agent_uid, participant_type="ai_agent", origin="AI_TTS",
    )
    TRANSCRIPT_BROADCASTER.publish(_transcript_payload(event, "ResQVoice AI", "AI Incident Co-Pilot", text, timestamp))
    return event


async def process_utterance(event: AgoraEvent, db: Session) -> Intervention:
    """Persist one Agora-finalized turn and pass its mutations through all engines."""
    identity = _identity_for(event, db)
    if identity.participant_type == "ai_agent":
        log_event("AI_SELF_AUDIO_IGNORED", channel=event.channel, uid=identity.agora_uid)
        return Intervention(action="remain_silent", reasons=["AI_SELF_AUDIO_IGNORED"])
    if event.utterance_id:
        recent = (
            db.query(EventLog)
            .filter(EventLog.event_type == "TRANSCRIPT_CHUNK")
            .order_by(EventLog.id.desc())
            .limit(250)
            .all()
        )
        if any(
            row.payload.get("utterance_id") == event.utterance_id
            and row.payload.get("channel") == event.channel
            for row in recent if isinstance(row.payload, dict)
        ):
            log_event("UTTERANCE_DEDUPLICATED", utterance_id=event.utterance_id,
                      reason="persisted_event_id")
            return Intervention(action="remain_silent", reasons=["DUPLICATE_PERSISTED_ID"])
    normalized = TRANSCRIPT_GUARD.process(TranscriptInput(
        channel=event.channel, speaker_uid=identity.agora_uid, text=event.text,
        utterance_id=event.utterance_id, sequence=event.sequence,
        is_final=event.is_final,
    ))
    if not normalized.persist:
        return Intervention(action="remain_silent", reasons=[normalized.reason])
    text = normalized.text
    note_human_activity()
    timestamp = event.timestamp or datetime.now(timezone.utc).isoformat()
    transcript_event = persist_transcript_chunk(
        db, identity.display_name, identity.role, text, timestamp,
        speaker_uid=identity.agora_uid, user_id=identity.user_id,
        participant_type=identity.participant_type, utterance_id=event.utterance_id,
        sequence=event.sequence, is_final=True, normalization=normalized.reason,
        channel=event.channel, origin="HUMAN_ASR",
    )
    TRANSCRIPT_BROADCASTER.publish(_transcript_payload(transcript_event, identity.display_name, identity.role, text, timestamp))

    # Agora turns are irregular and must not restore one-Gemini-call-per-turn.
    # Reuse the shared duration/word/max-wait batcher used by compatibility STT.
    from app.main import _schedule_extraction
    _schedule_extraction(
        transcript_event.id, identity.display_name, identity.role, text, timestamp,
        max(0, event.duration_seconds),
    )
    if is_summary_request(text):
        await enqueue_generated_intervention(
            "SUMMARY_REQUEST", "MEDIUM", db,
            channel=event.channel,
            trigger_context={"requested_by": identity.display_name, "utterance": text},
            related_claim_ids=[str(transcript_event.id)],
            bypass_severity_threshold=True,
        )
        from app.services.intervention_monitor import INTERVENTION_MONITOR
        await INTERVENTION_MONITOR.scan_and_dispatch()
        # The immediate scan correctly observes that the requester is still
        # active and defers low-priority speech. Retry after the short pause
        # guard instead of waiting for the periodic monitor's next tick.
        INTERVENTION_MONITOR.dispatch_after_speech_guard()
    mutations: dict[str, list[str]] = {"transcripts": [str(transcript_event.id)]}
    INCIDENT_STATE_BROADCASTER.publish({
        "type": "incident_state_mutation", "channel": event.channel,
        "transcript": _transcript_payload(
            transcript_event, identity.display_name, identity.role, text, timestamp
        ),
        "mutations": mutations,
    })
    return Intervention(action="remain_silent", reasons=["EXTRACTION_BATCH_QUEUED"], mutations=mutations)


async def process_event(event: AgoraEvent, db: Session) -> Intervention:
    if event.event_type in {"utterance", "transcript"}:
        return await process_utterance(event, db)
    if event.event_type == "silence":
        alerts = scan_for_unresolved(event.incident_id, db)
        if not alerts:
            return Intervention(action="remain_silent", reasons=["NO_UNRESOLVED_CRITICAL_SIGNAL"])
        alert = alerts[0]
        admitted = await enqueue_generated_intervention(
            alert.alert_type, alert.severity, db, channel=event.channel,
            trigger_context={
                "description": alert.description,
                "suggested_question": alert.suggested_question,
                "silence_duration_seconds": event.silence_duration_seconds,
            },
            related_claim_ids=[alert.source_id] if alert.source_id else [],
        )
        if admitted is None:
            return Intervention(action="remain_silent", reasons=["INTERVENTION_POLICY_REJECTED"])
        return Intervention(
            action="ask_clarifying_question", text=admitted.message,
            priority=speak_priority_for(alert.alert_type), reasons=[alert.alert_type],
            tool_result={"intervention_id": admitted.id},
        )
    if event.event_type == "tts_completed":
        log_event("TTS_COMPLETED", agent_id=event.agent_id, channel=event.channel)
        return Intervention(action="remain_silent", reasons=["TTS_COMPLETED_RECORDED"])
    if event.event_type == "tool_call" and event.tool_name == "verify_sop":
        result = recommend_action(
            str(event.arguments.get("proposed_action", "")),
            list(event.arguments.get("evidence", [])),
            list(event.arguments.get("completed_steps", [])), db,
        )
        return Intervention(action="tool_response", tool_result=result.model_dump(mode="json"))
    if event.event_type == "tool_call" and event.tool_name == "check_silence":
        alerts = scan_for_unresolved(event.incident_id, db)
        return Intervention(action="tool_response", tool_result=[a.model_dump(mode="json") for a in alerts])
    raise HTTPException(status_code=422, detail=f"Unsupported Agora event/tool: {event.event_type}/{event.tool_name}")


@router.post("/events", response_model=Intervention)
async def agora_event(event: AgoraEvent, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    _authorize(authorization)
    intervention = await process_event(event, db)
    if event.agent_id and intervention.text and intervention.action in {"speak", "ask_clarifying_question"}:
        if isinstance(intervention.tool_result, dict) and intervention.tool_result.get("intervention_id"):
            row = db.query(InterventionRow).filter(
                InterventionRow.id == intervention.tool_result["intervention_id"]
            ).first()
            if row:
                await speak_to_channel(event.agent_id, row, db)
    return intervention


def _chat_response(request_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": request_id, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@router.post("/llm/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest, authorization: str | None = Header(default=None),
    channel: str = Query(default="incident-room"), speaker_uid: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """OpenAI Chat Completions-compatible endpoint configured as Agora's LLM."""
    _authorize(authorization)
    user_message = next((message for message in reversed(request.messages) if message.role == "user"), None)
    if user_message is None:
        raise HTTPException(status_code=422, detail="A user message is required")
    context = {**request.context, **request.metadata}
    resolved_channel = str(context.get("channel") or channel)
    resolved_uid = context.get("speaker_uid") or context.get("user_id") or speaker_uid or request.user
    intervention = await process_utterance(AgoraEvent(
        event_type="utterance", text=_content_text(user_message.content),
        speaker="Agora participant", role="Incident responder",
        speaker_uid=resolved_uid, channel=resolved_channel,
        timestamp=context.get("timestamp"), duration_seconds=float(context.get("duration_seconds") or 0),
        utterance_id=str(context.get("utterance_id") or context.get("turn_id") or context.get("request_id") or "") or None,
        sequence=context.get("sequence"), is_final=True,
    ), db)
    spoken_text = intervention.text or ""
    if spoken_text:
        if isinstance(intervention.tool_result, dict) and intervention.tool_result.get("intervention_id"):
            row = db.query(InterventionRow).filter(
                InterventionRow.id == intervention.tool_result["intervention_id"]
            ).first()
            if row and row.agent_id:
                await speak_to_channel(row.agent_id, row, db)
                # The Speak API owns TTS for explicit interventions; return an
                # empty LLM response so Agora does not synthesize it twice.
                spoken_text = ""
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    if not request.stream:
        return _chat_response(request_id, request.model, spoken_text)

    async def stream():
        first = {"id": request_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": request.model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": spoken_text}, "finish_reason": None}]}
        final = {"id": request_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": request.model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(first)}\n\n"
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/participants", response_model=Participant)
def register_participant_endpoint(participant: Participant, db: Session = Depends(get_db)):
    return register_participant(participant, db)


def _state_snapshot(db: Session) -> dict[str, Any]:
    return {
        "type": "incident_state_snapshot",
        "counts": {
            "facts": db.query(Fact).count(), "hypotheses": db.query(Hypothesis).count(),
            "claims": db.query(Claim).count(), "actions": db.query(Action).count(),
            "decisions": db.query(Decision).count(), "conflicts": db.query(Conflict).filter(Conflict.status == "UNRESOLVED").count(),
            "unknowns": db.query(Unknown).filter(Unknown.status == "OPEN").count(),
        },
    }


@router.websocket("/state/ws")
async def state_websocket(websocket: WebSocket):
    await websocket.accept()
    queue = INCIDENT_STATE_BROADCASTER.subscribe()
    db = SessionLocal()
    try:
        await websocket.send_json(_state_snapshot(db))
        # Do not reserve a database-pool connection for the lifetime of a socket.
        db.close()
        while True:
            await websocket.send_json(await queue.get())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        INCIDENT_STATE_BROADCASTER.unsubscribe(queue)
        db.close()
