"""
Incident Orchestrator Service
=============================
Wires together the extraction pipeline, claim lifecycle, contradiction radar, and
silence signal services into a unified event-driven pipeline.

Public API
----------
    ingest_evidence(evidence: EvidenceInput, db: Session, comparator=None) -> OrchestrationResult
        The primary entrypoint for new evidence. Saves it, updates claim lifecycle,
        re-checks contradictions, and logs events.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import Claim, Evidence, Hypothesis
from app.schemas import EvidenceTargetType
from app.services.claim_lifecycle import (
    ClaimStatusUpdate,
    EvidenceInput,
    apply_evidence,
    apply_evidence_to_hypothesis,
)
from app.services.contradiction_radar import ClaimSnapshot, recheck_conflicts
from app.services.evidence_graph import GraphData, ProvenanceTrace, get_graph, get_provenance
from app.services.silence_signal import reset_silence_signal


class OrchestrationResult(BaseModel):
    """Result of an evidence ingestion flow."""
    evidence_id: str
    target_id: str
    target_type: EvidenceTargetType
    lifecycle_update: ClaimStatusUpdate
    new_conflicts: List[str]
    resolved_conflicts: List[str]
    evidence_graph: GraphData
    provenance: ProvenanceTrace
    silence_reset: bool


def ingest_evidence(
    evidence: EvidenceInput,
    db: Session,
    comparator=None,
) -> OrchestrationResult:
    """
    Ingest a new piece of Evidence, route it through the claim lifecycle, and
    re-evaluate contradictions if necessary.
    
    1. Persist Evidence.
    2. Route to claim_lifecycle for state machine transition.
    3. If target was a Claim, re-evaluate contradiction_radar.
    4. Materialize and verify the updated Evidence Graph view.
    5. Explicitly reset any persisted silence condition now resolved by evidence.

    All writes commit together. A failure in any stage rolls back the Evidence,
    lifecycle, conflict, and silence mutations.
    """
    if db.query(Evidence).filter(Evidence.id == evidence.id).first() is not None:
        raise ValueError(f"Evidence '{evidence.id}' already exists.")

    claim_row = db.query(Claim).filter(Claim.id == evidence.target_id).first()
    hypothesis_row = (
        db.query(Hypothesis).filter(Hypothesis.id == evidence.target_id).first()
    )
    if claim_row is not None and hypothesis_row is not None:
        raise ValueError(
            f"Target '{evidence.target_id}' is ambiguous across Claims and Hypotheses."
        )
    if claim_row is None and hypothesis_row is None:
        raise ValueError(
            f"Target '{evidence.target_id}' not found in Claims or Hypotheses."
        )

    target_type = (
        EvidenceTargetType.CLAIM if claim_row is not None else EvidenceTargetType.HYPOTHESIS
    )
    if evidence.target_type is not None and evidence.target_type != target_type:
        raise ValueError(
            f"Evidence target_type '{evidence.target_type.value}' does not match "
            f"the stored {target_type.value}."
        )

    try:
        # 1. Persist Evidence against the shared canonical target field.
        ev_row = Evidence(
            id=evidence.id,
            target_id=evidence.target_id,
            type=evidence.type.value,
            description=evidence.description,
            source=evidence.source,
        )
        db.add(ev_row)
        db.flush()

        # 2. Apply the lifecycle transition without an intermediate commit.
        if claim_row is not None:
            update = apply_evidence(
                evidence.target_id, evidence, db, commit=False
            )
        else:
            update = apply_evidence_to_hypothesis(
                evidence.target_id, evidence, db, commit=False
            )

        # 3. Re-check contradictions and upsert/resolve affected pairs.
        new_conflict_ids: list[str] = []
        resolved_conflict_ids: list[str] = []
        if claim_row is not None:
            snapshot = ClaimSnapshot(
                id=claim_row.id,
                text=claim_row.text or "",
                speaker=claim_row.speaker or "",
                role=claim_row.role or "",
            )
            conflict_result = recheck_conflicts(
                snapshot, db, comparator=comparator, commit=False
            )
            new_conflict_ids = conflict_result.new_conflict_ids
            resolved_conflict_ids = conflict_result.resolved_conflict_ids

        # 4. Evidence graph is a materialized read view; constructing it here
        # verifies that this same transaction exposes the new node and edge.
        graph = get_graph(db)
        provenance = get_provenance(evidence.target_id, db)

        # 5. Evidence means this target is no longer silent.
        silence_reset = reset_silence_signal(
            evidence.target_id, db, commit=False
        )
        db.commit()

        return OrchestrationResult(
            evidence_id=ev_row.id,
            target_id=evidence.target_id,
            target_type=target_type,
            lifecycle_update=update,
            new_conflicts=new_conflict_ids,
            resolved_conflicts=resolved_conflict_ids,
            evidence_graph=graph,
            provenance=provenance,
            silence_reset=silence_reset,
        )
    except Exception:
        db.rollback()
        raise
