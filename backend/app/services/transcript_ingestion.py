"""Final/interim transcript normalization, cumulative-delta extraction and dedupe."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass
class TranscriptInput:
    channel: str
    speaker_uid: str
    text: str
    utterance_id: str | None = None
    sequence: int | None = None
    is_final: bool = True


@dataclass
class TranscriptDecision:
    text: str
    persist: bool
    reason: str


class TranscriptIngestionGuard:
    def __init__(self, duplicate_window_seconds: float = 15.0) -> None:
        self.duplicate_window_seconds = duplicate_window_seconds
        self._last_raw: dict[tuple[str, str], tuple[str, float]] = {}
        self._seen_ids: dict[str, float] = {}
        self._partial: dict[tuple[str, str], str] = {}
        self._last_sequence: dict[tuple[str, str], int] = {}

    def process(self, item: TranscriptInput, *, now: float | None = None) -> TranscriptDecision:
        now = now if now is not None else time.monotonic()
        text = " ".join(item.text.split()).strip()
        if not text:
            return TranscriptDecision("", False, "EMPTY")
        key = (item.channel, item.speaker_uid)
        if not item.is_final:
            self._partial[key] = text
            print(f"[ASR_PARTIAL] channel={item.channel} uid={item.speaker_uid} chars={len(text)}")
            return TranscriptDecision(text, False, "PARTIAL_REPLACE")

        if item.sequence is not None:
            previous_sequence = self._last_sequence.get(key)
            if previous_sequence is not None and item.sequence <= previous_sequence:
                print(
                    f"[UTTERANCE_DEDUPLICATED] channel={item.channel} "
                    f"uid={item.speaker_uid} reason=stale_sequence sequence={item.sequence}"
                )
                return TranscriptDecision("", False, "STALE_SEQUENCE")

        if item.utterance_id:
            if item.utterance_id in self._seen_ids:
                print(f"[UTTERANCE_DEDUPLICATED] utterance_id={item.utterance_id} reason=event_id")
                return TranscriptDecision("", False, "DUPLICATE_ID")
            self._seen_ids[item.utterance_id] = now

        previous, previous_at = self._last_raw.get(key, ("", 0.0))
        if text == previous and now - previous_at <= self.duplicate_window_seconds:
            print(f"[UTTERANCE_DEDUPLICATED] channel={item.channel} uid={item.speaker_uid} reason=same_payload")
            return TranscriptDecision("", False, "DUPLICATE_PAYLOAD")

        delta = text
        if previous and text.startswith(previous) and len(text) > len(previous):
            delta = text[len(previous):].strip(" \t\n,;:-")
            # Some cumulative ASR payloads repeat the previous final sentence at
            # the seam. Drop that one leading sentence, not arbitrary repeats.
            previous_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", previous) if s.strip()]
            delta_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", delta) if s.strip()]
            if previous_sentences and delta_sentences:
                if self._canonical(delta_sentences[0]) == self._canonical(previous_sentences[-1]):
                    delta = " ".join(delta_sentences[1:]).strip()
            reason = "CUMULATIVE_DELTA"
        else:
            reason = "FINAL"

        self._last_raw[key] = (text, now)
        if item.sequence is not None:
            self._last_sequence[key] = item.sequence
        self._partial.pop(key, None)
        self._prune(now)
        if not delta:
            print(f"[UTTERANCE_DEDUPLICATED] channel={item.channel} uid={item.speaker_uid} reason=cumulative_seam")
            return TranscriptDecision("", False, "CUMULATIVE_SEAM")
        print(f"[ASR_FINAL] channel={item.channel} uid={item.speaker_uid} reason={reason} chars={len(delta)}")
        return TranscriptDecision(delta, True, reason)

    @staticmethod
    def _canonical(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    def _prune(self, now: float) -> None:
        cutoff = now - max(60.0, self.duplicate_window_seconds * 4)
        self._seen_ids = {key: value for key, value in self._seen_ids.items() if value >= cutoff}


TRANSCRIPT_GUARD = TranscriptIngestionGuard()
