"""Prometheus metrics computed from the metadata store at scrape time (works with N workers)."""
from __future__ import annotations

from datetime import timedelta

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select

from .db import session_factory
from .models import AlertDelivery, CheckRun, Incident, Monitor, WorkerHeartbeat, utcnow

STATUS = func.coalesce(Monitor.last_status, "pending").label("status")


class NullwakeCollector(Collector):
    def collect(self):
        with session_factory()() as db:
            checks = GaugeMetricFamily("nullwake_checks", "Checks by last status", labels=["status"])
            for status, n in db.execute(select(STATUS, func.count())
                                        .where(Monitor.enabled.is_(True)).group_by(STATUS)):
                checks.add_metric([status], n)
            yield checks
            incidents = GaugeMetricFamily("nullwake_incidents_active", "Unresolved incidents", labels=["severity"])
            for sev in ("warning", "critical"):
                n = db.scalar(select(func.count()).select_from(Incident)
                              .where(Incident.status != "resolved", Incident.severity == sev))
                incidents.add_metric([sev], n or 0)
            yield incidents
            runs = CounterMetricFamily("nullwake_check_runs_last_hour", "Check runs in the last hour", labels=["status"])
            since = utcnow() - timedelta(hours=1)
            for status, n in db.execute(select(CheckRun.status, func.count()).where(CheckRun.started_at >= since)
                                        .group_by(CheckRun.status)):
                runs.add_metric([status], n)
            yield runs
            queue = GaugeMetricFamily("nullwake_alert_deliveries", "Alert outbox by status", labels=["status"])
            for status, n in db.execute(select(AlertDelivery.status, func.count()).group_by(AlertDelivery.status)):
                queue.add_metric([status], n)
            yield queue
            newest = db.scalar(select(func.max(WorkerHeartbeat.heartbeat_at)))
            age = GaugeMetricFamily("nullwake_worker_heartbeat_age_seconds", "Seconds since any worker heartbeat")
            age.add_metric([], (utcnow() - newest).total_seconds() if newest else float("inf"))
            yield age
