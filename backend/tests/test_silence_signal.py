"""
Unit tests for the Silence-as-Signal Detector.

All tests run offline (in-memory SQLite) with a fast-forwarded clock.
No real-time waits, no Gemini API, no PostgreSQL.

Coverage:
    Explicit-unknown detection (pure engine):
        SS-01  "we don't know" → EXPLICIT_UNKNOWN alert
        SS-02  "unclear whether" → EXPLICIT_UNKNOWN alert
        SS-03  "not sure if" → EXPLICIT_UNKNOWN alert
        SS-04  no unknown language → no alerts
        SS-05  multiple unknowns in one text → multiple alerts
        SS-06  short/trivial captures (<4 chars) are filtered out

    Critical-keyword detection:
        SS-07  is_critical("database was compromised") → True
        SS-08  is_critical("latency increased slightly") → False
        SS-09  is_critical("security breach detected") → True

    Stale-critical detection (pure engine, fast-forwarded clock):
        SS-10  critical claim with no evidence, older than window → STALE alert
        SS-11  critical claim with no evidence, within window → no alert (too recent)
        SS-12  critical claim WITH supporting evidence → no alert (has evidence)
        SS-13  non-critical claim with no evidence, older than window → no alert
        SS-14  critical claim in RESOLVED status → no alert (terminal)
        SS-15  two stale critical claims → two alerts
        SS-16  suggested_question contains speaker name and claim text

    DB adapter (scan_for_unresolved):
        SS-17  transcript event with "we don't know" → Unknown row persisted + alert
        SS-18  stale critical claim detected from DB
        SS-19  scan with no transcript events and no claims → empty
        SS-20  combined: explicit unknown + stale critical in one scan
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator, Text

# ── ARRAY→JSON shim ──────────────────────────────────────────────────────────
import sqlalchemy as sa


class ArrayAsJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return "[]"
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return []
        return json.loads(value)


sa.ARRAY = lambda item_type, *a, **kw: ArrayAsJSON()  # type: ignore[assignment]

# ── App imports (after shim) ──────────────────────────────────────────────────
from app.database import Base
from app.models import Claim, EventLog, Unknown
from app.services.silence_signal import (
    Alert,
    SilenceDetector,
    extract_explicit_unknowns,
    is_critical,
    scan_for_unresolved,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


# Anchored "now" for deterministic tests
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
_WINDOW = timedelta(minutes=10)


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(engine, "connect")
    def _pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _seed_claim(
    db,
    text: str,
    timestamp: datetime | None = None,
    status: str = "UNVERIFIED",
    supporting: list | None = None,
    contradicting: list | None = None,
    speaker: str = "Alice",
    cid: str | None = None,
) -> Claim:
    row = Claim(
        id=cid or _uuid(),
        text=text,
        speaker=speaker,
        role="SRE",
        timestamp=timestamp or _NOW,
        status=status,
        supporting=supporting or [],
        contradicting=contradicting or [],
    )
    db.add(row)
    db.commit()
    return row


def _seed_transcript_event(
    db,
    text: str,
    speaker: str = "Bob",
    timestamp: datetime | None = None,
) -> EventLog:
    ts = timestamp or _NOW
    row = EventLog(
        event_type="TRANSCRIPT_CHUNK",
        payload={"text": text, "speaker": speaker, "role": "Engineer", "timestamp": ts.isoformat()},
        timestamp=ts,
    )
    db.add(row)
    db.commit()
    return row


# ════════════════════════════════════════════════════════════════════════════
#  Explicit-unknown detection tests (pure engine)
# ════════════════════════════════════════════════════════════════════════════

class TestExplicitUnknowns:

    def test_ss01_we_dont_know(self):
        """SS-01: 'we don't know' triggers EXPLICIT_UNKNOWN alert."""
        results = extract_explicit_unknowns("We don't know if the database was compromised.")
        assert len(results) == 1
        assert "database was compromised" in results[0].lower()

    def test_ss02_unclear_whether(self):
        """SS-02: 'unclear whether' triggers detection."""
        results = extract_explicit_unknowns("It's unclear whether the rollback completed.")
        assert len(results) == 1
        assert "rollback" in results[0].lower()

    def test_ss03_not_sure_if(self):
        """SS-03: 'not sure if' triggers detection."""
        results = extract_explicit_unknowns("I'm not sure if the payment data is affected.")
        assert len(results) == 1
        assert "payment data" in results[0].lower()

    def test_ss04_no_unknown_language(self):
        """SS-04: Normal operational text → no unknowns detected."""
        results = extract_explicit_unknowns("The payment service is returning 500 errors.")
        assert results == []

    def test_ss05_multiple_unknowns_in_one_text(self):
        """SS-05: Multiple unknown patterns in one text → multiple results."""
        text = (
            "We don't know the root cause. "
            "Also unclear whether the backup is intact."
        )
        results = extract_explicit_unknowns(text)
        assert len(results) >= 2

    def test_ss06_short_captures_filtered(self):
        """SS-06: Captures shorter than 4 chars are filtered out."""
        # "we don't know" followed by just "it" (2 chars) should be dropped
        results = extract_explicit_unknowns("We don't know it.")
        # "it" is only 2 chars → filtered
        assert results == []


# ════════════════════════════════════════════════════════════════════════════
#  Critical-keyword detection tests
# ════════════════════════════════════════════════════════════════════════════

class TestCriticalKeywords:

    def test_ss07_compromised_is_critical(self):
        """SS-07: 'compromised' matches critical keywords."""
        assert is_critical("The database may have been compromised") is True

    def test_ss08_latency_is_not_critical(self):
        """SS-08: 'latency increased slightly' is NOT critical."""
        assert is_critical("Latency increased slightly on the API gateway") is False

    def test_ss09_security_breach_is_critical(self):
        """SS-09: 'security breach' matches critical keywords."""
        assert is_critical("A security breach was detected in the auth service") is True


# ════════════════════════════════════════════════════════════════════════════
#  Stale-critical detection tests (pure engine, fast-forwarded clock)
# ════════════════════════════════════════════════════════════════════════════

class TestStaleCriticalDetection:

    def _detector(self, window: timedelta = _WINDOW) -> SilenceDetector:
        return SilenceDetector(staleness_window=window)

    def _claim_dict(
        self,
        text: str,
        timestamp: datetime,
        supporting: list | None = None,
        contradicting: list | None = None,
        status: str = "UNVERIFIED",
        speaker: str = "Alice",
        cid: str | None = None,
    ) -> dict:
        return {
            "id": cid or _uuid(),
            "text": text,
            "speaker": speaker,
            "timestamp": timestamp,
            "supporting": supporting or [],
            "contradicting": contradicting or [],
            "status": status,
        }

    def test_ss10_stale_critical_triggers_alert(self):
        """SS-10: Critical claim with no evidence, older than window → STALE alert."""
        claim = self._claim_dict(
            "The database was compromised",
            timestamp=_NOW - timedelta(minutes=15),   # 15 min ago — past the 10 min window
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "STALE_CRITICAL_CLAIM"
        assert alerts[0].source_id == claim["id"]
        assert alerts[0].severity == "CRITICAL"

    def test_ss11_recent_critical_no_alert(self):
        """SS-11: Critical claim within the window → no alert (too recent)."""
        claim = self._claim_dict(
            "Security breach detected in auth service",
            timestamp=_NOW - timedelta(minutes=5),   # only 5 min ago — within 10 min window
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert alerts == []

    def test_ss12_critical_with_evidence_no_alert(self):
        """SS-12: Critical claim WITH supporting evidence → no alert."""
        claim = self._claim_dict(
            "The database was compromised",
            timestamp=_NOW - timedelta(minutes=15),
            supporting=["evidence-1"],
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert alerts == []

    def test_ss13_non_critical_stale_no_alert(self):
        """SS-13: Non-critical claim with no evidence, older than window → no alert."""
        claim = self._claim_dict(
            "The API latency increased slightly",
            timestamp=_NOW - timedelta(minutes=30),
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert alerts == []

    def test_ss14_resolved_critical_no_alert(self):
        """SS-14: Critical claim in RESOLVED status → no alert (terminal)."""
        claim = self._claim_dict(
            "The database was compromised",
            timestamp=_NOW - timedelta(minutes=20),
            status="RESOLVED",
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert alerts == []

    def test_ss15_two_stale_criticals(self):
        """SS-15: Two stale critical claims → two alerts."""
        claims = [
            self._claim_dict(
                "The database was compromised",
                timestamp=_NOW - timedelta(minutes=15),
            ),
            self._claim_dict(
                "Credential theft suspected in the auth service",
                timestamp=_NOW - timedelta(minutes=12),
            ),
        ]
        alerts = self._detector().detect_stale_criticals(claims, now=_NOW)
        assert len(alerts) == 2

    def test_ss16_suggested_question_includes_speaker_and_text(self):
        """SS-16: Suggested question contains the speaker name and claim text."""
        claim = self._claim_dict(
            "Total data loss in the primary cluster",
            timestamp=_NOW - timedelta(minutes=20),
            speaker="Rahul",
        )
        alerts = self._detector().detect_stale_criticals([claim], now=_NOW)
        assert len(alerts) == 1
        q = alerts[0].suggested_question
        assert "Rahul" in q
        assert "Total data loss" in q


# ════════════════════════════════════════════════════════════════════════════
#  DB adapter tests (scan_for_unresolved)
# ════════════════════════════════════════════════════════════════════════════

class TestScanForUnresolved:

    def test_ss17_transcript_unknown_persists_unknown_row(self, db):
        """SS-17: Transcript event with 'we don't know' → Unknown row persisted + alert."""
        _seed_transcript_event(
            db,
            "We don't know if the database was compromised.",
            speaker="Priya",
            timestamp=_NOW - timedelta(minutes=2),
        )

        alerts = scan_for_unresolved(
            "INC-001", db, now=_NOW, staleness_window_sec=600,
        )

        # Should have at least one EXPLICIT_UNKNOWN alert
        unknown_alerts = [a for a in alerts if a.alert_type == "EXPLICIT_UNKNOWN"]
        assert len(unknown_alerts) >= 1

        # Verify Unknown row was persisted
        unknown_rows = db.query(Unknown).all()
        assert len(unknown_rows) >= 1
        assert unknown_alerts[0].source_id == unknown_rows[0].id
        assert unknown_rows[0].status == "OPEN"

    def test_ss18_stale_critical_from_db(self, db):
        """SS-18: Stale critical claim in DB is detected by scan_for_unresolved."""
        _seed_claim(
            db,
            "The database was compromised and data may have been exfiltrated",
            timestamp=_NOW - timedelta(minutes=15),
            status="UNVERIFIED",
        )

        alerts = scan_for_unresolved(
            "INC-001", db, now=_NOW, staleness_window_sec=600,
        )

        stale = [a for a in alerts if a.alert_type == "STALE_CRITICAL_CLAIM"]
        assert len(stale) == 1
        assert stale[0].severity == "CRITICAL"

    def test_ss19_empty_db_returns_empty(self, db):
        """SS-19: Empty DB → no alerts."""
        alerts = scan_for_unresolved("INC-001", db, now=_NOW, staleness_window_sec=600)
        assert alerts == []

    def test_ss20_combined_unknown_and_stale(self, db):
        """SS-20: Both explicit unknown + stale critical detected in one scan."""
        # Seed a transcript event with unknown language
        _seed_transcript_event(
            db,
            "We don't know who deployed the bad config.",
            speaker="Meera",
            timestamp=_NOW - timedelta(minutes=3),
        )

        # Seed a stale critical claim
        _seed_claim(
            db,
            "Unauthorized access detected in production",
            timestamp=_NOW - timedelta(minutes=20),
            status="UNVERIFIED",
        )

        alerts = scan_for_unresolved(
            "INC-001", db, now=_NOW, staleness_window_sec=600,
        )

        unknown_alerts = [a for a in alerts if a.alert_type == "EXPLICIT_UNKNOWN"]
        stale_alerts   = [a for a in alerts if a.alert_type == "STALE_CRITICAL_CLAIM"]

        assert len(unknown_alerts) >= 1
        assert len(stale_alerts) == 1

        # Verify both types of results
        assert unknown_alerts[0].suggested_question  # non-empty
        assert stale_alerts[0].severity == "CRITICAL"
