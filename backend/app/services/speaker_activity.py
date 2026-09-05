"""Short-lived hints used to attribute wildcard Agora ASR turns.

Agora's custom LLM request can omit the RTC UID when an agent subscribes to an
entire channel. Browsers therefore report local microphone activity and this
registry supplies a conservative fallback. Authoritative UID metadata from
Agora always takes precedence.
"""
from __future__ import annotations

import time
from threading import Lock


_ACTIVITY_TTL_SECONDS = 15.0
_activity: dict[tuple[str, str], float] = {}
_lock = Lock()


def note_speaker_activity(channel: str, speaker_uid: str | int) -> None:
    now = time.monotonic()
    key = (channel, str(speaker_uid))
    with _lock:
        _activity[key] = now
        _prune(now)


def recent_speaker_uid(channel: str) -> str | None:
    """Return the most recently active human UID, if the hint is still fresh."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        candidates = [
            (seen_at, uid)
            for (candidate_channel, uid), seen_at in _activity.items()
            if candidate_channel == channel
        ]
    return max(candidates, default=(0.0, None))[1]


def _prune(now: float) -> None:
    expired = [
        key for key, seen_at in _activity.items()
        if now - seen_at > _ACTIVITY_TTL_SECONDS
    ]
    for key in expired:
        _activity.pop(key, None)


def clear_speaker_activity() -> None:
    """Test helper."""
    with _lock:
        _activity.clear()
