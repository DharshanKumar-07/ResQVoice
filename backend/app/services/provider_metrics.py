from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class ProviderRequestCounter:
    """Small in-process rolling request counter for development observability."""

    def __init__(self) -> None:
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._totals: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def record(self, provider: str) -> None:
        now = time.monotonic()
        with self._lock:
            timestamps = self._timestamps[provider]
            self._prune(timestamps, now)
            timestamps.append(now)
            self._totals[provider] += 1
            rpm = len(timestamps)
            total = self._totals[provider]
        print(f"[Provider Requests] provider={provider} rpm={rpm} total={total}")

    def snapshot(self) -> dict[str, dict[str, int]]:
        now = time.monotonic()
        with self._lock:
            providers = set(self._timestamps) | {"groq", "gemini"}
            result: dict[str, dict[str, int]] = {}
            for provider in sorted(providers):
                timestamps = self._timestamps[provider]
                self._prune(timestamps, now)
                result[provider] = {
                    "requests_last_minute": len(timestamps),
                    "requests_total": self._totals[provider],
                }
            return result

    @staticmethod
    def _prune(timestamps: deque[float], now: float) -> None:
        cutoff = now - 60
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()


PROVIDER_REQUESTS = ProviderRequestCounter()
