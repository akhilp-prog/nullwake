"""Server-rendered UI. No client-side JavaScript; strict CSP."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import __version__, engine
from .config import Settings, get_settings
from .deps import SESSION_COOKIE, client_ip, get_db, optional_principal, settings_dep, verify_mutation
from .models import (AlertChannel, AlertDelivery, ApiToken, AuditEntry, CheckRun, Connection, Contract, Incident,
                     Monitor, WorkerHeartbeat, utcnow)
from .monitors import human_minutes
from .security import (Principal, create_api_token, create_session, record_failure, throttled, token_hash,
                       verify_login)

STATUS = func.coalesce(Monitor.last_status, "pending").label("status")

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    seconds = (utcnow() - value).total_seconds()
    if seconds < 0:
        return "in " + human_minutes(-seconds / 60)
    if seconds < 60:
        return "just now"
    return human_minutes(seconds / 60) + " ago"


def utc(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"


def sparkline(values: list[float | None], width: int = 220, height: int = 36) -> str:
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = width / (len(pts) - 1)
    coords = " ".join(f"{i * step:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}" for i, v in enumerate(pts))
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="trend">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.6" points="{coords}"/></svg>')


templates.env.filters.update(ago=ago, utc=utc)
templates.env.globals.update(version=__version__)


class LoginRequired(Exception):
    pass


def page_user(principal: Principal | None = Depends(optional_principal)) -> Principal:
    if principal is None:
        raise LoginRequired()
    if principal.via == "token":
        # Tokens are for automation on /api/v1. Keeping them off the web UI means a leaked token cannot mint a
        # replacement token (settings) or read the audit log, so revoking it is enough.
        raise HTTPException(403, "API tokens work on /api/v1 only; sign in to use the web interface")
    return principal


def render(request: Request, name: str, user: Principal | None, code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", get_settings())  # the shared footer shows the operator's contact links
    return templates.TemplateResponse(request, name, {"user": user, **ctx}, status_code=code)


@router.get("/login")
def login_page(request: Request, principal: Principal | None = Depends(optional_principal),
               settings: Settings = Depends(settings_dep)):
    if principal:
        return RedirectResponse("/", 303)
    return render(request, "login.html", None, settings=settings)


@router.post("/login")
def login(request: Request, email: str = Form(..., max_length=254), password: str = Form(..., max_length=1024),
          db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    if request.headers.get("origin") != settings.origin:
        raise HTTPException(403, "request origin could not be verified")
    keys = [f"ip:{client_ip(request, settings)}", f"user:{email.strip().lower()}"]
    if throttled(db, keys):
        return render(request, "login.html", None, 429, settings=settings,
                      error="Too many sign-in attempts. Wait 15 minutes and try again.")
    user = verify_login(db, email, password)
    if user is None:
        record_failure(db, keys)
        return render(request, "login.html", None, 401, settings=settings, error="Email or password is incorrect.")
    token, _ = create_session(db, user, settings.session_hours)
    if _is_demo_user(settings, user.email):
        engine.audit(db, user.email, "auth.login", "")  # demo visitors stay anonymous
        _forget_demo_ips(db, settings)
    else:
        engine.audit(db, user.email, "auth.login", "", ip=keys[0][3:])
    response = RedirectResponse("/", 303)
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.session_hours * 3600, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path="/")
    return response


def _demo_available(settings: Settings) -> bool:
    return bool(settings.demo_mode and settings.demo_user and settings.demo_password)


def _is_demo_user(settings: Settings, email: str) -> bool:
    return bool(settings.demo_mode and settings.demo_user and email == settings.demo_user.strip().lower())


def _forget_demo_ips(db: Session, settings: Settings) -> None:
    """Strip any IP left on the demo account's audit entries (for example from before demo sign-ins were anonymous)."""
    db.execute(text("UPDATE audit_log SET detail = detail - 'ip' WHERE actor = :u AND detail ? 'ip'"),
               {"u": settings.demo_user.strip().lower()})


def _start_demo_session(request: Request, db: Session, settings: Settings):
    keys = [f"ip:{client_ip(request, settings)}"]
    if throttled(db, keys):
        return render(request, "login.html", None, 429, settings=settings,
                      error="Too many sign-in attempts. Wait 15 minutes and try again.")
    user = verify_login(db, settings.demo_user, settings.demo_password)
    if user is None or user.role != "viewer":
        record_failure(db, keys)
        return render(request, "login.html", None, 503, settings=settings,
                      error="The demo is not available right now. Try again in a few minutes.")
    hours = min(settings.session_hours, 8)
    token, _ = create_session(db, user, hours)
    engine.audit(db, user.email, "auth.demo", "")  # no IP: demo visitors stay anonymous
    _forget_demo_ips(db, settings)
    response = RedirectResponse("/", 303)
    response.set_cookie(SESSION_COOKIE, token, max_age=hours * 3600, httponly=True,
                        secure=settings.secure_cookies, samesite="lax", path="/")
    return response


@router.post("/demo")
def demo_login(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep)):
    """One-click sign-in to the public demo. Exists only in demo mode, and only for a read-only (viewer) account."""
    if not _demo_available(settings):
        raise HTTPException(404)
    if request.headers.get("origin") != settings.origin:
        raise HTTPException(403, "request origin could not be verified")
    return _start_demo_session(request, db, settings)


@router.get("/demo")
def demo_link(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(settings_dep),
              principal: Principal | None = Depends(optional_principal)):
    """A link that opens the demo dashboard directly, e.g. from the product site.

    Signs in only on a navigation the person made (Sec-Fetch-User: ?1), so link previews, crawlers and embedded
    requests get the sign-in page with its one-click button instead (307), and never replaces a session someone
    already has. Only the read-only demo account can be reached this way, so a link from another site can at most
    land a visitor in the public demo."""
    if not _demo_available(settings):
        raise HTTPException(404)
    if principal is not None:
        return RedirectResponse("/", 303 if principal.email == settings.demo_user else 307)
    if request.headers.get("sec-fetch-user") != "?1" or request.headers.get("sec-fetch-dest", "document") != "document":
        return RedirectResponse("/login", 307)
    return _start_demo_session(request, db, settings)


@router.post("/logout")
def logout(request: Request, csrf: str = Form(""), db: Session = Depends(get_db),
           user: Principal = Depends(page_user), settings: Settings = Depends(settings_dep)):
    verify_mutation(request, user, settings, csrf)
    from .models import Session as SessionRow
    db.query(SessionRow).filter(SessionRow.token_hash == token_hash(request.cookies.get(SESSION_COOKIE, ""))).delete()
    response = RedirectResponse("/login", 303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/")
def overview(request: Request, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    status_counts = dict(db.execute(select(STATUS, func.count())
                                    .where(Monitor.enabled.is_(True)).group_by(STATUS)).all())
    total = sum(status_counts.values())
    active = db.scalars(select(Incident).where(Incident.status != "resolved")
                        .order_by(Incident.severity.desc(), Incident.opened_at)).unique().all()
    recent = db.scalars(select(Incident).where(Incident.status == "resolved")
                        .order_by(Incident.resolved_at.desc()).limit(5)).unique().all()
    contracts = db.scalars(select(Contract).where(Contract.retired_at.is_(None)).order_by(Contract.name)).all()
    health = []
    for c in contracts:
        statuses = [m.last_status or "pending" for m in c.active_monitors]
        worst = next((s for s in ("error", "fail", "pending") if s in statuses), "pass")
        health.append({"contract": c, "worst": worst, "passing": statuses.count("pass"), "total": len(statuses)})
    beat = db.scalar(select(func.max(WorkerHeartbeat.heartbeat_at)))
    resolved_24h = db.scalars(select(Incident).where(Incident.status == "resolved",
                                                     Incident.resolved_at >= utcnow() - timedelta(hours=24))).unique().all()
    mttr = None
    if resolved_24h:
        mttr = sum((i.resolved_at - i.opened_at).total_seconds() for i in resolved_24h) / len(resolved_24h) / 60
    return render(request, "overview.html", user, counts=status_counts, total=total, active=active, recent=recent,
                  health=health, worker_beat=beat, worker_ok=bool(beat and (utcnow() - beat).total_seconds() < 120),
                  mttr=human_minutes(mttr) if mttr is not None else "—", resolved_24h=len(resolved_24h))


@router.get("/incidents")
def incidents(request: Request, status: str = "active", db: Session = Depends(get_db),
              user: Principal = Depends(page_user)):
    stmt = select(Incident).order_by(Incident.id.desc()).limit(200)
    if status == "active":
        stmt = stmt.where(Incident.status != "resolved")
    elif status == "resolved":
        stmt = stmt.where(Incident.status == "resolved")
    else:
        status = "all"
    return render(request, "incidents.html", user, incidents=db.scalars(stmt).unique().all(), status=status)


def _incident_or_404(db: Session, incident_id: int) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return incident


@router.get("/incidents/{incident_id}")
def incident_detail(request: Request, incident_id: int, db: Session = Depends(get_db),
                    user: Principal = Depends(page_user)):
    incident = _incident_or_404(db, incident_id)
    runs = db.scalars(select(CheckRun).where(CheckRun.monitor_id == incident.monitor_id)
                      .order_by(CheckRun.id.desc()).limit(10)).all()
    deliveries = db.scalars(select(AlertDelivery).where(AlertDelivery.incident_id == incident.id)
                            .order_by(AlertDelivery.id)).all()
    duration = ((incident.resolved_at or utcnow()) - incident.opened_at).total_seconds() / 60
    return render(request, "incident.html", user, incident=incident, runs=runs, deliveries=deliveries,
                  duration=human_minutes(duration))


@router.post("/incidents/{incident_id}/{action}")
def incident_action(request: Request, incident_id: int, action: str, csrf: str = Form(""),
                    note: str = Form("", max_length=2000), db: Session = Depends(get_db),
                    user: Principal = Depends(page_user), settings: Settings = Depends(settings_dep)):
    verify_mutation(request, user, settings, csrf)
    if not user.can("editor"):
        raise HTTPException(403, "requires the editor role")
    incident = _incident_or_404(db, incident_id)
    note = note.strip()
    if action == "acknowledge":
        engine.acknowledge(db, incident, user.email, note)
    elif action == "note" and note:
        engine.add_note(db, incident, user.email, note)
    elif action == "resolve":
        if not note:
            raise HTTPException(400, "add a resolution note")
        engine.resolve(db, incident, user.email, note)
    else:
        raise HTTPException(400, "unknown action or empty note")
    return RedirectResponse(f"/incidents/{incident_id}", 303)


@router.get("/contracts")
def contracts(request: Request, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    rows = db.scalars(select(Contract).where(Contract.retired_at.is_(None)).order_by(Contract.name)).all()
    return render(request, "contracts.html", user, contracts=rows)


@router.get("/contracts/{name}")
def contract_detail(request: Request, name: str, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    contract = db.scalar(select(Contract).where(Contract.name == name))
    if contract is None:
        raise HTTPException(404, "contract not found")
    sparks = {}
    for m in contract.active_monitors:
        values = db.scalars(select(CheckRun.value).where(CheckRun.monitor_id == m.id)
                            .order_by(CheckRun.id.desc()).limit(40)).all()
        sparks[m.id] = sparkline(list(reversed(values)))
    return render(request, "contract.html", user, contract=contract, sparks=sparks)


@router.get("/checks/{check_id}")
def check_detail(request: Request, check_id: int, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    monitor = db.get(Monitor, check_id)
    if monitor is None:
        raise HTTPException(404, "check not found")
    runs = db.scalars(select(CheckRun).where(CheckRun.monitor_id == check_id).order_by(CheckRun.id.desc()).limit(50)).all()
    values = [r.value for r in reversed(runs)]
    incident = engine.active_incident(db, check_id)
    can_accept = monitor.kind == "schema" and monitor.params.get("expected") is None and monitor.last_status == "fail"
    return render(request, "check.html", user, monitor=monitor, runs=runs, spark=sparkline(values, 560, 60),
                  incident=incident, can_accept=can_accept)


@router.post("/checks/{check_id}/{action}")
def check_action(request: Request, check_id: int, action: str, csrf: str = Form(""), db: Session = Depends(get_db),
                 user: Principal = Depends(page_user), settings: Settings = Depends(settings_dep)):
    verify_mutation(request, user, settings, csrf)
    if not user.can("editor"):
        raise HTTPException(403, "requires the editor role")
    monitor = db.get(Monitor, check_id)
    if monitor is None:
        raise HTTPException(404, "check not found")
    if action == "run":
        if monitor.retired_at is not None or not monitor.enabled:
            raise HTTPException(409, "check is retired or disabled and will not run")
        engine.request_run(monitor)
        engine.audit(db, user.email, "check.run_now", f"check/{monitor.name}")
    elif action == "accept-schema":
        if not engine.accept_schema(db, monitor, user.email):
            raise HTTPException(409, "nothing to accept for this check")
    else:
        raise HTTPException(404, "unknown action")
    return RedirectResponse(f"/checks/{check_id}", 303)


@router.get("/audit")
def audit_log(request: Request, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    if not user.can("admin"):
        raise HTTPException(403, "requires the admin role")
    rows = db.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(300)).all()
    return render(request, "audit.html", user, rows=rows)


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db), user: Principal = Depends(page_user)):
    if not user.can("admin"):
        raise HTTPException(403, "requires the admin role")
    return render(request, "settings.html", user,
                  connections=db.scalars(select(Connection).order_by(Connection.name)).all(),
                  channels=db.scalars(select(AlertChannel).order_by(AlertChannel.name)).all(),
                  tokens=db.scalars(select(ApiToken).order_by(ApiToken.id.desc())).all(),
                  workers=db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())).all(),
                  new_token=None)


@router.get("/visitors", response_class=HTMLResponse)
def visitors_page(request: Request, days: int = 30, user: Principal = Depends(page_user),
                  settings: Settings = Depends(settings_dep)):
    if not user.can("admin"):
        raise HTTPException(403, "requires the admin role")
    from urllib.parse import urlsplit

    from . import visitors
    days = 7 if days <= 7 else 30
    stats = visitors.compute(settings.access_log_path, (urlsplit(settings.public_url).hostname or "").lower(), days)
    top = max([d["visitors"] for d in stats.daily] + [1])
    return render(request, "visitors.html", user, stats=stats, top=top)


@router.post("/settings/tokens")
def create_token(request: Request, csrf: str = Form(""), name: str = Form(..., min_length=1, max_length=80),
                 role: str = Form("viewer"), days: int = Form(90), db: Session = Depends(get_db),
                 user: Principal = Depends(page_user),
                 settings: Settings = Depends(settings_dep)):
    verify_mutation(request, user, settings, csrf)
    if not user.can("admin"):
        raise HTTPException(403, "requires the admin role")
    try:
        row, raw = create_api_token(db, name.strip(), role, user.email, days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    engine.audit(db, user.email, "token.create", f"token/{row.prefix}", role=role, name=row.name, days=days)
    return render(request, "settings.html", user,
                  connections=db.scalars(select(Connection).order_by(Connection.name)).all(),
                  channels=db.scalars(select(AlertChannel).order_by(AlertChannel.name)).all(),
                  tokens=db.scalars(select(ApiToken).order_by(ApiToken.id.desc())).all(),
                  workers=db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())).all(),
                  new_token=raw)


@router.post("/settings/tokens/{token_id}/revoke")
def revoke_token(request: Request, token_id: int, csrf: str = Form(""), db: Session = Depends(get_db),
                 user: Principal = Depends(page_user), settings: Settings = Depends(settings_dep)):
    verify_mutation(request, user, settings, csrf)
    if not user.can("admin"):
        raise HTTPException(403, "requires the admin role")
    row = db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(404, "token not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        engine.audit(db, user.email, "token.revoke", f"token/{row.prefix}")
    return RedirectResponse("/settings", 303)
