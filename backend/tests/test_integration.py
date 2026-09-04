"""
End-to-end integration test for the Incident Response pipeline.

Replays the `payment_outage_transcript.json` fixture and tests the full
orchestration flow:
  1. Transcript parsing (mocked extractor) -> Claim creation.
  2. Silence Signal detects stale critical claims.
  3. Orchestrator ingests new evidence.
  4. Claim Lifecycle state transitions.
  5. Contradiction Radar re-evaluates conflicts.
  6. Evidence Graph reflects the final state.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event as sa_event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator, Text

# ── ARRAY→JSON shim ──────────────────────────────────────────────────────────
import sqlalchemy as sa


class ArrayAsJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return "[]"
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return []
        return json.loads(value)


sa.ARRAY = lambda item_type, *a, **kw: ArrayAsJSON()  # type: ignore[assignment]


# ── App imports ───────────────────────────────────────────────────────────────
from app.database import Base
from app import schemas as app_schemas
from app.models import Claim, Conflict, Evidence, Hypothesis, Unknown
from app.schemas import Evidence as EvidenceInput
from app.schemas import EvidenceTargetType, EvidenceType
from app.services.claim_lifecycle import ClaimStatusUpdate as LifecycleStatusUpdate
from app.services.contradiction_radar import ComparisonResult
from app.services.contradiction_radar import ConflictRecheckResult as RadarRecheckResult
from app.services.event_store import process_transcript_chunk
from app.services.evidence_graph import GraphData as EvidenceGraphData
from app.services.evidence_graph import get_graph, get_provenance
from app.services.extractor import ExtractedData
from app.services.orchestrator import ingest_evidence
from app.services.silence_signal import Alert as SilenceAlert
from app.services.silence_signal import scan_for_unresolved
from shared.python import schemas as shared_schemas


# ── Setup & Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(engine, "connect")
    def _pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def load_transcript():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "payment_outage_transcript.json")
    with open(path, "r") as f:
        return json.load(f)


def _mock_extractor(*args, **kwargs):
    # This mock represents what the Gemini structured extractor WOULD return
    # if it processed the payment_outage_transcript.json chunks.
    # We return an ExtractedData payload with hardcoded Claims for the test.
    text = args[2]
    
    claims = []
    if "100%" in text:  # Arjun's database CPU claim
        claims.append({
            "id": "claim-db-cpu",
            "text": "The primary DB CPU is at 100% and there are stuck queries.",
            "speaker": "Arjun",
            "role": "Database Engineer",
            "timestamp": args[3],
            "status": "UNVERIFIED",
            "supporting": [],
            "contradicting": []
        })
    elif "pricing tier deployment" in text:  # Meera's hypothesis
        claims.append({
            "id": "hypo-pricing",
            "text": "The new pricing tier deployment might be causing inefficient queries.",
            "speaker": "Meera",
            "role": "DevOps/SRE",
            "timestamp": args[3],
            "status": "UNVERIFIED",
            "supporting": [],
            "contradicting": []
        })
        
    return ExtractedData(
        facts=[],
        hypotheses=[
            {
                "id": c["id"], 
                "description": c["text"], 
                "origin": c["speaker"], 
                "supporting_evidence": [],
                "contradicting_evidence": [],
                "confidence": 0.5,
                "status": "UNCONFIRMED"
            } for c in claims if "hypo" in c["id"]
        ],
        claims=[c for c in claims if "claim" in c["id"]],
        actions=[],
        decisions=[]
    )


def _mock_comparator(text_a: str, text_b: str) -> ComparisonResult:
    # A simple mock comparator for contradiction radar
    has_100 = "100%" in text_a or "100%" in text_b
    has_normal = "normal" in text_a.lower() or "normal" in text_b.lower()
    if has_100 and has_normal:
        return ComparisonResult(
            contradicts=True,
            topic="database-performance",
            reason="One claim says 100% CPU, the other says it is normal.",
            recommended_verification="Check metrics dashboard."
        )
    return ComparisonResult(contradicts=False, topic="", reason="", recommended_verification="")


# ── Tests ─────────────────────────────────────────────────────────────────────

@patch("app.services.extractor.extract_claims", side_effect=_mock_extractor)
def test_end_to_end_orchestration(mock_extract, db):
    # The backend facade must expose the exact shared classes, not copies.
    assert app_schemas.Evidence is shared_schemas.Evidence
    assert app_schemas.Conflict is shared_schemas.Conflict
    assert app_schemas.Unknown is shared_schemas.Unknown
    assert app_schemas.GraphData is shared_schemas.GraphData
    assert LifecycleStatusUpdate is shared_schemas.ClaimStatusUpdate
    assert RadarRecheckResult is shared_schemas.ConflictRecheckResult
    assert EvidenceGraphData is shared_schemas.GraphData
    assert SilenceAlert is shared_schemas.SilenceAlert

    database_columns = {
        table: {column["name"] for column in inspect(db.bind).get_columns(table)}
        for table in ("evidence", "unknowns")
    }
    assert {"target_id", "target_type"} <= database_columns["evidence"]
    assert "claim_id" not in database_columns["evidence"]
    assert "source_id" in database_columns["unknowns"]
    assert "linked_action_id" not in database_columns["unknowns"]

    transcript = load_transcript()
    
    # 1. Replay the transcript through the event store
    for chunk in transcript:
        process_transcript_chunk(
            db=db,
            speaker=chunk["speaker"],
            role=chunk["role"],
            text=chunk["text"],
            timestamp=chunk["timestamp"]
        )
    
    # Verify claims were extracted and persisted
    claims = db.query(Claim).all()
    assert len(claims) == 1
    assert claims[0].id == "claim-db-cpu"
    
    hypos = db.query(Hypothesis).all()
    assert len(hypos) == 1
    assert hypos[0].id == "hypo-pricing"
    
    # 2. Silence Signal (fast forward time)
    # The claim contains "stuck queries", "100%". 
    # Actually our critical regex has "critical failure" but not "100%". 
    # Let's manually force the claim text in the DB to include a critical keyword to test silence signal.
    c = db.query(Claim).first()
    c.text = "The primary DB is experiencing a critical outage with 100% CPU."
    db.commit()
    
    # Now simulate 15 minutes passing
    future_time = datetime(2023, 10, 27, 10, 20, 0, tzinfo=timezone.utc)
    alerts = scan_for_unresolved("INC-001", db, now=future_time, staleness_window_sec=600)
    
    stale_alerts = [a for a in alerts if a.alert_type == "STALE_CRITICAL_CLAIM"]
    assert len(stale_alerts) == 1
    assert stale_alerts[0].source_id == "claim-db-cpu"
    silence_row = db.query(Unknown).filter(Unknown.source_id == "claim-db-cpu").one()
    assert silence_row.status == "OPEN"

    # 3. Orchestrator ingests new evidence
    evidence1 = EvidenceInput(
        id=str(uuid.uuid4()),
        target_id="claim-db-cpu",
        target_type=EvidenceTargetType.CLAIM,
        type=EvidenceType.SUPPORTING,
        description="Datadog dashboard confirms CPU is pegged at 100%.",
        source="Datadog"
    )
    result1 = ingest_evidence(evidence1, db, comparator=_mock_comparator)
    
    # 4. Verify Claim Lifecycle transitioned it
    assert result1.lifecycle_update.previous_status == "UNVERIFIED"
    assert result1.lifecycle_update.new_status == "CORROBORATED"
    assert result1.silence_reset is True
    assert result1.provenance.status == "CORROBORATED"
    assert result1.provenance.supporting_evidence[0].id == evidence1.id
    assert any(
        edge.source == evidence1.id
        and edge.target == "claim-db-cpu"
        and edge.relation == "supports"
        for edge in result1.evidence_graph.edges
    )
    
    c = db.query(Claim).first()
    assert c.status == "CORROBORATED"
    assert len(c.supporting) == 1
    db.refresh(silence_row)
    assert silence_row.status == "RESOLVED"

    stored_evidence1 = db.query(Evidence).filter(Evidence.id == evidence1.id).one()
    assert stored_evidence1.target_id == "claim-db-cpu"
    assert stored_evidence1.target_type == "claim"
    assert stored_evidence1.type == "supporting"
    
    # Add a second piece of supporting evidence to confirm it
    evidence2 = EvidenceInput(
        id=str(uuid.uuid4()),
        target_id="claim-db-cpu",
        target_type=EvidenceTargetType.CLAIM,
        type=EvidenceType.SUPPORTING,
        description="AWS RDS console shows zero burst balance left.",
        source="AWS"
    )
    result2 = ingest_evidence(evidence2, db, comparator=_mock_comparator)
    assert result2.lifecycle_update.new_status == "CONFIRMED"
    assert result2.provenance.status == "CONFIRMED"
    assert len(result2.provenance.supporting_evidence) == 2
    
    # 5. Silence Signal should be reset (no longer alert)
    # The claim is now CONFIRMED (terminal) AND has evidence.
    alerts_after = scan_for_unresolved("INC-001", db, now=future_time, staleness_window_sec=600)
    stale_alerts_after = [a for a in alerts_after if a.alert_type == "STALE_CRITICAL_CLAIM"]
    assert len(stale_alerts_after) == 0

    # 6. Evidence Graph verification
    prov = get_provenance("claim-db-cpu", db)
    assert len(prov.supporting_evidence) == 2
    assert prov.status == "CONFIRMED"
    
    graph = get_graph(db)
    # Should contain 1 claim, 1 hypothesis, and 2 evidence nodes (total 4)
    assert len(graph.nodes) == 4
    # Should have 2 edges supporting the claim
    assert len(graph.edges) == 2
    
    # 7. Contradiction Radar triggers on new contradicting evidence
    # First, let's add a NEW claim that contradicts
    c_new = Claim(
        id="claim-db-fine",
        text="The database is operating normally.",
        speaker="System",
        role="Monitor",
        timestamp=datetime(2023, 10, 27, 10, 25, 0),
        status="UNVERIFIED"
    )
    db.add(c_new)
    db.commit()
    
    # Now someone adds evidence to this new claim
    evidence3 = EvidenceInput(
        id=str(uuid.uuid4()),
        target_id="claim-db-fine",
        target_type=EvidenceTargetType.CLAIM,
        type=EvidenceType.SUPPORTING,
        description="Health check is 200 OK.",
        source="Pingdom"
    )
    result3 = ingest_evidence(evidence3, db, comparator=_mock_comparator)
    
    # The orchestrator should have flagged a conflict between claim-db-fine and claim-db-cpu
    assert len(result3.new_conflicts) == 1
    conflict = db.query(Conflict).filter(Conflict.id == result3.new_conflicts[0]).one()
    assert {conflict.claim_a_id, conflict.claim_b_id} == {
        "claim-db-cpu",
        "claim-db-fine",
    }
    assert conflict.status == "UNRESOLVED"

    # 8. Assert Final Incident State matches expected statuses
    final_incident_state = {
        "claims": {
            row.id: row.status
            for row in db.query(Claim).order_by(Claim.id).all()
        },
        "hypotheses": {
            row.id: row.status.value
            for row in db.query(Hypothesis).order_by(Hypothesis.id).all()
        },
        "conflicts": {
            tuple(sorted((row.claim_a_id, row.claim_b_id))): row.status
            for row in db.query(Conflict).all()
        },
        "silence_signals": {
            row.source_id: row.status
            for row in db.query(Unknown).all()
        },
    }
    assert final_incident_state == {
        "claims": {
            "claim-db-cpu": "CONFIRMED",
            "claim-db-fine": "CORROBORATED",
        },
        "hypotheses": {"hypo-pricing": "UNCONFIRMED"},
        "conflicts": {
            ("claim-db-cpu", "claim-db-fine"): "UNRESOLVED",
        },
        "silence_signals": {"claim-db-cpu": "RESOLVED"},
    }

    # Ensure silence signal was removed for the confirmed claim
    final_unknowns = scan_for_unresolved("INC-001", db, now=future_time, staleness_window_sec=600)
    assert not any(a.source_id == "claim-db-cpu" for a in final_unknowns)
