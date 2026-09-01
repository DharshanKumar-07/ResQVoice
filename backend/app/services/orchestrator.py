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

import uuid
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import Claim, Conflict, Evidence, Hypothesis
from app.services.claim_lifecycle import (
    ClaimStatusUpdate,
    EvidenceInput,
    apply_evidence,
    apply_evidence_to_hypothesis,
)
from app.services.contradiction_radar import ClaimSnapshot, detect_conflicts


class OrchestrationResult(BaseModel):
    """Result of an evidence ingestion flow."""
    evidence_id: str
    lifecycle_update: ClaimStatusUpdate
    new_conflicts: List[str]  # IDs of any new Conflict records created


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
    4. Evidence Graph naturally reflects the new Evidence row.
    5. Silence Signal naturally resets for this claim as it is no longer unevidenced
       and may enter a terminal state.
    """
    # 1. Persist Evidence
    ev_row = Evidence(
        id=evidence.id,
        claim_id=evidence.claim_id,
        type=evidence.type.value if hasattr(evidence.type, "value") else str(evidence.type),
        description=evidence.description,
        source=evidence.source,
    )
    db.add(ev_row)
    db.commit()

    # 2. Claim Lifecycle
    # Determine if target is Claim or Hypothesis
    is_claim = db.query(Claim).filter(Claim.id == evidence.claim_id).count() > 0
    is_hypo = db.query(Hypothesis).filter(Hypothesis.id == evidence.claim_id).count() > 0

    if is_claim:
        update = apply_evidence(evidence.claim_id, evidence, db)
    elif is_hypo:
        update = apply_evidence_to_hypothesis(evidence.claim_id, evidence, db)
    else:
        raise ValueError(f"Target '{evidence.claim_id}' not found in Claims or Hypotheses.")

    new_conflict_ids = []

    # 3. Contradiction Radar Re-check
    # Only re-check if it's a Claim (contradiction_radar currently scans Claims).
    if is_claim:
        claim_row = db.query(Claim).filter(Claim.id == evidence.claim_id).first()
        if claim_row:
            snapshot = ClaimSnapshot(
                id=claim_row.id,
                text=claim_row.text or "",
                speaker=claim_row.speaker or "",
                role=claim_row.role or "",
            )
            # Scan against the updated claim
            conflicts = detect_conflicts(snapshot, db, comparator=comparator)
            new_conflict_ids = [c.id for c in conflicts]

    return OrchestrationResult(
        evidence_id=ev_row.id,
        lifecycle_update=update,
        new_conflicts=new_conflict_ids,
    )
