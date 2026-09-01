"""
Silence-as-Signal Detector
===========================
Background detector that identifies two categories of dangerous silence during
an active incident:

1.  **Explicit Unknowns** — gaps the team has *stated* aloud
    ("we don't know X", "unclear whether Y", "no idea about Z").
    These are surfaced as ``Unknown`` records in the Incident State store.

2.  **Unresolved Critical Claims** — claims whose text contains critical-severity
    keywords (compromise, data loss, security breach, etc.) that remain
    semantically *unresolved*: no supporting OR contradicting evidence has
    been added within a configurable staleness window (default 10 min).
    This measures *silence around important claims*, not overdue tasks.

On detection the service produces ``Alert`` records with:
    - alert_type: ``EXPLICIT_UNKNOWN`` or ``STALE_CRITICAL_CLAIM``
    - source_id: the Unknown/Claim id
    - description: what was detected
    - suggested_question: a concrete clarifying question

Public API
----------
    scan_for_unresolved(incident_id, db, *, now=None, staleness_window_sec=600)
        → list[Alert]

The function is designed to be called on a periodic timer (e.g. every 60 s).

The detector does NOT modify the extraction pipeline, claim lifecycle, or
contradiction radar.  It only *reads* existing Claims and transcript events
and *writes* Unknown rows + returns Alert objects.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session


# ── Alert output model (not persisted; returned for the caller to dispatch) ───

class Alert(BaseModel):
    """A single alert produced by the silence-signal detector."""
    id: str
    alert_type: str                 # EXPLICIT_UNKNOWN | STALE_CRITICAL_CLAIM
    source_id: str                  # id of the Unknown or Claim row that triggered this
    description: str                # human-readable summary of the gap
    suggested_question: str         # concrete clarifying question for the team
    severity: str = "HIGH"          # default severity for silence-alerts
    created_at: datetime


# ── Configuration constants (overridable via constructor / params) ─────────────

DEFAULT_STALENESS_WINDOW_SEC = 600   # 10 minutes


# ── Critical-keyword vocabulary ───────────────────────────────────────────────

_CRITICAL_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(r"\bcompromise[d]?\b", re.I),
    re.compile(r"\bdata[\s_-]?loss\b", re.I),
    re.compile(r"\bsecurity[\s_-]?breach\b", re.I),
    re.compile(r"\bbreach(?:ed)?\b", re.I),
    re.compile(r"\bransomware\b", re.I),
    re.compile(r"\bexfiltrat(?:ion|ed)\b", re.I),
    re.compile(r"\bunauthori[sz]ed[\s_-]?access\b", re.I),
    re.compile(r"\bpii[\s_-]?(?:leak|expos)\b", re.I),
    re.compile(r"\bcredential[\s_-]?(?:leak|expos|stolen|theft)\b", re.I),
    re.compile(r"\bcritical[\s_-]?(?:failure|outage|vuln)\b", re.I),
    re.compile(r"\btotal[\s_-]?(?:outage|failure|loss)\b", re.I),
    re.compile(r"\bcorrupt(?:ion|ed)\b", re.I),
    re.compile(r"\bdata[\s_-]?corrupt\b", re.I),
    re.compile(r"\broot[\s_-]?access\b", re.I),
    re.compile(r"\bescalat(?:ion|ed)\s+privil", re.I),
]


def is_critical(text: str) -> bool:
    """Return True if the claim text contains critical-severity keywords."""
    return any(p.search(text) for p in _CRITICAL_KEYWORDS)


# ── Explicit-unknown detection patterns ───────────────────────────────────────

_UNKNOWN_PATTERNS: list[re.Pattern[str]] = [
    # "we don't know ..."
    re.compile(
        r"\bwe\s+(?:don['']?t|do\s+not|don't)\s+know\s+(.+)",
        re.I,
    ),
    # "I don't know ..."
    re.compile(
        r"\bi\s+(?:don['']?t|do\s+not|don't)\s+know\s+(.+)",
        re.I,
    ),
    # "unclear whether / if ..."
    re.compile(r"\bunclear\s+(?:whether|if|what|how|why)\s+(.+)", re.I),
    # "not sure ..."
    re.compile(r"\bnot\s+sure\s+(?:whether|if|what|how|why|about)\s+(.+)", re.I),
    # "no idea ..."
    re.compile(r"\bno\s+idea\s+(?:whether|if|what|how|why|about)?\s*(.+)", re.I),
    # "unknown at this time / unknown whether ..."
    re.compile(r"\bunknown\s+(?:at\s+this\s+(?:time|point)|whether|if)\b\s*(.*)", re.I),
    # "we haven't determined ..."
    re.compile(r"\bwe\s+haven['']?t\s+(?:determined|established|confirmed)\s+(.+)", re.I),
    # "nobody knows ..."
    re.compile(r"\bnobody\s+knows?\s+(.+)", re.I),
]


def extract_explicit_unknowns(text: str) -> list[str]:
    """
    Return a list of explicit-unknown descriptions found in ``text``.

    Each returned string is the captured gap description, e.g. for the input
    "We don't know if the database was compromised" the result is
    ["if the database was compromised"].
    """
    results: list[str] = []
    for pattern in _UNKNOWN_PATTERNS:
        for match in pattern.finditer(text):
            captured = match.group(1).strip().rstrip(".")
            if captured and len(captured) > 3:
                results.append(captured)
    return results


# ── Question generator ────────────────────────────────────────────────────────

def _question_for_unknown(description: str) -> str:
    """Generate a clarifying question for an explicit unknown."""
    # Simple template — avoids LLM call for deterministic testability
    desc_lower = description.lower()
    if any(w in desc_lower for w in ("who", "team", "person", "owner")):
        return f"Can we identify who is responsible for verifying: {description}?"
    if any(w in desc_lower for w in ("when", "time", "timeline")):
        return f"Can someone establish a timeline for: {description}?"
    if any(w in desc_lower for w in ("how", "root cause", "mechanism")):
        return f"What investigation steps can reveal: {description}?"
    return f"Who can provide clarity on: {description}?"


def _question_for_stale_claim(claim_text: str, claim_speaker: str) -> str:
    """Generate a clarifying question for a stale critical claim."""
    # Extract the core subject for the question
    return (
        f"Who can verify the claim made by {claim_speaker}: "
        f'"{claim_text}"? No evidence has been provided.'
    )


# ── Pure detector engine (no DB) ─────────────────────────────────────────────

class SilenceDetector:
    """
    Pure detection engine.  No DB dependency; operates on plain Python values.

    Args:
        staleness_window: timedelta after which an unevidenced critical claim
                          is considered "stale".
    """

    def __init__(self, staleness_window: timedelta = timedelta(seconds=DEFAULT_STALENESS_WINDOW_SEC)):
        self.staleness_window = staleness_window

    # ── Part 1: Explicit-unknown detection ────────────────────────────────

    def detect_unknowns_in_text(
        self,
        text: str,
        speaker: str = "",
        *,
        now: datetime | None = None,
    ) -> list[Alert]:
        """Scan a single text for explicit-unknown language."""
        now = now or datetime.now(timezone.utc)
        descriptions = extract_explicit_unknowns(text)
        alerts: list[Alert] = []
        for desc in descriptions:
            alerts.append(Alert(
                id=str(uuid.uuid4()),
                alert_type="EXPLICIT_UNKNOWN",
                source_id="",  # caller fills in after persisting Unknown row
                description=f"Explicit gap stated by {speaker or 'a team member'}: {desc}",
                suggested_question=_question_for_unknown(desc),
                severity="HIGH",
                created_at=now,
            ))
        return alerts

    # ── Part 2: Stale-critical-claim detection ────────────────────────────

    def detect_stale_criticals(
        self,
        claims: list[dict],
        *,
        now: datetime | None = None,
    ) -> list[Alert]:
        """
        Scan a list of claim dicts for critical claims that have gone stale.

        Each dict must have keys: id, text, speaker, timestamp (datetime),
        supporting (list), contradicting (list), status.

        A claim is "stale-critical" when:
            - its text matches critical-severity keywords, AND
            - it has zero supporting AND zero contradicting evidence, AND
            - its timestamp is older than ``now - staleness_window``, AND
            - it is not in a terminal status (RESOLVED / REJECTED / CONFIRMED).
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - self.staleness_window

        terminal = {"RESOLVED", "REJECTED", "CONFIRMED"}

        alerts: list[Alert] = []
        for claim in claims:
            status = (claim.get("status") or "").upper()
            if status in terminal:
                continue
            text = claim.get("text", "")
            if not is_critical(text):
                continue

            sup = claim.get("supporting") or []
            con = claim.get("contradicting") or []
            if len(sup) > 0 or len(con) > 0:
                continue  # has evidence; not silent

            ts = claim.get("timestamp")
            if ts is None:
                continue
            # Normalise tz-naive timestamps to UTC for comparison
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                continue  # too recent; give the team more time

            alerts.append(Alert(
                id=str(uuid.uuid4()),
                alert_type="STALE_CRITICAL_CLAIM",
                source_id=claim["id"],
                description=(
                    f"Critical claim has had no evidence for "
                    f"{int(self.staleness_window.total_seconds() // 60)} min: "
                    f'"{text}"'
                ),
                suggested_question=_question_for_stale_claim(text, claim.get("speaker", "unknown")),
                severity="CRITICAL",
                created_at=now,
            ))

        return alerts


# ── Public DB-coupled entry-point ─────────────────────────────────────────────

def scan_for_unresolved(
    incident_id: str,
    db: Session,
    *,
    now: datetime | None = None,
    staleness_window_sec: int = DEFAULT_STALENESS_WINDOW_SEC,
) -> list[Alert]:
    """
    Run a full silence-signal scan for the given incident.

    1. Scan recent transcript events for explicit-unknown language.
    2. Scan all claims for stale critical claims.
    3. Persist any new Unknown records.
    4. Return a combined alert list.

    Args:
        incident_id:          Incident identifier (for future multi-incident support).
        db:                   SQLAlchemy session.
        now:                  Injectable clock for testing; defaults to UTC now.
        staleness_window_sec: Seconds before an unevidenced critical claim is stale.

    Returns:
        List of Alert objects (not persisted — the caller decides how to route them).
    """
    from app.models import Claim, EventLog, Unknown  # local imports for offline testability

    now = now or datetime.now(timezone.utc)
    window = timedelta(seconds=staleness_window_sec)
    detector = SilenceDetector(staleness_window=window)

    all_alerts: list[Alert] = []

    # ── Part 1: Explicit unknowns from recent transcript chunks ───────────
    # Look at EventLog entries of type TRANSCRIPT_CHUNK within the window
    window_start = now - window
    events = (
        db.query(EventLog)
        .filter(EventLog.event_type == "TRANSCRIPT_CHUNK")
        .filter(EventLog.timestamp >= window_start)
        .all()
    )

    for event in events:
        payload = event.payload or {}
        text = payload.get("text", "")
        speaker = payload.get("speaker", "")

        unknown_alerts = detector.detect_unknowns_in_text(text, speaker, now=now)
        for alert in unknown_alerts:
            # Persist an Unknown row
            unknown_row = Unknown(
                id=str(uuid.uuid4()),
                description=alert.description,
                status="OPEN",
                linked_action_id=None,
            )
            db.add(unknown_row)
            alert.source_id = unknown_row.id
            all_alerts.append(alert)

    # ── Part 2: Stale critical claims ─────────────────────────────────────
    claim_rows = db.query(Claim).all()
    claim_dicts = [
        {
            "id": c.id,
            "text": c.text or "",
            "speaker": c.speaker or "",
            "timestamp": c.timestamp,
            "supporting": c.supporting or [],
            "contradicting": c.contradicting or [],
            "status": c.status or "",
        }
        for c in claim_rows
    ]

    stale_alerts = detector.detect_stale_criticals(claim_dicts, now=now)
    all_alerts.extend(stale_alerts)

    if all_alerts:
        db.commit()

    return all_alerts
