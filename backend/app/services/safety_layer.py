"""SOP-gated recommendation, approval, mocked execution, and verification."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.models import Decision as DecisionRow
from app.models import EventLog, Fact as FactRow
from app.schemas import (
    ApprovalRecord,
    Decision,
    ExecutionResult,
    Fact,
    PotentialSOPConflict,
    SafetyRecommendation,
    VerificationResult,
)
from app.services.sop_knowledge import embedding_similarity, retrieve_relevant_sop


POTENTIAL_SOP_CONFLICT = "POTENTIAL_SOP_CONFLICT"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
COMPLETED = "COMPLETED"

PROTECTED_ACTION_PHRASES = (
    "rollback",
    "restart production",
    "production config",
    "production configuration",
    "scale infrastructure",
    "shut down",
    "shutdown",
    "trigger escalation",
    "send external",
    "external incident communication",
)


def is_protected_action(action: str) -> bool:
    normalized = " ".join(action.lower().split())
    return any(phrase in normalized for phrase in PROTECTED_ACTION_PHRASES)


def _decision_schema(row: DecisionRow) -> Decision:
    return Decision.model_validate(
        {
            "id": row.id,
            "recommendation": row.recommendation or "",
            "evidence": list(row.evidence or []),
            "sop_reference": row.sop_reference or "",
            "approved_by": row.approved_by,
            "approval_time": row.approval_time,
            "execution_status": row.execution_status or "",
            "result": row.result,
        }
    )


def _preflight_steps(required_steps: list[str]) -> list[str]:
    """Steps before approval/execution are required at recommendation time."""
    for index, step in enumerate(required_steps):
        if "approval" in step.lower() or "approve" in step.lower():
            return required_steps[:index]
    return required_steps


def _step_matches(candidate: str, expected: str) -> bool:
    operation_groups = (
        {"confirm", "confirmed", "assess", "assessed", "determine", "determined"},
        {"capture", "captured", "collect", "collected", "snapshot", "baseline"},
        {"identify", "identified", "validate", "validated", "choose", "selected"},
        {"obtain", "obtained", "request", "requested", "approve", "approved"},
        {"execute", "executed", "perform", "performed", "run", "ran"},
        {"verify", "verified", "check", "checked", "measure", "measured"},
        {"record", "recorded", "document", "documented", "communicate", "communicated"},
    )
    candidate_tokens = set(candidate.lower().replace("-", " ").split())
    expected_tokens = set(expected.lower().replace("-", " ").split())
    same_operation = any(
        candidate_tokens & group and expected_tokens & group
        for group in operation_groups
    )
    return same_operation and embedding_similarity(candidate, expected) >= 0.2


def recommend_action(
    proposed_action: str,
    evidence_trail: list[str],
    completed_steps: list[str],
    db: Session,
) -> SafetyRecommendation:
    """Persist a gated Decision after checking action preparation against its SOP."""
    action = proposed_action.strip()
    if not action:
        raise ValueError("proposed_action must not be empty.")
    sop = retrieve_relevant_sop(action)
    preflight = _preflight_steps(sop.required_steps)
    matched_indices: list[int] = []
    missing_steps: list[str] = []
    for expected in preflight:
        match = next(
            (
                candidate_index
                for candidate_index, candidate in enumerate(completed_steps)
                if candidate_index not in matched_indices and _step_matches(candidate, expected)
            ),
            None,
        )
        if match is None:
            missing_steps.append(expected)
        else:
            matched_indices.append(match)

    out_of_order = matched_indices != sorted(matched_indices)
    # Mentioning an execution step while prerequisites remain missing is also an
    # ordering conflict, even though post-approval steps are not preflight inputs.
    if missing_steps and any(
        _step_matches(candidate, step)
        for candidate in completed_steps
        for step in sop.required_steps[len(preflight):]
    ):
        out_of_order = True

    conflict = None
    status = PENDING_APPROVAL
    result = None
    if missing_steps or out_of_order:
        conflict = PotentialSOPConflict(
            proposed_action=action,
            sop_reference=sop.reference,
            missing_steps=missing_steps,
            out_of_order=out_of_order,
            recommended_sequence=sop.required_steps,
        )
        status = POTENTIAL_SOP_CONFLICT
        result = "Potential SOP Conflict: correct the required sequence before approval."

    confidence = min(0.99, 0.55 + (0.3 * sop.score) + (0.04 * len(evidence_trail)))
    row = DecisionRow(
        id=str(uuid.uuid4()),
        recommendation=action,
        evidence=list(evidence_trail),
        sop_reference=sop.reference,
        approved_by=None,
        approval_time=None,
        execution_status=status,
        result=result,
    )
    db.add(row)
    db.add(EventLog(
        event_type="SAFETY_RECOMMENDATION",
        payload={
            "decision_id": row.id,
            "protected_action": is_protected_action(action),
            "confidence": round(confidence, 4),
            "sop_reference": sop.reference,
            "conflict": conflict.model_dump(mode="json") if conflict else None,
        },
    ))
    db.commit()
    db.refresh(row)
    return SafetyRecommendation(
        decision=_decision_schema(row),
        sop_match=sop,
        confidence=round(confidence, 4),
        conflict=conflict,
    )


def approve_decision(
    decision_id: str,
    approved_by: str,
    db: Session,
    *,
    now: datetime | None = None,
) -> ApprovalRecord:
    row = db.query(DecisionRow).filter(DecisionRow.id == decision_id).first()
    if row is None:
        raise ValueError(f"Decision '{decision_id}' not found.")
    approver = approved_by.strip()
    if not approver:
        raise ValueError("approved_by must identify a human approver.")
    if row.execution_status == POTENTIAL_SOP_CONFLICT:
        raise RuntimeError("Potential SOP Conflict must be corrected before approval.")
    if row.execution_status != PENDING_APPROVAL:
        raise RuntimeError(f"Decision cannot be approved from '{row.execution_status}'.")

    approval_time = now or datetime.now(timezone.utc)
    row.approved_by = approver
    row.approval_time = approval_time
    row.execution_status = APPROVED
    db.add(EventLog(
        event_type="DECISION_APPROVED",
        payload={
            "decision_id": row.id,
            "approved_by": approver,
            "approval_time": approval_time.isoformat(),
        },
    ))
    db.commit()
    return ApprovalRecord(
        decision_id=row.id,
        approved_by=approver,
        approval_time=approval_time,
    )


def _mock_executor(action: str) -> str:
    return f"Mock execution accepted for: {action}"


def execute_decision(
    decision_id: str,
    db: Session,
    *,
    executor: Callable[[str], str] = _mock_executor,
) -> ExecutionResult:
    row = db.query(DecisionRow).filter(DecisionRow.id == decision_id).first()
    if row is None:
        raise ValueError(f"Decision '{decision_id}' not found.")
    if not row.approved_by or row.approval_time is None or row.execution_status != APPROVED:
        raise PermissionError("Explicit human approval is required before execution.")

    execution_output = executor(row.recommendation or "")
    row.execution_status = AWAITING_VERIFICATION
    row.result = f"{execution_output}; verification required."
    db.add(EventLog(
        event_type="DECISION_EXECUTED",
        payload={"decision_id": row.id, "mocked": True, "result": execution_output},
    ))
    db.commit()
    db.refresh(row)
    return ExecutionResult(
        decision=_decision_schema(row),
        executed=True,
        verification_required=True,
    )


def verify_decision(
    decision_id: str,
    fact_id: str,
    observed_metric: str,
    source: str,
    verified_by: str,
    db: Session,
    *,
    now: datetime | None = None,
) -> VerificationResult:
    row = db.query(DecisionRow).filter(DecisionRow.id == decision_id).first()
    if row is None:
        raise ValueError(f"Decision '{decision_id}' not found.")
    if row.execution_status != AWAITING_VERIFICATION:
        raise RuntimeError("Decision must be executed before it can be verified.")
    fact = db.query(FactRow).filter(FactRow.id == fact_id).first()
    if fact is None:
        raise ValueError(f"Linked Fact '{fact_id}' not found.")
    metric = observed_metric.strip()
    verifier = verified_by.strip()
    if not metric or not verifier:
        raise ValueError("observed_metric and verified_by are required.")

    verified_at = now or datetime.now(timezone.utc)
    fact.description = metric
    fact.source = source.strip() or fact.source
    fact.speaker = verifier
    fact.timestamp = verified_at
    fact.status = "VERIFIED"
    row.execution_status = COMPLETED
    row.result = f"Verified by {verifier}: {metric}"
    db.add(EventLog(
        event_type="DECISION_VERIFIED",
        payload={
            "decision_id": row.id,
            "fact_id": fact.id,
            "verified_by": verifier,
            "observed_metric": metric,
        },
    ))
    db.commit()
    db.refresh(row)
    db.refresh(fact)
    return VerificationResult(
        decision=_decision_schema(row),
        fact=Fact.model_validate({
            "id": fact.id,
            "description": fact.description,
            "source": fact.source or "",
            "speaker": fact.speaker or "",
            "timestamp": fact.timestamp,
            "status": fact.status or "VERIFIED",
        }),
    )
