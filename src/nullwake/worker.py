"""Scheduler/worker. Run one or more replicas; due checks are claimed with FOR UPDATE SKIP LOCKED."""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from datetime import timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from .alerts import deliver_due
from .config import Settings
from .db import session_factory
from .engine import prune_history, record_result, run_check
from .models import Monitor, WorkerHeartbeat, utcnow

log = logging.getLogger("nullwake.worker")


def claim_due(session, batch_size: int) -> list[int]:
    """Atomically claim due checks by pushing next_run_at forward; returns the claimed ids."""
    now = utcnow()
    rows = session.scalars(
        select(Monitor).where(Monitor.enabled.is_(True), Monitor.next_run_at <= now)
        .order_by(Monitor.next_run_at).limit(batch_size).with_for_update(skip_locked=True)
    ).all()
    for m in rows:
        m.next_run_at = now + timedelta(seconds=m.interval_seconds)
    session.commit()
    return [m.id for m in rows]


def execute(monitor_id: int, settings: Settings, trigger: str = "schedule") -> str | None:
    factory = session_factory()
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        result, duration, started = run_check(session, monitor, settings.query_timeout_seconds)
        session.rollback()  # do not hold a metadata transaction open across warehouse I/O
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        record_result(session, monitor, result, duration, started, trigger, settings.public_url)
        session.commit()
        log.info("check finished", extra={"check": monitor.name, "status": result.status, "duration_ms": duration})
        return result.status


def heartbeat(session, worker_id: str, ran: int) -> None:
    now = utcnow()
    session.execute(insert(WorkerHeartbeat).values(worker_id=worker_id, started_at=now, heartbeat_at=now, checks_run=ran)
                    .on_conflict_do_update(index_elements=["worker_id"],
                                           set_={"heartbeat_at": now, "checks_run": WorkerHeartbeat.checks_run + ran}))
    session.commit()


def run_forever(settings: Settings, once: bool = False) -> None:
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        log.info("shutdown requested; finishing current work")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    factory = session_factory()
    last_prune = 0.0
    log.info("worker started", extra={"worker": worker_id})
    with httpx.Client(timeout=10.0) as client:
        while not stopping:
            ran = 0
            try:
                with factory() as session:
                    ids = claim_due(session, settings.worker_batch_size)
                for mid in ids:
                    if stopping:
                        break
                    execute(mid, settings)
                    ran += 1
                with factory() as session:
                    deliver_due(session, client)
                    session.commit()
                    heartbeat(session, worker_id, ran)
                    if time.monotonic() - last_prune > 3600:
                        removed = prune_history(session, settings.retention_days)
                        session.execute(delete(WorkerHeartbeat).where(
                            WorkerHeartbeat.heartbeat_at < utcnow() - timedelta(days=1)))
                        session.commit()
                        last_prune = time.monotonic()
                        if removed:
                            log.info("pruned old check runs", extra={"removed": removed})
            except Exception:
                log.exception("worker loop error; retrying")
                time.sleep(min(settings.worker_poll_seconds * 2, 30))
            if once:
                break
            if ran < settings.worker_batch_size and not stopping:
                time.sleep(settings.worker_poll_seconds)
    with factory() as session:
        session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
        session.commit()
    log.info("worker stopped")
