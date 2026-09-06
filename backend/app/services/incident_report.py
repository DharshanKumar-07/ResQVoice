"""Build an evidence-bound incident handoff and postmortem draft."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Action, Claim, Conflict, Decision, EventLog, Fact, Hypothesis, Unknown


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value or ""))


def _lines(items: list[str], empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def build_incident_report(db: Session, incident_id: str = "INC-001") -> dict[str, Any]:
    facts = db.query(Fact).order_by(Fact.timestamp.asc()).all()
    hypotheses = db.query(Hypothesis).all()
    conflicts = db.query(Conflict).filter(Conflict.status == "UNRESOLVED").all()
    unknowns = db.query(Unknown).filter(Unknown.status == "OPEN").all()
    actions = db.query(Action).order_by(Action.created_at.asc()).all()
    decisions = db.query(Decision).all()
    transcripts = (
        db.query(EventLog)
        .filter(EventLog.event_type == "TRANSCRIPT_CHUNK")
        .order_by(EventLog.id.asc())
        .limit(250)
        .all()
    )

    open_actions = [row for row in actions if _enum_value(row.status).upper() not in {"COMPLETED", "CANCELLED"}]
    status = "ATTENTION_REQUIRED" if conflicts or unknowns or open_actions else "STABLE"
    generated_at = datetime.now(timezone.utc).isoformat()
    timeline = [
        {
            "timestamp": str((row.payload or {}).get("timestamp") or row.timestamp.isoformat()),
            "speaker": str((row.payload or {}).get("speaker") or "Unknown"),
            "text": str((row.payload or {}).get("text") or ""),
        }
        for row in transcripts
    ]
    summary = (
        f"{len(facts)} facts, {len(hypotheses)} hypotheses, {len(conflicts)} unresolved "
        f"conflicts, {len(unknowns)} open questions, and {len(open_actions)} open actions."
    )
    markdown = f"""# ResQVoice Incident Report — {incident_id}

Generated: {generated_at}
Status: {status}

## Executive summary

{summary}

## Established facts

{_lines([f'{row.description} ({row.status})' for row in facts], 'No established facts recorded.')}

## Active hypotheses

{_lines([f'{row.description} ({_enum_value(row.status)}, {round((row.confidence or 0) * 100)}% confidence)' for row in hypotheses], 'No active hypotheses recorded.')}

## Unresolved conflicts

{_lines([f'{row.topic} — verify by: {row.recommended_verification}' for row in conflicts], 'No unresolved conflicts.')}

## Open questions and evidence gaps

{_lines([row.description for row in unknowns], 'No open questions recorded.')}

## Actions

{_lines([f'{row.task} — owner: {row.owner or "UNASSIGNED"}; status: {_enum_value(row.status)}; priority: {row.priority or "normal"}' for row in actions], 'No actions recorded.')}

## Decisions

{_lines([f'{row.recommendation} — status: {row.execution_status}; approved by: {row.approved_by or "not approved"}' for row in decisions], 'No decisions recorded.')}

## Timeline

{_lines([f'{item["timestamp"]} — {item["speaker"]}: {item["text"]}' for item in timeline], 'No transcript timeline recorded.')}

## Follow-up

- Resolve every open conflict and evidence gap.
- Assign an owner and deadline to every remaining action.
- Verify recovery metrics before declaring the incident resolved.
"""
    return {
        "incident_id": incident_id,
        "generated_at": generated_at,
        "status": status,
        "summary": summary,
        "counts": {
            "facts": len(facts), "hypotheses": len(hypotheses),
            "conflicts": len(conflicts), "open_questions": len(unknowns),
            "open_actions": len(open_actions), "decisions": len(decisions),
        },
        "timeline": timeline,
        "markdown": markdown,
    }
