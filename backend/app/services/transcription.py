import os
from pathlib import Path

from aiolimiter import AsyncLimiter
from google import genai
from google.genai import errors


GEMINI_MODEL = "gemini-3.5-transcribe"
GEMINI_RATE_LIMITER = AsyncLimiter(max_rate=10, time_period=60)


class TranscriptionQuotaExceeded(Exception):
    """Gemini rejected a transcription because its request quota was exhausted."""


class TranscriptionServiceError(Exception):
    """Gemini could not complete a transcription for a non-quota reason."""


async def transcribe_audio_file(path: Path, mime_type: str) -> str:
    """Transcribe one audio file while keeping Gemini traffic under 10 RPM."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise TranscriptionServiceError("GEMINI_API_KEY is not configured")

    client = genai.Client(api_key=api_key)
    uploaded_file = None

    try:
        async with GEMINI_RATE_LIMITER:
            uploaded_file = await client.aio.files.upload(
                file=path,
                config={"mime_type": mime_type},
            )
            interaction = await client.aio.interactions.create(
                model=GEMINI_MODEL,
                input=[
                    {
                        "type": "audio",
                        "uri": uploaded_file.uri,
                        "mime_type": uploaded_file.mime_type,
                    }
                ],
                generation_config={
                    "transcription_config": {
                        "language_codes": [],
                        "mode": {"type": "verbatim"},
                    }
                },
            )
    except errors.ClientError as exc:
        if exc.code == 429 or exc.status == "RESOURCE_EXHAUSTED":
            raise TranscriptionQuotaExceeded from exc
        raise TranscriptionServiceError(str(exc)) from exc
    except Exception as exc:
        # Some Gemini transports expose the HTTP code without using ClientError.
        if getattr(exc, "status_code", None) == 429 or getattr(exc, "code", None) == 429:
            raise TranscriptionQuotaExceeded from exc
        raise TranscriptionServiceError(str(exc)) from exc
    finally:
        if uploaded_file is not None and uploaded_file.name:
            try:
                await client.aio.files.delete(name=uploaded_file.name)
            except Exception as cleanup_error:
                print(f"[Audio STT] Temporary Gemini file cleanup failed: {cleanup_error}")

    return interaction.output_text.strip() if interaction.output_text else ""
