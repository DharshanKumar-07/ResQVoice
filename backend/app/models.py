from sqlalchemy import Column, String, Integer, Float, DateTime, Enum, ARRAY, ForeignKey, JSON
from app.database import Base
from app.schemas import ActionStatus, HypothesisStatus
import enum
from datetime import datetime

class FactStatus(str, enum.Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"

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
    # Evidence may target either a Claim or Hypothesis. ``target_id`` is the
    # canonical shared-schema name; legacy API payloads are normalized by Pydantic.
    target_id = Column(String, nullable=False)
    target_type = Column(String, nullable=True)
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
    source_id = Column(String, nullable=True)

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


class Participant(Base):
    __tablename__ = "participants"
    agora_uid = Column(String, primary_key=True)
    channel = Column(String, primary_key=True, default="incident-room")
    user_id = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    participant_type = Column(String, nullable=False, default="human")


class Intervention(Base):
    __tablename__ = "interventions"
    id = Column(String, primary_key=True)
    trigger_type = Column(String, index=True, nullable=False)
    severity = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    message = Column(String, nullable=False)
    related_claim_ids = Column(ARRAY(String), default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    spoken_at = Column(DateTime, nullable=True)
    status = Column(String, index=True, nullable=False)
    agent_id = Column(String, nullable=True)
