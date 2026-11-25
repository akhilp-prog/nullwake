"""Apply config bundles, run checks, and manage the incident lifecycle."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import telemetry
from .connectors import ConnectorError, open_connector, raw_errors_logged
from .contracts import Bundle
from .models import (AlertChannel, AlertDelivery, AuditEntry, CheckRun, Connection, Contract, Incident,
                     IncidentEvent, Monitor, utcnow)
from .monitors import Result, evaluate

log = logging.getLogger("nullwake.engine")
SEVERITY_RANK = {"warning": 1, "critical": 2}


def audit(session: Session, actor: str, action: str, target: str = "", **detail) -> None:
    session.add(AuditEntry(actor=actor, action=action, target=target[:255], detail=detail))


@dataclass
class ApplySummary:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("created", "updated", "unchanged", "deleted")}


def retire_monitor(session: Session, mon: Monitor, actor: str, reason: str) -> None:
    """Stop running a check without erasing what it found: runs, incidents and queued alerts are kept.
    An open incident is closed with a note, since nothing will ever run to resolve it."""
    now = utcnow()
    mon.enabled, mon.retired_at = False, now
    session.execute(update(Monitor).where(Monitor.id == mon.id).values(
        lease_owner=None, lease_token=None, lease_expires_at=None))
    incident = active_incident(session, mon.id, lock=True)
    if incident is not None:
        incident.status, incident.resolved_at, incident.resolved_by = "resolved", now, actor
        session.add(IncidentEvent(incident_id=incident.id, at=now, actor=actor, kind="resolved",
                                  message=f"Closed because the check was retired: {reason}"))


def apply_bundle(session: Session, bundle: Bundle, actor: str, prune: bool = False) -> ApplySummary:
    summary = ApplySummary()

    def upsert(model, name: str, label: str, values: dict):
        row = session.scalar(select(model).where(model.name == name))
        if row is None:
            session.add(model(name=name, **values))
            summary.created.append(f"{label}/{name}")
            return
        changed = {k: v for k, v in values.items() if getattr(row, k) != v}
        if changed:
            for k, v in changed.items():
                setattr(row, k, v)
            summary.updated.append(f"{label}/{name}")
            return True
        summary.unchanged.append(f"{label}/{name}")
        return False

    for c in bundle.connections:
        if upsert(Connection, c.name, "connection", {"type": c.type, "spec": c.spec}):
            # Same name, different warehouse settings: a run in flight against the old settings must not decide
            # the health of the new ones. Drop those claims (their results are discarded) and rerun at once.
            session.execute(update(Monitor).where(Monitor.connection_name == c.name, Monitor.retired_at.is_(None))
                            .values(lease_owner=None, lease_token=None, lease_expires_at=None, next_run_at=utcnow()))
    for ch in bundle.channels:
        upsert(AlertChannel, ch.name, "channel", {"type": ch.type, "spec": ch.spec,
                                                  "min_severity": ch.min_severity, "enabled": True})
    session.flush()
    known = set(session.scalars(select(Connection.name)))
    for cdef in bundle.contracts:
        if cdef.connection not in known:
            raise ValueError(f"contract {cdef.name}: connection '{cdef.connection}' is not defined")
        contract = session.scalar(select(Contract).where(Contract.name == cdef.name))
        if contract is None:
            contract = Contract(name=cdef.name, version=1)
            session.add(contract)
            summary.created.append(f"contract/{cdef.name}")
        elif contract.checksum != cdef.checksum:
            contract.version += 1
            summary.updated.append(f"contract/{cdef.name}")
        else:
            summary.unchanged.append(f"contract/{cdef.name}")
        contract.owner, contract.description = cdef.owner, cdef.description
        contract.connection_name, contract.dataset = cdef.connection, cdef.dataset
        contract.source_yaml, contract.checksum, contract.spec = cdef.source_yaml, cdef.checksum, cdef.spec
        contract.retired_at = None
        session.flush()
        wanted = {m.name: m for m in cdef.monitors}
        for existing in list(contract.monitors):
            if existing.name not in wanted and existing.retired_at is None:
                retire_monitor(session, existing, actor, "removed from the contract")
                summary.deleted.append(f"check/{existing.name}")
        for mdef in cdef.monitors:
            mon = session.scalar(select(Monitor).where(Monitor.name == mdef.name))
            values = {"contract_id": contract.id, "connection_name": mdef.connection, "kind": mdef.kind,
                      "dataset": mdef.dataset, "params": mdef.params, "severity": mdef.severity,
                      "interval_seconds": mdef.interval_seconds, "enabled": True, "retired_at": None}
            if mon is None:
                session.add(Monitor(name=mdef.name, next_run_at=utcnow(), state={}, **values))
            else:
                revived = mon.retired_at is not None
                definition_changed = any(getattr(mon, k) != values[k] for k in ("kind", "dataset", "params"))
                connection_changed = mon.connection_name != values["connection_name"]
                for k, v in values.items():
                    setattr(mon, k, v)
                if definition_changed or connection_changed:
                    # A run of the old definition may be in flight: drop its claim so its result is discarded.
                    # Written as an UPDATE, not via attributes: this session's copy of the row may predate the claim.
                    session.execute(update(Monitor).where(Monitor.id == mon.id).values(
                        lease_owner=None, lease_token=None, lease_expires_at=None))
                if definition_changed or revived:
                    mon.state, mon.next_run_at = {}, utcnow()
                if revived:  # its last result predates the retirement: show "pending" until it runs again
                    mon.last_status, mon.last_message = None, "Re-added; waiting for its first run"
    if prune:
        keep = {c.name for c in bundle.contracts}
        for contract in session.scalars(select(Contract).where(Contract.retired_at.is_(None))):
            if contract.name not in keep:
                contract.retired_at = utcnow()
                for mon in contract.monitors:
                    if mon.retired_at is None:
                        retire_monitor(session, mon, actor, f"contract {contract.name} was removed")
                summary.deleted.append(f"contract/{contract.name}")
    audit(session, actor, "config.apply", "", **summary.as_dict())
    session.flush()
    return summary


@dataclass(frozen=True)
class CheckSpec:
    """Everything a run needs, copied out of the metadata store so no metadata transaction stays open while the
    warehouse query runs."""
    monitor_id: int
    name: str
    kind: str
    dataset: str
    params: dict
    state: dict
    connection_name: str
    connection_type: str | None
    connection_spec: dict | None


def load_check(session: Session, monitor: Monitor) -> CheckSpec:
    conn_row = session.scalar(select(Connection).where(Connection.name == monitor.connection_name))
    return CheckSpec(monitor.id, monitor.name, monitor.kind, monitor.dataset, dict(monitor.params or {}),
                     dict(monitor.state or {}), monitor.connection_name,
                     conn_row.type if conn_row else None, dict(conn_row.spec) if conn_row else None)


def run_spec(spec: CheckSpec, timeout_seconds: int) -> tuple[Result, int, datetime]:
    """Execute against the warehouse. Touches no metadata session."""
    started = utcnow()
    t0 = time.monotonic()
    if spec.connection_type is None:
        result = Result("error", f"connection '{spec.connection_name}' is not defined")
    else:
        try:
            with open_connector(spec.connection_name, spec.connection_type, spec.connection_spec,
                                timeout_seconds) as conn:
                result = evaluate(conn, spec.kind, spec.dataset, spec.params, spec.state)
        except ConnectorError as exc:
            result = Result("error", str(exc))
        except Exception as exc:  # never let one bad check kill the worker
            if raw_errors_logged():
                log.exception("check %s crashed", spec.name)
            else:  # an unexpected driver exception's text can quote warehouse values
                log.error("check %s crashed: %s", spec.name, exc.__class__.__name__)
            result = Result("error", f"internal error: {exc.__class__.__name__}")
    return result, int((time.monotonic() - t0) * 1000), started


def run_check(session: Session, monitor: Monitor, timeout_seconds: int) -> tuple[Result, int, datetime]:
    """Load and run a check in one call (CLI and tests). The worker uses load_check + run_spec instead."""
    return run_spec(load_check(session, monitor), timeout_seconds)


def request_run(monitor: Monitor) -> None:
    """"Run now": make the check due, and bump the counter so a run that is already going is followed by another."""
    monitor.run_requests = Monitor.run_requests + 1  # incremented in SQL, so concurrent clicks are all counted
    monitor.next_run_at = func.now()


def record_result(session: Session, monitor: Monitor, result: Result, duration_ms: int, started: datetime,
                  trigger: str = "schedule", public_url: str = "") -> CheckRun:
    run = CheckRun(monitor_id=monitor.id, started_at=started, duration_ms=duration_ms, status=result.status,
                   value=result.value, message=result.message[:4000], observed=result.observed, trigger=trigger)
    session.add(run)
    monitor.last_run_at, monitor.last_status, monitor.last_message = started, result.status, result.message[:4000]
    if result.state is not None:
        monitor.state = result.state
    _update_incident(session, monitor, result, public_url)
    session.flush()
    return run


def active_incident(session: Session, monitor_id: int, lock: bool = False) -> Incident | None:
    stmt = select(Incident).where(Incident.monitor_id == monitor_id, Incident.status != "resolved")
    if lock:
        stmt = stmt.with_for_update(of=Incident)
    return session.scalar(stmt)


def _update_incident(session: Session, monitor: Monitor, result: Result, public_url: str) -> None:
    incident = active_incident(session, monitor.id, lock=True)
    now = utcnow()
    if result.status in ("fail", "error"):
        if incident is None:
            prefix = "Check error" if result.status == "error" else "Check failed"
            incident = Incident(monitor_id=monitor.id, status="open", severity=monitor.severity,
                                title=f"{prefix}: {monitor.name}"[:300], opened_at=now, last_seen_at=now,
                                failure_count=1)
            session.add(incident)
            session.flush()
            session.add(IncidentEvent(incident_id=incident.id, at=now, actor="nullwake", kind="opened",
                                      message=result.message))
            enqueue_alerts(session, incident, monitor, "opened", result.message, public_url)
            telemetry.count("incidents", event="opened", severity=monitor.severity)
        else:
            incident.failure_count += 1
            incident.last_seen_at = now
            last = session.scalar(select(IncidentEvent).where(IncidentEvent.incident_id == incident.id,
                                                              IncidentEvent.kind.in_(("opened", "changed")))
                                  .order_by(IncidentEvent.id.desc()).limit(1))
            if last is None or last.message != result.message:
                session.add(IncidentEvent(incident_id=incident.id, at=now, actor="nullwake", kind="changed",
                                          message=result.message))
    elif result.status == "pass" and incident is not None:
        incident.status, incident.resolved_at, incident.resolved_by = "resolved", now, "nullwake"
        session.add(IncidentEvent(incident_id=incident.id, at=now, actor="nullwake", kind="resolved",
                                  message=f"Check passed: {result.message}"))
        enqueue_alerts(session, incident, monitor, "resolved", result.message, public_url)
        telemetry.count("incidents", event="resolved", severity=monitor.severity)


def enqueue_alerts(session: Session, incident: Incident, monitor: Monitor, event: str, message: str,
                   public_url: str) -> int:
    channels = session.scalars(select(AlertChannel).where(AlertChannel.enabled.is_(True))).all()
    count = 0
    for ch in channels:
        if SEVERITY_RANK[incident.severity] < SEVERITY_RANK.get(ch.min_severity, 1):
            continue
        payload = {
            "event": event,
            "incident": {"id": incident.id, "title": incident.title, "severity": incident.severity,
                         "status": incident.status, "message": message, "check": monitor.name,
                         "kind": monitor.kind, "dataset": monitor.dataset,
                         "contract": monitor.contract.name if monitor.contract else None,
                         "owner": monitor.contract.owner if monitor.contract else None,
                         "openedAt": incident.opened_at.isoformat(),
                         "url": f"{public_url}/incidents/{incident.id}" if public_url else None},
        }
        session.add(AlertDelivery(channel_id=ch.id, incident_id=incident.id, event=event, payload=payload))
        count += 1
    return count


def acknowledge(session: Session, incident: Incident, actor: str, note: str = "") -> bool:
    if incident.status != "open":
        return False
    incident.status, incident.acknowledged_at, incident.acknowledged_by = "acknowledged", utcnow(), actor
    session.add(IncidentEvent(incident_id=incident.id, actor=actor, kind="acknowledged", message=note))
    audit(session, actor, "incident.acknowledge", f"incident/{incident.id}")
    return True


def add_note(session: Session, incident: Incident, actor: str, note: str) -> None:
    session.add(IncidentEvent(incident_id=incident.id, actor=actor, kind="note", message=note))
    audit(session, actor, "incident.note", f"incident/{incident.id}")


def resolve(session: Session, incident: Incident, actor: str, note: str) -> bool:
    if incident.status == "resolved":
        return False
    incident.status, incident.resolved_at, incident.resolved_by = "resolved", utcnow(), actor
    session.add(IncidentEvent(incident_id=incident.id, actor=actor, kind="resolved", message=note))
    audit(session, actor, "incident.resolve", f"incident/{incident.id}")
    return True


def accept_schema(session: Session, monitor: Monitor, actor: str) -> bool:
    """Adopt the latest observed schema as the new baseline (baseline-mode schema checks only)."""
    if monitor.kind != "schema" or monitor.params.get("expected") is not None:
        return False
    last = session.scalar(select(CheckRun).where(CheckRun.monitor_id == monitor.id)
                          .order_by(CheckRun.id.desc()).limit(1))
    if not last or not last.observed.get("columns"):
        return False
    monitor.state = {**monitor.state, "baseline": last.observed["columns"]}
    request_run(monitor)
    audit(session, actor, "schema.accept", f"check/{monitor.name}")
    return True


def prune_history(session: Session, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    result = session.execute(delete(CheckRun).where(CheckRun.started_at < cutoff))
    return result.rowcount or 0
