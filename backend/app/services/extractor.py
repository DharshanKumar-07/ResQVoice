import os
from google import genai
from pydantic import BaseModel, Field
from typing import Dict, List
from app.schemas import Fact, Hypothesis, Claim, Action, Decision
from app.services.provider_metrics import PROVIDER_REQUESTS


NO_PROVIDER_RETRIES = {"retry_options": {"attempts": 1}}

class TranscriptSegment(BaseModel):
    segment_id: int
    speaker: str
    role: str
    text: str
    timestamp: str
    duration_seconds: float = 0


class TranscriptProvenance(BaseModel):
    segment_id: int
    speaker: str
    role: str
    timestamp: str

class ExtractedData(BaseModel):
    facts: List[Fact] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    claims: List[Claim] = Field(default_factory=list)
    actions: List[Action] = Field(default_factory=list)
    decisions: List[Decision] = Field(default_factory=list)
    provenance: Dict[str, TranscriptProvenance] = Field(default_factory=dict)


class _SegmentFact(Fact):
    segment_id: int


class _SegmentHypothesis(Hypothesis):
    segment_id: int


class _SegmentClaim(Claim):
    segment_id: int


class _SegmentAction(Action):
    segment_id: int


class _SegmentDecision(Decision):
    segment_id: int


class _BatchedModelOutput(BaseModel):
    facts: List[_SegmentFact] = Field(default_factory=list)
    hypotheses: List[_SegmentHypothesis] = Field(default_factory=list)
    claims: List[_SegmentClaim] = Field(default_factory=list)
    actions: List[_SegmentAction] = Field(default_factory=list)
    decisions: List[_SegmentDecision] = Field(default_factory=list)

SINGLE_SEGMENT_SYSTEM_PROMPT = """
You are the ResQVoice Claim Extractor.
Your task is to analyze transcript segments from an incident response voice room and extract structured data: Facts, Hypotheses, Claims, Actions, and Decisions.

CRITICAL RULES:
1. Human statements default to Hypothesis or Claim status. 
2. NEVER automatically make a human statement a CONFIRMED Fact. Only monitoring/system data can create CONFIRMED Facts directly.
3. Map each entity to its matching Pydantic schema precisely. Generate unique UUIDs for all `id` fields.
4. Extract only what is explicitly stated.
"""

SYSTEM_PROMPT = SINGLE_SEGMENT_SYSTEM_PROMPT + """
5. Every extracted entity MUST include the segment_id of the one original segment
   that supports it. Never use a batch-level speaker or timestamp.
"""

def extract_claims(speaker: str, role: str, text: str, timestamp: str) -> ExtractedData:
    # Uses standard google-genai client. Ensure GEMINI_API_KEY is in environment.
    client = genai.Client(http_options=NO_PROVIDER_RETRIES)
    
    prompt = f"""
Transcript Chunk:
Time: {timestamp}
Speaker: {speaker}
Role: {role}
Text: {text}

Extract any relevant entities based on the system instructions.
"""
    PROVIDER_REQUESTS.record("gemini")
    response = client.models.generate_content(
        model=os.environ.get("GEMINI_EXTRACTION_MODEL", "gemini-3.5-flash-lite"),
        contents=prompt,
        config={
            'system_instruction': SINGLE_SEGMENT_SYSTEM_PROMPT,
            'response_mime_type': 'application/json',
            'response_schema': ExtractedData,
        }
    )
    
    if hasattr(response, 'parsed') and response.parsed:
        extracted = response.parsed
    else:
        extracted = ExtractedData.model_validate_json(response.text)
    return _gate_extracted_decisions(extracted)


def _gate_extracted_decisions(extracted: ExtractedData) -> ExtractedData:
    """Never infer a human approval record from model-extracted speech."""
    extracted.decisions = [
        decision.model_copy(update={
            "approved_by": None,
            "approval_time": None,
            "execution_status": "PENDING_APPROVAL",
            "result": None,
        })
        for decision in extracted.decisions
    ]
    return extracted


async def extract_claims_async(
    speaker: str, role: str, text: str, timestamp: str
) -> ExtractedData:
    """Compatibility wrapper for callers that still submit one text segment."""
    return await extract_claims_batch_async([
        TranscriptSegment(
            segment_id=0,
            speaker=speaker,
            role=role,
            text=text,
            timestamp=timestamp,
        )
    ])


async def extract_claims_batch_async(segments: List[TranscriptSegment]) -> ExtractedData:
    """Extract one structured result from many independently attributed segments."""
    if not segments:
        return ExtractedData()

    client = genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY"),
        http_options=NO_PROVIDER_RETRIES,
    )
    prompt = (
        "Transcript segments (preserve each segment_id exactly):\n"
        + "\n".join(
            f"[{segment.segment_id}] Time: {segment.timestamp} | "
            f"Speaker: {segment.speaker} | Role: {segment.role}\n{segment.text}"
            for segment in segments
        )
        + "\n\nExtract relevant entities and attach each to its supporting segment_id."
    )
    PROVIDER_REQUESTS.record("gemini")
    response = await client.aio.models.generate_content(
        model=os.environ.get("GEMINI_EXTRACTION_MODEL", "gemini-3.5-flash-lite"),
        contents=prompt,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": _BatchedModelOutput,
        },
    )

    if hasattr(response, "parsed") and response.parsed:
        raw = response.parsed
    else:
        raw = _BatchedModelOutput.model_validate_json(response.text)

    by_id = {segment.segment_id: segment for segment in segments}
    result = ExtractedData()

    def provenance_for(entity) -> tuple[TranscriptSegment, dict]:
        if entity.segment_id not in by_id:
            raise ValueError(f"Gemini returned unknown segment_id={entity.segment_id}")
        segment = by_id[entity.segment_id]
        payload = entity.model_dump(exclude={"segment_id"})
        result.provenance[payload["id"]] = TranscriptProvenance(
            segment_id=segment.segment_id,
            speaker=segment.speaker,
            role=segment.role,
            timestamp=segment.timestamp,
        )
        return segment, payload

    for entity in raw.facts:
        segment, payload = provenance_for(entity)
        payload.update(speaker=segment.speaker, timestamp=segment.timestamp)
        result.facts.append(Fact.model_validate(payload))
    for entity in raw.hypotheses:
        segment, payload = provenance_for(entity)
        payload["origin"] = segment.speaker
        result.hypotheses.append(Hypothesis.model_validate(payload))
    for entity in raw.claims:
        segment, payload = provenance_for(entity)
        payload.update(
            speaker=segment.speaker,
            role=segment.role,
            timestamp=segment.timestamp,
        )
        result.claims.append(Claim.model_validate(payload))
    for entity in raw.actions:
        segment, payload = provenance_for(entity)
        payload["created_at"] = segment.timestamp
        result.actions.append(Action.model_validate(payload))
    for entity in raw.decisions:
        _, payload = provenance_for(entity)
        payload.update(
            approved_by=None,
            approval_time=None,
            execution_status="PENDING_APPROVAL",
            result=None,
        )
        result.decisions.append(Decision.model_validate(payload))

    return result
