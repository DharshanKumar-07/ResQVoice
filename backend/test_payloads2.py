import asyncio
import os
import httpx
import base64
from dotenv import load_dotenv

load_dotenv()
AGORA_APP_ID = os.environ.get("AGORA_APP_ID", "")
AGORA_CUSTOMER_ID = os.environ.get("AGORA_CUSTOMER_ID", "")
AGORA_CUSTOMER_SECRET = os.environ.get("AGORA_CUSTOMER_SECRET", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

url = f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{AGORA_APP_ID}/join"

credential = f"{AGORA_CUSTOMER_ID}:{AGORA_CUSTOMER_SECRET}"
encoded = base64.b64encode(credential.encode("utf-8")).decode("utf-8")
headers = {
    "Authorization": f"Basic {encoded}",
    "Content-Type": "application/json"
}

payloads = [
    {
        "name": "test_4",
        "properties": {
            "channel": "incident-room",
            "token": "test",
            "agent_rtc_uid": "12345",
            "remote_rtc_uids": ["*"],
            "idle_timeout": 120,
            "asr": {"vendor": "ares", "language": "en-US"},
            "tts": {"vendor": "microsoft", "params": {"voice_name": "en-US-AndrewMultilingualNeural"}},
            "llm": {"url": "https://api.groq.com/openai/v1/chat/completions", "api_key": GROQ_API_KEY, "system_messages": [{"role": "system", "content": "hello"}], "params": {"model": "llama-3.1-70b-versatile"}}
        }
    },
    {
        "name": "test_5",
        "properties": {
            "channel": "incident-room",
            "token": "test",
            "agent_rtc_uid": "12345",
            "remote_rtc_uids": ["*"],
            "idle_timeout": 120,
            "llm": {"url": "https://api.groq.com/openai/v1/chat/completions", "api_key": GROQ_API_KEY, "system_messages": [{"role": "system", "content": "hello"}], "params": {"model": "llama-3.1-70b-versatile"}}
        }
    }
]

async def main():
    async with httpx.AsyncClient() as client:
        for i, p in enumerate(payloads):
            print(f"Testing payload {i+4}...")
            resp = await client.post(url, json=p, headers=headers, timeout=20.0)
            print(f"Status: {resp.status_code}")
            print(f"Body: {resp.text}\n")

asyncio.run(main())
