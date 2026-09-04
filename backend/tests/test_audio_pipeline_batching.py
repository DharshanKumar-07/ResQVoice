from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import extractor
from app.services.extraction_batcher import ExtractionBatcher
from app.services.extractor import TranscriptSegment
from app.services.transcript_stream import TranscriptBroadcaster


def _segment(segment_id: int, text: str, duration: float = 5) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        speaker="Priya" if segment_id == 1 else "Rahul",
        role="Commander" if segment_id == 1 else "Engineer",
        text=text,
        timestamp=f"2026-09-02T12:00:0{segment_id}Z",
        duration_seconds=duration,
    )


def test_extraction_batch_flushes_once_at_duration_threshold():
    async def scenario():
        handler = AsyncMock()
        batcher = ExtractionBatcher(handler)
        with patch.dict(
            "os.environ",
            {
                "EXTRACTION_BATCH_MAX_WORDS": "1000",
                "EXTRACTION_BATCH_MAX_DURATION_SECONDS": "10",
                "EXTRACTION_BATCH_MAX_WAIT_SECONDS": "60",
            },
        ):
            await batcher.add(_segment(1, "First statement."), generation=3)
            await batcher.add(_segment(2, "Second statement."), generation=3)
            await asyncio.sleep(0)
            handler.assert_awaited_once()
            segments, generation = handler.await_args.args
            assert [item.segment_id for item in segments] == [1, 2]
            assert generation == 3
            assert batcher.snapshot()["pending_segments"] == 0

    asyncio.run(scenario())


def test_sixty_seconds_of_speech_produces_two_batched_extractions():
    async def scenario():
        handler = AsyncMock()
        batcher = ExtractionBatcher(handler)
        with patch.dict(
            "os.environ",
            {
                "EXTRACTION_BATCH_MAX_WORDS": "1000",
                "EXTRACTION_BATCH_MAX_DURATION_SECONDS": "30",
                "EXTRACTION_BATCH_MAX_WAIT_SECONDS": "20",
            },
        ):
            for segment_id in range(1, 5):
                await batcher.add(
                    _segment(segment_id, f"Speech chunk {segment_id}.", duration=15),
                    generation=5,
                )
            await asyncio.sleep(0)
            assert handler.await_count == 2
            assert [
                [item.segment_id for item in call.args[0]]
                for call in handler.await_args_list
            ] == [[1, 2], [3, 4]]

    asyncio.run(scenario())


def test_extraction_batch_flushes_short_remark_on_max_wait():
    async def scenario():
        handler = AsyncMock()
        batcher = ExtractionBatcher(handler)
        with patch.dict(
            "os.environ",
            {
                "EXTRACTION_BATCH_MAX_WORDS": "1000",
                "EXTRACTION_BATCH_MAX_DURATION_SECONDS": "1000",
                "EXTRACTION_BATCH_MAX_WAIT_SECONDS": "0.01",
            },
        ):
            await batcher.add(_segment(1, "Short remark.", duration=1), generation=4)
            await asyncio.sleep(0.03)
            handler.assert_awaited_once()

    asyncio.run(scenario())


def test_irregular_utterances_flush_by_accumulated_speech_duration():
    async def scenario():
        handler = AsyncMock()
        batcher = ExtractionBatcher(handler)
        with patch.dict(
            "os.environ",
            {
                "EXTRACTION_BATCH_MAX_WORDS": "1000",
                "EXTRACTION_BATCH_MAX_DURATION_SECONDS": "20",
                "EXTRACTION_BATCH_MAX_WAIT_SECONDS": "60",
            },
        ):
            for segment_id, duration in enumerate((1.25, 6.5, 2.75, 9.5), 1):
                await batcher.add(
                    _segment(segment_id, f"Irregular utterance {segment_id}.", duration),
                    generation=6,
                )
            await asyncio.sleep(0)
            handler.assert_awaited_once()
            assert [item.duration_seconds for item in handler.await_args.args[0]] == [
                1.25, 6.5, 2.75, 9.5,
            ]

    asyncio.run(scenario())


def test_transcript_broadcaster_pushes_each_segment_immediately():
    async def scenario():
        broadcaster = TranscriptBroadcaster()
        stream = broadcaster.event_stream()
        next_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        broadcaster.publish({"id": 7, "speaker": "Priya", "text": "Declare P1"})
        event = await asyncio.wait_for(next_event, timeout=0.1)
        assert '"id": 7' in event
        assert "Declare P1" in event
        await stream.aclose()

    asyncio.run(scenario())


def test_batched_extraction_restores_each_segments_provenance():
    async def scenario():
        raw = extractor._BatchedModelOutput.model_validate({
            "facts": [{
                "segment_id": 1, "id": "fact-1", "description": "Incident declared",
                "source": "voice", "speaker": "wrong", "timestamp": "2000-01-01T00:00:00Z",
                "status": "UNVERIFIED",
            }],
            "hypotheses": [{
                "segment_id": 2, "id": "hyp-1", "description": "Database overload",
                "origin": "wrong", "supporting_evidence": [], "contradicting_evidence": [],
                "confidence": 0.5, "status": "UNCONFIRMED",
            }],
            "claims": [{
                "segment_id": 2, "id": "claim-1", "text": "Database is overloaded",
                "speaker": "wrong", "role": "wrong", "timestamp": "2000-01-01T00:00:00Z",
                "status": "UNVERIFIED", "supporting": [], "contradicting": [],
            }],
            "actions": [{
                "segment_id": 1, "id": "action-1", "task": "Open incident channel",
                "owner": "Priya", "status": "TODO", "priority": "HIGH",
                "created_at": "2000-01-01T00:00:00Z", "deadline": None,
            }],
            "decisions": [{
                "segment_id": 2, "id": "decision-1", "recommendation": "Scale database",
                "evidence": [], "sop_reference": "", "approved_by": None,
                "approval_time": None, "execution_status": "PENDING", "result": None,
            }],
        })
        response = SimpleNamespace(parsed=raw, text="")
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)

        with patch.object(extractor.genai, "Client", return_value=client):
            result = await extractor.extract_claims_batch_async([
                _segment(1, "Declare the incident and open a channel."),
                _segment(2, "The database is overloaded; scale it."),
            ])

        assert result.facts[0].speaker == "Priya"
        assert result.facts[0].timestamp.isoformat().startswith("2026-09-02T12:00:01")
        assert result.hypotheses[0].origin == "Rahul"
        assert result.claims[0].speaker == "Rahul"
        assert result.claims[0].role == "Engineer"
        assert result.actions[0].created_at.isoformat().startswith("2026-09-02T12:00:01")
        assert result.decisions[0].approved_by is None
        assert result.decisions[0].approval_time is None
        assert result.decisions[0].execution_status == "PENDING_APPROVAL"
        assert result.provenance["hyp-1"].timestamp == "2026-09-02T12:00:02Z"
        assert result.provenance["action-1"].speaker == "Priya"
        client.aio.models.generate_content.assert_awaited_once()

    asyncio.run(scenario())
