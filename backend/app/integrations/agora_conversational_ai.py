"""Agora Conversational AI REST client and session configuration."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from urllib.parse import urlencode
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, Field

DEFAULT_SYSTEM_PROMPT = """You are the ResQVoice Incident Co-pilot in a live incident channel.
Listen carefully, keep spoken answers concise, and never invent incident facts.
ResQVoice middleware evaluates each turn for contradictions, unresolved critical
claims, SOP compliance, and approval requirements before your response is spoken.
Never claim a protected production action executed without explicit human approval."""


class StartAgentRequest(BaseModel):
    channel_name: str = Field(min_length=1, max_length=64)
    agent_uid: int = Field(default=1000, ge=1)
    token: str = Field(min_length=1)
    remote_rtc_uids: list[str] = Field(default_factory=lambda: ["*"])
    speaker_uid: str | None = None
    speaker_name: str = "Agora participant"
    speaker_role: str = "Incident responder"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class StartAgentResponse(BaseModel):
    agent_id: str
    session_id: str  # compatibility alias used by the current UI
    agent_uid: int
    status: str
    callback_ready: bool = True


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)
    priority: Literal["INTERRUPT", "APPEND", "IGNORE"] = "INTERRUPT"
    interruptable: bool = True


@dataclass(frozen=True)
class AgoraAgentSettings:
    app_id: str
    customer_id: str
    customer_secret: str
    public_base_url: str
    webhook_secret: str
    api_base_url: str = "https://api.agora.io/api/conversational-ai-agent/v2/projects"
    asr_vendor: str = "ares"
    asr_language: str = "en-US"
    vad_speech_threshold: float = 0.5
    vad_silence_ms: int = 600
    agent_preset: str = "minimax_speech_2_6_turbo"
    tts_config_json: str = '{"params":{"voice_setting":{"voice_id":"English_captivating_female1","speed":1.0},"audio_setting":{"sample_rate":44100}}}'
    llm_model: str = "resqvoice-incident-brain"
    idle_timeout_seconds: int = 300
    validate_public_url: bool = True
    mock: bool = False

    @classmethod
    def from_env(cls) -> "AgoraAgentSettings":
        public_base_url = (
            os.getenv("AGORA_PUBLIC_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        ).rstrip("/")
        # Render's self-referenced `host` property intentionally contains no
        # scheme. Its external web-service hostname is always HTTPS capable.
        if public_base_url and "://" not in public_base_url:
            public_base_url = f"https://{public_base_url}"
        return cls(
            app_id=os.getenv("AGORA_APP_ID", "").strip(),
            customer_id=os.getenv("AGORA_CUSTOMER_ID", "").strip(),
            customer_secret=os.getenv("AGORA_CUSTOMER_SECRET", "").strip(),
            public_base_url=public_base_url,
            webhook_secret=os.getenv("AGORA_WEBHOOK_SECRET", "").strip(),
            api_base_url=os.getenv("AGORA_CONVERSATIONAL_AI_BASE_URL", cls.api_base_url).rstrip("/"),
            asr_vendor=os.getenv("AGORA_ASR_VENDOR", "ares"),
            asr_language=os.getenv("AGORA_ASR_LANGUAGE", "en-US"),
            vad_speech_threshold=float(os.getenv("AGORA_VAD_SPEECH_THRESHOLD", "0.5")),
            vad_silence_ms=int(os.getenv("AGORA_VAD_SILENCE_MS", "600")),
            agent_preset=os.getenv("AGORA_AGENT_PRESET", "minimax_speech_2_6_turbo"),
            tts_config_json=os.getenv("AGORA_TTS_CONFIG_JSON", cls.tts_config_json),
            llm_model=os.getenv("AGORA_LLM_MODEL", "resqvoice-incident-brain"),
            idle_timeout_seconds=int(os.getenv("AGORA_AGENT_IDLE_TIMEOUT_SECONDS", "300")),
            validate_public_url=os.getenv("AGORA_VALIDATE_PUBLIC_URL", "true").lower() == "true",
            mock=os.getenv("MOCK_AGORA_AGENT", "false").lower() == "true",
        )


Sender = Callable[..., Awaitable[httpx.Response]]
ACTIVE_AGENT_SESSIONS: dict[str, dict[str, str]] = {}


def callback_health_is_ready(response: httpx.Response) -> bool:
    """Require the exact ResQVoice callback signature, not merely HTTP 200."""
    if response.status_code != 200:
        return False
    try:
        return response.json() == {
            "status": "ok",
            "service": "resqvoice-agora-callback",
        }
    except ValueError:
        return False


class AgoraConversationalAI:
    def __init__(self, settings: AgoraAgentSettings | None = None, sender: Sender | None = None):
        self.settings = settings or AgoraAgentSettings.from_env()
        self._sender = sender

    def _headers(self) -> dict[str, str]:
        raw = f"{self.settings.customer_id}:{self.settings.customer_secret}"
        return {
            "Authorization": f"Basic {base64.b64encode(raw.encode()).decode()}",
            "Content-Type": "application/json",
        }

    def _validate(self) -> None:
        missing = [name for name, value in (
            ("AGORA_APP_ID", self.settings.app_id),
            ("AGORA_CUSTOMER_ID", self.settings.customer_id),
            ("AGORA_CUSTOMER_SECRET", self.settings.customer_secret),
            ("AGORA_PUBLIC_BASE_URL", self.settings.public_base_url),
            ("AGORA_WEBHOOK_SECRET", self.settings.webhook_secret),
        ) if not value]
        if missing:
            raise RuntimeError(f"Missing Agora configuration: {', '.join(missing)}")
        if not re.match(r"^https://", self.settings.public_base_url):
            raise RuntimeError("AGORA_PUBLIC_BASE_URL must be a publicly reachable HTTPS URL")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._sender:
            return await self._sender(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.request(method, url, **kwargs)

    async def start(self, request: StartAgentRequest) -> StartAgentResponse:
        callback_ready = self.settings.mock
        session_metadata = {
            "channel": request.channel_name,
            "agent_uid": str(request.agent_uid),
            "speaker_uid": request.speaker_uid or "",
            "speaker_name": request.speaker_name,
            "speaker_role": request.speaker_role,
        }
        if self.settings.mock:
            agent_id = f"mock-{request.channel_name}"
            ACTIVE_AGENT_SESSIONS[agent_id] = session_metadata
            return StartAgentResponse(
                agent_id=agent_id, session_id=agent_id,
                agent_uid=request.agent_uid, status="RUNNING",
                callback_ready=True,
            )
        self._validate()
        if self._sender is None and self.settings.validate_public_url:
            health_url = f"{self.settings.public_base_url}/api/agora-agent/health"
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    health = await client.get(health_url)
                if not callback_health_is_ready(health):
                    raise RuntimeError(
                        f"invalid callback health response (HTTP {health.status_code})"
                    )
                callback_ready = True
            except (httpx.RequestError, RuntimeError) as exc:
                raise RuntimeError(
                    "Agora public callback is unavailable. Restart the HTTPS "
                    f"tunnel and update AGORA_PUBLIC_BASE_URL ({health_url}: {exc})."
                ) from exc
        try:
            tts = json.loads(self.settings.tts_config_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AGORA_TTS_CONFIG_JSON must be valid JSON") from exc
        query = {"channel": request.channel_name}
        concrete_uids = [uid for uid in request.remote_rtc_uids if uid != "*"]
        speaker_uid = request.speaker_uid or (
            concrete_uids[0] if len(concrete_uids) == 1 else None
        )
        if speaker_uid:
            query["speaker_uid"] = speaker_uid
        middleware_url = (
            f"{self.settings.public_base_url}/api/agora-agent/llm/chat/completions?"
            f"{urlencode(query)}"
        )
        payload = {
            "name": f"resqvoice-{request.channel_name}-{request.agent_uid}",
            "properties": {
                "channel": request.channel_name,
                "token": request.token,
                "agent_rtc_uid": str(request.agent_uid),
                "remote_rtc_uids": request.remote_rtc_uids or ["*"],
                "idle_timeout": self.settings.idle_timeout_seconds,
                "advanced_features": {"enable_rtm": True},
                "parameters": {"data_channel": "rtm"},
                "asr": {
                    "vendor": self.settings.asr_vendor,
                    "language": self.settings.asr_language,
                    "params": {},
                },
                "turn_detection": {
                    "mode": "default",
                    "config": {
                        "speech_threshold": self.settings.vad_speech_threshold,
                        "start_of_speech": {
                            "mode": "vad",
                            "vad_config": {
                                "interrupt_duration_ms": 160,
                                "speaking_interrupt_duration_ms": 160,
                                "prefix_padding_ms": 800,
                            },
                        },
                        "end_of_speech": {
                            "mode": "vad",
                            "vad_config": {
                                "silence_duration_ms": self.settings.vad_silence_ms,
                            },
                        },
                    },
                },
                "interruption": {"enable": True, "mode": "start_of_speech"},
                "tts": tts,
                "llm": {
                    "vendor": "custom",
                    "url": middleware_url,
                    "api_key": self.settings.webhook_secret,
                    "system_messages": [{"role": "system", "content": request.system_prompt}],
                    "params": {"model": self.settings.llm_model},
                },
            },
        }
        if self.settings.agent_preset:
            # An Agora-managed preset supplies the MiniMax key, group ID, model,
            # and endpoint. The tts object above then contains only voice/audio
            # overrides. A vendor-only config without BYOK credentials accepts
            # Speak requests but produces no audio.
            payload["preset"] = self.settings.agent_preset
        url = f"{self.settings.api_base_url}/{self.settings.app_id}/join"
        try:
            response = await self._request("POST", url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                "Agora agent creation timed out after 60 seconds. Check the agent "
                "pipeline configuration and Agora service status."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Could not reach Agora Conversational AI: {exc.__class__.__name__}"
            ) from exc
        # Agora uses the agent name as an idempotency boundary. A second browser
        # can join the same incident while its singleton agent is already alive;
        # in that case the 409 body includes the existing agent_id. Adopt it
        # instead of turning a healthy shared agent into a room-join failure.
        if response.status_code == 409:
            try:
                conflict = response.json()
            except ValueError:
                conflict = {}
            existing_id = str(conflict.get("agent_id") or conflict.get("agentId") or "")
            if existing_id and conflict.get("reason") == "TaskConflict":
                previous = ACTIVE_AGENT_SESSIONS.get(existing_id)
                shared_listener = "*" in request.remote_rtc_uids
                if shared_listener or (
                    previous
                    and previous.get("speaker_uid") == session_metadata["speaker_uid"]
                ):
                    # A wildcard listener is the room singleton. Adopt it from
                    # any browser (and after backend restarts) instead of
                    # replacing an agent that is serving other users.
                    ACTIVE_AGENT_SESSIONS[existing_id] = session_metadata
                    return StartAgentResponse(
                        agent_id=existing_id,
                        session_id=existing_id,
                        agent_uid=request.agent_uid,
                        status="RUNNING",
                        callback_ready=callback_ready,
                    )

                # Browser RTC UIDs change on every join. Reusing an older agent
                # leaves it subscribed to the departed UID: it looks RUNNING but
                # receives no microphone audio and emits no ASR callbacks. Stop
                # that stale cloud session and recreate it with the current UID.
                leave_url = (
                    f"{self.settings.api_base_url}/{self.settings.app_id}"
                    f"/agents/{existing_id}/leave"
                )
                leave = await self._request(
                    "POST", leave_url, json={}, headers=self._headers()
                )
                if leave.status_code not in {200, 404}:
                    raise RuntimeError(
                        "Could not replace stale Agora agent "
                        f"({leave.status_code}): {leave.text[:500]}"
                    )
                ACTIVE_AGENT_SESSIONS.pop(existing_id, None)
                await asyncio.sleep(0.75)
                response = await self._request(
                    "POST", url, json=payload, headers=self._headers()
                )
                if response.status_code == 409:
                    raise RuntimeError(
                        "Agora is still stopping the previous voice agent; "
                        "please retry joining in a few seconds."
                    )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Agora agent start failed ({response.status_code}): {response.text[:500]}") from exc
        data = response.json()
        agent_id = str(data.get("agent_id") or data.get("agentId") or data.get("session_id") or "")
        if not agent_id:
            raise RuntimeError("Agora join response did not include an agent id")
        ACTIVE_AGENT_SESSIONS[agent_id] = session_metadata
        return StartAgentResponse(
            agent_id=agent_id, session_id=agent_id,
            agent_uid=request.agent_uid,
            status=str(data.get("status") or "RUNNING"),
            callback_ready=callback_ready,
        )

    async def stop(self, agent_id: str) -> dict[str, Any]:
        ACTIVE_AGENT_SESSIONS.pop(agent_id, None)
        if self.settings.mock or agent_id.startswith("mock-"):
            return {"agent_id": agent_id, "status": "STOPPED"}
        self._validate()
        url = f"{self.settings.api_base_url}/{self.settings.app_id}/agents/{agent_id}/leave"
        response = await self._request("POST", url, json={}, headers=self._headers())
        if response.status_code == 404:
            return {"agent_id": agent_id, "status": "STOPPED"}
        response.raise_for_status()
        return response.json() if response.content else {"agent_id": agent_id, "status": "STOPPED"}

    async def speak(self, agent_id: str, request: SpeakRequest) -> dict[str, Any]:
        if self.settings.mock or agent_id.startswith("mock-"):
            return {"agent_id": agent_id, "status": "QUEUED", **request.model_dump()}
        self._validate()
        url = f"{self.settings.api_base_url}/{self.settings.app_id}/agents/{agent_id}/speak"
        response = await self._request("POST", url, headers=self._headers(), json=request.model_dump())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Agora speak failed ({response.status_code}): {response.text[:500]}"
            ) from exc
        return response.json() if response.content else {"status": "QUEUED"}


AGORA_AGENT = AgoraConversationalAI()


async def start_agent_session(request: StartAgentRequest) -> StartAgentResponse:
    return await AGORA_AGENT.start(request)


async def stop_agent_session(agent_id: str) -> dict[str, Any]:
    return await AGORA_AGENT.stop(agent_id)
