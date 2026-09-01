"""
Contradiction Radar Service
============================
Given a new claim, scans existing claims from the Incident State store on the same
topic and detects direct contradictions. On conflict, creates a Conflict record
(schema: topic, claim_a_id, claim_b_id, status=UNRESOLVED, recommended_verification).

Architecture
------------
The service is split into two layers:

1. ContradictionDetector  — pure logic, injected comparator, no DB, fully testable.
   - topic_key(text) → str          : lightweight keyword extraction for bucketing
   - compare(claim_a, claim_b) → ComparisonResult : calls the injected comparator
   - detect(new_claim, existing)    : returns list[ConflictRecord]

2. detect_conflicts(new_claim, db) → list[Conflict]
   The public DB-coupled entry-point:
   - Loads existing claims from the same topic bucket.
   - Runs ContradictionDetector with the Gemini LLM comparator.
   - Persists and returns Conflict ORM rows.

Comparison strategy
-------------------
Each candidate pair is sent to gemini-3-flash-preview with structured output:
  {
    "contradicts": true | false,
    "topic": "<noun phrase summarising the disputed subject>",
    "reason": "<one sentence>",
    "recommended_verification": "<one actionable step>"
  }

This avoids maintaining embeddings/vector stores and works with the packages already
in requirements.txt. The comparator is dependency-injected, so tests run fully
offline with a mock.

Do NOT modify extractor.py, event_store.py, claim_lifecycle.py, or main.py.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from pydantic import BaseModel
from sqlalchemy.orm import Session


# ── Pydantic models (public I/O) ──────────────────────────────────────────────

class ClaimSnapshot(BaseModel):
    """Lightweight view of a Claim row, used as input to detect_conflicts."""
    id: str
    text: str
    speaker: str
    role: str
    topic_hint: Optional[str] = None   # optional caller-supplied topic tag


class ComparisonResult(BaseModel):
    """Structured output from the pairwise comparator."""
    contradicts: bool
    topic: str                          # noun phrase, e.g. "payment service health"
    reason: str                         # one sentence explaining the contradiction
    recommended_verification: str       # one actionable verification step


class ConflictRecord(BaseModel):
    """Mirrors the Conflict schema row; returned by detect_conflicts."""
    id: str
    topic: str
    claim_a_id: str
    claim_b_id: str
    status: str = "UNRESOLVED"
    recommended_verification: str


# ── Comparator protocol (dependency-injection boundary) ───────────────────────

class ClaimComparator(Protocol):
    """Callable that compares two claim texts and returns a ComparisonResult."""
    def __call__(self, text_a: str, text_b: str) -> ComparisonResult: ...


# ── Pure engine ───────────────────────────────────────────────────────────────

# Keywords → topic bucket mapping.  Order matters: more specific patterns first.
_TOPIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpayment[\s_-]?service\b", re.I),          "payment-service"),
    (re.compile(r"\bcheckout\b", re.I),                        "payment-service"),
    (re.compile(r"\bpayment\b", re.I),                         "payment-service"),
    (re.compile(r"\bdatabase\b|\bdb\b|\bpostgres\b|\bmysql\b", re.I), "database"),
    (re.compile(r"\bquery\b|\bqueries\b|\bsql\b", re.I),       "database"),
    (re.compile(r"\bdeployment\b|\brelease\b|\brollback\b", re.I), "deployment"),
    (re.compile(r"\bcache\b|\bredis\b|\bmemcached\b", re.I),   "cache"),
    (re.compile(r"\bauth\b|\bauthentication\b|\btoken\b|\bjwt\b", re.I), "auth"),
    (re.compile(r"\blatency\b|\bresponse[\s-]time\b|\btimeout\b", re.I), "latency"),
    (re.compile(r"\bcpu\b|\bmemory\b|\bram\b|\bdisk\b", re.I), "infrastructure"),
    (re.compile(r"\bnetwork\b|\bdns\b|\bfirewall\b", re.I),    "network"),
    (re.compile(r"\berror\b|\b500\b|\b503\b|\bfailure\b", re.I), "errors"),
]

_FALLBACK_TOPIC = "general-incident"


def topic_key(text: str, hint: Optional[str] = None) -> str:
    """
    Return a short topic bucket key for a claim text.

    If the caller supplies a ``hint``, that string is checked first.
    Falls back to regex keyword matching, then to a generic bucket.
    """
    if hint:
        return hint.lower().replace(" ", "-")
    for pattern, bucket in _TOPIC_PATTERNS:
        if pattern.search(text):
            return bucket
    return _FALLBACK_TOPIC


class ContradictionDetector:
    """
    Pure claim-comparison engine.  No DB, no network — fully testable.

    Args:
        comparator: Callable(text_a, text_b) → ComparisonResult.
                    In production this wraps Gemini; in tests it's a mock.
        similarity_threshold_chars: minimum total characters to bother comparing
                    (avoids wasting LLM calls on trivially short claims).
    """

    def __init__(
        self,
        comparator: ClaimComparator,
        similarity_threshold_chars: int = 10,
    ) -> None:
        self._compare = comparator
        self._min_chars = similarity_threshold_chars

    def detect(
        self,
        new_claim: ClaimSnapshot,
        existing_claims: list[ClaimSnapshot],
    ) -> list[ConflictRecord]:
        """
        Compare ``new_claim`` against each item in ``existing_claims``.
        Returns one ConflictRecord per detected contradiction.
        """
        conflicts: list[ConflictRecord] = []
        new_topic = topic_key(new_claim.text, new_claim.topic_hint)

        for existing in existing_claims:
            # Skip self-comparison (same ID)
            if existing.id == new_claim.id:
                continue
            # Only compare claims in the same topic bucket
            if topic_key(existing.text, existing.topic_hint) != new_topic:
                continue
            # Skip trivially short texts
            if len(new_claim.text) < self._min_chars or len(existing.text) < self._min_chars:
                continue

            result = self._compare(new_claim.text, existing.text)

            if result.contradicts:
                conflicts.append(
                    ConflictRecord(
                        id=str(uuid.uuid4()),
                        topic=result.topic,
                        claim_a_id=new_claim.id,
                        claim_b_id=existing.id,
                        status="UNRESOLVED",
                        recommended_verification=result.recommended_verification,
                    )
                )

        return conflicts


# ── Gemini LLM comparator (production implementation) ─────────────────────────

_COMPARATOR_SYSTEM_PROMPT = """\
You are the ResQVoice Contradiction Radar.
You receive two statements made during a live incident response.

Determine whether they directly CONTRADICT each other on the same specific subject
(e.g. one says a service is healthy, the other says it is returning errors; one says
the DB has capacity, the other says it is at 100% CPU).

Similarity or vagueness is NOT a contradiction. Contradiction requires mutually
exclusive claims about the same concrete subject at approximately the same time.

Respond ONLY with a JSON object matching this schema:
{
  "contradicts": true | false,
  "topic": "<concise noun phrase – the disputed subject, e.g. 'payment service health'>",
  "reason": "<one sentence explaining the contradiction, or empty string if none>",
  "recommended_verification": "<one concrete verification action, e.g. 'Query payment-service /healthz endpoint and compare HTTP status codes from both reporters', or empty string if no contradiction>"
}
"""


def _make_gemini_comparator() -> ClaimComparator:
    """
    Returns a ClaimComparator backed by Gemini structured output.
    Lazily imports google.genai so the module stays importable in offline tests.
    """
    def _comparator(text_a: str, text_b: str) -> ComparisonResult:
        from google import genai  # noqa: PLC0415

        client = genai.Client()
        prompt = f"Statement A: {text_a}\n\nStatement B: {text_b}"

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config={
                "system_instruction": _COMPARATOR_SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "response_schema": ComparisonResult,
            },
        )

        if hasattr(response, "parsed") and response.parsed:
            return response.parsed
        return ComparisonResult.model_validate_json(response.text)

    return _comparator


# ── Public DB-coupled entry-point ─────────────────────────────────────────────

def detect_conflicts(
    new_claim: ClaimSnapshot,
    db: Session,
    comparator: Optional[ClaimComparator] = None,
) -> list:
    """
    Scan existing claims in the Incident State store for contradictions with
    ``new_claim`` and persist any discovered Conflict rows.

    Args:
        new_claim:   The incoming claim to check.
        db:          SQLAlchemy session (must have 'claims' and 'conflicts' tables).
        comparator:  Optional override for the LLM comparator (for testing).

    Returns:
        List of persisted Conflict ORM objects.
    """
    from app.models import Claim, Conflict  # local import keeps module importable offline

    if comparator is None:
        comparator = _make_gemini_comparator()

    detector = ContradictionDetector(comparator=comparator)

    # Load all existing claims from DB (excluding the new one)
    rows = db.query(Claim).filter(Claim.id != new_claim.id).all()
    existing = [
        ClaimSnapshot(id=row.id, text=row.text or "", speaker=row.speaker or "",
                      role=row.role or "")
        for row in rows
    ]

    conflict_records = detector.detect(new_claim, existing)

    persisted: list[Conflict] = []
    for cr in conflict_records:
        orm_row = Conflict(
            id=cr.id,
            topic=cr.topic,
            claim_a_id=cr.claim_a_id,
            claim_b_id=cr.claim_b_id,
            status=cr.status,
            recommended_verification=cr.recommended_verification,
        )
        db.add(orm_row)
        persisted.append(orm_row)

    if persisted:
        db.commit()

    return persisted
