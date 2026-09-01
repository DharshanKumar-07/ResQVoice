import os
from google import genai
from pydantic import BaseModel, Field
from typing import List
from app.schemas import Fact, Hypothesis, Claim, Action, Decision
from app.services.transcription import GEMINI_RATE_LIMITER

class ExtractedData(BaseModel):
    facts: List[Fact] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    claims: List[Claim] = Field(default_factory=list)
    actions: List[Action] = Field(default_factory=list)
    decisions: List[Decision] = Field(default_factory=list)

SYSTEM_PROMPT = """
You are the ResQVoice Claim Extractor.
Your task is to analyze a transcript chunk from an incident response voice room and extract structured data: Facts, Hypotheses, Claims, Actions, and Decisions.

CRITICAL RULES:
1. Human statements default to Hypothesis or Claim status. 
2. NEVER automatically make a human statement a CONFIRMED Fact. Only monitoring/system data can create CONFIRMED Facts directly.
3. Map each entity to its matching Pydantic schema precisely. Generte unique UUIDs for all `id` fields.
4. Extract only what is explicitly stated.
"""

def extract_claims(speaker: str, role: str, text: str, timestamp: str) -> ExtractedData:
    # Uses standard google-genai client. Ensure GEMINI_API_KEY is in environment.
    client = genai.Client()
    
    prompt = f"""
Transcript Chunk:
Time: {timestamp}
Speaker: {speaker}
Role: {role}
Text: {text}

Extract any relevant entities based on the system instructions.
"""
    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'system_instruction': SYSTEM_PROMPT,
            'response_mime_type': 'application/json',
            'response_schema': ExtractedData,
        }
    )
    
    if hasattr(response, 'parsed') and response.parsed:
        return response.parsed
    else:
        return ExtractedData.model_validate_json(response.text)


async def extract_claims_async(
    speaker: str, role: str, text: str, timestamp: str
) -> ExtractedData:
    """Extract structured entities without blocking the transcription response.

    This shares the same provider-wide limiter as audio STT. A transcript chunk
    therefore consumes two queued Gemini calls at most: transcription and
    structured extraction.
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = f"""
Transcript Chunk:
Time: {timestamp}
Speaker: {speaker}
Role: {role}
Text: {text}

Extract any relevant entities based on the system instructions.
"""
    async with GEMINI_RATE_LIMITER:
        response = await client.aio.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
            config={
                "system_instruction": SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "response_schema": ExtractedData,
            },
        )

    if hasattr(response, "parsed") and response.parsed:
        return response.parsed
    return ExtractedData.model_validate_json(response.text)
