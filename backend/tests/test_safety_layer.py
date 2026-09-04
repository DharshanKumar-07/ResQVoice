"""Integration coverage for SOP-gated recommendation through audit completion."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event as sa_event
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
from app.models import Decision, EventLog, Fact
from app.services.safety_layer import (
    APPROVED,
    AWAITING_VERIFICATION,
    COMPLETED,
    PENDING_APPROVAL,
    POTENTIAL_SOP_CONFLICT,
    approve_decision,
    execute_decision,
    recommend_action,
    verify_decision,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")

    @sa_event.listens_for(engine, "connect")
    def _foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def test_sop_conflict_is_flagged_before_execution(db):
    result = recommend_action(
        "Rollback the pricing tier deployment",
        ["Database CPU is at 100%."],
        ["Execute the rollback immediately."],
        db,
    )

    assert result.sop_match.reference == "docs/sops/production_rollback.md"
    assert result.conflict is not None
    assert result.conflict.out_of_order is True
    assert len(result.conflict.missing_steps) == 3
    assert result.decision.execution_status == POTENTIAL_SOP_CONFLICT
    assert result.decision.approved_by is None
    assert len(result.conflict.recommended_sequence) == 7

    executor = MagicMock()
    with pytest.raises(RuntimeError, match="SOP Conflict"):
        approve_decision(result.decision.id, "Priya", db)
    with pytest.raises(PermissionError, match="approval"):
        execute_decision(result.decision.id, db, executor=executor)
    executor.assert_not_called()


def test_action_never_executes_without_complete_approval_record(db):
    row = Decision(
        id="decision-without-approval",
        recommendation="Restart production service",
        evidence=["Health checks are failing"],
        sop_reference="docs/sops/server_shutdown.md",
        approved_by=None,
        approval_time=None,
        execution_status=APPROVED,
        result=None,
    )
    db.add(row)
    db.commit()
    executor = MagicMock()

    with pytest.raises(PermissionError, match="Explicit human approval"):
        execute_decision(row.id, db, executor=executor)

    executor.assert_not_called()
    db.refresh(row)
    assert row.execution_status == APPROVED
    assert row.result is None


def test_full_rollback_recommend_approve_execute_verify_audit_cycle(db):
    fact = Fact(
        id="fact-payment-error-rate",
        description="Payment error rate is 38%",
        source="Datadog",
        speaker="Monitoring",
        timestamp=datetime(2023, 10, 27, 10, 5),
        status="UNVERIFIED",
    )
    db.add(fact)
    db.commit()

    completed_preflight = [
        "Confirm the incident symptoms and identify the affected production service and deployment.",
        "Capture the current release version, error rate, latency, and customer-impact baseline.",
        "Identify and validate the last known-good release and confirm rollback compatibility.",
    ]
    recommendation = recommend_action(
        "Rollback the pricing tier deployment",
        [
            "The primary DB CPU is at 100%.",
            "The pricing tier deployment preceded the payment outage.",
        ],
        completed_preflight,
        db,
    )

    assert recommendation.conflict is None
    assert recommendation.confidence > 0.7
    assert recommendation.decision.execution_status == PENDING_APPROVAL
    assert recommendation.decision.evidence == [
        "The primary DB CPU is at 100%.",
        "The pricing tier deployment preceded the payment outage.",
    ]
    assert recommendation.decision.sop_reference == "docs/sops/production_rollback.md"
    assert recommendation.decision.approved_by is None
    assert recommendation.decision.approval_time is None
    assert recommendation.decision.result is None

    approved_at = datetime(2023, 10, 27, 10, 6, tzinfo=timezone.utc)
    approval = approve_decision(
        recommendation.decision.id,
        "Priya (Incident Commander)",
        db,
        now=approved_at,
    )
    assert approval.approved_by == "Priya (Incident Commander)"

    executor = MagicMock(return_value="Rollback job deploy-42 completed")
    execution = execute_decision(recommendation.decision.id, db, executor=executor)
    executor.assert_called_once_with("Rollback the pricing tier deployment")
    assert execution.executed is True
    assert execution.verification_required is True
    assert execution.decision.execution_status == AWAITING_VERIFICATION
    db.refresh(fact)
    assert fact.status == "UNVERIFIED"

    verification = verify_decision(
        recommendation.decision.id,
        fact.id,
        "Payment error rate returned to 0.4% and p95 latency is 180 ms",
        "Datadog post-rollback dashboard",
        "Arjun",
        db,
        now=datetime(2023, 10, 27, 10, 8, tzinfo=timezone.utc),
    )
    assert verification.decision.execution_status == COMPLETED
    assert verification.fact.status == "VERIFIED"
    assert verification.fact.description.startswith("Payment error rate returned")

    audit = db.query(Decision).filter(Decision.id == recommendation.decision.id).one()
    assert audit.recommendation == "Rollback the pricing tier deployment"
    assert audit.evidence == recommendation.decision.evidence
    assert audit.sop_reference == "docs/sops/production_rollback.md"
    assert audit.approved_by == "Priya (Incident Commander)"
    assert audit.approval_time is not None
    assert audit.execution_status == COMPLETED
    assert audit.result == (
        "Verified by Arjun: Payment error rate returned to 0.4% and p95 latency is 180 ms"
    )

    assert [
        event.event_type
        for event in db.query(EventLog)
        .filter(EventLog.event_type.like("%DECISION%") | (EventLog.event_type == "SAFETY_RECOMMENDATION"))
        .order_by(EventLog.id)
        .all()
    ] == [
        "SAFETY_RECOMMENDATION",
        "DECISION_APPROVED",
        "DECISION_EXECUTED",
        "DECISION_VERIFIED",
    ]
