"""Deterministic intervention admission, anti-spam, and lifecycle policy."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Intervention as InterventionRow
from app.schemas import Intervention
from app.services.observability import log_event

SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass
class InterventionCandidate:
    trigger_type: str
    severity: str
    confidence: float
    message: str
    related_claim_ids: list[str] = field(default_factory=list)
    agent_id: str | None = None
    bypass_severity_threshold: bool = False


class InterventionPolicy:
    @property
    def minimum_severity(self) -> str:
        return os.getenv("INTERVENTION_MIN_SEVERITY", "HIGH").upper()

    @property
    def cooldown_seconds(self) -> int:
        return _int_env("INTERVENTION_COOLDOWN_SECONDS", 120)

    @property
    def expiration_seconds(self) -> int:
        return _int_env("INTERVENTION_EXPIRATION_SECONDS", 300)

    @property
    def max_per_window(self) -> int:
        return _int_env("INTERVENTION_MAX_PER_WINDOW", 3)

    @property
    def window_seconds(self) -> int:
        return _int_env("INTERVENTION_WINDOW_SECONDS", 300)

    def admit(
        self, candidate: InterventionCandidate, db: Session, *,
        human_speaking: bool = False, now: datetime | None = None,
    ) -> Intervention | None:
        now = now or datetime.now(timezone.utc)
        if (
            not candidate.bypass_severity_threshold
            and SEVERITY_RANK.get(candidate.severity.upper(), 0)
            < SEVERITY_RANK.get(self.minimum_severity, 3)
        ):
            log_event("INTERVENTION_DISMISSED", trigger=candidate.trigger_type, reason="below_severity_threshold")
            return None
        recent_cutoff = now - timedelta(seconds=self.cooldown_seconds)
        recent = (
            db.query(InterventionRow)
            .filter(InterventionRow.trigger_type == candidate.trigger_type)
            .filter(InterventionRow.created_at >= recent_cutoff.replace(tzinfo=None))
            .order_by(InterventionRow.created_at.desc())
            .all()
        )
        related = set(candidate.related_claim_ids)
        if any(row.message == candidate.message or related.intersection(row.related_claim_ids or []) for row in recent):
            log_event("INTERVENTION_DISMISSED", trigger=candidate.trigger_type, reason="duplicate_cooldown")
            return None
        window_cutoff = now - timedelta(seconds=self.window_seconds)
        spoken_count = (
            db.query(InterventionRow)
            .filter(InterventionRow.status == "SPOKEN")
            .filter(InterventionRow.spoken_at >= window_cutoff.replace(tzinfo=None))
            .count()
        )
        status = "DEFERRED" if human_speaking or spoken_count >= self.max_per_window else "PENDING"
        row = InterventionRow(
            id=str(uuid.uuid4()), trigger_type=candidate.trigger_type,
            severity=candidate.severity.upper(), confidence=candidate.confidence,
            message=candidate.message, related_claim_ids=candidate.related_claim_ids,
            created_at=now, expires_at=now + timedelta(seconds=self.expiration_seconds),
            spoken_at=None, status=status, agent_id=candidate.agent_id,
        )
        db.add(row)
        db.commit()
        log_event("INTERVENTION_DEFERRED" if status == "DEFERRED" else "INTERVENTION_CREATED", intervention_id=row.id, trigger=candidate.trigger_type, severity=row.severity)
        return _schema(row)

    def mark_spoken(self, intervention_id: str, db: Session, *, now: datetime | None = None) -> Intervention:
        row = db.query(InterventionRow).filter(InterventionRow.id == intervention_id).first()
        if row is None:
            raise ValueError(f"Intervention '{intervention_id}' not found")
        now = now or datetime.now(timezone.utc)
        expires_at = row.expires_at
        if expires_at and expires_at.replace(tzinfo=timezone.utc) <= now:
            row.status = "EXPIRED"
            db.commit()
            log_event("INTERVENTION_EXPIRED", intervention_id=row.id)
            return _schema(row)
        row.status = "SPOKEN"
        row.spoken_at = now
        db.commit()
        log_event("INTERVENTION_SPOKEN", intervention_id=row.id, trigger=row.trigger_type)
        return _schema(row)


def _schema(row: InterventionRow) -> Intervention:
    return Intervention(
        id=row.id, trigger_type=row.trigger_type, severity=row.severity,
        confidence=row.confidence, message=row.message,
        related_claim_ids=list(row.related_claim_ids or []), created_at=row.created_at,
        expires_at=row.expires_at, spoken_at=row.spoken_at, status=row.status,
        agent_id=row.agent_id,
    )


INTERVENTION_POLICY = InterventionPolicy()

_LAST_HUMAN_ACTIVITY: datetime | None = None


def note_human_activity(now: datetime | None = None) -> None:
    global _LAST_HUMAN_ACTIVITY
    _LAST_HUMAN_ACTIVITY = now or datetime.now(timezone.utc)


def human_recently_active(now: datetime | None = None) -> bool:
    if _LAST_HUMAN_ACTIVITY is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - _LAST_HUMAN_ACTIVITY).total_seconds() < _int_env("INTERVENTION_SPEECH_GUARD_SECONDS", 2)
