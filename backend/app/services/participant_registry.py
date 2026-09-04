"""Canonical Agora UID to application identity mapping."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Participant as ParticipantRow
from app.schemas import Participant
from app.services.observability import log_event


AI_DISPLAY_NAME = "ResQVoice AI"
AI_ROLE = "AI Incident Co-Pilot"


def register_participant(participant: Participant, db: Session, *, commit: bool = True) -> Participant:
    row = ParticipantRow(**participant.model_dump())
    db.merge(row)
    if commit:
        db.commit()
    else:
        db.flush()
    log_event(
        "PARTICIPANT_JOINED", channel=participant.channel,
        uid=participant.agora_uid, participant_type=participant.participant_type,
        display_name=participant.display_name, role=participant.role,
    )
    return participant


def resolve_participant(channel: str, agora_uid: str | int | None, db: Session) -> Participant:
    uid = str(agora_uid) if agora_uid is not None else "unknown"
    row = (
        db.query(ParticipantRow)
        .filter(ParticipantRow.channel == channel, ParticipantRow.agora_uid == uid)
        .first()
    )
    if row:
        return Participant(
            agora_uid=row.agora_uid, user_id=row.user_id,
            display_name=row.display_name, role=row.role,
            participant_type=row.participant_type, channel=row.channel,
        )
    return Participant(
        agora_uid=uid,
        user_id=uid,
        display_name=f"Participant {uid}" if uid != "unknown" else "Unknown participant",
        role="Unspecified role",
        participant_type="human",
        channel=channel,
    )


def register_agent(channel: str, agent_uid: str | int, db: Session) -> Participant:
    return register_participant(Participant(
        agora_uid=str(agent_uid), user_id="resqvoice-ai",
        display_name=AI_DISPLAY_NAME, role=AI_ROLE,
        participant_type="ai_agent", channel=channel,
    ), db)
