import asyncio

import httpx
from unittest.mock import patch

from app.integrations.agora_conversational_ai import (
    ACTIVE_AGENT_SESSIONS,
    AgoraAgentSettings,
    AgoraConversationalAI,
    StartAgentRequest,
    callback_health_is_ready,
)


def test_agent_join_routes_custom_llm_through_resqvoice():
    calls = []

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"agent_id": "agent-42", "status": "RUNNING"}, request=httpx.Request(method, url))

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="webhook-token",
    ), sender=sender)
    result = asyncio.run(service.start(StartAgentRequest(
        channel_name="incident-room", agent_uid=1000, token="rtc-token",
        remote_rtc_uids=["77"], speaker_name="Priya", speaker_role="Incident Commander",
    )))

    assert result.agent_id == "agent-42"
    assert result.callback_ready is False  # injected sender bypasses public preflight
    method, url, options = calls[0]
    assert method == "POST"
    assert url.endswith("/app-id/join")
    properties = options["json"]["properties"]
    assert options["json"]["preset"] == "minimax_speech_2_6_turbo"
    assert properties["channel"] == "incident-room"
    assert properties["agent_rtc_uid"] == "1000"
    assert properties["remote_rtc_uids"] == ["77"]
    assert properties["asr"]["params"] == {}
    assert properties["turn_detection"]["config"]["end_of_speech"]["vad_config"]["silence_duration_ms"] == 600
    assert properties["interruption"] == {"enable": True, "mode": "start_of_speech"}
    assert properties["asr"]["vendor"] == "ares"
    assert properties["llm"]["url"] == (
        "https://resqvoice.example.test/api/agora-agent/llm/chat/completions"
        "?channel=incident-room&speaker_uid=77"
    )
    assert properties["llm"]["api_key"] == "webhook-token"
    assert properties["llm"]["vendor"] == "custom"
    assert "Incident Co-pilot" in properties["llm"]["system_messages"][0]["content"]


def test_wildcard_listener_keeps_registered_speaker_uid_in_callback():
    calls = []

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"agent_id": "agent-wildcard"}, request=httpx.Request(method, url))

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    asyncio.run(service.start(StartAgentRequest(
        channel_name="incident-room", agent_uid=1000, token="rtc-token",
        remote_rtc_uids=["*"], speaker_uid="4242",
        speaker_name="Priya", speaker_role="Incident Commander",
    )))

    properties = calls[0][2]["json"]["properties"]
    assert properties["remote_rtc_uids"] == ["*"]
    assert properties["llm"]["url"].endswith(
        "?channel=incident-room&speaker_uid=4242"
    )


def test_agent_speak_uses_documented_speak_endpoint_and_interruptability():
    calls = []

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"status": "QUEUED"}, request=httpx.Request(method, url))

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    from app.integrations.agora_conversational_ai import SpeakRequest
    result = asyncio.run(service.speak("agent-42", SpeakRequest(
        text="Verify database health.", priority="APPEND", interruptable=True,
    )))

    assert result["status"] == "QUEUED"
    assert calls[0][1].endswith("/app-id/agents/agent-42/speak")
    assert calls[0][2]["json"] == {
        "text": "Verify database health.", "priority": "APPEND", "interruptable": True,
    }


def test_agent_stop_uses_documented_leave_endpoint():
    calls = []

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"status": "STOPPED"}, request=httpx.Request(method, url))

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    result = asyncio.run(service.stop("agent-42"))

    assert result["status"] == "STOPPED"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/app-id/agents/agent-42/leave")


def test_agent_join_adopts_existing_session_on_task_conflict():
    async def sender(method, url, **kwargs):
        return httpx.Response(
            409,
            json={
                "agent_id": "existing-agent",
                "reason": "TaskConflict",
                "detail": "A session with the same name already exists",
            },
            request=httpx.Request(method, url),
        )

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    ACTIVE_AGENT_SESSIONS["existing-agent"] = {
        "channel": "incident-room", "agent_uid": "1000", "speaker_uid": "42",
    }
    try:
        result = asyncio.run(service.start(StartAgentRequest(
            channel_name="incident-room", agent_uid=1000, token="rtc-token",
            speaker_uid="42", remote_rtc_uids=["42"],
        )))
    finally:
        ACTIVE_AGENT_SESSIONS.pop("existing-agent", None)

    assert result.agent_id == "existing-agent"
    assert result.status == "RUNNING"


def test_agent_join_replaces_task_conflict_bound_to_stale_speaker_uid():
    calls = []
    responses = [
        (409, {"agent_id": "stale-agent", "reason": "TaskConflict"}),
        (200, {"status": "STOPPED"}),
        (200, {"agent_id": "fresh-agent", "status": "RUNNING"}),
    ]

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    result = asyncio.run(service.start(StartAgentRequest(
        channel_name="incident-room", agent_uid=1000, token="rtc-token",
        speaker_uid="new-uid", remote_rtc_uids=["new-uid"],
    )))

    assert result.agent_id == "fresh-agent"
    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/agents/stale-agent/leave")
    assert calls[2][1].endswith("/app-id/join")


def test_wildcard_agent_is_adopted_for_another_participant_without_replacement():
    calls = []

    async def sender(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(
            409,
            json={"agent_id": "shared-agent", "reason": "TaskConflict"},
            request=httpx.Request(method, url),
        )

    service = AgoraConversationalAI(AgoraAgentSettings(
        app_id="app-id", customer_id="customer", customer_secret="secret",
        public_base_url="https://resqvoice.example.test", webhook_secret="token",
    ), sender=sender)
    result = asyncio.run(service.start(StartAgentRequest(
        channel_name="incident-room", agent_uid=1000, token="rtc-token",
        remote_rtc_uids=["*"], speaker_uid=None,
    )))

    try:
        assert result.agent_id == "shared-agent"
        assert len(calls) == 1
        assert ACTIVE_AGENT_SESSIONS["shared-agent"]["channel"] == "incident-room"
    finally:
        ACTIVE_AGENT_SESSIONS.pop("shared-agent", None)


def test_summary_intent_accepts_natural_qualifiers():
    from app.services.voice_interventions import is_summary_request

    assert is_summary_request("Give me a concise live pipeline verification summary.")
    assert is_summary_request("Please give us a quick status summary")
    assert is_summary_request("Summary.")
    assert is_summary_request("Could you give me a summary?")
    assert not is_summary_request("We attached a summary to the ticket.")


def test_render_hostname_becomes_public_https_callback():
    with patch.dict(
        "os.environ",
        {"AGORA_PUBLIC_BASE_URL": "", "RENDER_EXTERNAL_HOSTNAME": "resqvoice-api.onrender.com"},
    ):
        settings = AgoraAgentSettings.from_env()
    assert settings.public_base_url == "https://resqvoice-api.onrender.com"


def test_callback_health_requires_exact_resqvoice_signature():
    request = httpx.Request("GET", "https://resqvoice.example.test/api/agora-agent/health")
    assert callback_health_is_ready(httpx.Response(
        200,
        json={"status": "ok", "service": "resqvoice-agora-callback"},
        request=request,
    ))
    assert not callback_health_is_ready(httpx.Response(
        200, text="tunnel provider placeholder", request=request,
    ))
    assert not callback_health_is_ready(httpx.Response(
        200, json={"status": "ok", "service": "some-other-app"}, request=request,
    ))
