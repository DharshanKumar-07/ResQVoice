"""Observable, deterministic agent loop for the ResQVoice product demo.

The runtime never exposes chain-of-thought. It records goals, chosen safe tools,
evidence, recommendations, and verification outcomes as auditable events.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Action, Conflict, EventLog, Fact, Hypothesis, Intervention, Participant, Unknown
from app.schemas import HypothesisStatus
from app.services.observability import log_event


TOOL_ROLE = {
    "check_service_health": ("devops", "sre", "backend"),
    "get_error_rate": ("devops", "sre", "support"),
    "get_recent_deployments": ("devops", "sre", "backend"),
    "get_database_metrics": ("database", "backend", "devops"),
    "check_feature_flags": ("product", "backend", "devops"),
    "inspect_service_logs": ("backend", "sre", "devops"),
}

OUTAGE_SIGNALS = (
    "prod is down", "production is down", "service is down", "site is down",
    "outage", "503", "cannot checkout", "can't checkout", "payments are failing",
)


def _status(value: Any) -> str:
    return str(getattr(value, "value", value or "")).upper()


def _latest_human_text(db: Session) -> str:
    rows = (
        db.query(EventLog)
        .filter(EventLog.event_type == "TRANSCRIPT_CHUNK")
        .order_by(EventLog.id.desc())
        .limit(20)
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if payload.get("participant_type") != "ai_agent" and payload.get("speaker") != "ResQVoice AI":
            return str(payload.get("text") or "")
    return ""


def choose_tool(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("database", "connection", "postgres", "db ")):
        return "get_database_metrics"
    if any(word in lower for word in ("deploy", "release", "rollback")):
        return "get_recent_deployments"
    if any(word in lower for word in ("flag", "rollout", "experiment")):
        return "check_feature_flags"
    if any(word in lower for word in ("error", "failure", "recovered", "recovery", "fixed")):
        return "get_error_rate"
    return "check_service_health"


def is_outage_trigger(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(signal in normalized for signal in OUTAGE_SIGNALS)


def run_mock_tool(tool_name: str, context: str = "") -> dict[str, Any]:
    recovering = any(word in context.lower() for word in ("recovered", "recovery", "fixed", "resolved", "rollback complete"))
    results: dict[str, dict[str, Any]] = {
        "check_service_health": {
            "service": "payment-service", "status": "healthy" if recovering else "degraded",
            "http_status": 200 if recovering else 503,
        },
        "get_error_rate": {
            "service": "payment-service", "error_rate_percent": 0.7 if recovering else 38.2,
            "baseline_percent": 1.0, "recovered": recovering,
        },
        "get_recent_deployments": {
            "deployment": "pricing-service-v42", "minutes_ago": 12, "status": "active",
        },
        "get_database_metrics": {
            "connection_pool_utilization_percent": 96, "waiting_connections": 41,
            "status": "critical",
        },
        "check_feature_flags": {
            "flag": "new-pricing-rollout", "enabled": True, "exposure_percent": 100,
        },
        "inspect_service_logs": {
            "service": "pricing-servlet", "level": "ERROR", "matches": 1842,
            "signature": "Database connection pool timeout", "window_minutes": 5,
        },
    }
    if tool_name not in results:
        raise ValueError(f"Unsupported read-only tool: {tool_name}")
    return {"tool": tool_name, "mode": "DEMO_READ_ONLY", "result": results[tool_name]}


def _tool_fact(tool: dict[str, Any]) -> str:
    name, result = tool["tool"], tool["result"]
    if name == "get_error_rate":
        return f"Payment error rate is {result['error_rate_percent']}% (baseline {result['baseline_percent']}%)."
    if name == "get_database_metrics":
        return f"Database connection pool utilization is {result['connection_pool_utilization_percent']}% with {result['waiting_connections']} waiting connections."
    if name == "get_recent_deployments":
        return f"Deployment {result['deployment']} became active {result['minutes_ago']} minutes ago."
    if name == "check_feature_flags":
        return f"Feature flag {result['flag']} is enabled for {result['exposure_percent']}% of traffic."
    if name == "inspect_service_logs":
        return f"Logs show {result['matches']} {result['level']} events from {result['service']}: {result['signature']}."
    return f"Payment service health is {result['status']} with HTTP {result['http_status']}."


def _recommend_owner(db: Session, tool_name: str) -> dict[str, str] | None:
    participants = db.query(Participant).filter(Participant.participant_type == "human").all()
    keywords = TOOL_ROLE[tool_name]
    ranked = sorted(
        participants,
        key=lambda row: next((index for index, word in enumerate(keywords) if word in row.role.lower()), 99),
    )
    if not ranked or not any(word in ranked[0].role.lower() for word in keywords):
        return None
    return {"uid": ranked[0].agora_uid, "name": ranked[0].display_name, "role": ranked[0].role}


def _update_hypotheses(hypotheses: list[Hypothesis], fact_text: str) -> list[dict[str, Any]]:
    evidence_words = {
        word.strip(".,:;()") for word in fact_text.lower().split()
        if len(word.strip(".,:;()")) >= 5
    }
    updates = []
    for row in hypotheses:
        hypothesis_words = {
            word.strip(".,:;()") for word in row.description.lower().split()
            if len(word.strip(".,:;()")) >= 5
        }
        overlap = sorted(evidence_words.intersection(hypothesis_words))
        if not overlap:
            continue
        previous = float(row.confidence or 0)
        row.confidence = min(0.95, previous + 0.15)
        if row.confidence >= 0.7 and _status(row.status) == "UNCONFIRMED":
            row.status = HypothesisStatus.CORROBORATED
        updates.append({
            "hypothesis_id": row.id, "previous_confidence": previous,
            "new_confidence": row.confidence, "evidence_overlap": overlap,
        })
    return updates


def latest_agent_cycle(db: Session) -> dict[str, Any] | None:
    row = db.query(EventLog).filter(EventLog.event_type == "AGENT_CYCLE").order_by(EventLog.id.desc()).first()
    return dict(row.payload or {}) if row else None


def agent_activity_feed(db: Session, limit: int = 32) -> list[dict[str, Any]]:
    """Build a concise, judge-friendly audit trail from persisted runtime events."""
    feed: list[dict[str, Any]] = []
    events = db.query(EventLog).order_by(EventLog.id.desc()).limit(limit).all()
    for event in events:
        payload = event.payload or {}
        timestamp = (event.timestamp or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc).isoformat()
        if event.event_type == "AGENT_CYCLE":
            tool = payload.get("tool_call") or {}
            result = tool.get("result") or {}
            updates = payload.get("hypothesis_updates") or []
            stages = [
                ("observe", "Observed incident context", (payload.get("activity") or ["No new responder update."])[0], "complete"),
                ("plan", "Updated response plan", f"Evaluated {len(payload.get('plan') or [])} recovery steps against current evidence.", "complete"),
                ("act", f"Called {tool.get('tool', 'safe monitoring tool')}", ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in result.items()), "complete"),
                ("verify", "Verified monitoring evidence", (payload.get("activity") or ["", "", "Evidence recorded."])[-1 if len(payload.get("activity") or []) < 3 else 2], "complete"),
                ("decide", "Selected the next best action", payload.get("next_question") or "Waiting for the next incident update.", "active"),
            ]
            if updates:
                stages.insert(4, ("learn", "Re-ranked a root-cause hypothesis", f"Confidence updated for {len(updates)} hypothesis or hypotheses as new evidence matched.", "complete"))
            for index, (phase, title, detail, status) in enumerate(stages):
                feed.append({
                    "id": f"event-{event.id}-{index}", "timestamp": timestamp,
                    "phase": phase, "title": title, "detail": detail, "status": status,
                })
        elif event.event_type == "EXTRACTION_RESULT":
            counts = [
                f"{len(payload.get(name) or [])} {name}"
                for name in ("facts", "hypotheses", "actions", "decisions")
                if payload.get(name)
            ]
            feed.append({
                "id": f"event-{event.id}", "timestamp": timestamp, "phase": "extract",
                "title": "Structured the conversation", "detail": ", ".join(counts) or "No new operational assertions extracted.",
                "status": "complete",
            })
        elif event.event_type == "TRANSCRIPT_CHUNK":
            if payload.get("participant_type") == "ai_agent" or payload.get("speaker") == "ResQVoice AI":
                phase, title = "communicate", "Agent spoke to the response team"
            else:
                phase, title = "listen", f"Heard {payload.get('speaker') or 'a responder'}"
            feed.append({
                "id": f"event-{event.id}", "timestamp": timestamp, "phase": phase,
                "title": title, "detail": str(payload.get("text") or "")[:240], "status": "complete",
            })
        elif event.event_type in {"REMEDIATION_SELECTED", "REMEDIATION_EXECUTED", "RECOVERY_CHECK"}:
            labels = {
                "REMEDIATION_SELECTED": ("remediate", "Selected the smallest safe remediation"),
                "REMEDIATION_EXECUTED": ("remediate", "Executed sandbox remediation"),
                "RECOVERY_CHECK": ("verify", "Ran post-remediation health checks"),
            }
            phase, title = labels[event.event_type]
            feed.append({
                "id": f"event-{event.id}", "timestamp": timestamp, "phase": phase,
                "title": title, "detail": str(payload.get("detail") or payload.get("action") or ""),
                "status": "complete" if payload.get("status") != "AWAITING_APPROVAL" else "active",
            })

    interventions = db.query(Intervention).order_by(Intervention.created_at.desc()).limit(8).all()
    for row in interventions:
        created = (row.created_at or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc).isoformat()
        feed.append({
            "id": f"intervention-{row.id}", "timestamp": created, "phase": "intervene",
            "title": f"{row.severity.title()} intervention · {row.status.lower()}",
            "detail": row.message, "status": "active" if row.status in {"PENDING", "DEFERRED"} else "complete",
        })
    return sorted(feed, key=lambda item: item["timestamp"], reverse=True)[:limit]


def run_autonomous_outage_playbook(
    db: Session, text: str, trigger_event_id: int, channel: str = "incident-room",
) -> dict[str, Any]:
    """Investigate an outage and safely simulate the chosen remediation for the demo."""
    existing = (
        db.query(EventLog)
        .filter(EventLog.event_type == "AGENT_CYCLE")
        .order_by(EventLog.id.desc())
        .limit(30)
        .all()
    )
    for row in existing:
        if (row.payload or {}).get("trigger_event_id") == trigger_event_id:
            return dict(row.payload or {})

    checks = [
        run_mock_tool("check_service_health", text),
        run_mock_tool("get_error_rate", text),
        run_mock_tool("inspect_service_logs", text),
        run_mock_tool("get_recent_deployments", text),
    ]
    evidence = [_tool_fact(tool) for tool in checks]
    for tool, description in zip(checks, evidence):
        if db.query(Fact).filter(Fact.description == description).first() is None:
            db.add(Fact(
                id=str(uuid.uuid4()), description=description,
                source=f"Demo tool: {tool['tool']}", speaker="ResQVoice Agent",
                timestamp=datetime.now(timezone.utc), status="VERIFIED",
            ))

    diagnosis = "The pricing servlet is exhausting the database connection pool after pricing-service-v42; the failure is isolated, so a full-platform restart is unnecessary."
    remediation = "Restart only the pricing servlet and drain its stale database connections"
    auto_execute = os.getenv("AGENT_DEMO_AUTOREMEDIATE", "true").lower() in {"1", "true", "yes", "on"}
    remediation_status = "SIMULATED_EXECUTED" if auto_execute else "AWAITING_APPROVAL"
    db.add(EventLog(event_type="REMEDIATION_SELECTED", payload={
        "trigger_event_id": trigger_event_id, "action": remediation,
        "detail": f"{remediation}. Whole-system restart rejected as unnecessarily broad.",
        "status": remediation_status,
    }))
    if auto_execute:
        db.add(EventLog(event_type="REMEDIATION_EXECUTED", payload={
            "trigger_event_id": trigger_event_id, "action": remediation,
            "detail": "Sandbox connector restarted pricing-servlet; no real production infrastructure was changed.",
            "mode": "DEMO_SANDBOX", "status": "COMPLETED",
        }))
        recovery = run_mock_tool("get_error_rate", "service recovered after restart")
        recovery_fact = _tool_fact(recovery)
        if db.query(Fact).filter(Fact.description == recovery_fact).first() is None:
            db.add(Fact(
                id=str(uuid.uuid4()), description=recovery_fact,
                source="Demo tool: get_error_rate", speaker="ResQVoice Agent",
                timestamp=datetime.now(timezone.utc), status="VERIFIED",
            ))
        db.add(EventLog(event_type="RECOVERY_CHECK", payload={
            "trigger_event_id": trigger_event_id,
            "detail": f"{recovery_fact} Customer-impact metric returned to baseline.",
            "status": "RECOVERY_VERIFIED",
        }))
    else:
        recovery = None

    owner = _recommend_owner(db, "inspect_service_logs")
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "channel": channel,
        "reason": "automatic_outage_trigger", "trigger_event_id": trigger_event_id,
        "trigger": text,
        "objective": "Verify the reported production outage, isolate the fault, restore service safely, and prove recovery.",
        "plan": [
            {"step": "Verify the reported outage against live health metrics", "status": "DONE"},
            {"step": "Inspect errors, logs, and recent changes", "status": "DONE"},
            {"step": "Isolate the smallest failing component", "status": "DONE"},
            {"step": "Apply the least-disruptive remediation", "status": "DONE" if auto_execute else "BLOCKED"},
            {"step": "Re-check recovery metrics", "status": "DONE" if auto_execute else "PENDING"},
        ],
        "activity": [
            f"Detected outage signal in {text}",
            f"Ran {len(checks)} independent diagnostic checks.",
            f"Root cause isolated: {diagnosis}",
            f"Remediation {remediation_status.lower().replace('_', ' ')}: {remediation}",
        ],
        "tool_call": checks[-1], "tool_calls": checks,
        "diagnosis": diagnosis,
        "remediation": {"action": remediation, "status": remediation_status, "mode": "DEMO_SANDBOX"},
        "recommended_owner": owner, "hypothesis_updates": [],
        "next_question": "Recovery is verified. Please confirm checkout success from a customer path." if auto_execute else f"Approve this targeted remediation: {remediation}?",
    }
    db.add(EventLog(event_type="AGENT_CYCLE", payload=payload))
    db.commit()
    log_event("AUTONOMOUS_OUTAGE_PLAYBOOK_COMPLETED", trigger_event_id=trigger_event_id,
              remediation_status=remediation_status)
    return payload


def run_agent_cycle(db: Session, channel: str = "incident-room", reason: str = "manual") -> dict[str, Any]:
    text = _latest_human_text(db)
    tool_name = choose_tool(text)
    tool = run_mock_tool(tool_name, text)
    fact_text = _tool_fact(tool)
    existing_fact = db.query(Fact).filter(Fact.description == fact_text).first()
    if existing_fact is None:
        existing_fact = Fact(
            id=str(uuid.uuid4()), description=fact_text, source=f"Demo tool: {tool_name}",
            speaker="ResQVoice Agent", timestamp=datetime.now(timezone.utc), status="VERIFIED",
        )
        db.add(existing_fact)

    facts = db.query(Fact).all()
    hypotheses = db.query(Hypothesis).all()
    hypothesis_updates = _update_hypotheses(hypotheses, fact_text)
    conflicts = db.query(Conflict).filter(Conflict.status == "UNRESOLVED").all()
    unknowns = db.query(Unknown).filter(Unknown.status == "OPEN").all()
    actions = db.query(Action).all()
    open_actions = [row for row in actions if _status(row.status) not in {"COMPLETED", "CANCELLED"}]
    unowned = [row for row in open_actions if not (row.owner or "").strip()]
    owner = _recommend_owner(db, tool_name)

    plan = [
        {"step": "Confirm customer impact with monitoring evidence", "status": "DONE" if facts else "ACTIVE"},
        {"step": "Identify and rank likely causes", "status": "DONE" if hypotheses else "ACTIVE"},
        {"step": "Resolve contradictions and evidence gaps", "status": "BLOCKED" if conflicts or unknowns else "DONE"},
        {"step": "Assign and execute recovery actions", "status": "BLOCKED" if unowned else ("ACTIVE" if open_actions else "PENDING")},
        {"step": "Verify recovery metrics and close incident", "status": "PENDING"},
    ]
    if unknowns:
        next_question = f"Who can provide evidence for this gap: {unknowns[0].description}?"
    elif conflicts:
        next_question = f"Please verify the conflicting evidence about {conflicts[0].topic}."
    elif unowned and owner:
        next_question = f"{owner['name']}, can you own this action: {unowned[0].task}?"
    elif unowned:
        next_question = f"Who can own this action: {unowned[0].task}?"
    else:
        next_question = "Please report the latest recovery metric and customer impact."

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "reason": reason,
        "objective": "Stabilize customer impact, establish the cause, and verify recovery with evidence.",
        "plan": plan,
        "activity": [
            f"Observed latest responder update: {text or 'No human update yet.'}",
            f"Selected safe read-only tool: {tool_name}",
            f"Recorded verified monitoring evidence: {fact_text}",
            *(
                [f"Updated confidence for {len(hypothesis_updates)} matching hypothesis or hypotheses."]
                if hypothesis_updates else []
            ),
        ],
        "tool_call": tool,
        "recommended_owner": owner,
        "hypothesis_updates": hypothesis_updates,
        "next_question": next_question,
    }
    db.add(EventLog(event_type="AGENT_CYCLE", payload=payload))
    db.commit()
    log_event("AGENT_CYCLE_COMPLETED", tool=tool_name, reason=reason, next_question=next_question)
    return payload


def verify_recovery(db: Session) -> dict[str, Any]:
    verified = db.query(Fact).filter(Fact.status == "VERIFIED").all()
    text = " ".join(row.description.lower() for row in verified)
    healthy = "health is healthy" in text or "http 200" in text
    baseline = any(
        "error rate" in row.description.lower()
        and ("0.7%" in row.description or "baseline" in row.description.lower() and "38.2%" not in row.description)
        for row in verified
    )
    blockers = []
    if not healthy and not baseline:
        blockers.append("No verified healthy service check or baseline error-rate evidence.")
    if db.query(Conflict).filter(Conflict.status == "UNRESOLVED").count():
        blockers.append("Unresolved contradictions remain.")
    if db.query(Unknown).filter(Unknown.status == "OPEN").count():
        blockers.append("Open evidence gaps remain.")
    ready = not blockers
    return {
        "ready_to_resolve": ready,
        "status": "RECOVERY_VERIFIED" if ready else "VERIFICATION_BLOCKED",
        "blockers": blockers,
        "message": "Recovery is verified with monitoring evidence." if ready else "Incident closure is blocked until recovery evidence is complete.",
    }
