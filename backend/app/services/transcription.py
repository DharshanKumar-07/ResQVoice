import os
import asyncio
import weakref
from pathlib import Path

from aiolimiter import AsyncLimiter
from groq import AsyncGroq, APIStatusError, RateLimitError

from app.services.provider_metrics import PROVIDER_REQUESTS


DEFAULT_GROQ_MODEL = "whisper-large-v3-turbo"


def _groq_rpm_limit() -> int:
    try:
        configured = int(os.environ.get("GROQ_REQUESTS_PER_MINUTE", "18"))
    except ValueError:
        return 18
    return max(1, configured)


GROQ_RATE_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _groq_rate_limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = GROQ_RATE_LIMITERS.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(max_rate=_groq_rpm_limit(), time_period=60)
        GROQ_RATE_LIMITERS[loop] = limiter
    return limiter


class TranscriptionQuotaExceeded(Exception):
    """Groq rejected a transcription because its request quota was exhausted."""


class TranscriptionServiceError(Exception):
    """Groq could not complete a transcription for a non-quota reason."""


async def transcribe_audio_file(path: Path, mime_type: str) -> str:
    """Transcribe one browser audio chunk with Groq Whisper."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise TranscriptionServiceError("GROQ_API_KEY is not configured")

    model = os.environ.get("GROQ_TRANSCRIPTION_MODEL", DEFAULT_GROQ_MODEL).strip()
    client = AsyncGroq(api_key=api_key, max_retries=0)

    try:
        # Groq accepts WebM/Opus, Ogg, MP4/M4A, MP3, and WAV directly, so the
        # browser-native chunk can be forwarded without conversion.
        async with _groq_rate_limiter():
            PROVIDER_REQUESTS.record("groq")
            response = await client.audio.transcriptions.create(
                file=(path.name, path.read_bytes(), mime_type),
                model=model,
                response_format="json",
                temperature=0.0,
            )
    except RateLimitError as exc:
        raise TranscriptionQuotaExceeded from exc
    except APIStatusError as exc:
        if exc.status_code == 429:
            raise TranscriptionQuotaExceeded from exc
        raise TranscriptionServiceError(str(exc)) from exc
    except Exception as exc:
        if getattr(exc, "status_code", None) == 429 or getattr(exc, "code", None) == 429:
            raise TranscriptionQuotaExceeded from exc
        raise TranscriptionServiceError(str(exc)) from exc
    finally:
        await client.close()

    return (response.text or "").strip()
