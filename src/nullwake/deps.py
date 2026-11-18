"""Request-scoped dependencies shared by the API and the web UI."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import session_factory
from .security import Principal, session_principal, token_principal

SESSION_COOKIE = "nw_session"


def get_db() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def settings_dep() -> Settings:
    return get_settings()


def client_ip(request: Request, settings: Settings) -> str:
    if settings.client_ip_header:
        value = request.headers.get(settings.client_ip_header, "")
        if value:
            return value.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def optional_principal(request: Request, db: Session = Depends(get_db)) -> Principal | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        principal = token_principal(db, auth[7:].strip())
        if principal is None:
            raise HTTPException(401, "invalid or revoked API token")
        return principal
    return session_principal(db, request.cookies.get(SESSION_COOKIE))


def verify_mutation(request: Request, principal: Principal, settings: Settings, csrf_value: str | None) -> None:
    """Cookie-authenticated writes need a same-origin request and the session's CSRF token."""
    if principal.via == "token":
        return
    if request.headers.get("origin") != settings.origin:
        raise HTTPException(403, "request origin could not be verified")
    if not csrf_value or csrf_value != principal.csrf:
        raise HTTPException(403, "form session expired; reload the page")


def require(role: str):
    def dependency(principal: Principal | None = Depends(optional_principal)) -> Principal:
        if principal is None:
            raise HTTPException(401, "sign in required")
        if not principal.can(role):
            raise HTTPException(403, f"requires the {role} role")
        return principal
    return dependency
