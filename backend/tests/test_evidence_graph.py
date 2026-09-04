"""
Unit tests for the Evidence Graph service.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event as sa_event
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
from app.models import Claim, Evidence, Hypothesis
from app.services.evidence_graph import get_provenance, get_graph

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


def test_get_provenance_claim(db):
    claim_id = str(uuid.uuid4())
    ev1_id = str(uuid.uuid4())
    ev2_id = str(uuid.uuid4())

    claim = Claim(
        id=claim_id,
        text="Test Claim",
        speaker="Test Speaker",
        role="SRE",
        timestamp=datetime.now(timezone.utc),
        status="UNVERIFIED",
        supporting=[ev1_id],
        contradicting=[ev2_id]
    )
    db.add(claim)
    
    ev1 = Evidence(id=ev1_id, target_id=claim_id, target_type="claim", type="supporting", description="Support", source="Log")
    ev2 = Evidence(id=ev2_id, target_id=claim_id, target_type="claim", type="contradicting", description="Contra", source="Log")
    db.add_all([ev1, ev2])
    db.commit()

    trace = get_provenance(claim_id, db)
    assert trace.id == claim_id
    assert trace.entity_type == "claim"
    assert trace.text == "Test Claim"
    assert len(trace.supporting_evidence) == 1
    assert trace.supporting_evidence[0].id == ev1_id
    assert len(trace.contradicting_evidence) == 1
    assert trace.contradicting_evidence[0].id == ev2_id

def test_get_provenance_hypothesis(db):
    hypo_id = str(uuid.uuid4())
    ev1_id = str(uuid.uuid4())

    hypo = Hypothesis(
        id=hypo_id,
        description="Test Hypothesis",
        origin="Test Speaker",
        status="UNCONFIRMED",
        supporting_evidence=[ev1_id],
        contradicting_evidence=[]
    )
    db.add(hypo)

    ev1 = Evidence(id=ev1_id, target_id=hypo_id, target_type="hypothesis", type="supporting", description="Support", source="Log")
    db.add(ev1)
    db.commit()

    trace = get_provenance(hypo_id, db)
    assert trace.id == hypo_id
    assert trace.entity_type == "hypothesis"
    assert trace.text == "Test Hypothesis"
    assert len(trace.supporting_evidence) == 1
    assert len(trace.contradicting_evidence) == 0

def test_get_provenance_not_found(db):
    with pytest.raises(ValueError):
        get_provenance("non-existent-id", db)

def test_get_graph(db):
    claim_id = str(uuid.uuid4())
    hypo_id = str(uuid.uuid4())
    ev1_id = str(uuid.uuid4())
    ev2_id = str(uuid.uuid4())

    db.add(Claim(id=claim_id, text="C1", status="UNVERIFIED"))
    db.add(Hypothesis(id=hypo_id, description="H1", status="UNCONFIRMED"))
    db.add(Evidence(id=ev1_id, target_id=claim_id, target_type="claim", type="supporting", description="E1"))
    db.add(Evidence(id=ev2_id, target_id=hypo_id, target_type="hypothesis", type="contradicting", description="E2"))
    db.commit()

    graph = get_graph(db)
    
    assert len(graph.nodes) == 4 # 1 claim, 1 hypo, 2 evidence
    node_ids = {n.id for n in graph.nodes}
    assert {claim_id, hypo_id, ev1_id, ev2_id} == node_ids

    assert len(graph.edges) == 2
    edges = {(e.source, e.target, e.relation) for e in graph.edges}
    assert (ev1_id, claim_id, "supports") in edges
    assert (ev2_id, hypo_id, "contradicts") in edges
