"""
Claim Lifecycle Service
=======================
Implements the ResQVoice claim/hypothesis state machine.

State transitions:
    UNVERIFIED / UNCONFIRMED
        + supporting (sup≥1, con=0)  → CORROBORATED
        + contradicting (con≥1)       → DISPUTED

    CORROBORATED
        + supporting (sup≥2 total, con=0) → CONFIRMED
        + contradicting (con≥1)           → DISPUTED

    CONFIRMED
        + contradicting (con≥1) → DISPUTED

    DISPUTED
        + supporting dominates (sup > con) → RESOLVED  (Claims)
                                           → CONFIRMED (Hypotheses)
        + contradicting dominates (con≥sup) → REJECTED

    RESOLVED / CONFIRMED / REJECTED — terminal, no further transitions.

Public API
----------
    apply_evidence(claim_id, evidence, db) -> ClaimStatusUpdate
    apply_evidence_to_hypothesis(hypothesis_id, evidence, db) -> ClaimStatusUpdate

The engine itself (ClaimLifecycleEngine) has no DB dependency and can be
instantiated and tested in complete isolation.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Status vocabularies
# ---------------------------------------------------------------------------

# Statuses that can appear on a Claim row (free-string column)
CLAIM_INITIAL_STATUSES = {"UNVERIFIED", "UNCONFIRMED"}
# CONFIRMED is intentionally NOT terminal for Claims:
# a contradicting piece of evidence can re-open it to DISPUTED.
CLAIM_TERMINAL_STATUSES = {"RESOLVED", "REJECTED"}

# Statuses that can appear on a Hypothesis row (enum column)
HYPOTHESIS_INITIAL_STATUSES = {"UNCONFIRMED"}
HYPOTHESIS_TERMINAL_STATUSES = {"CONFIRMED", "REJECTED"}


# ---------------------------------------------------------------------------
# Public I/O models
# ---------------------------------------------------------------------------

class EvidenceType(str, Enum):
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"


class EvidenceInput(BaseModel):
    """Evidence piece to apply to a claim or hypothesis."""
    id: str
    claim_id: str                    # claim_id or hypothesis_id depending on context
    type: EvidenceType               # "supporting" | "contradicting"
    description: str
    source: str


class ClaimStatusUpdate(BaseModel):
    """Result returned by apply_evidence / apply_evidence_to_hypothesis."""
    claim_id: str
    previous_status: str
    new_status: str
    changed: bool
    reason: str


# ---------------------------------------------------------------------------
# Pure state-machine engine  (no DB, fully testable in isolation)
# ---------------------------------------------------------------------------

class ClaimLifecycleEngine:
    """
    Stateless engine.  All inputs are plain Python values; no SQLAlchemy objects.

    Args:
        current_status:   The current status string of the claim/hypothesis.
        supporting:       Current list of supporting evidence IDs.
        contradicting:    Current list of contradicting evidence IDs.
        terminal_statuses: Set of statuses considered terminal for this entity type.
        disputed_positive_outcome: Status to use when supporting dominates from DISPUTED.
                          'RESOLVED' for Claims, 'CONFIRMED' for Hypotheses.
    """

    def __init__(
        self,
        current_status: str,
        supporting: list[str],
        contradicting: list[str],
        terminal_statuses: set[str],
        disputed_positive_outcome: str,
    ) -> None:
        self.current_status = current_status.upper()
        self.supporting = list(supporting or [])
        self.contradicting = list(contradicting or [])
        self.terminal_statuses = {s.upper() for s in terminal_statuses}
        self.disputed_positive_outcome = disputed_positive_outcome.upper()

    def apply(self, evidence: EvidenceInput) -> ClaimStatusUpdate:
        """Apply one piece of evidence and return the resulting update."""
        prev = self.current_status
        ev_type = evidence.type

        # ── Terminal guard ──────────────────────────────────────────────────
        if self.current_status in self.terminal_statuses:
            return ClaimStatusUpdate(
                claim_id=evidence.claim_id,
                previous_status=prev,
                new_status=prev,
                changed=False,
                reason=f"Status '{prev}' is terminal; no transition possible.",
            )

        # ── Append evidence to the running lists ────────────────────────────
        if ev_type == EvidenceType.SUPPORTING:
            if evidence.id not in self.supporting:
                self.supporting.append(evidence.id)
        else:
            if evidence.id not in self.contradicting:
                self.contradicting.append(evidence.id)

        n_sup = len(self.supporting)
        n_con = len(self.contradicting)

        # ── State machine ────────────────────────────────────────────────────
        new_status, reason = self._compute_transition(prev, ev_type, n_sup, n_con)

        self.current_status = new_status
        return ClaimStatusUpdate(
            claim_id=evidence.claim_id,
            previous_status=prev,
            new_status=new_status,
            changed=(new_status != prev),
            reason=reason,
        )

    def _compute_transition(
        self,
        prev: str,
        ev_type: EvidenceType,
        n_sup: int,
        n_con: int,
    ) -> tuple[str, str]:
        """Return (new_status, reason) given the current counters."""

        # ── UNVERIFIED / UNCONFIRMED ─────────────────────────────────────────
        if prev in {"UNVERIFIED", "UNCONFIRMED"}:
            if ev_type == EvidenceType.SUPPORTING and n_con == 0:
                return "CORROBORATED", (
                    f"First supporting evidence added ({n_sup} supporting, {n_con} contradicting). "
                    "Claim is now CORROBORATED."
                )
            if ev_type == EvidenceType.CONTRADICTING:
                return "DISPUTED", (
                    f"Contradicting evidence added ({n_sup} supporting, {n_con} contradicting). "
                    "Claim is now DISPUTED."
                )
            # supporting added but contradicting already exists → DISPUTED
            if n_con > 0:
                return "DISPUTED", (
                    f"Supporting added but contradicting evidence already present "
                    f"({n_sup} sup, {n_con} con). Claim is DISPUTED."
                )
            return prev, "No transition condition met; status unchanged."

        # ── CORROBORATED ────────────────────────────────────────────────────
        if prev == "CORROBORATED":
            if ev_type == EvidenceType.CONTRADICTING:
                return "DISPUTED", (
                    f"Contradicting evidence received while CORROBORATED "
                    f"({n_sup} sup, {n_con} con). Now DISPUTED."
                )
            # supporting evidence
            if n_con == 0 and n_sup >= 2:
                return "CONFIRMED", (
                    f"≥2 supporting with no contradicting ({n_sup} sup). "
                    "Claim is CONFIRMED."
                )
            return prev, (
                f"Supporting added but thresholds not met ({n_sup} sup, {n_con} con); "
                "still CORROBORATED."
            )

        # ── CONFIRMED ───────────────────────────────────────────────────────
        if prev == "CONFIRMED":
            if ev_type == EvidenceType.CONTRADICTING:
                return "DISPUTED", (
                    f"Contradicting evidence received on a CONFIRMED claim "
                    f"({n_sup} sup, {n_con} con). Re-opened as DISPUTED."
                )
            return prev, "Confirmed claim received additional supporting evidence; no change."

        # ── DISPUTED ────────────────────────────────────────────────────────
        if prev == "DISPUTED":
            if n_sup > n_con:
                return self.disputed_positive_outcome, (
                    f"Supporting now dominates ({n_sup} sup > {n_con} con). "
                    f"Claim moves to {self.disputed_positive_outcome}."
                )
            return "REJECTED", (
                f"Contradicting evidence dominates ({n_con} con ≥ {n_sup} sup). "
                "Claim is REJECTED."
            )

        # Catch-all for unknown statuses
        return prev, f"Unrecognised status '{prev}'; no transition applied."


# ---------------------------------------------------------------------------
# DB adapters
# ---------------------------------------------------------------------------

def apply_evidence(
    claim_id: str,
    evidence: EvidenceInput,
    db: Session,
) -> ClaimStatusUpdate:
    """
    Apply `evidence` to a Claim row in the Incident State store.

    Loads the Claim from the DB, runs it through ClaimLifecycleEngine,
    persists the updated status and evidence lists, and returns a
    ClaimStatusUpdate.

    Raises:
        ValueError: if the claim is not found.
    """
    from app.models import Claim  # local import keeps this module importable without app context

    claim = db.query(Claim).filter(Claim.id == claim_id).first()
    if claim is None:
        raise ValueError(f"Claim '{claim_id}' not found in the Incident State store.")

    engine = ClaimLifecycleEngine(
        current_status=claim.status or "UNVERIFIED",
        supporting=list(claim.supporting or []),
        contradicting=list(claim.contradicting or []),
        terminal_statuses=CLAIM_TERMINAL_STATUSES,
        disputed_positive_outcome="RESOLVED",
    )
    update = engine.apply(evidence)

    # Persist changes
    claim.status = update.new_status
    claim.supporting = engine.supporting
    claim.contradicting = engine.contradicting
    db.add(claim)
    db.commit()
    db.refresh(claim)

    return update


def apply_evidence_to_hypothesis(
    hypothesis_id: str,
    evidence: EvidenceInput,
    db: Session,
) -> ClaimStatusUpdate:
    """
    Apply `evidence` to a Hypothesis row in the Incident State store.

    For hypotheses, the positive terminal from DISPUTED is CONFIRMED
    (not RESOLVED), matching the HypothesisStatus enum.

    Raises:
        ValueError: if the hypothesis is not found.
    """
    from app.models import Hypothesis  # local import

    hypo = db.query(Hypothesis).filter(Hypothesis.id == hypothesis_id).first()
    if hypo is None:
        raise ValueError(f"Hypothesis '{hypothesis_id}' not found.")

    # SQLAlchemy may serialize the Enum as "HYPOTHESISSTATUS.DISPUTED" in
    # SQLite; strip the type-prefix so the engine sees a plain value.
    raw_status = str(hypo.status) if hypo.status else "UNCONFIRMED"
    if "." in raw_status:
        raw_status = raw_status.split(".", 1)[1]

    engine = ClaimLifecycleEngine(
        current_status=raw_status,
        supporting=list(hypo.supporting_evidence or []),
        contradicting=list(hypo.contradicting_evidence or []),
        terminal_statuses=HYPOTHESIS_TERMINAL_STATUSES,
        disputed_positive_outcome="CONFIRMED",
    )
    update = engine.apply(evidence)

    # Persist changes
    hypo.status = update.new_status
    hypo.supporting_evidence = engine.supporting
    hypo.contradicting_evidence = engine.contradicting
    db.add(hypo)
    db.commit()
    db.refresh(hypo)

    return update
