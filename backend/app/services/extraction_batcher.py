from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

from app.services.extractor import TranscriptSegment


BatchHandler = Callable[[list[TranscriptSegment], int], Awaitable[None]]


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


class ExtractionBatcher:
    """Collect transcript segments and flush by size, speech duration, or age."""

    def __init__(self, handler: BatchHandler) -> None:
        self.handler = handler
        self._segments: list[TranscriptSegment] = []
        self._generation: int | None = None
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._handler_tasks: set[asyncio.Task] = set()

    @property
    def word_limit(self) -> int:
        return _positive_int("EXTRACTION_BATCH_MAX_WORDS", 75)

    @property
    def duration_limit(self) -> float:
        return _positive_float("EXTRACTION_BATCH_MAX_DURATION_SECONDS", 30)

    @property
    def max_wait(self) -> float:
        return _positive_float("EXTRACTION_BATCH_MAX_WAIT_SECONDS", 20)

    async def add(self, segment: TranscriptSegment, generation: int) -> None:
        async with self._lock:
            # A workspace reset starts a new generation. Never mix pre-reset
            # text into a post-reset extraction batch.
            if self._generation is not None and generation != self._generation:
                self._clear_locked()
            self._generation = generation
            self._segments.append(segment)

            words = sum(len(item.text.split()) for item in self._segments)
            duration = sum(item.duration_seconds for item in self._segments)
            if words >= self.word_limit or duration >= self.duration_limit:
                self._flush_locked("threshold")
            elif self._timer is None:
                self._timer = asyncio.create_task(self._flush_after_wait(generation))

    async def clear(self) -> None:
        async with self._lock:
            self._clear_locked()
            self._generation = None

    def snapshot(self) -> dict[str, float | int]:
        return {
            "pending_segments": len(self._segments),
            "pending_words": sum(len(item.text.split()) for item in self._segments),
            "pending_duration_seconds": round(
                sum(item.duration_seconds for item in self._segments), 3
            ),
            "max_words": self.word_limit,
            "max_duration_seconds": self.duration_limit,
            "max_wait_seconds": self.max_wait,
        }

    async def _flush_after_wait(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.max_wait)
            async with self._lock:
                if self._generation == generation:
                    self._flush_locked("timer")
        except asyncio.CancelledError:
            pass

    def _flush_locked(self, reason: str) -> None:
        if not self._segments or self._generation is None:
            return
        segments = self._segments
        generation = self._generation
        self._segments = []
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None
        print(
            f"[Extraction Batch] reason={reason} segments={len(segments)} "
            f"words={sum(len(item.text.split()) for item in segments)} "
            f"speech_seconds={sum(item.duration_seconds for item in segments):.1f}"
        )
        task = asyncio.create_task(self.handler(segments, generation))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _clear_locked(self) -> None:
        self._segments = []
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
