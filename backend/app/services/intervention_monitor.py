"""Periodic delayed-intervention scanner and Agora speech dispatcher."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.integrations.agora_conversational_ai import ACTIVE_AGENT_SESSIONS, AGORA_AGENT, SpeakRequest
from app.models import Action, Intervention as InterventionRow
from app.services.event_store import persist_transcript_chunk
from app.services.intervention_policy import (
    INTERVENTION_POLICY, InterventionCandidate, SEVERITY_RANK,
    human_recently_active,
)
from app.services.observability import log_event
from app.services.silence_signal import scan_for_unresolved
from app.services.transcript_stream import TRANSCRIPT_BROADCASTER
from app.services.voice_interventions import (
    enqueue_generated_intervention,
    speak_priority_for,
    speak_to_channel,
)


class InterventionMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._last_periodic_summary: dict[str, datetime] = {}
        self._pause_dispatch_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in tuple(self._pause_dispatch_tasks):
            task.cancel()
        self._pause_dispatch_tasks.clear()

    def dispatch_after_speech_guard(self) -> None:
        """Retry deferred speech as soon as the human-pause guard expires."""
        try:
            guard_seconds = max(
                0.1, float(os.getenv("INTERVENTION_SPEECH_GUARD_SECONDS", "2"))
            )
        except ValueError:
            guard_seconds = 2.0

        async def delayed_dispatch() -> None:
            await asyncio.sleep(guard_seconds + 0.15)
            try:
                await self.scan_and_dispatch()
            except Exception as exc:
                log_event(
                    "INTERVENTION_PAUSE_DISPATCH_ERROR",
                    error_type=type(exc).__name__, message=str(exc)[:300],
                )

        task = asyncio.create_task(delayed_dispatch())
        self._pause_dispatch_tasks.add(task)
        task.add_done_callback(self._pause_dispatch_tasks.discard)

    async def _run(self) -> None:
        interval = max(5, int(os.getenv("INTERVENTION_SCAN_INTERVAL_SECONDS", "30")))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.scan_and_dispatch()
            except Exception as exc:
                log_event("INTERVENTION_MONITOR_ERROR", error_type=type(exc).__name__, message=str(exc)[:300])

    async def scan_and_dispatch(self) -> None:
        if not ACTIVE_AGENT_SESSIONS:
            return
        agent_id, session = next(iter(ACTIVE_AGENT_SESSIONS.items()))
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            summary_interval = max(
                30, int(os.getenv("INTERVENTION_PERIODIC_SUMMARY_SECONDS", "300"))
            )
            last_summary = self._last_periodic_summary.setdefault(agent_id, now)
            if (now - last_summary).total_seconds() >= summary_interval:
                created = await enqueue_generated_intervention(
                    "PERIODIC_SUMMARY", "MEDIUM", db,
                    channel=session.get("channel", "incident-room"),
                    trigger_context={"interval_seconds": summary_interval},
                    bypass_severity_threshold=True,
                )
                if created is not None:
                    self._last_periodic_summary[agent_id] = now

            alerts = scan_for_unresolved("INC-001", db)
            for alert in alerts:
                if alert.severity.upper() not in {"HIGH", "CRITICAL"}:
                    continue
                await enqueue_generated_intervention(
                    alert.alert_type, alert.severity, db,
                    channel=session.get("channel", "incident-room"),
                    trigger_context={
                        "description": alert.description,
                        "suggested_question": alert.suggested_question,
                    },
                    related_claim_ids=[alert.source_id] if alert.source_id else [],
                )

            critical_actions = [
                action for action in db.query(Action).all()
                if not (action.owner or "").strip()
                and (action.priority or "").strip().upper() in {"P1", "CRITICAL", "HIGH"}
            ]
            for action in critical_actions:
                await enqueue_generated_intervention(
                    "UNASSIGNED_CRITICAL_ACTION", "HIGH", db,
                    channel=session.get("channel", "incident-room"),
                    trigger_context={"action_id": action.id, "task": action.task},
                    related_claim_ids=[action.id],
                )

            # Demo-speed ownership follow-ups make the agent visibly persistent.
            # Production deployments can use longer deadlines without changing
            # the decision logic.
            due_cutoff = now + timedelta(seconds=120)
            due_actions = [
                action for action in db.query(Action).all()
                if action.deadline is not None
                and action.deadline.replace(tzinfo=timezone.utc) <= due_cutoff
                and str(getattr(action.status, "value", action.status)).upper()
                not in {"COMPLETED", "CANCELLED"}
            ]
            for action in due_actions:
                deadline = action.deadline.replace(tzinfo=timezone.utc)
                await enqueue_generated_intervention(
                    "ACTION_FOLLOW_UP", "HIGH", db,
                    channel=session.get("channel", "incident-room"),
                    trigger_context={
                        "action_id": action.id, "task": action.task,
                        "owner": action.owner or "unassigned",
                        "deadline": deadline.isoformat(), "overdue": deadline <= now,
                    },
                    related_claim_ids=[action.id],
                )

            expired = (
                db.query(InterventionRow)
                .filter(InterventionRow.status.in_(["PENDING", "DEFERRED"]))
                .filter(InterventionRow.expires_at <= now.replace(tzinfo=None))
                .all()
            )
            for row in expired:
                row.status = "EXPIRED"
                log_event("INTERVENTION_EXPIRED", intervention_id=row.id)
            if expired:
                db.commit()

            window_cutoff = now - timedelta(seconds=INTERVENTION_POLICY.window_seconds)
            spoken_count = (
                db.query(InterventionRow)
                .filter(InterventionRow.status == "SPOKEN")
                .filter(InterventionRow.spoken_at >= window_cutoff.replace(tzinfo=None))
                .count()
            )
            if spoken_count >= INTERVENTION_POLICY.max_per_window:
                return

            rows = (
                db.query(InterventionRow)
                .filter(InterventionRow.status.in_(["PENDING", "DEFERRED"]))
                .all()
            )
            # One Commander voice, deterministic severity-first queue, FIFO
            # within the same severity. Only one item is spoken per scan.
            rows.sort(key=lambda row: (
                -SEVERITY_RANK.get(row.severity.upper(), 0), row.created_at,
            ))
            for row in rows:
                priority = speak_priority_for(row.trigger_type)
                if human_recently_active() and priority != "INTERRUPT":
                    continue
                await speak_to_channel(agent_id, row, db)
                break
        finally:
            db.close()


INTERVENTION_MONITOR = InterventionMonitor()
