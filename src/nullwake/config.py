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
    lease_seconds_override: int = 0
    deadman_url: str = ""
    watchdog_grace_seconds: int = 300
    access_log_path: str = "/var/log/nullwake/access.log"
    contact_name: str = ""
    contact_email: str = ""
    contact_linkedin: str = ""
    contact_github: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def lease_seconds(self) -> int:
        """How long a claimed run is protected. A check runs a few bounded queries, so 3x the query timeout + 60s."""
        return self.lease_seconds_override or max(120, 3 * self.query_timeout_seconds + 60)

    @property
    def stall_seconds(self) -> int:
        """A worker loop that makes no progress for this long exits so Docker restarts it."""
        return self.lease_seconds + 120

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
        lease_seconds_override=int(os.environ.get("NULLWAKE_WORKER_LEASE_SECONDS", "0")),
        deadman_url=os.environ.get("NULLWAKE_DEADMAN_URL", ""),
        watchdog_grace_seconds=int(os.environ.get("NULLWAKE_WATCHDOG_GRACE_SECONDS", "300")),
        access_log_path=os.environ.get("NULLWAKE_ACCESS_LOG", "/var/log/nullwake/access.log"),
        contact_name=os.environ.get("NULLWAKE_CONTACT_NAME", ""),
        contact_email=os.environ.get("NULLWAKE_CONTACT_EMAIL", ""),
        contact_linkedin=os.environ.get("NULLWAKE_CONTACT_LINKEDIN", ""),
        contact_github=os.environ.get("NULLWAKE_CONTACT_GITHUB", ""),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
