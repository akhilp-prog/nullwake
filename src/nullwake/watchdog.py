"""Is Nullwake itself still watching? Signals that do not depend on the worker being alive to report them.

`/watchdog` serves this to an external uptime monitor (Google Cloud Monitoring, UptimeRobot, Pingdom, ...): it returns
503 when no worker has reported in, when checks are overdue, or when alert delivery is stuck. The external monitor
alerts through its own channel, so a dead worker cannot silence the alarm about itself. It reports counts only, never
check, dataset or channel names, so it is safe to expose.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select

from .config import Settings
from .models import AlertDelivery, Monitor, WorkerHeartbeat, utcnow

STALLED_DELIVERY_SECONDS = 15 * 60


def overdue_checks(session, grace_seconds: int) -> int:
    """Enabled checks that should have run more than grace_seconds ago and are not protected by a live lease."""
    now = utcnow()
    return session.scalar(
        select(func.count()).select_from(Monitor).where(
            Monitor.enabled.is_(True), Monitor.next_run_at < now - timedelta(seconds=grace_seconds),
            or_(Monitor.lease_expires_at.is_(None), Monitor.lease_expires_at < now))) or 0


def stalled_deliveries(session) -> int:
    """Alerts that are stuck: still pending 15 minutes after they were written, or given up on in the last day."""
    now = utcnow()
    return session.scalar(
        select(func.count()).select_from(AlertDelivery).where(or_(
            (AlertDelivery.status == "pending")
            & (AlertDelivery.created_at < now - timedelta(seconds=STALLED_DELIVERY_SECONDS)),
            (AlertDelivery.status == "failed") & (AlertDelivery.created_at > now - timedelta(days=1)),
        ))) or 0


def monitoring_status(session, settings: Settings) -> dict:
    now = utcnow()
    beat = session.scalar(select(func.max(WorkerHeartbeat.heartbeat_at)))
    beat_age = (now - beat).total_seconds() if beat else None
    max_beat_age = settings.stall_seconds + 60
    enabled = session.scalar(select(func.count()).select_from(Monitor).where(Monitor.enabled.is_(True))) or 0
    overdue = overdue_checks(session, settings.watchdog_grace_seconds)
    stalled = stalled_deliveries(session)
    problems = []
    if enabled and (beat_age is None or beat_age > max_beat_age):
        problems.append("no worker heartbeat")
    if overdue:
        problems.append("checks overdue")
    if stalled:
        problems.append("alert delivery stalled")
    return {"ok": not problems, "problems": problems, "checksEnabled": enabled, "checksOverdue": overdue,
            "alertDeliveriesStalled": stalled,
            "workerHeartbeatAgeSeconds": round(beat_age, 1) if beat_age is not None else None,
            "workerHeartbeatMaxAgeSeconds": max_beat_age}
