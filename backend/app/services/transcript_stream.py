from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


class TranscriptBroadcaster:
    """In-process fan-out for low-latency transcript UI updates."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()

    def publish(self, transcript: dict) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(transcript)
            except asyncio.QueueFull:
                # Keep the newest data for a slow browser rather than allowing
                # an unbounded per-client queue to grow.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(transcript)

    async def event_stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                try:
                    transcript = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(transcript)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            self._subscribers.discard(queue)


TRANSCRIPT_BROADCASTER = TranscriptBroadcaster()
