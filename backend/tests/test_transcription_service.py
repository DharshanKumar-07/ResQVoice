from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import transcription


def test_groq_transcription_forwards_native_webm_and_configured_model(tmp_path):
    audio_path = tmp_path / "chunk.webm"
    audio_path.write_bytes(b"webm-audio")

    client = MagicMock()
    client.audio.transcriptions.create = AsyncMock(
        return_value=SimpleNamespace(text="  Database latency is high.  ")
    )
    client.close = AsyncMock()

    with (
        patch.dict(
            transcription.os.environ,
            {
                "GROQ_API_KEY": "test-key",
                "GROQ_TRANSCRIPTION_MODEL": "whisper-large-v3-turbo",
            },
        ),
        patch.object(transcription, "AsyncGroq", return_value=client) as groq_client,
        patch.object(transcription.PROVIDER_REQUESTS, "record") as record,
    ):
        text = asyncio.run(
            transcription.transcribe_audio_file(audio_path, "audio/webm")
        )

    assert text == "Database latency is high."
    groq_client.assert_called_once_with(api_key="test-key", max_retries=0)
    kwargs = client.audio.transcriptions.create.await_args.kwargs
    assert kwargs["file"] == ("chunk.webm", b"webm-audio", "audio/webm")
    assert kwargs["model"] == "whisper-large-v3-turbo"
    assert kwargs["response_format"] == "json"
    record.assert_called_once_with("groq")
    client.close.assert_awaited_once()


def test_groq_transcription_requires_server_key(tmp_path):
    audio_path = tmp_path / "chunk.webm"
    audio_path.write_bytes(b"webm-audio")

    with patch.dict(transcription.os.environ, {"GROQ_API_KEY": ""}):
        try:
            asyncio.run(transcription.transcribe_audio_file(audio_path, "audio/webm"))
        except transcription.TranscriptionServiceError as exc:
            assert str(exc) == "GROQ_API_KEY is not configured"
        else:
            raise AssertionError("Expected missing Groq key to fail")


def test_parallel_groq_transcriptions_keep_files_and_results_independent(tmp_path):
    first_path = tmp_path / "speaker-one.webm"
    second_path = tmp_path / "speaker-two.webm"
    first_path.write_bytes(b"first-audio")
    second_path.write_bytes(b"second-audio")

    entered = 0
    both_entered = asyncio.Event()

    def make_client(label: str):
        client = MagicMock()

        async def create(**kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=0.1)
            return SimpleNamespace(text=label)

        client.audio.transcriptions.create = AsyncMock(side_effect=create)
        client.close = AsyncMock()
        return client

    first_client = make_client("first transcript")
    second_client = make_client("second transcript")

    async def scenario():
        with (
            patch.dict(transcription.os.environ, {"GROQ_API_KEY": "test-key"}),
            patch.object(
                transcription,
                "AsyncGroq",
                side_effect=[first_client, second_client],
            ),
            patch.object(transcription.PROVIDER_REQUESTS, "record"),
        ):
            return await asyncio.gather(
                transcription.transcribe_audio_file(first_path, "audio/webm"),
                transcription.transcribe_audio_file(second_path, "audio/webm"),
            )

    assert asyncio.run(scenario()) == ["first transcript", "second transcript"]
    assert first_client.audio.transcriptions.create.await_args.kwargs["file"][1] == b"first-audio"
    assert second_client.audio.transcriptions.create.await_args.kwargs["file"][1] == b"second-audio"
