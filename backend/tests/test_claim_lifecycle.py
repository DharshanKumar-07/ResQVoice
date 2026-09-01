"""
Unit tests for ClaimLifecycleEngine and the DB adapters.

All tests run against an in-memory SQLite database — no PostgreSQL, no
Gemini API key, no Agora, no running backend required.

Coverage:
    Engine (pure, no DB):
        TC-01  UNVERIFIED + supporting  → CORROBORATED
        TC-02  UNCONFIRMED + supporting → CORROBORATED
        TC-03  UNVERIFIED + contradicting → DISPUTED
        TC-04  CORROBORATED + supporting (2 total) → CONFIRMED
        TC-05  CORROBORATED + supporting (still 1) → stays CORROBORATED
        TC-06  CORROBORATED + contradicting → DISPUTED
        TC-07  CONFIRMED + contradicting → DISPUTED (re-opened)
        TC-08  CONFIRMED + supporting → no change
        TC-09  DISPUTED + supporting dominates → RESOLVED (Claims)
        TC-10  DISPUTED + contradicting dominates → REJECTED
        TC-11  RESOLVED is terminal (supporting ignored)
        TC-12  REJECTED is terminal (any evidence ignored)

    DB adapters:
        TC-13  apply_evidence persists new status to DB
        TC-14  apply_evidence raises ValueError for unknown claim_id
        TC-15  apply_evidence_to_hypothesis uses CONFIRMED (not RESOLVED) for disputed positive
        TC-16  apply_evidence_to_hypothesis raises ValueError for unknown hypothesis_id
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

# ── In-memory SQLite setup ────────────────────────────────────────────────────
# We swap out PostgreSQL-specific column types so the test suite runs without
# a live database.  The ARRAY columns are emulated via JSON.

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator, Text
import json


class ArrayAsJSON(TypeDecorator):
    """Stores a Python list as a JSON string in SQLite."""
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


# Monkey-patch ARRAY used inside app.models so SQLite doesn't choke
import sqlalchemy
_original_array = sqlalchemy.ARRAY

def _patched_array(item_type, *args, **kwargs):
    return ArrayAsJSON()

sqlalchemy.ARRAY = _patched_array

# Also patch the import in app.models
import sqlalchemy as sa
sa.ARRAY = _patched_array

# Now import our app modules (they must see the patched ARRAY)
from app.database import Base
from app.models import Claim, Hypothesis, HypothesisStatus
from app.services.claim_lifecycle import (
    ClaimLifecycleEngine,
    ClaimStatusUpdate,
    EvidenceInput,
    EvidenceType,
    CLAIM_TERMINAL_STATUSES,
    HYPOTHESIS_TERMINAL_STATUSES,
    apply_evidence,
    apply_evidence_to_hypothesis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db():
    """Provides a fresh in-memory SQLite session for each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    # SQLite foreign-key support
    @sa_event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _make_evidence(
    claim_id: str,
    ev_type: EvidenceType,
    ev_id: str | None = None,
) -> EvidenceInput:
    return EvidenceInput(
        id=ev_id or str(uuid.uuid4()),
        claim_id=claim_id,
        type=ev_type,
        description="Synthetic evidence for unit testing",
        source="test-suite",
    )


def _make_engine(
    status: str,
    supporting: list[str] | None = None,
    contradicting: list[str] | None = None,
    terminal: set[str] | None = None,
    positive: str = "RESOLVED",
) -> ClaimLifecycleEngine:
    return ClaimLifecycleEngine(
        current_status=status,
        supporting=supporting or [],
        contradicting=contradicting or [],
        terminal_statuses=terminal or CLAIM_TERMINAL_STATUSES,
        disputed_positive_outcome=positive,
    )


def _claim(claim_id: str, status: str, supporting=None, contradicting=None) -> Claim:
    return Claim(
        id=claim_id,
        text="Test claim text",
        speaker="Alice",
        role="Incident Commander",
        timestamp=datetime.utcnow(),
        status=status,
        supporting=supporting or [],
        contradicting=contradicting or [],
    )


def _hypothesis(hypo_id: str, status: str, supporting=None, contradicting=None) -> Hypothesis:
    return Hypothesis(
        id=hypo_id,
        description="Test hypothesis",
        origin="Bob",
        supporting_evidence=supporting or [],
        contradicting_evidence=contradicting or [],
        confidence=0.5,
        status=status,
    )


# ── Engine tests (pure, no DB) ────────────────────────────────────────────────

class TestEngineTransitions:

    def test_tc01_unverified_plus_supporting_to_corroborated(self):
        """TC-01: UNVERIFIED + 1 supporting → CORROBORATED"""
        engine = _make_engine("UNVERIFIED")
        ev = _make_evidence("c1", EvidenceType.SUPPORTING, "e1")
        result = engine.apply(ev)
        assert result.new_status == "CORROBORATED"
        assert result.changed is True
        assert result.previous_status == "UNVERIFIED"

    def test_tc02_unconfirmed_plus_supporting_to_corroborated(self):
        """TC-02: UNCONFIRMED + 1 supporting → CORROBORATED"""
        engine = _make_engine("UNCONFIRMED")
        ev = _make_evidence("c1", EvidenceType.SUPPORTING)
        result = engine.apply(ev)
        assert result.new_status == "CORROBORATED"
        assert result.changed is True

    def test_tc03_unverified_plus_contradicting_to_disputed(self):
        """TC-03: UNVERIFIED + contradicting → DISPUTED"""
        engine = _make_engine("UNVERIFIED")
        ev = _make_evidence("c1", EvidenceType.CONTRADICTING)
        result = engine.apply(ev)
        assert result.new_status == "DISPUTED"
        assert result.changed is True

    def test_tc04_corroborated_plus_second_supporting_to_confirmed(self):
        """TC-04: CORROBORATED + 2nd supporting (total ≥ 2, no contradicting) → CONFIRMED"""
        engine = _make_engine("CORROBORATED", supporting=["e1"])
        ev = _make_evidence("c1", EvidenceType.SUPPORTING, "e2")
        result = engine.apply(ev)
        assert result.new_status == "CONFIRMED"
        assert result.changed is True

    def test_tc05_corroborated_first_supporting_stays_corroborated(self):
        """TC-05: CORROBORATED with only 1 total supporting → stays CORROBORATED (threshold not met yet)"""
        # Engine starts with 0 supporting (we apply first piece here)
        engine = _make_engine("CORROBORATED", supporting=[])
        ev = _make_evidence("c1", EvidenceType.SUPPORTING, "e1")
        result = engine.apply(ev)
        # 1 supporting total — not yet ≥ 2, so stays CORROBORATED
        assert result.new_status == "CORROBORATED"
        assert result.changed is False

    def test_tc06_corroborated_plus_contradicting_to_disputed(self):
        """TC-06: CORROBORATED + contradicting → DISPUTED"""
        engine = _make_engine("CORROBORATED", supporting=["e1"])
        ev = _make_evidence("c1", EvidenceType.CONTRADICTING)
        result = engine.apply(ev)
        assert result.new_status == "DISPUTED"
        assert result.changed is True

    def test_tc07_confirmed_plus_contradicting_reopens_to_disputed(self):
        """TC-07: CONFIRMED + contradicting → DISPUTED (re-opened)"""
        engine = _make_engine("CONFIRMED", supporting=["e1", "e2"])
        ev = _make_evidence("c1", EvidenceType.CONTRADICTING)
        result = engine.apply(ev)
        assert result.new_status == "DISPUTED"
        assert result.changed is True

    def test_tc08_confirmed_plus_supporting_no_change(self):
        """TC-08: CONFIRMED + supporting → no change (CONFIRMED is stable for supporting)"""
        engine = _make_engine("CONFIRMED", supporting=["e1", "e2"])
        ev = _make_evidence("c1", EvidenceType.SUPPORTING)
        result = engine.apply(ev)
        assert result.new_status == "CONFIRMED"
        assert result.changed is False

    def test_tc09_disputed_supporting_dominates_to_resolved(self):
        """TC-09 (Claim): DISPUTED + supporting dominates → RESOLVED"""
        # 1 contradicting already; apply 2 supporting so sup(2) > con(1)
        engine = _make_engine(
            "DISPUTED",
            supporting=["e1"],
            contradicting=["e_contra"],
            positive="RESOLVED",
        )
        ev1 = _make_evidence("c1", EvidenceType.SUPPORTING, "e2")
        result = engine.apply(ev1)
        assert result.new_status == "RESOLVED"
        assert result.changed is True

    def test_tc10_disputed_contradicting_dominates_to_rejected(self):
        """TC-10: DISPUTED + contradicting dominates → REJECTED"""
        engine = _make_engine(
            "DISPUTED",
            supporting=["e1"],
            contradicting=["ec1"],
        )
        ev = _make_evidence("c1", EvidenceType.CONTRADICTING, "ec2")
        result = engine.apply(ev)
        # sup=1, con=2 → con dominates
        assert result.new_status == "REJECTED"
        assert result.changed is True

    def test_tc11_resolved_is_terminal(self):
        """TC-11: RESOLVED is terminal; supporting evidence does not change status"""
        engine = _make_engine("RESOLVED", supporting=["e1", "e2"])
        ev = _make_evidence("c1", EvidenceType.SUPPORTING)
        result = engine.apply(ev)
        assert result.new_status == "RESOLVED"
        assert result.changed is False
        assert "terminal" in result.reason.lower()

    def test_tc12_rejected_is_terminal(self):
        """TC-12: REJECTED is terminal; any evidence is ignored"""
        engine = _make_engine("REJECTED")
        for ev_type in (EvidenceType.SUPPORTING, EvidenceType.CONTRADICTING):
            ev = _make_evidence("c1", ev_type)
            result = engine.apply(ev)
            assert result.new_status == "REJECTED"
            assert result.changed is False


# ── DB adapter tests ──────────────────────────────────────────────────────────

class TestDBAdapters:

    def test_tc13_apply_evidence_persists_to_db(self, db):
        """TC-13: apply_evidence loads, transitions, and persists the new status."""
        claim_id = str(uuid.uuid4())
        claim = _claim(claim_id, "UNVERIFIED")
        db.add(claim)
        db.commit()

        ev = _make_evidence(claim_id, EvidenceType.SUPPORTING)
        result = apply_evidence(claim_id, ev, db)

        assert result.new_status == "CORROBORATED"
        assert result.changed is True

        # Verify the DB row was actually updated
        refreshed = db.query(Claim).filter(Claim.id == claim_id).first()
        assert refreshed.status == "CORROBORATED"
        assert ev.id in refreshed.supporting

    def test_tc14_apply_evidence_raises_for_unknown_claim(self, db):
        """TC-14: apply_evidence raises ValueError for an unknown claim_id."""
        ev = _make_evidence("nonexistent-id", EvidenceType.SUPPORTING)
        with pytest.raises(ValueError, match="not found"):
            apply_evidence("nonexistent-id", ev, db)

    def test_tc15_hypothesis_disputed_positive_is_confirmed(self, db):
        """TC-15: Hypothesis uses CONFIRMED (not RESOLVED) as disputed positive outcome."""
        hypo_id = str(uuid.uuid4())
        hypo = _hypothesis(
            hypo_id,
            status="DISPUTED",
            supporting=["e1", "e2"],   # sup=2
            contradicting=["ec1"],      # con=1
        )
        db.add(hypo)
        db.commit()

        # Add one more supporting to make sup(3) > con(1)
        ev = _make_evidence(hypo_id, EvidenceType.SUPPORTING, "e3")
        result = apply_evidence_to_hypothesis(hypo_id, ev, db)

        assert result.new_status == "CONFIRMED"
        assert result.changed is True

        refreshed = db.query(Hypothesis).filter(Hypothesis.id == hypo_id).first()
        # The ORM may return a HypothesisStatus enum or a prefixed string;
        # normalise to just the bare value for comparison.
        raw = str(refreshed.status)
        bare = raw.split(".")[-1]   # e.g. "HypothesisStatus.CONFIRMED" → "CONFIRMED"
        assert bare == "CONFIRMED"

    def test_tc16_apply_evidence_to_hypothesis_raises_for_unknown(self, db):
        """TC-16: apply_evidence_to_hypothesis raises ValueError for unknown hypothesis_id."""
        ev = _make_evidence("ghost-hypothesis", EvidenceType.SUPPORTING)
        with pytest.raises(ValueError, match="not found"):
            apply_evidence_to_hypothesis("ghost-hypothesis", ev, db)
