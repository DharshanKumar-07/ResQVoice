from sqlalchemy.orm import Session
from app.models import EventLog, Fact, Hypothesis, Claim, Action, Decision
from app.services.extractor import ExtractedData

def append_event(db: Session, event_type: str, payload: dict):
    event = EventLog(event_type=event_type, payload=payload)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event

def project_extracted_data(db: Session, data: ExtractedData):
    # This acts as the materialized "current state" update
    for f in data.facts:
        db.merge(Fact(**f.model_dump()))
        
    for h in data.hypotheses:
        dumped = h.model_dump()
        dumped['status'] = dumped['status'].value if hasattr(dumped['status'], 'value') else dumped['status']
        db.merge(Hypothesis(**dumped))
        
    for c in data.claims:
        db.merge(Claim(**c.model_dump()))
        
    for a in data.actions:
        dumped = a.model_dump()
        dumped['status'] = dumped['status'].value if hasattr(dumped['status'], 'value') else dumped['status']
        db.merge(Action(**dumped))
        
    for d in data.decisions:
        db.merge(Decision(**d.model_dump()))
        
    db.commit()

def process_transcript_chunk(db: Session, speaker: str, role: str, text: str, timestamp: str):
    persist_transcript_chunk(db, speaker, role, text, timestamp)
    
    from app.services.extractor import extract_claims
    try:
        extracted = extract_claims(speaker, role, text, timestamp)
        append_event(db, "EXTRACTION_RESULT", extracted.model_dump(mode='json'))
        project_extracted_data(db, extracted)
    except Exception as e:
        print(f"Extraction failed: {e}")


def persist_transcript_chunk(
    db: Session, speaker: str, role: str, text: str, timestamp: str
):
    """Persist raw speech immediately so every frontend can display it."""
    return append_event(db, "TRANSCRIPT_CHUNK", {
        "speaker": speaker,
        "role": role,
        "text": text,
        "timestamp": timestamp
    })


async def extract_and_project_transcript(
    db: Session, speaker: str, role: str, text: str, timestamp: str
):
    """Run the slower structured extraction after the HTTP response is sent."""
    from app.services.extractor import extract_claims_async
    try:
        extracted = await extract_claims_async(speaker, role, text, timestamp)
        append_event(db, "EXTRACTION_RESULT", extracted.model_dump(mode='json'))
        project_extracted_data(db, extracted)
    except Exception as e:
        print(f"Background extraction failed: {e}")
