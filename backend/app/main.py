import os
import asyncio
import time
from dotenv import load_dotenv

load_dotenv()

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from agora_token_builder import RtcTokenBuilder

from app.database import engine, Base, SessionLocal, get_db
from app.models import (
    Action,
    Claim,
    Conflict,
    Decision,
    Evidence,
    EventLog,
    Fact,
    Hypothesis,
    TimelineEvent,
    Unknown,
    Participant,
    Intervention,
)
from app.services.event_store import (
    append_event,
    persist_transcript_chunk,
    project_extracted_data,
)
from app.services.extractor import (
    TranscriptSegment,
    extract_claims_batch_async,
    is_transient_provider_error,
)
from app.services.extraction_batcher import ExtractionBatcher
from app.services.provider_metrics import PROVIDER_REQUESTS
from app.services.transcript_stream import TRANSCRIPT_BROADCASTER
from app.services.incident_state_stream import INCIDENT_STATE_BROADCASTER
from app.schemas import ClaimSnapshot, Evidence as EvidenceInput
from app.services.orchestrator import ingest_evidence
from app.services.evidence_graph import get_provenance, get_graph
from app.services.silence_signal import scan_for_unresolved
from app.services.contradiction_radar import detect_conflicts
from app.services.transcription import (
    TranscriptionQuotaExceeded,
    TranscriptionServiceError,
    transcribe_audio_file,
)
from app.services.safety_layer import (
    approve_decision,
    execute_decision,
    recommend_action,
    verify_decision,
)
from app.services.participant_registry import register_agent, register_participant
from app.schemas import Participant as ParticipantSchema
from app.services.observability import log_event
from app.services.intervention_monitor import INTERVENTION_MONITOR
from app.services.intervention_policy import (
    INTERVENTION_POLICY,
    InterventionCandidate,
    human_recently_active,
)
from app.services.voice_interventions import enqueue_generated_intervention

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="ResQVoice Backend")


@app.get("/healthz", tags=["Operations"])
@app.get("/health", tags=["Operations"], include_in_schema=False)
def deployment_health() -> JSONResponse:
    """Render/readiness probe: process is live and PostgreSQL is reachable."""
    try:
        with engine.connect() as connection:
            connection.execute(sql_text("SELECT 1"))
    except Exception as exc:
        log_event(
            "READINESS_CHECK_FAILED",
            dependency="database",
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"status": "error", "service": "resqvoice-api", "database": "unavailable"},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "resqvoice-api", "database": "ok"},
    )


@app.on_event("startup")
async def start_intervention_monitor():
    INTERVENTION_MONITOR.start()


@app.on_event("shutdown")
async def stop_intervention_monitor():
    await INTERVENTION_MONITOR.stop()

WORKSPACE_GENERATION = 0
WORKSPACE_COMMIT_LOCK = asyncio.Lock()
BACKGROUND_EXTRACTION_TASKS: set[asyncio.Task] = set()

# Import new integrations
from app.api.agora_agent_webhook import router as agora_webhook_router
from app.integrations.agora_conversational_ai import (
    start_agent_session,
    stop_agent_session,
    StartAgentRequest,
    SpeakRequest,
    AGORA_AGENT,
)


_cors_raw = os.environ.get(
    "CORS_ALLOW_ORIGINS", "http://localhost:5173,http://localhost:3000"
).strip()
_cors_origins: list[str] = (
    [o.strip() for o in _cors_raw.split(",") if o.strip()]
    if _cors_raw != "*"
    else ["*"]
)
# Render may retain an older `CORS_ALLOW_ORIGINS` value after a Blueprint
# update. Always admit the two loopback spellings used by the local Vite dev
# server, so a developer can connect to the deployed API without needing a
# dashboard-only environment-variable edit. Hosted frontend origins must still
# be explicitly configured through `CORS_ALLOW_ORIGINS`.
if _cors_origins != ["*"]:
    for _local_vite_origin in (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ):
        if _local_vite_origin not in _cors_origins:
            _cors_origins.append(_local_vite_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # The API authenticates Agora webhooks with a bearer secret and does not use
    # browser cookies. Keeping this false makes an explicit origin allowlist
    # work correctly and avoids the invalid wildcard-plus-credentials pairing.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class TranscriptChunkRequest(BaseModel):
    speaker: str
    role: str
    text: str
    timestamp: str
    duration_seconds: float = 0

class AgoraTokenRequest(BaseModel):
    channel: str
    uid: int = 0

class AgoraTokenResponse(BaseModel):
    token: str
    uid: int
    channel: str
    app_id: str


class SafetyRecommendationRequest(BaseModel):
    proposed_action: str
    evidence: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)


class DecisionApprovalRequest(BaseModel):
    approved_by: str


class DecisionVerificationRequest(BaseModel):
    fact_id: str
    observed_metric: str
    source: str
    verified_by: str


def _transcript_payload(event: EventLog, speaker: str, role: str, text: str, timestamp: str):
    return {
        "id": event.id,
        "speaker": speaker,
        "role": role,
        "text": text,
        "timestamp": timestamp,
    }

# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

async def _extract_transcript_batch_background(
    segments: list[TranscriptSegment], generation: int, retry_attempt: int = 0,
):
    retry_delay_seconds: float | None = None
    try:
        extracted = await extract_claims_batch_async(segments)
        db = SessionLocal()
        async with WORKSPACE_COMMIT_LOCK:
            if generation != WORKSPACE_GENERATION:
                print("[Workspace] Ignoring stale extraction completed before reset")
                return
            append_event(db, "EXTRACTION_RESULT", extracted.model_dump(mode="json"))
            project_extracted_data(db, extracted)
            conflict_ids: list[str] = []
            for claim in extracted.claims:
                rows = detect_conflicts(ClaimSnapshot(
                    id=claim.id, text=claim.text, speaker=claim.speaker,
                    role=claim.role,
                ), db)
                conflict_ids.extend(row.id for row in rows if row.status == "UNRESOLVED")
            if conflict_ids:
                log_event("CONTRADICTION_DETECTED", conflict_ids=conflict_ids)
                row = db.query(Conflict).filter(Conflict.id == conflict_ids[0]).first()
                await enqueue_generated_intervention(
                    "CONFLICT_DETECTED", "HIGH", db,
                    trigger_context={
                        "topic": row.topic,
                        "recommended_verification": row.recommended_verification,
                    },
                    related_claim_ids=[row.claim_a_id, row.claim_b_id],
                )

            decision_ids: list[str] = []
            for action in extracted.actions:
                if action.owner:
                    log_event("ACTION_ASSIGNED", action_id=action.id, owner=action.owner)
                result = recommend_action(
                    action.task,
                    [f"Transcript segment {segment.segment_id}: {segment.text}" for segment in segments],
                    [], db,
                )
                decision_ids.append(result.decision.id)
                if result.conflict:
                    log_event("SOP_CONFLICT", decision_id=result.decision.id,
                              sop_reference=result.decision.sop_reference)
                    await enqueue_generated_intervention(
                        "SOP_CONFLICT", "CRITICAL", db,
                        trigger_context={
                            "recommendation": result.decision.recommendation,
                            "sop_reference": result.decision.sop_reference,
                            "conflict": result.conflict.model_dump(mode="json"),
                        },
                        related_claim_ids=[result.decision.id],
                    )

            log_event(
                "INCIDENT_EVENT_EXTRACTED", segments=len(segments),
                facts=len(extracted.facts), hypotheses=len(extracted.hypotheses),
                claims=len(extracted.claims), actions=len(extracted.actions),
                decisions=len(extracted.decisions),
            )
            INCIDENT_STATE_BROADCASTER.publish({
                "type": "incident_state_mutation",
                "mutations": {
                    "facts": [item.id for item in extracted.facts],
                    "hypotheses": [item.id for item in extracted.hypotheses],
                    "claims": [item.id for item in extracted.claims],
                    "actions": [item.id for item in extracted.actions],
                    "decisions": decision_ids,
                    "conflicts": list(dict.fromkeys(conflict_ids)),
                },
            })
    except Exception as exc:
        if "db" in locals():
            db.rollback()
        log_event("EXTRACTION_BATCH_FAILED", error_type=type(exc).__name__, message=str(exc)[:300])
        try:
            max_retries = max(0, int(os.getenv("EXTRACTION_BATCH_RETRY_ATTEMPTS", "2")))
            base_delay = max(0.1, float(os.getenv("EXTRACTION_BATCH_RETRY_DELAY_SECONDS", "3")))
        except ValueError:
            max_retries, base_delay = 2, 3.0
        if (
            generation == WORKSPACE_GENERATION
            and retry_attempt < max_retries
            and is_transient_provider_error(exc)
        ):
            retry_delay_seconds = base_delay * (2 ** retry_attempt)
            log_event(
                "EXTRACTION_BATCH_RETRY_SCHEDULED",
                attempt=retry_attempt + 1,
                delay_seconds=retry_delay_seconds,
                segments=len(segments),
            )
    finally:
        if "db" in locals():
            db.close()
    if retry_delay_seconds is not None:
        await asyncio.sleep(retry_delay_seconds)
        if generation == WORKSPACE_GENERATION:
            return await _extract_transcript_batch_background(
                segments, generation, retry_attempt + 1
            )
    # A completed batch may have created a queued intervention. Dispatching is
    # still gated by human-activity, cooldown, and rate-window policy.
    if generation == WORKSPACE_GENERATION:
        await INTERVENTION_MONITOR.scan_and_dispatch()


EXTRACTION_BATCHER = ExtractionBatcher(_extract_transcript_batch_background)


def _schedule_extraction(
    segment_id: int,
    speaker: str,
    role: str,
    text: str,
    timestamp: str,
    duration_seconds: float = 0,
):
    """Queue a segment without blocking the audio/transcript HTTP response."""
    segment = TranscriptSegment(
        segment_id=segment_id,
        speaker=speaker,
        role=role,
        text=text,
        timestamp=timestamp,
        duration_seconds=max(0, duration_seconds),
    )
    task = asyncio.create_task(
        EXTRACTION_BATCHER.add(segment, WORKSPACE_GENERATION)
    )
    BACKGROUND_EXTRACTION_TASKS.add(task)
    task.add_done_callback(BACKGROUND_EXTRACTION_TASKS.discard)


@app.post("/api/transcript")
async def receive_transcript(
    chunk: TranscriptChunkRequest,
    db: Session = Depends(get_db),
):
    event = persist_transcript_chunk(
        db, chunk.speaker, chunk.role, chunk.text, chunk.timestamp
    )
    TRANSCRIPT_BROADCASTER.publish(
        _transcript_payload(event, chunk.speaker, chunk.role, chunk.text, chunk.timestamp)
    )
    _schedule_extraction(
        event.id,
        chunk.speaker,
        chunk.role,
        chunk.text,
        chunk.timestamp,
        chunk.duration_seconds,
    )
    return {"status": "accepted", "event_id": event.id}


@app.get("/api/transcripts")
def get_transcripts(
    after_id: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Return persisted raw transcript chunks for room/dashboard synchronization."""
    safe_limit = max(1, min(limit, 250))
    query = db.query(EventLog).filter(EventLog.event_type == "TRANSCRIPT_CHUNK")
    if after_id > 0:
        events = (
            query.filter(EventLog.id > after_id)
            .order_by(EventLog.id.asc())
            .limit(safe_limit)
            .all()
        )
    else:
        events = list(reversed(query.order_by(EventLog.id.desc()).limit(safe_limit).all()))
    return [
        {
            "id": event.id,
            "speaker": event.payload.get("speaker", "Unknown"),
            "role": event.payload.get("role", "Unknown"),
            "text": event.payload.get("text", ""),
            "timestamp": event.payload.get("timestamp") or event.timestamp.isoformat(),
        }
        for event in events
    ]


@app.get("/api/transcripts/stream")
async def stream_transcripts(request: Request):
    """Push each persisted transcript to browsers as soon as it is available."""
    async def events():
        async for event in TRANSCRIPT_BROADCASTER.event_stream():
            if await request.is_disconnected():
                break
            yield event

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/workspace/reset")
async def reset_workspace(db: Session = Depends(get_db)):
    """Clear all persisted incident and transcript state atomically."""
    global WORKSPACE_GENERATION

    async with WORKSPACE_COMMIT_LOCK:
        WORKSPACE_GENERATION += 1
        await EXTRACTION_BATCHER.clear()
        try:
            for model in (
                Intervention,
                Participant,
                Evidence,
                Conflict,
                Unknown,
                TimelineEvent,
                EventLog,
                Decision,
                Action,
                Claim,
                Hypothesis,
                Fact,
            ):
                db.query(model).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise

    return {"status": "reset", "generation": WORKSPACE_GENERATION}


@app.get("/api/provider-metrics")
def get_provider_metrics():
    """Expose rolling in-process provider counts for development verification."""
    return {
        "providers": PROVIDER_REQUESTS.snapshot(),
        "extraction_batch": EXTRACTION_BATCHER.snapshot(),
    }

@app.get("/api/state")
def get_state(db: Session = Depends(get_db)):
    facts = db.query(Fact).all()
    hypotheses = db.query(Hypothesis).all()
    actions = db.query(Action).all()
    decisions = db.query(Decision).all()
    claims = db.query(Claim).all()
    conflicts = db.query(Conflict).all()
    participants = db.query(Participant).all()
    interventions = db.query(Intervention).all()
    
    return {
        "facts": facts,
        "hypotheses": hypotheses,
        "actions": actions,
        "decisions": decisions,
        "claims": claims,
        "conflicts": conflicts,
        "participants": participants,
        "interventions": interventions,
    }

@app.post("/api/evidence")
def ingest_evidence_endpoint(evidence: EvidenceInput, db: Session = Depends(get_db)):
    try:
        result = ingest_evidence(evidence, db)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/provenance/{claim_id}")
def get_provenance_endpoint(claim_id: str, db: Session = Depends(get_db)):
    try:
        return get_provenance(claim_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/graph")
def get_graph_endpoint(incident_id: Optional[str] = None, db: Session = Depends(get_db)):
    return get_graph(db, incident_id=incident_id)

@app.get("/api/silence-alerts")
def get_silence_alerts_endpoint(incident_id: str = "INC-001", db: Session = Depends(get_db)):
    return scan_for_unresolved(incident_id, db)

@app.post("/api/conflicts/check")
def check_conflicts_endpoint(claim: ClaimSnapshot, db: Session = Depends(get_db)):
    return detect_conflicts(claim, db)


# ---------------------------------------------------------------------------
# Safety-gated decision workflow
# ---------------------------------------------------------------------------

@app.post("/api/safety/recommend")
def recommend_action_endpoint(
    request: SafetyRecommendationRequest,
    db: Session = Depends(get_db),
):
    try:
        return recommend_action(
            request.proposed_action,
            request.evidence,
            request.completed_steps,
            db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/safety/decisions/{decision_id}/approve")
async def approve_decision_endpoint(
    decision_id: str,
    request: DecisionApprovalRequest,
    db: Session = Depends(get_db),
):
    try:
        approval = approve_decision(decision_id, request.approved_by, db)
        decision = db.query(Decision).filter(Decision.id == decision_id).first()
        await enqueue_generated_intervention(
            "DECISION_APPROVED", "MEDIUM", db,
            trigger_context={
                "decision_id": decision_id,
                "recommendation": decision.recommendation if decision else "",
                "approved_by": request.approved_by,
            },
            related_claim_ids=[decision_id],
            bypass_severity_threshold=True,
        )
        await INTERVENTION_MONITOR.scan_and_dispatch()
        return approval
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/safety/decisions/{decision_id}/execute")
def execute_decision_endpoint(decision_id: str, db: Session = Depends(get_db)):
    try:
        return execute_decision(decision_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/api/safety/decisions/{decision_id}/verify")
def verify_decision_endpoint(
    decision_id: str,
    request: DecisionVerificationRequest,
    db: Session = Depends(get_db),
):
    try:
        return verify_decision(
            decision_id,
            request.fact_id,
            request.observed_metric,
            request.source,
            request.verified_by,
            db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# ---------------------------------------------------------------------------
# PART 1: Agora token generation
# ---------------------------------------------------------------------------

@app.post("/api/agora/token", response_model=AgoraTokenResponse)
def generate_agora_token(req: AgoraTokenRequest):
    """
    Generate a temporary Agora RTC token server-side.
    Requires AGORA_APP_ID and AGORA_APP_CERTIFICATE in environment.
    """
    channel = req.channel.strip()
    if not channel:
        raise HTTPException(status_code=422, detail="channel must not be empty")

    app_id = os.environ.get("AGORA_APP_ID", "").strip()
    app_cert = os.environ.get("AGORA_APP_CERTIFICATE", "").strip()

    if not app_id or not app_cert:
        raise HTTPException(
            status_code=500,
            detail="AGORA_APP_ID and AGORA_APP_CERTIFICATE must be set in environment.",
        )

    # Token valid for 1 hour
    privilege_expired_ts = int(time.time()) + 3600
    # Role: 1 = publisher (can send audio)
    role = 1

    token = RtcTokenBuilder.buildTokenWithUid(
        app_id, app_cert, channel, req.uid, role, privilege_expired_ts
    )

    print(f"[Agora Token] Generated token for channel='{channel}' uid={req.uid}")

    return AgoraTokenResponse(
        token=token,
        uid=req.uid,
        channel=channel,
        app_id=app_id,
    )

@app.post("/api/agora/agent/start")
async def api_start_agora_agent(req: StartAgentRequest, db: Session = Depends(get_db)):
    """
    Endpoint to spin up the Agora Conversational AI Agent in a channel.
    The frontend calls this to trigger the agent.
    """
    try:
        for uid in req.remote_rtc_uids:
            if uid != "*":
                register_participant(ParticipantSchema(
                    agora_uid=str(uid), user_id=str(uid), display_name=req.speaker_name,
                    role=req.speaker_role, participant_type="human", channel=req.channel_name,
                ), db)
        register_agent(req.channel_name, req.agent_uid, db)
        response = await start_agent_session(req)
        log_event("RTC_JOINED", channel=req.channel_name, agent_id=response.agent_id, agent_uid=req.agent_uid)
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/agora/agent/stop/{session_id}")
async def api_stop_agora_agent(session_id: str):
    try:
        return await stop_agent_session(session_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/agora/agent/{agent_id}/speak")
async def api_speak_as_agora_agent(agent_id: str, request: SpeakRequest):
    try:
        return await AGORA_AGENT.speak(agent_id, request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

# ---------------------------------------------------------------------------
# PART 2: Audio transcription via Groq Whisper
# ---------------------------------------------------------------------------

@app.post("/api/audio/transcribe")
async def transcribe_audio(
    audio: UploadFile = File(...),
    speaker: str = Form("Unknown"),
    role: str = Form("Unknown"),
    uid: str = Form("0"),
    speech_detected: bool = Form(False),
    recorded_at: str | None = Form(None),
    duration_seconds: float = Form(0),
    db: Session = Depends(get_db),
):
    """
    Receive a browser audio chunk, transcribe it via Groq Whisper,
    and feed the result into the transcript pipeline.
    """
    audio_bytes = await audio.read()
    if len(audio_bytes) < 1_000:
        # Too small to contain meaningful audio (likely silence)
        await audio.close()
        return {"transcript": "", "status": "skipped", "reason": "chunk too small"}

    mime_type = (audio.content_type or "audio/webm").split(";", 1)[0].lower()
    supported_audio_types = {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }
    if mime_type not in supported_audio_types:
        await audio.close()
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported audio content type: {mime_type}",
        )

    if len(audio_bytes) > 10 * 1024 * 1024:
        await audio.close()
        raise HTTPException(status_code=413, detail="Audio chunk exceeds 10 MB limit")

    temp_path: Path | None = None

    try:
        # Persist each browser chunk before handing it to the model. Keeping the
        # suffix aligned with the MIME type also makes local debugging possible.
        with tempfile.NamedTemporaryFile(
            suffix=supported_audio_types[mime_type], delete=False
        ) as temp_audio:
            temp_audio.write(audio_bytes)
            temp_path = Path(temp_audio.name)

        transcript_text = await transcribe_audio_file(temp_path, mime_type)

        # Filter out silence/empty results
        if not transcript_text or transcript_text.upper() == "SILENCE":
            return {"transcript": "", "status": "silence"}

        print(f"[Audio STT] uid={uid} speaker={speaker}: {transcript_text[:80]}...")

        # Persist first so the raw text is immediately visible everywhere.
        timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
        event = persist_transcript_chunk(db, speaker, role, transcript_text, timestamp)
        TRANSCRIPT_BROADCASTER.publish(
            _transcript_payload(event, speaker, role, transcript_text, timestamp)
        )
        _schedule_extraction(
            event.id,
            speaker,
            role,
            transcript_text,
            timestamp,
            duration_seconds,
        )

        return {
            "transcript": transcript_text,
            "status": "ok",
            "speaker": speaker,
            "event_id": event.id,
        }

    except TranscriptionQuotaExceeded:
        print("[Audio STT] Groq quota exceeded; returning HTTP 429")
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "15"},
            content={
                "status": "error",
                "code": "QUOTA_EXCEEDED",
                "message": (
                    "Groq transcription rate limit exceeded (429). "
                    "Please slow down or pause audio."
                ),
            },
        )
    except TranscriptionServiceError as exc:
        print(f"[Audio STT] Groq transcription error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "TRANSCRIPTION_FAILED",
                "message": "Groq audio transcription failed.",
            },
        )
    except Exception as exc:
        print(f"[Audio STT] Unhandled transcription error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": "Audio transcription failed unexpectedly.",
            },
        )
    finally:
        await audio.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


app.include_router(agora_webhook_router)
