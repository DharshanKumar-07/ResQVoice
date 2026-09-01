from sqlalchemy import Column, String, Integer, Float, DateTime, Enum, ARRAY, ForeignKey, JSON
from sqlalchemy.orm import synonym
from app.database import Base
import enum
from datetime import datetime

class FactStatus(str, enum.Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"

class HypothesisStatus(str, enum.Enum):
    UNCONFIRMED = "UNCONFIRMED"
    CORROBORATED = "CORROBORATED"
    DISPUTED = "DISPUTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"

class ActionStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"

class Fact(Base):
    __tablename__ = "facts"
    id = Column(String, primary_key=True, index=True)
    description = Column(String, nullable=False)
    source = Column(String)
    speaker = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    status = Column(String)

class Hypothesis(Base):
    __tablename__ = "hypotheses"
    id = Column(String, primary_key=True, index=True)
    description = Column(String, nullable=False)
    origin = Column(String)
    supporting_evidence = Column(ARRAY(String))
    contradicting_evidence = Column(ARRAY(String))
    confidence = Column(Float)
    status = Column(Enum(HypothesisStatus))

class Claim(Base):
    __tablename__ = "claims"
    id = Column(String, primary_key=True, index=True)
    text = Column(String, nullable=False)
    speaker = Column(String)
    role = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    status = Column(String)
    supporting = Column(ARRAY(String))
    contradicting = Column(ARRAY(String))

class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(String, primary_key=True, index=True)
    # ``claim_id`` is the legacy physical column name. Evidence may target a
    # Claim or a Hypothesis, so services use the canonical ``target_id`` name.
    target_id = Column("claim_id", String, nullable=False)
    claim_id = synonym("target_id")
    type = Column(String)
    description = Column(String)
    source = Column(String)

class Conflict(Base):
    __tablename__ = "conflicts"
    id = Column(String, primary_key=True, index=True)
    topic = Column(String)
    claim_a_id = Column(String)
    claim_b_id = Column(String)
    status = Column(String)
    recommended_verification = Column(String)

class Unknown(Base):
    __tablename__ = "unknowns"
    id = Column(String, primary_key=True, index=True)
    description = Column(String)
    status = Column(String)
    # Preserve the installed column while exposing what it actually represents:
    # the claim or transcript event responsible for the silence signal.
    source_id = Column("linked_action_id", String, nullable=True)
    linked_action_id = synonym("source_id")

class Action(Base):
    __tablename__ = "actions"
    id = Column(String, primary_key=True, index=True)
    task = Column(String, nullable=False)
    owner = Column(String)
    status = Column(Enum(ActionStatus))
    priority = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    deadline = Column(DateTime, nullable=True)

class Decision(Base):
    __tablename__ = "decisions"
    id = Column(String, primary_key=True, index=True)
    recommendation = Column(String)
    evidence = Column(ARRAY(String))
    sop_reference = Column(String)
    approved_by = Column(String, nullable=True)
    approval_time = Column(DateTime, nullable=True)
    execution_status = Column(String)
    result = Column(String, nullable=True)

class TimelineEvent(Base):
    __tablename__ = "timeline_events"
    id = Column(String, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    description = Column(String)
    event_type = Column(String)

class EventLog(Base):
    __tablename__ = "event_log"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    event_type = Column(String, index=True)
    payload = Column(JSON)
    timestamp = Column(DateTime, default=datetime.utcnow)
