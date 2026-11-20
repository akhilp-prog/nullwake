"""Metadata store schema. Only metadata and check results live here; warehouse rows never do."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


ROLES = ("viewer", "editor", "admin")
SEVERITIES = ("warning", "critical")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(lazy="joined")


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Connection(Base):
    """A warehouse connection. `spec` holds non-secret settings and env-var *names* for secrets."""
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    type: Mapped[str] = mapped_column(String(20))
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    owner: Mapped[str] = mapped_column(String(254), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    connection_name: Mapped[str] = mapped_column(String(80))
    dataset: Mapped[str] = mapped_column(String(255))
    source_yaml: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monitors: Mapped[list["Monitor"]] = relationship(back_populates="contract", order_by="Monitor.name")

    @property
    def active_monitors(self) -> list["Monitor"]:
        return [m for m in self.monitors if m.retired_at is None]


class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id", ondelete="CASCADE"), index=True)
    connection_name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    dataset: Mapped[str] = mapped_column(String(255))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    severity: Mapped[str] = mapped_column(String(10), default="critical")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(10))
    last_message: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Lease held by the worker that is running this check. A crashed worker's lease expires and the check is reclaimed.
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fencing token for the current claim: a result is accepted only if it carries the token that is still on the row.
    lease_token: Mapped[str | None] = mapped_column(String(36))
    # Bumped by "Run now". A run that finishes with a higher count than it was claimed with is rerun at once.
    run_requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Set when the check is removed from its contract. Retired checks keep their runs, incidents and queued alerts.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contract: Mapped[Contract | None] = relationship(back_populates="monitors")

    __table_args__ = (Index("ix_monitors_due", "enabled", "next_run_at"),)


class CheckRun(Base):
    __tablename__ = "check_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))
    value: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text, default="")
    observed: Mapped[dict] = mapped_column(JSONB, default=dict)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")

    __table_args__ = (Index("ix_check_runs_monitor_started", "monitor_id", "started_at"),)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(14), default="open")
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    failure_count: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(254))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(254))
    monitor: Mapped[Monitor] = relationship(lazy="joined")
    events: Mapped[list["IncidentEvent"]] = relationship(order_by="IncidentEvent.id", back_populates="incident")

    __table_args__ = (
        # At most one unresolved incident per monitor, enforced by the database.
        Index("uq_incident_active_monitor", "monitor_id", unique=True,
              postgresql_where="status <> 'resolved'"),
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(254))
    kind: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(Text, default="")
    incident: Mapped[Incident] = relationship(back_populates="events")


class AlertChannel(Base):
    __tablename__ = "alert_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    type: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    min_severity: Mapped[str] = mapped_column(String(10), default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertDelivery(Base):
    """Transactional outbox: written in the same transaction as the incident change."""
    __tablename__ = "alert_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("alert_channels.id", ondelete="CASCADE"))
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    channel: Mapped[AlertChannel] = relationship(lazy="joined")

    __table_args__ = (Index("ix_alert_deliveries_due", "status", "next_attempt_at"),)


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checks_run: Mapped[int] = mapped_column(Integer, default=0)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
