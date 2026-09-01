from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import List, Optional
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
