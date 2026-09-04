"""Runtime guards for Agora transcript identity and intervention behavior."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import Text, TypeDecorator

import sqlalchemy as sa


class ArrayAsJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value or [])

    def process_result_value(self, value, dialect):
        return json.loads(value or "[]")


sa.ARRAY = lambda item_type, *args, **kwargs: ArrayAsJSON()  # type: ignore[assignment]

from app.database import Base
from app.models import Intervention as InterventionRow
from app.schemas import Participant
from app.services.intervention_policy import InterventionCandidate, InterventionPolicy
from app.services.participant_registry import register_agent, register_participant, resolve_participant
from app.services.transcript_ingestion import TranscriptIngestionGuard, TranscriptInput
from app.integrations.agora_conversational_ai import ACTIVE_AGENT_SESSIONS


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def _turn(guard: TranscriptIngestionGuard, text: str, **updates):
    payload = dict(channel="incident-room", speaker_uid="42", text=text, is_final=True)
    payload.update(updates)
    return guard.process(TranscriptInput(**payload), now=updates.pop("now", None))


def test_partial_is_replaced_and_never_persisted_as_final():
    guard = TranscriptIngestionGuard()
    partial = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Hel", is_final=False,
    ), now=1)
    final = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Hello.", is_final=True,
        utterance_id="u-1",
    ), now=2)

    assert partial.persist is False
    assert partial.reason == "PARTIAL_REPLACE"
    assert final.persist is True
    assert final.text == "Hello."


def test_final_event_id_and_same_payload_are_deduplicated():
    guard = TranscriptIngestionGuard()
    first = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Hello.",
        utterance_id="u-1",
    ), now=1)
    duplicate_id = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Hello.",
        utterance_id="u-1",
    ), now=2)
    duplicate_payload = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Hello.",
        utterance_id="u-2",
    ), now=3)

    assert first.persist is True
    assert duplicate_id.reason == "DUPLICATE_ID" and not duplicate_id.persist
    assert duplicate_payload.reason == "DUPLICATE_PAYLOAD" and not duplicate_payload.persist


def test_out_of_order_sequence_is_ignored():
    guard = TranscriptIngestionGuard()
    accepted = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="Second.", sequence=2,
    ), now=2)
    stale = guard.process(TranscriptInput(
        channel="incident-room", speaker_uid="42", text="First.", sequence=1,
    ), now=3)
    assert accepted.persist is True
    assert stale.reason == "STALE_SEQUENCE" and not stale.persist


def test_cumulative_asr_history_becomes_three_distinct_final_utterances():
    guard = TranscriptIngestionGuard()
    outputs = []
    for sequence, text in enumerate([
        "Hello.",
        "Hello. So, what's up?",
        "Hello. So, what's up? So, what's up. Okay.",
    ], start=1):
        result = guard.process(TranscriptInput(
            channel="incident-room", speaker_uid="42", text=text,
            utterance_id=f"u-{sequence}", sequence=sequence,
        ), now=float(sequence))
        if result.persist:
            outputs.append(result.text)

    assert outputs == ["Hello.", "So, what's up?", "Okay."]


def test_participant_uid_maps_name_role_and_unknown_fallback(db):
    register_participant(Participant(
        agora_uid="42", user_id="user-42", display_name="Dharshan Kumar",
        role="Incident Commander", participant_type="human",
    ), db)

    known = resolve_participant("incident-room", "42", db)
    unknown = resolve_participant("incident-room", "99", db)

    assert (known.display_name, known.role, known.user_id) == (
        "Dharshan Kumar", "Incident Commander", "user-42",
    )
    assert (unknown.display_name, unknown.role) == (
        "Participant 99", "Unspecified role",
    )


def test_ai_agent_has_separate_identity_and_is_rejected_by_human_pipeline(db):
    register_agent("incident-room", "1000", db)
    identity = resolve_participant("incident-room", "1000", db)
    assert identity.participant_type == "ai_agent"
    assert identity.display_name == "ResQVoice AI"

    from app.api.agora_agent_webhook import AgoraEvent, process_utterance
    result = asyncio.run(process_utterance(AgoraEvent(
        event_type="utterance", channel="incident-room", speaker_uid="1000",
        text="This is the co-pilot speaking.",
    ), db))
    assert result.action == "remain_silent"
    assert result.reasons == ["AI_SELF_AUDIO_IGNORED"]


def test_callback_without_uid_uses_agent_session_speaker_identity(db):
    register_participant(Participant(
        agora_uid="42", user_id="user-42", display_name="Priya",
        role="Incident Commander", participant_type="human",
    ), db)
    ACTIVE_AGENT_SESSIONS["agent-identity"] = {
        "channel": "incident-room", "agent_uid": "1000", "speaker_uid": "42",
    }
    try:
        from app.api.agora_agent_webhook import AgoraEvent, _identity_for
        identity = _identity_for(AgoraEvent(
            event_type="utterance", channel="incident-room", text="Status update",
        ), db)
    finally:
        ACTIVE_AGENT_SESSIONS.pop("agent-identity", None)

    assert (identity.display_name, identity.role) == ("Priya", "Incident Commander")


def test_high_priority_uses_native_interrupt_and_low_priority_appends():
    from app.services.voice_interventions import speak_priority_for
    with patch.dict("os.environ", {
        "INTERVENTION_HIGH_PRIORITY_TRIGGERS": "CONFLICT_DETECTED,STALE_CRITICAL_CLAIM",
        "INTERVENTION_HIGH_PRIORITY_MODE": "interrupt",
    }):
        assert speak_priority_for("CONFLICT_DETECTED") == "INTERRUPT"
        assert speak_priority_for("DECISION_APPROVED") == "APPEND"
    with patch.dict("os.environ", {"INTERVENTION_HIGH_PRIORITY_MODE": "queue"}):
        assert speak_priority_for("CONFLICT_DETECTED") == "APPEND"


def test_speak_dispatch_emits_started_event_and_persists_ai_transcript(db):
    from app.services.incident_state_stream import INCIDENT_STATE_BROADCASTER
    from app.services.voice_interventions import speak_to_channel

    row = InterventionRow(
        id="voice-1", trigger_type="CONFLICT_DETECTED", severity="HIGH",
        confidence=0.9, message="Pause and verify the conflicting database evidence.",
        related_claim_ids=["c1", "c2"], status="PENDING", agent_id="agent-voice",
    )
    db.add(row)
    db.commit()
    ACTIVE_AGENT_SESSIONS["agent-voice"] = {
        "channel": "incident-room", "agent_uid": "1000", "speaker_uid": "42",
    }
    queue = INCIDENT_STATE_BROADCASTER.subscribe()

    async def run():
        with patch("app.services.voice_interventions.AGORA_AGENT.speak", new=AsyncMock(
            return_value={"status": "QUEUED", "request_id": "speak-1"}
        )) as speak, patch("app.services.voice_interventions.log_event") as logged:
            response = await speak_to_channel("agent-voice", row, db)
            event = await asyncio.wait_for(queue.get(), timeout=0.2)
            return response, event, speak, logged

    try:
        response, event, speak, logged = asyncio.run(run())
    finally:
        INCIDENT_STATE_BROADCASTER.unsubscribe(queue)
        ACTIVE_AGENT_SESSIONS.pop("agent-voice", None)

    assert response["request_id"] == "speak-1"
    assert speak.await_args.args[1].priority == "INTERRUPT"
    assert event["type"] == "speaking_started"
    assert event["trigger_type"] == "CONFLICT_DETECTED"
    assert db.query(InterventionRow).filter_by(id="voice-1").one().status == "SPOKEN"
    ai_transcript = db.query(__import__("app.models", fromlist=["EventLog"]).EventLog).filter_by(
        event_type="TRANSCRIPT_CHUNK"
    ).one()
    assert ai_transcript.payload["speaker"] == "ResQVoice AI"
    success_log = next(call for call in logged.call_args_list if call.args[0] == "SPEAK_ATTEMPT_SUCCEEDED")
    assert success_log.kwargs["raw_response"]["request_id"] == "speak-1"


def test_intervention_severity_cooldown_and_human_speech_guard(db):
    policy = InterventionPolicy()
    high = InterventionCandidate(
        trigger_type="HIGH_SEVERITY_CONTRADICTION", severity="HIGH",
        confidence=0.9, message="Verify the conflicting database evidence.",
        related_claim_ids=["claim-a", "claim-b"],
    )
    with patch.dict("os.environ", {
        "INTERVENTION_MIN_SEVERITY": "HIGH",
        "INTERVENTION_COOLDOWN_SECONDS": "120",
    }):
        low = policy.admit(InterventionCandidate(
            trigger_type="LOW_NOTE", severity="LOW", confidence=0.5,
            message="A low-priority note.",
        ), db)
        deferred = policy.admit(high, db, human_speaking=True)
        duplicate = policy.admit(high, db, human_speaking=False)

    assert low is None
    assert deferred is not None and deferred.status == "DEFERRED"
    assert duplicate is None
    assert db.query(InterventionRow).count() == 1


def test_intervention_expiration_prevents_marking_old_item_spoken(db):
    policy = InterventionPolicy()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    row = InterventionRow(
        id="expired-1", trigger_type="STALE_CRITICAL_CLAIM", severity="CRITICAL",
        confidence=0.9, message="Investigate compromise.", related_claim_ids=["c1"],
        created_at=old, expires_at=old + timedelta(seconds=30),
        status="PENDING",
    )
    db.add(row)
    db.commit()

    result = policy.mark_spoken("expired-1", db)
    assert result.status == "EXPIRED"
    assert result.spoken_at is None
