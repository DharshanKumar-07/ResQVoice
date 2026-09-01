import os
import asyncio
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
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
)
from app.services.event_store import (
    append_event,
    persist_transcript_chunk,
    project_extracted_data,
)
from app.services.extractor import extract_claims_async
from app.services.claim_lifecycle import EvidenceInput
from app.services.orchestrator import ingest_evidence
from app.services.evidence_graph import get_provenance, get_graph
from app.services.silence_signal import scan_for_unresolved
from app.services.contradiction_radar import detect_conflicts, ClaimSnapshot
from app.services.transcription import (
    TranscriptionQuotaExceeded,
    TranscriptionServiceError,
    transcribe_audio_file,
)

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="ResQVoice Backend")

WORKSPACE_GENERATION = 0
WORKSPACE_COMMIT_LOCK = asyncio.Lock()
BACKGROUND_EXTRACTION_TASKS: set[asyncio.Task] = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

class AgoraTokenRequest(BaseModel):
    channel: str
    uid: int = 0

class AgoraTokenResponse(BaseModel):
    token: str
    uid: int
    channel: str
    app_id: str

# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

async def _extract_transcript_background(
    speaker: str,
    role: str,
    text: str,
    timestamp: str,
    generation: int,
):
    try:
        extracted = await extract_claims_async(speaker, role, text, timestamp)
        db = SessionLocal()
        async with WORKSPACE_COMMIT_LOCK:
            if generation != WORKSPACE_GENERATION:
                print("[Workspace] Ignoring stale extraction completed before reset")
                return
            append_event(db, "EXTRACTION_RESULT", extracted.model_dump(mode="json"))
            project_extracted_data(db, extracted)
    except Exception as exc:
        if "db" in locals():
            db.rollback()
        print(f"Background extraction failed: {exc}")
    finally:
        if "db" in locals():
            db.close()


def _schedule_extraction(speaker: str, role: str, text: str, timestamp: str):
    """Detach structured extraction from the audio/transcript HTTP response."""
    task = asyncio.create_task(
        _extract_transcript_background(
            speaker,
            role,
            text,
            timestamp,
            WORKSPACE_GENERATION,
        )
    )
    BACKGROUND_EXTRACTION_TASKS.add(task)
    task.add_done_callback(BACKGROUND_EXTRACTION_TASKS.discard)


@app.post("/api/transcript")
def receive_transcript(
    chunk: TranscriptChunkRequest,
    db: Session = Depends(get_db),
):
    event = persist_transcript_chunk(
        db, chunk.speaker, chunk.role, chunk.text, chunk.timestamp
    )
    _schedule_extraction(
        chunk.speaker,
        chunk.role,
        chunk.text,
        chunk.timestamp,
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


@app.post("/api/workspace/reset")
async def reset_workspace(db: Session = Depends(get_db)):
    """Clear all persisted incident and transcript state atomically."""
    global WORKSPACE_GENERATION

    async with WORKSPACE_COMMIT_LOCK:
        WORKSPACE_GENERATION += 1
        try:
            for model in (
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

@app.get("/api/state")
def get_state(db: Session = Depends(get_db)):
    facts = db.query(Fact).all()
    hypotheses = db.query(Hypothesis).all()
    actions = db.query(Action).all()
    decisions = db.query(Decision).all()
    claims = db.query(Claim).all()
    conflicts = db.query(Conflict).all()
    
    return {
        "facts": facts,
        "hypotheses": hypotheses,
        "actions": actions,
        "decisions": decisions,
        "claims": claims,
        "conflicts": conflicts,
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

# ---------------------------------------------------------------------------
# PART 2: Audio transcription via Gemini
# ---------------------------------------------------------------------------

@app.post("/api/audio/transcribe")
async def transcribe_audio(
    audio: UploadFile = File(...),
    speaker: str = Form("Unknown"),
    role: str = Form("Unknown"),
    uid: str = Form("0"),
    speech_detected: bool = Form(False),
    db: Session = Depends(get_db),
):
    """
    Receive an audio chunk (webm/ogg), transcribe it via Gemini,
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

        # The browser only queues chunks whose analyser detected speech. Gemini
        # can occasionally return SILENCE for the same clearly audible audio,
        # so confirm that provider-side false negative once with the same file.
        if (
            speech_detected
            and (not transcript_text or transcript_text.upper() == "SILENCE")
        ):
            print(f"[Audio STT] Confirming false-silence result for uid={uid}")
            transcript_text = await transcribe_audio_file(temp_path, mime_type)

        # Filter out silence/empty results
        if not transcript_text or transcript_text.upper() == "SILENCE":
            return {"transcript": "", "status": "silence"}

        print(f"[Audio STT] uid={uid} speaker={speaker}: {transcript_text[:80]}...")

        # Persist first so the raw text is immediately visible everywhere.
        timestamp = datetime.now(timezone.utc).isoformat()
        event = persist_transcript_chunk(db, speaker, role, transcript_text, timestamp)
        _schedule_extraction(
            speaker,
            role,
            transcript_text,
            timestamp,
        )

        return {
            "transcript": transcript_text,
            "status": "ok",
            "speaker": speaker,
            "event_id": event.id,
        }

    except TranscriptionQuotaExceeded:
        print("[Audio STT] Gemini quota exceeded; returning HTTP 429")
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "15"},
            content={
                "status": "error",
                "code": "QUOTA_EXCEEDED",
                "message": (
                    "Gemini transcription rate limit exceeded (429). "
                    "Please slow down or pause audio."
                ),
            },
        )
    except TranscriptionServiceError as exc:
        print(f"[Audio STT] Gemini transcription error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "TRANSCRIPTION_FAILED",
                "message": "Gemini audio transcription failed.",
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
