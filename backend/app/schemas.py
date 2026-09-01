from pydantic import BaseModel
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
    id: str
    claim_id: str
    type: str
    description: str
    source: str

class Conflict(BaseModel):
    id: str
    topic: str
    claim_a_id: str
    claim_b_id: str
    status: str
    recommended_verification: str

class Unknown(BaseModel):
    id: str
    description: str
    status: str
    linked_action_id: Optional[str]

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
