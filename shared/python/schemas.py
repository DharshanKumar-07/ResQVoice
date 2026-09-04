from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import List, Literal, Optional
from datetime import datetime
from enum import Enum

class HypothesisStatus(str, Enum):
    UNCONFIRMED = "UNCONFIRMED"
    CORROBORATED = "CORROBORATED"
    DISPUTED = "DISPUTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"

class ActionStatus(str, Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"

class ClaimStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    UNCONFIRMED = "UNCONFIRMED"
    CORROBORATED = "CORROBORATED"
    DISPUTED = "DISPUTED"
    CONFIRMED = "CONFIRMED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"

class EvidenceType(str, Enum):
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"

class EvidenceTargetType(str, Enum):
    CLAIM = "claim"
    HYPOTHESIS = "hypothesis"

class Fact(BaseModel):
    id: str
    description: str
    source: str
    speaker: str
    timestamp: datetime
    status: str

class Hypothesis(BaseModel):
    id: str
    description: str
    origin: str
    supporting_evidence: List[str]
    contradicting_evidence: List[str]
    confidence: float
    status: HypothesisStatus

class Claim(BaseModel):
    id: str
    text: str
    speaker: str
    role: str
    timestamp: datetime
    status: str
    supporting: List[str]
    contradicting: List[str]

class Evidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    target_id: str = Field(validation_alias=AliasChoices("target_id", "claim_id"))
    target_type: Optional[EvidenceTargetType] = None
    type: EvidenceType
    description: str
    source: str

    @property
    def claim_id(self) -> str:
        """Compatibility alias for the pre-shared-schema field name."""
        return self.target_id

class Conflict(BaseModel):
    id: str
    topic: str
    claim_a_id: str
    claim_b_id: str
    status: str
    recommended_verification: str

class Unknown(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    description: str
    status: str
    source_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("source_id", "linked_action_id"),
    )

    @property
    def linked_action_id(self) -> Optional[str]:
        """Compatibility alias for the legacy database/API field name."""
        return self.source_id

class Action(BaseModel):
    id: str
    task: str
    owner: str
    status: ActionStatus
    priority: str
    created_at: datetime
    deadline: Optional[datetime]

class Decision(BaseModel):
    id: str
    recommendation: str
    evidence: List[str]
    sop_reference: str
    approved_by: Optional[str]
    approval_time: Optional[datetime]
    execution_status: str
    result: Optional[str]

class TimelineEvent(BaseModel):
    id: str
    timestamp: datetime
    description: str
    event_type: str


class Participant(BaseModel):
    agora_uid: str
    user_id: str
    display_name: str
    role: str
    participant_type: Literal["human", "ai_agent", "system"] = "human"
    channel: str = "incident-room"


class Intervention(BaseModel):
    id: str
    trigger_type: str
    severity: str
    confidence: float
    message: str
    related_claim_ids: List[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: Optional[datetime] = None
    spoken_at: Optional[datetime] = None
    status: Literal["PENDING", "DEFERRED", "SPOKEN", "DISMISSED", "EXPIRED"]
    agent_id: Optional[str] = None


# Service contracts live here as well as persisted Incident State entities. This
# keeps the four evidence-processing services from growing subtly different
# copies of the same fields and enums.
class ClaimStatusUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_id: str = Field(validation_alias=AliasChoices("target_id", "claim_id"))
    previous_status: str
    new_status: str
    changed: bool
    reason: str

    @property
    def claim_id(self) -> str:
        return self.target_id


class ClaimSnapshot(BaseModel):
    id: str
    text: str
    speaker: str
    role: str
    topic_hint: Optional[str] = None


class ComparisonResult(BaseModel):
    contradicts: bool
    topic: str
    reason: str
    recommended_verification: str


class ConflictRecheckResult(BaseModel):
    active_conflict_ids: List[str]
    new_conflict_ids: List[str]
    resolved_conflict_ids: List[str]


class EvidenceView(BaseModel):
    id: str
    type: EvidenceType
    description: str
    source: str


class ProvenanceTrace(BaseModel):
    id: str
    entity_type: Literal["claim", "hypothesis"]
    text: str
    speaker: str
    timestamp: Optional[datetime]
    status: str
    supporting_evidence: List[EvidenceView]
    contradicting_evidence: List[EvidenceView]


class GraphNode(BaseModel):
    id: str
    label: str
    type: Literal["claim", "hypothesis", "evidence"]
    status: Optional[str] = None
    speaker: Optional[str] = None
    source: Optional[str] = None


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: Literal["supports", "contradicts"]


class GraphData(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]


class SilenceAlert(BaseModel):
    id: str
    alert_type: Literal["EXPLICIT_UNKNOWN", "STALE_CRITICAL_CLAIM"]
    source_id: str
    description: str
    suggested_question: str
    severity: str = "HIGH"
    created_at: datetime


class SOPMatch(BaseModel):
    title: str
    reference: str
    score: float
    required_steps: List[str]


class PotentialSOPConflict(BaseModel):
    proposed_action: str
    sop_reference: str
    missing_steps: List[str]
    out_of_order: bool
    recommended_sequence: List[str]


class SafetyRecommendation(BaseModel):
    decision: Decision
    sop_match: SOPMatch
    confidence: float
    conflict: Optional[PotentialSOPConflict] = None


class ApprovalRecord(BaseModel):
    decision_id: str
    approved_by: str
    approval_time: datetime


class ExecutionResult(BaseModel):
    decision: Decision
    executed: bool
    verification_required: bool


class VerificationResult(BaseModel):
    decision: Decision
    fact: Fact
