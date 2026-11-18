"""Is Nullwake itself still watching? Signals that do not depend on the worker being alive to report them.

`/watchdog` serves this to an external uptime monitor (Google Cloud Monitoring, UptimeRobot, Pingdom, ...): it returns
503 when no worker has reported in, checks are overdue, no check has completed recently, every check on a connection
fails to execute, or alert delivery is stuck. The worker's dead man's switch pings only while this is healthy. Both
alert through a channel of their own, so a dead worker cannot silence the alarm about itself. It reports counts only,
never check, dataset or channel names, so it is safe to expose.

Heartbeats track the worker loop; "no checks completing" and "checks cannot execute" track work actually getting
done, so a live loop that cannot reach the warehouse is not reported as healthy. All ages use the database clock.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Integer, cast, func, or_, select

from .config import Settings
from .models import AlertDelivery, CheckRun, Monitor, WorkerHeartbeat

STALLED_DELIVERY_SECONDS = 15 * 60


def _age_seconds(column):
    return func.extract("epoch", func.now() - column)


def overdue_checks(session, grace_seconds: int) -> int:
    """Enabled checks that should have run more than grace_seconds ago and are not protected by a live lease."""
    return session.scalar(
        select(func.count()).select_from(Monitor).where(
            Monitor.enabled.is_(True), Monitor.next_run_at < func.now() - timedelta(seconds=grace_seconds),
            or_(Monitor.lease_expires_at.is_(None), Monitor.lease_expires_at < func.now()))) or 0


def stalled_deliveries(session) -> int:
    """Alerts that are stuck: still pending 15 minutes after they were written, or given up on in the last day."""
    return session.scalar(
        select(func.count()).select_from(AlertDelivery).where(or_(
            (AlertDelivery.status == "pending")
            & (AlertDelivery.created_at < func.now() - timedelta(seconds=STALLED_DELIVERY_SECONDS)),
            (AlertDelivery.status == "failed") & (AlertDelivery.created_at > func.now() - timedelta(days=1)),
        ))) or 0


def connections_not_executing(session) -> int:
    """Connections on which every enabled check's last run was an execution error (warehouse unreachable, revoked
    grant, ...). One broken check is an incident of its own; all of them failing means Nullwake cannot watch."""
    errored = func.sum(cast(Monitor.last_status == "error", Integer))
    rows = session.execute(
        select(Monitor.connection_name).where(Monitor.enabled.is_(True), Monitor.last_status.is_not(None))
        .group_by(Monitor.connection_name).having(func.count() == errored)).all()
    return len(rows)


def monitoring_status(session, settings: Settings) -> dict:
    beat_age = session.scalar(select(_age_seconds(func.max(WorkerHeartbeat.heartbeat_at))))
    max_beat_age = settings.stall_seconds + 60
    enabled = session.scalar(select(func.count()).select_from(Monitor).where(Monitor.enabled.is_(True))) or 0
    longest_interval = session.scalar(select(func.max(Monitor.interval_seconds)).where(Monitor.enabled.is_(True))) or 0
    last_run_age = session.scalar(select(_age_seconds(func.max(CheckRun.started_at))))
    max_run_gap = longest_interval + settings.watchdog_grace_seconds + settings.lease_seconds
    overdue = overdue_checks(session, settings.watchdog_grace_seconds)
    not_executing = connections_not_executing(session)
    stalled = stalled_deliveries(session)
    problems = []
    if enabled and (beat_age is None or beat_age > max_beat_age):
        problems.append("no worker heartbeat")
    if overdue:
        problems.append("checks overdue")
    if enabled and last_run_age is not None and last_run_age > max_run_gap:
        problems.append("no checks completing")
    if not_executing:
        problems.append("checks cannot execute")
    if stalled:
        problems.append("alert delivery stalled")
    return {"ok": not problems, "problems": problems, "checksEnabled": enabled, "checksOverdue": overdue,
            "connectionsNotExecuting": not_executing, "alertDeliveriesStalled": stalled,
            "workerHeartbeatAgeSeconds": round(float(beat_age), 1) if beat_age is not None else None,
            "workerHeartbeatMaxAgeSeconds": max_beat_age,
            "lastCheckSecondsAgo": round(float(last_run_age), 1) if last_run_age is not None else None}
