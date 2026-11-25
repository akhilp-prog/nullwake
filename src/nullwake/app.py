"""ASGI application factory."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from sqlalchemy import func, select, text

from . import __version__, telemetry
from .api import router as api_router
from .config import get_settings
from .db import session_factory
from .metrics import NullwakeCollector
from .models import WorkerHeartbeat, utcnow
from .watchdog import monitoring_status
from .web import LoginRequired, render
from .web import router as web_router

log = logging.getLogger("nullwake.http")
CSP = ("default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def migration_state() -> tuple[str | None, str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with session_factory()() as db:
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return current, head


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Nullwake API", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json",
                  description="Data contracts, freshness/volume/schema checks and incident response.")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(api_router)
    app.include_router(web_router)
    registry = CollectorRegistry()
    registry.register(NullwakeCollector())
    if telemetry.setup("web"):
        telemetry.instrument_app(app)

    @app.middleware("http")
    async def headers_and_logging(request: Request, call_next):
        started = time.monotonic()
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"  # "no-referrer" makes browsers send Origin: null on POST
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path not in ("/healthz", "/metrics"):
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": response.status_code,
                                       "ms": round((time.monotonic() - started) * 1000, 1)})
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", 303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", None, exc.status_code, status=exc.status_code, message=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "invalid request", "details": exc.errors()}, status_code=422)
        return render(request, "error.html", None, 400, status=400, message="That form submission was not valid.")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok\n")

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        try:
            current, head = migration_state()
            with session_factory()() as db:
                beat = db.scalar(select(func.max(WorkerHeartbeat.heartbeat_at)))
        except Exception as exc:
            return JSONResponse({"ready": False, "reason": f"database: {exc.__class__.__name__}"}, 503)
        if current != head:
            return JSONResponse({"ready": False, "reason": f"migrations at {current}, expected {head}"}, 503)
        worker_age = (utcnow() - beat).total_seconds() if beat else None
        return {"ready": True, "version": __version__, "migration": current,
                "workerHeartbeatAgeSeconds": round(worker_age, 1) if worker_age is not None else None}

    @app.get("/watchdog", include_in_schema=False)
    def watchdog():
        """For an external uptime monitor: 503 when Nullwake has stopped watching. Counts only, no names."""
        try:
            with session_factory()() as db:
                status = monitoring_status(db, get_settings())
        except Exception as exc:
            return JSONResponse({"ok": False, "problems": [f"database: {exc.__class__.__name__}"]}, 503)
        return JSONResponse(status, 200 if status["ok"] else 503)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
