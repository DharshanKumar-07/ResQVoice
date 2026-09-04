import asyncio
from dotenv import load_dotenv
load_dotenv()
from app.integrations.agora_conversational_ai import start_agent_session, StartAgentRequest

async def main():
    req = StartAgentRequest(
        channel_name="incident-room",
        agent_uid=12345,
        system_prompt="hello",
        webhook_url="http://localhost:8000/api/agora/webhook",
        token="test"
    )
    try:
        await start_agent_session(req)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(main())
