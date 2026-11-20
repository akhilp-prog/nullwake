"""REST API (v1). Authenticate with `Authorization: Bearer nw_...` tokens or a browser session."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import engine
from .config import Settings
from .contracts import ContractError, load_documents, parse_bundle
from .deps import get_db, require, settings_dep, verify_mutation
from .models import CheckRun, Connection, Contract, Incident, Monitor
from .security import Principal

STATUS = func.coalesce(Monitor.last_status, "pending").label("status")

router = APIRouter(prefix="/api/v1", tags=["v1"])


def _check(m: Monitor) -> dict:
    return {"id": m.id, "name": m.name, "kind": m.kind, "dataset": m.dataset, "severity": m.severity,
            "contract": m.contract.name if m.contract else None, "status": m.last_status or "pending",
            "message": m.last_message, "lastRunAt": m.last_run_at, "nextRunAt": m.next_run_at,
            "intervalSeconds": m.interval_seconds}


def _incident(i: Incident, events: bool = False) -> dict:
    out = {"id": i.id, "title": i.title, "status": i.status, "severity": i.severity, "check": i.monitor.name,
           "checkId": i.monitor_id, "dataset": i.monitor.dataset, "openedAt": i.opened_at,
           "lastSeenAt": i.last_seen_at, "failureCount": i.failure_count, "acknowledgedBy": i.acknowledged_by,
           "resolvedAt": i.resolved_at, "resolvedBy": i.resolved_by}
    if events:
        out["events"] = [{"at": e.at, "actor": e.actor, "kind": e.kind, "message": e.message} for e in i.events]
    return out


@router.get("/status")
def status(db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    counts = dict(db.execute(select(STATUS, func.count())
                             .where(Monitor.enabled.is_(True)).group_by(STATUS)).all())
    active = db.scalar(select(func.count()).select_from(Incident).where(Incident.status != "resolved"))
    return {"checks": counts, "activeIncidents": active,
            "contracts": db.scalar(select(func.count()).select_from(Contract))}


@router.get("/contracts")
def contracts(db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    rows = db.scalars(select(Contract).where(Contract.retired_at.is_(None)).order_by(Contract.name)).all()
    return [{"name": c.name, "owner": c.owner, "dataset": c.dataset, "connection": c.connection_name,
             "version": c.version, "updatedAt": c.updated_at, "checks": [_check(m) for m in c.active_monitors]} for c in rows]


@router.get("/contracts/{name}")
def contract(name: str, db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    c = db.scalar(select(Contract).where(Contract.name == name))
    if c is None:
        raise HTTPException(404, "contract not found")
    return {"name": c.name, "owner": c.owner, "description": c.description, "dataset": c.dataset,
            "connection": c.connection_name, "version": c.version, "checksum": c.checksum,
            "yaml": c.source_yaml, "checks": [_check(m) for m in c.active_monitors]}


@router.get("/checks")
def checks(status: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    stmt = select(Monitor).where(Monitor.retired_at.is_(None)).order_by(Monitor.name)
    if status:
        stmt = stmt.where(func.coalesce(Monitor.last_status, "pending") == status)
    return [_check(m) for m in db.scalars(stmt)]


@router.get("/checks/{check_id}/runs")
def runs(check_id: int, limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db),
         _: Principal = Depends(require("viewer"))):
    if db.get(Monitor, check_id) is None:
        raise HTTPException(404, "check not found")
    rows = db.scalars(select(CheckRun).where(CheckRun.monitor_id == check_id)
                      .order_by(CheckRun.id.desc()).limit(limit))
    return [{"startedAt": r.started_at, "status": r.status, "value": r.value, "message": r.message,
             "durationMs": r.duration_ms, "trigger": r.trigger, "observed": r.observed} for r in rows]


@router.post("/checks/{check_id}/run", status_code=202)
def run_now(check_id: int, request: Request, db: Session = Depends(get_db),
            principal: Principal = Depends(require("editor")), settings: Settings = Depends(settings_dep)):
    verify_mutation(request, principal, settings, request.headers.get("x-csrf-token"))
    m = db.get(Monitor, check_id)
    if m is None:
        raise HTTPException(404, "check not found")
    if m.retired_at is not None or not m.enabled:
        raise HTTPException(409, "check is retired or disabled and will not run")
    engine.request_run(m)
    engine.audit(db, principal.email, "check.run_now", f"check/{m.name}")
    return {"queued": True}


@router.get("/incidents")
def incidents(status: str = Query("active", pattern="^(active|resolved|all)$"), limit: int = Query(100, le=500),
              db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    stmt = select(Incident).order_by(Incident.id.desc()).limit(limit)
    if status == "active":
        stmt = stmt.where(Incident.status != "resolved")
    elif status == "resolved":
        stmt = stmt.where(Incident.status == "resolved")
    return [_incident(i) for i in db.scalars(stmt).unique()]


@router.get("/incidents/{incident_id}")
def incident(incident_id: int, db: Session = Depends(get_db), _: Principal = Depends(require("viewer"))):
    i = db.get(Incident, incident_id)
    if i is None:
        raise HTTPException(404, "incident not found")
    return _incident(i, events=True)


class Note(BaseModel):
    note: str = Field("", max_length=2000)


@router.post("/incidents/{incident_id}/acknowledge")
def acknowledge(incident_id: int, request: Request, body: Note = Body(default_factory=Note),
                db: Session = Depends(get_db), principal: Principal = Depends(require("editor")),
                settings: Settings = Depends(settings_dep)):
    verify_mutation(request, principal, settings, request.headers.get("x-csrf-token"))
    i = db.get(Incident, incident_id)
    if i is None:
        raise HTTPException(404, "incident not found")
    if not engine.acknowledge(db, i, principal.email, body.note):
        raise HTTPException(409, f"incident is {i.status}")
    return _incident(i)


def _sql_changes(db: Session, bundle) -> list[str]:
    """Custom SQL checks that are new, changed, or moved to another connection compared with what is stored.

    Re-applying unchanged SQL (the usual CI run) needs no extra permission; writing new SQL does."""
    out = []
    for c in bundle.contracts:
        for m in c.monitors:
            if m.kind != "assertion":
                continue
            row = db.scalar(select(Monitor).where(Monitor.name == m.name))
            if row is None or row.retired_at is not None or row.kind != "assertion" \
                    or (row.params or {}).get("sql") != m.params.get("sql") or row.connection_name != m.connection:
                out.append(m.name)
    return out


@router.post("/config/apply")
async def apply_config(request: Request, dry_run: bool = False, prune: bool = False,
                       db: Session = Depends(get_db), principal: Principal = Depends(require("editor")),
                       settings: Settings = Depends(settings_dep)):
    """Apply Connection / DataContract / AlertChannel YAML (multi-document). Use ?dry_run=true in CI."""
    verify_mutation(request, principal, settings, request.headers.get("x-csrf-token"))
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "config bundle too large")
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bundle.yaml"
        path.write_bytes(raw)
        known = dict(db.execute(select(Connection.name, Connection.type)).all())
        try:
            bundle = parse_bundle(load_documents([path]), known)
        except ContractError as exc:
            raise HTTPException(422, str(exc).replace(str(path), "body")) from None
    if (bundle.connections or bundle.channels) and not principal.can("admin"):
        raise HTTPException(403, "changing connections or alert channels requires the admin role")
    if prune and not principal.can("admin"):
        raise HTTPException(403, "prune retires every contract missing from the bundle and requires the admin role")
    sql_changes = _sql_changes(db, bundle)
    if sql_changes and not principal.can(settings.sql_author_role):
        raise HTTPException(403, f"new or changed custom SQL ({', '.join(sql_changes[:5])}) requires the "
                                 f"{settings.sql_author_role} role: custom SQL can read anything the connection's "
                                 "warehouse role can read")
    if dry_run:
        retire = sorted(set(db.scalars(select(Contract.name).where(Contract.retired_at.is_(None))))
                        - {c.name for c in bundle.contracts}) if prune else []
        return {"valid": True, "contracts": [c.name for c in bundle.contracts],
                "checks": sum(len(c.monitors) for c in bundle.contracts), "sqlChanges": sql_changes,
                "wouldRetire": retire}
    try:
        summary = engine.apply_bundle(db, bundle, principal.email, prune=prune)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return summary.as_dict()
