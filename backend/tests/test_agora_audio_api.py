from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

@pytest.fixture()
def api_client():
    from app.database import Base

    # Import lazily so this test does not affect the SQLAlchemy type adapters
    # installed by the existing SQLite-backed test modules during collection.
    # The API tests themselves do not need a live PostgreSQL instance.
    with patch.object(Base.metadata, "create_all"):
        from app import main

    fake_db = MagicMock()

    def override_db():
        yield fake_db

    main.app.dependency_overrides[main.get_db] = override_db
    with TestClient(main.app) as client:
        yield client, fake_db, main
    main.app.dependency_overrides.clear()


def test_agora_token_uses_server_credentials_and_one_hour_expiry(api_client):
    client, _, main = api_client
    before = int(time.time())

    with (
        patch.dict(
            main.os.environ,
            {
                "AGORA_APP_ID": "a" * 32,
                "AGORA_APP_CERTIFICATE": "b" * 32,
            },
        ),
        patch.object(
            main.RtcTokenBuilder,
            "buildTokenWithUid",
            return_value="signed-token",
        ) as build_token,
    ):
        response = client.post(
            "/api/agora/token",
            json={"channel": "incident-room", "uid": 42},
        )

    assert response.status_code == 200
    assert response.json() == {
        "token": "signed-token",
        "uid": 42,
        "channel": "incident-room",
        "app_id": "a" * 32,
    }
    app_id, certificate, channel, uid, role, expires_at = build_token.call_args.args
    assert (app_id, certificate, channel, uid, role) == (
        "a" * 32,
        "b" * 32,
        "incident-room",
        42,
        1,
    )
    assert before + 3599 <= expires_at <= int(time.time()) + 3600


def test_deployment_health_requires_database(api_client):
    client, _, main = api_client
    connection = MagicMock()
    with patch.object(main.engine, "connect") as connect:
        connect.return_value.__enter__.return_value = connection
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "service": "resqvoice-api", "database": "ok",
    }
    connection.execute.assert_called_once()


def test_deployment_health_fails_when_database_is_unavailable(api_client):
    client, _, main = api_client
    with patch.object(main.engine, "connect", side_effect=RuntimeError("offline")):
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_audio_transcription_forwards_speaker_metadata(api_client):
    client, fake_db, main = api_client

    with (
        patch.object(
            main,
            "transcribe_audio_file",
            AsyncMock(return_value="The database is healthy."),
        ) as transcribe,
        patch.object(
            main,
            "persist_transcript_chunk",
            return_value=SimpleNamespace(id=123),
        ) as persist_chunk,
        patch.object(main, "_schedule_extraction") as schedule_extraction,
    ):
        response = client.post(
            "/api/audio/transcribe",
            files={"audio": ("chunk.webm", b"audio" * 200, "audio/webm")},
            data={"speaker": "Rahul", "role": "Backend Engineer", "uid": "42"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "transcript": "The database is healthy.",
        "status": "ok",
        "speaker": "Rahul",
        "event_id": 123,
    }
    transcribe.assert_awaited_once()
    persist_chunk.assert_called_once()
    db, speaker, role, text, timestamp = persist_chunk.call_args.args
    assert db is fake_db
    assert (speaker, role, text) == (
        "Rahul",
        "Backend Engineer",
        "The database is healthy.",
    )
    assert timestamp.endswith("+00:00")
    schedule_extraction.assert_called_once_with(
        123, "Rahul", "Backend Engineer", "The database is healthy.", timestamp, 0.0
    )


def test_json_transcript_is_persisted_before_background_extraction(api_client):
    client, fake_db, main = api_client

    with (
        patch.object(
            main,
            "persist_transcript_chunk",
            return_value=SimpleNamespace(id=77),
        ) as persist_chunk,
        patch.object(main, "_schedule_extraction") as schedule_extraction,
    ):
        response = client.post(
            "/api/transcript",
            json={
                "speaker": "Priya",
                "role": "Incident Commander",
                "text": "Declare a P1 incident.",
                "timestamp": "2026-09-01T12:00:00Z",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "event_id": 77}
    persist_chunk.assert_called_once_with(
        fake_db,
        "Priya",
        "Incident Commander",
        "Declare a P1 incident.",
        "2026-09-01T12:00:00Z",
    )
    schedule_extraction.assert_called_once()


def test_audio_transcription_rejects_unsupported_content_type(api_client):
    client, _, _ = api_client
    response = client.post(
        "/api/audio/transcribe",
        files={"audio": ("chunk.txt", b"not audio" * 200, "text/plain")},
        data={"speaker": "Priya", "role": "Incident Commander", "uid": "7"},
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported audio content type: text/plain"


def test_tiny_silent_chunk_never_calls_transcription_provider(api_client):
    client, _, main = api_client
    with patch.object(main, "transcribe_audio_file", AsyncMock()) as transcribe:
        response = client.post(
            "/api/audio/transcribe",
            files={"audio": ("chunk.webm", b"silence", "audio/webm")},
            data={"speaker": "Priya", "role": "Incident Commander", "uid": "7"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
    transcribe.assert_not_awaited()


def test_audio_transcription_does_not_duplicate_call_for_silence_result(api_client):
    client, _, main = api_client

    with (
        patch.object(
            main,
            "transcribe_audio_file",
            AsyncMock(return_value="SILENCE"),
        ) as transcribe,
        patch.object(
            main,
            "persist_transcript_chunk",
            return_value=SimpleNamespace(id=88),
        ),
        patch.object(main, "_schedule_extraction"),
    ):
        response = client.post(
            "/api/audio/transcribe",
            files={"audio": ("chunk.webm", b"audio" * 300, "audio/webm")},
            data={
                "speaker": "Room Mix",
                "role": "Agora Channel",
                "uid": "42",
                "speech_detected": "true",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"transcript": "", "status": "silence"}
    assert transcribe.await_count == 1


def test_reset_workspace_clears_every_persisted_model(api_client):
    client, fake_db, main = api_client
    fake_query = MagicMock()
    fake_db.query.return_value = fake_query

    response = client.post("/api/workspace/reset")

    assert response.status_code == 200
    assert response.json()["status"] == "reset"
    assert fake_db.query.call_count == 12
    assert fake_query.delete.call_count == 12
    fake_db.commit.assert_called_once()


def test_audio_transcription_returns_429_and_retry_after(api_client):
    client, _, main = api_client

    with patch.object(
        main,
        "transcribe_audio_file",
        AsyncMock(side_effect=main.TranscriptionQuotaExceeded),
    ):
        response = client.post(
            "/api/audio/transcribe",
            files={"audio": ("chunk.webm", b"audio" * 300, "audio/webm")},
            data={"speaker": "Room Mix", "role": "Agora Channel", "uid": "42"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "15"
    assert response.json() == {
        "status": "error",
        "code": "QUOTA_EXCEEDED",
        "message": (
            "Groq transcription rate limit exceeded (429). "
            "Please slow down or pause audio."
        ),
    }


def test_audio_transcription_returns_500_for_service_failure(api_client):
    client, _, main = api_client

    with patch.object(
        main,
        "transcribe_audio_file",
        AsyncMock(side_effect=main.TranscriptionServiceError("provider failed")),
    ):
        response = client.post(
            "/api/audio/transcribe",
            files={"audio": ("chunk.webm", b"audio" * 300, "audio/webm")},
            data={"speaker": "Room Mix", "role": "Agora Channel", "uid": "42"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "code": "TRANSCRIPTION_FAILED",
        "message": "Groq audio transcription failed.",
    }
