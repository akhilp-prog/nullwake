"""Scheduler/worker. Run one or more replicas.

A due check is claimed by taking an expiring lease on it (FOR UPDATE SKIP LOCKED keeps two workers from claiming the
same row at the same moment). next_run_at only moves when the run finishes, so:

* a check is never started while another worker holds a live lease on it, so runs do not overlap, even when a run
  takes longer than the check's interval;
* if a worker dies mid-run, its lease expires and another worker reclaims the check, so the run is delayed, not lost.

The lease is renewed right before each check in a batch starts. A run that outlives its lease (it should not: every
warehouse query has a statement timeout, and the lease is several timeouts long) can overlap with a reclaimed run;
the database's one-active-incident rule still keeps that from opening duplicate incidents.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .alerts import deliver_due
from .config import Settings
from .db import session_factory
from .engine import prune_history, record_result, run_check
from .models import Monitor, WorkerHeartbeat, utcnow

log = logging.getLogger("nullwake.worker")


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_due(session, batch_size: int, worker_id: str = "", lease_seconds: int = 300) -> list[tuple[int, datetime]]:
    """Lease up to batch_size due checks that no live lease protects. Returns (id, next_run_at as claimed)."""
    now = utcnow()
    rows = session.scalars(
        select(Monitor)
        .where(Monitor.enabled.is_(True), Monitor.next_run_at <= now,
               or_(Monitor.lease_expires_at.is_(None), Monitor.lease_expires_at <= now))
        .order_by(Monitor.next_run_at).limit(batch_size).with_for_update(skip_locked=True)
    ).all()
    claimed = []
    for m in rows:
        if m.lease_owner:
            log.warning("reclaiming check from an expired lease",
                        extra={"check": m.name, "previous_owner": m.lease_owner})
        m.lease_owner, m.lease_expires_at = worker_id or "anonymous", now + timedelta(seconds=lease_seconds)
        claimed.append((m.id, m.next_run_at))
    session.commit()
    return claimed


def renew_lease(session, monitor_id: int, worker_id: str, lease_seconds: int) -> bool:
    """Extend our lease right before running. False if the lease was lost (expired and reclaimed)."""
    result = session.execute(
        update(Monitor).where(Monitor.id == monitor_id, Monitor.lease_owner == worker_id)
        .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds)))
    session.commit()
    return result.rowcount == 1


def execute(monitor_id: int, settings: Settings, trigger: str = "schedule", worker_id: str | None = None,
            claimed_due: datetime | None = None) -> str | None:
    """Run one check and record the result. With worker_id, also finish the scheduled run: release the lease and set
    the next run from now, unless someone asked for a run while this one was going ("run now"), which is kept."""
    factory = session_factory()
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        result, duration, started = run_check(session, monitor, settings.query_timeout_seconds)
        session.rollback()  # do not hold a metadata transaction open across warehouse I/O
        monitor = session.get(Monitor, monitor_id, with_for_update=True)
        if monitor is None:
            return None
        record_result(session, monitor, result, duration, started, trigger, settings.public_url)
        if worker_id is not None and monitor.lease_owner == worker_id:
            now = utcnow()
            requested_meanwhile = claimed_due is not None and monitor.next_run_at > claimed_due
            if not requested_meanwhile:
                monitor.next_run_at = now + timedelta(seconds=monitor.interval_seconds)
            monitor.lease_owner, monitor.lease_expires_at = None, None
        elif worker_id is not None:
            log.warning("lease lost before the run finished; result recorded, schedule left to the new owner",
                        extra={"check": monitor.name})
        session.commit()
        log.info("check finished", extra={"check": monitor.name, "status": result.status, "duration_ms": duration})
        return result.status


def heartbeat(session, worker_id: str, ran: int) -> None:
    now = utcnow()
    session.execute(insert(WorkerHeartbeat).values(worker_id=worker_id, started_at=now, heartbeat_at=now, checks_run=ran)
                    .on_conflict_do_update(index_elements=["worker_id"],
                                           set_={"heartbeat_at": now, "checks_run": WorkerHeartbeat.checks_run + ran}))
    session.commit()


class StallWatchdog:
    """Exits the process if the worker loop stops making progress, so the container's restart policy brings it back.
    A hung warehouse driver or a deadlocked loop would otherwise leave a live process that monitors nothing."""

    def __init__(self, stall_seconds: float):
        self.stall_seconds = stall_seconds
        self.last_progress = time.monotonic()
        self._stop = threading.Event()

    def progress(self) -> None:
        self.last_progress = time.monotonic()

    def start(self) -> None:
        threading.Thread(target=self._run, name="stall-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(min(15.0, self.stall_seconds / 4)):
            stalled = time.monotonic() - self.last_progress
            if stalled > self.stall_seconds:
                log.critical("worker made no progress; exiting so it is restarted",
                             extra={"stalled_seconds": round(stalled)})
                logging.shutdown()
                os._exit(70)


def ping_deadman(client: httpx.Client, url: str, state: dict) -> None:
    """Outbound dead man's switch: ping an external service (e.g. healthchecks.io) at most once a minute. If the pings
    stop, that service alerts through its own channel, independent of Nullwake's alerting."""
    if not url or time.monotonic() - state.get("last", 0.0) < 60:
        return
    state["last"] = time.monotonic()
    try:
        client.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        log.warning("dead man's switch ping failed", extra={"error": exc.__class__.__name__})


def run_forever(settings: Settings, once: bool = False) -> None:
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        log.info("shutdown requested; finishing current work")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker_id = worker_identity()
    factory = session_factory()
    last_prune = 0.0
    ping_state: dict = {}
    watchdog = StallWatchdog(settings.stall_seconds)
    if not once:
        watchdog.start()
    log.info("worker started", extra={"worker": worker_id, "lease_seconds": settings.lease_seconds})
    with httpx.Client(timeout=10.0) as client:
        while not stopping:
            ran = 0
            try:
                with factory() as session:
                    claimed = claim_due(session, settings.worker_batch_size, worker_id, settings.lease_seconds)
                    heartbeat(session, worker_id, 0)
                watchdog.progress()
                for mid, due in claimed:
                    if stopping:
                        break  # unstarted claims keep their lease until it expires, then another worker takes them
                    with factory() as session:
                        if not renew_lease(session, mid, worker_id, settings.lease_seconds):
                            continue
                    execute(mid, settings, worker_id=worker_id, claimed_due=due)
                    ran += 1
                    with factory() as session:
                        heartbeat(session, worker_id, 1)
                    watchdog.progress()
                if stopping and claimed:
                    with factory() as session:  # hand back what we did not start, so it runs promptly elsewhere
                        session.execute(update(Monitor).where(Monitor.lease_owner == worker_id)
                                        .values(lease_owner=None, lease_expires_at=None))
                        session.commit()
                with factory() as session:
                    deliver_due(session, client)
                    session.commit()
                    if time.monotonic() - last_prune > 3600:
                        removed = prune_history(session, settings.retention_days)
                        session.execute(delete(WorkerHeartbeat).where(
                            WorkerHeartbeat.heartbeat_at < utcnow() - timedelta(days=1)))
                        session.commit()
                        last_prune = time.monotonic()
                        if removed:
                            log.info("pruned old check runs", extra={"removed": removed})
                ping_deadman(client, settings.deadman_url, ping_state)
                watchdog.progress()
            except Exception:
                log.exception("worker loop error; retrying")
                time.sleep(min(settings.worker_poll_seconds * 2, 30))
            if once:
                break
            if ran < settings.worker_batch_size and not stopping:
                time.sleep(settings.worker_poll_seconds)
    watchdog.stop()
    with factory() as session:
        session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
        session.commit()
    log.info("worker stopped")

