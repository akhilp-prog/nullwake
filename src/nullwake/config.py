"""Runtime settings, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    public_url: str = "http://localhost:8000"
    session_hours: int = 8
    worker_poll_seconds: float = 5.0
    worker_batch_size: int = 10
    retention_days: int = 30
    query_timeout_seconds: int = 60
    client_ip_header: str = ""
    demo_mode: bool = False
    demo_user: str = ""
    demo_password: str = ""
    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        return self.public_url.rstrip("/")


def load_settings() -> Settings:
    url = os.environ.get("NULLWAKE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("NULLWAKE_DATABASE_URL is not set (postgresql+psycopg://user:pass@host/db)")
    return Settings(
        database_url=url,
        public_url=os.environ.get("NULLWAKE_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        session_hours=int(os.environ.get("NULLWAKE_SESSION_HOURS", "8")),
        worker_poll_seconds=float(os.environ.get("NULLWAKE_WORKER_POLL_SECONDS", "5")),
        worker_batch_size=int(os.environ.get("NULLWAKE_WORKER_BATCH_SIZE", "10")),
        retention_days=int(os.environ.get("NULLWAKE_RETENTION_DAYS", "30")),
        query_timeout_seconds=int(os.environ.get("NULLWAKE_QUERY_TIMEOUT_SECONDS", "60")),
        client_ip_header=os.environ.get("NULLWAKE_CLIENT_IP_HEADER", ""),
        demo_mode=_bool(os.environ.get("NULLWAKE_DEMO_MODE")),
        demo_user=os.environ.get("NULLWAKE_DEMO_USER", ""),
        demo_password=os.environ.get("NULLWAKE_DEMO_PASSWORD", ""),
        log_level=os.environ.get("NULLWAKE_LOG_LEVEL", "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
