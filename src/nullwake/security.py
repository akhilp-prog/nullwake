"""Authentication primitives: Argon2 passwords, hashed session/API tokens, RBAC, sign-in throttling."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as DBSession

from .models import ROLES, ApiToken, LoginAttempt, Session, User, utcnow

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("nullwake-timing-equalizer")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}$")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}
THROTTLE_WINDOW = timedelta(minutes=15)
THROTTLE_PER_KEY = 10


@dataclass
class Principal:
    email: str
    role: str
    csrf: str = ""
    via: str = "session"
    user_id: int | None = None

    def can(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_password(password: str) -> None:
    if not 14 <= len(password) <= 256:
        raise ValueError("passwords must be 14-256 characters")


def upsert_user(db: DBSession, email: str, role: str, password: str, name: str = "") -> User:
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise ValueError("enter a valid email address")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    validate_password(password)
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, role=role, name=name, password_hash=_hasher.hash(password))
        db.add(user)
    else:
        user.role, user.password_hash, user.is_active = role, _hasher.hash(password), True
        if name:
            user.name = name
        db.execute(delete(Session).where(Session.user_id == user.id))
        # Tokens this person created may not outrank them: a demoted admin's admin tokens are revoked with the change.
        above = [r for r in ROLES if ROLE_RANK[r] > ROLE_RANK[role]]
        if above:
            db.execute(update(ApiToken).where(ApiToken.created_by == email, ApiToken.role.in_(above),
                                              ApiToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    db.flush()
    return user


def verify_login(db: DBSession, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower(), User.is_active.is_(True)))
    try:
        _hasher.verify(user.password_hash if user else _DUMMY_HASH, password[:256])
    except (VerificationError, InvalidHashError):
        return None
    if user is None:
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return user


def throttled(db: DBSession, keys: list[str]) -> bool:
    since = utcnow() - THROTTLE_WINDOW
    db.execute(delete(LoginAttempt).where(LoginAttempt.at < since))
    for key in keys:
        count = db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key))
        if count >= THROTTLE_PER_KEY:
            return True
    return False


def record_failure(db: DBSession, keys: list[str]) -> None:
    for key in keys:
        db.add(LoginAttempt(key=key[:300]))


def create_session(db: DBSession, user: User, hours: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.execute(delete(Session).where(Session.expires_at <= utcnow()))
    db.add(Session(token_hash=token_hash(token), user_id=user.id, csrf=csrf,
                   expires_at=utcnow() + timedelta(hours=hours)))
    return token, csrf


def session_principal(db: DBSession, token: str | None) -> Principal | None:
    if not token:
        return None
    row = db.scalar(select(Session).where(Session.token_hash == token_hash(token), Session.expires_at > utcnow()))
    if row is None or not row.user.is_active:
        return None
    return Principal(row.user.email, row.user.role, row.csrf, "session", row.user.id)


MAX_TOKEN_DAYS = 365


def create_api_token(db: DBSession, name: str, role: str, created_by: str, days: int = 90) -> tuple[ApiToken, str]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if not 1 <= int(days) <= MAX_TOKEN_DAYS:
        raise ValueError(f"token lifetime must be 1 to {MAX_TOKEN_DAYS} days")
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    raw = f"nw_{prefix}_{secret}"
    row = ApiToken(name=name[:80], prefix=prefix, token_hash=token_hash(raw), role=role, created_by=created_by,
                   expires_at=utcnow() + timedelta(days=int(days)))
    db.add(row)
    db.flush()
    return row, raw


def token_principal(db: DBSession, raw: str) -> Principal | None:
    m = re.fullmatch(r"nw_([0-9a-f]{8})_[A-Za-z0-9_-]{20,}", raw or "")
    if not m:
        return None
    row = db.scalar(select(ApiToken).where(ApiToken.prefix == m[1], ApiToken.revoked_at.is_(None),
                                           (ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > utcnow())))
    if row is None or not hmac.compare_digest(row.token_hash, token_hash(raw)):
        return None
    row.last_used_at = utcnow()
    return Principal(f"token:{row.name}", row.role, via="token")
