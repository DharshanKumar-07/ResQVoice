"""
Evidence Graph Service
======================
Generates a directed graph representation of Claims, Hypotheses, and their linked
Evidence records for visualization and provenance tracing.

Public API
----------
    get_provenance(claim_id, db) -> ProvenanceTrace
        Returns the direct provenance (speaker, timestamp, status, and resolved Evidence
        records) for a specific Claim or Hypothesis.

    get_graph(db, incident_id=None) -> GraphData
        Returns a complete graph of all Claims, Hypotheses, and Evidence records,
        including "supports" and "contradicts" edges.

This service is read-only and does not modify any database state.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import Claim, Evidence, Hypothesis
from app.schemas import EvidenceType


# ── Public I/O models ─────────────────────────────────────────────────────────

class EvidenceView(BaseModel):
    """A view of an Evidence record."""
    id: str
    type: EvidenceType
    description: str
    source: str


class ProvenanceTrace(BaseModel):
    """The provenance of a single Claim or Hypothesis."""
    id: str
    entity_type: Literal["claim", "hypothesis"]
    text: str
    speaker: str
    timestamp: Optional[datetime]
    status: str
    supporting_evidence: List[EvidenceView]
    contradicting_evidence: List[EvidenceView]


class GraphNode(BaseModel):
    """A node in the evidence graph."""
    id: str
    label: str
    type: Literal["claim", "hypothesis", "evidence"]
    status: Optional[str] = None
    speaker: Optional[str] = None
    source: Optional[str] = None


class GraphEdge(BaseModel):
    """A directed edge in the evidence graph."""
    source: str
    target: str
    relation: Literal["supports", "contradicts"]


class GraphData(BaseModel):
    """The complete graph structure for frontend visualization."""
    nodes: List[GraphNode]
    edges: List[GraphEdge]


# ── Core Service ──────────────────────────────────────────────────────────────

def get_provenance(target_id: str, db: Session) -> ProvenanceTrace:
    """
    Retrieve the provenance trace for a given Claim or Hypothesis ID.
    Raises ValueError if not found.
    """
    # Try Claim first
    claim = db.query(Claim).filter(Claim.id == target_id).first()
    if claim:
        return _build_claim_provenance(claim, db)

    # Try Hypothesis
    hypo = db.query(Hypothesis).filter(Hypothesis.id == target_id).first()
    if hypo:
        return _build_hypothesis_provenance(hypo, db)

    raise ValueError(f"No Claim or Hypothesis found with ID '{target_id}'")


def get_graph(db: Session, incident_id: Optional[str] = None) -> GraphData:
    """
    Retrieve the full evidence graph.
    (Note: incident_id is accepted for API signature completeness but currently
    all records in the DB are assumed to belong to the active incident).
    """
    nodes: List[GraphNode] = []
    edges: List[GraphEdge] = []
    seen_evidence_ids = set()

    # 1. Add Claims
    claims = db.query(Claim).all()
    for c in claims:
        nodes.append(GraphNode(
            id=c.id,
            label=c.text or "Unknown Claim",
            type="claim",
            status=c.status,
            speaker=c.speaker,
        ))

    # 2. Add Hypotheses
    hypotheses = db.query(Hypothesis).all()
    for h in hypotheses:
        status_str = h.status.value if hasattr(h.status, "value") else str(h.status)
        if "." in status_str:
            status_str = status_str.split(".")[-1]

        nodes.append(GraphNode(
            id=h.id,
            label=h.description or "Unknown Hypothesis",
            type="hypothesis",
            status=status_str,
            speaker=h.origin,
        ))

    # 3. Add Evidence and Edges
    # In this schema, Evidence records are stored in the Evidence table,
    # and their IDs are referenced in the supporting/contradicting arrays of Claims/Hypotheses.
    # Alternatively, the Evidence table has a claim_id foreign key.
    
    # We will just iterate over all Evidence records.
    all_evidence = db.query(Evidence).all()
    for e in all_evidence:
        if e.id not in seen_evidence_ids:
            nodes.append(GraphNode(
                id=e.id,
                label=e.description or "Evidence",
                type="evidence",
                source=e.source,
            ))
            seen_evidence_ids.add(e.id)
            
        # Draw edge from Evidence to the Claim/Hypothesis it targets
        relation: Literal["supports", "contradicts"] = "supports" if e.type == "supporting" else "contradicts"
        edges.append(GraphEdge(
            source=e.id,
            target=e.target_id,
            relation=relation,
        ))

    return GraphData(nodes=nodes, edges=edges)


# ── Internal Helpers ──────────────────────────────────────────────────────────

def _build_claim_provenance(claim: Claim, db: Session) -> ProvenanceTrace:
    sup_ids = claim.supporting or []
    con_ids = claim.contradicting or []
    
    sup_ev = db.query(Evidence).filter(Evidence.id.in_(sup_ids)).all() if sup_ids else []
    con_ev = db.query(Evidence).filter(Evidence.id.in_(con_ids)).all() if con_ids else []

    return ProvenanceTrace(
        id=claim.id,
        entity_type="claim",
        text=claim.text or "",
        speaker=claim.speaker or "Unknown",
        timestamp=claim.timestamp,
        status=claim.status or "UNVERIFIED",
        supporting_evidence=[
            EvidenceView(id=e.id, type=EvidenceType.SUPPORTING, description=e.description or "", source=e.source or "")
            for e in sup_ev
        ],
        contradicting_evidence=[
            EvidenceView(id=e.id, type=EvidenceType.CONTRADICTING, description=e.description or "", source=e.source or "")
            for e in con_ev
        ],
    )


def _build_hypothesis_provenance(hypo: Hypothesis, db: Session) -> ProvenanceTrace:
    sup_ids = hypo.supporting_evidence or []
    con_ids = hypo.contradicting_evidence or []
    
    sup_ev = db.query(Evidence).filter(Evidence.id.in_(sup_ids)).all() if sup_ids else []
    con_ev = db.query(Evidence).filter(Evidence.id.in_(con_ids)).all() if con_ids else []

    status_str = hypo.status.value if hasattr(hypo.status, "value") else str(hypo.status)
    if "." in status_str:
        status_str = status_str.split(".")[-1]

    return ProvenanceTrace(
        id=hypo.id,
        entity_type="hypothesis",
        text=hypo.description or "",
        speaker=hypo.origin or "Unknown",
        timestamp=None,  # Hypotheses in this schema don't have a timestamp
        status=status_str or "UNCONFIRMED",
        supporting_evidence=[
            EvidenceView(id=e.id, type=EvidenceType.SUPPORTING, description=e.description or "", source=e.source or "")
            for e in sup_ev
        ],
        contradicting_evidence=[
            EvidenceView(id=e.id, type=EvidenceType.CONTRADICTING, description=e.description or "", source=e.source or "")
            for e in con_ev
        ],
    )
