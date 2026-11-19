"""Scheduler/worker. Run one or more replicas.

A due check is claimed by writing a lease on its row: an owner, an expiry and a fresh random **token**
(FOR UPDATE SKIP LOCKED keeps two workers from claiming the same row at the same moment). Then:

* the check runs with no metadata transaction open; a background thread renews the lease while it runs;
* the result is accepted only if, under the row lock, the row still carries this claim's token. A worker whose lease
  expired and was reclaimed, or whose check definition was changed while it ran, gets its result discarded before it
  can touch the check's state, incidents or the alert outbox (fencing);
* next_run_at moves only when an accepted run finishes, so a crashed worker's check is reclaimed when its lease
  expires instead of being skipped, and a check is not started again while a live lease protects it.

All lease and schedule times are taken from the database clock (now()), never from a worker's own clock.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .alerts import deliver_due
from .config import Settings
from .db import session_factory
from .engine import load_check, prune_history, record_result, run_spec
from .models import Monitor, WorkerHeartbeat
from .watchdog import monitoring_status

log = logging.getLogger("nullwake.worker")


@dataclass(frozen=True)
class Claim:
    monitor_id: int
    token: str
    run_requests: int


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _lease_until(lease_seconds: int):
    return func.now() + timedelta(seconds=lease_seconds)


def claim_due(session, batch_size: int, worker_id: str = "", lease_seconds: int = 300) -> list[Claim]:
    """Lease up to batch_size due checks that no live lease protects, each with a fresh token."""
    rows = session.scalars(
        select(Monitor)
        .where(Monitor.enabled.is_(True), Monitor.next_run_at <= func.now(),
               or_(Monitor.lease_expires_at.is_(None), Monitor.lease_expires_at <= func.now()))
        .order_by(Monitor.next_run_at).limit(batch_size).with_for_update(skip_locked=True)
    ).all()
    claims = []
    for m in rows:
        if m.lease_owner:
            log.warning("reclaiming check from an expired lease",
                        extra={"check": m.name, "previous_owner": m.lease_owner})
        token = str(uuid.uuid4())
        session.execute(update(Monitor).where(Monitor.id == m.id).values(
            lease_owner=worker_id or "anonymous", lease_token=token, lease_expires_at=_lease_until(lease_seconds)))
        claims.append(Claim(m.id, token, m.run_requests or 0))
    session.commit()
    return claims


def renew_lease(session, monitor_id: int, token: str, lease_seconds: int) -> bool:
    """Extend a claim that is still ours and still live. False if it expired or was taken over."""
    result = session.execute(
        update(Monitor).where(Monitor.id == monitor_id, Monitor.lease_token == token,
                              Monitor.lease_expires_at > func.now())
        .values(lease_expires_at=_lease_until(lease_seconds)))
    session.commit()
    return result.rowcount == 1


class _LeaseKeeper:
    """Renews a claim every lease/3 seconds while a check runs, for at most two lease lengths. Renewal narrows the
    window for overlap; it is the token check at completion that makes a late result harmless."""

    def __init__(self, monitor_id: int, token: str, lease_seconds: int):
        self.args = (monitor_id, token, lease_seconds)
        self.deadline = time.monotonic() + 2 * lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-keeper", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        monitor_id, token, lease_seconds = self.args
        while not self._stop.wait(max(5.0, lease_seconds / 3)) and time.monotonic() < self.deadline:
            try:
                with session_factory()() as session:
                    if not renew_lease(session, monitor_id, token, lease_seconds):
                        log.warning("lease lost while the check was running", extra={"check_id": monitor_id})
                        return
            except Exception:
                log.exception("lease renewal failed")


def execute(monitor_id: int, settings: Settings, trigger: str = "schedule", claim: Claim | None = None) -> str | None:
    """Run one check and record the result. With a claim, the result is fenced by the claim's token, then the lease is
    released and the next run scheduled ("Run now" requests made during the run trigger an immediate rerun)."""
    factory = session_factory()
    with factory() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        spec = load_check(session, monitor)
    # The metadata session is closed here: nothing is held open while the warehouse query runs.
    if claim is not None:
        with _LeaseKeeper(monitor_id, claim.token, settings.lease_seconds):
            result, duration, started = run_spec(spec, settings.query_timeout_seconds)
    else:
        result, duration, started = run_spec(spec, settings.query_timeout_seconds)
    with factory() as session:
        monitor = session.get(Monitor, monitor_id, with_for_update=True)
        if monitor is None:
            return None
        if claim is not None and monitor.lease_token != claim.token:
            session.rollback()
            log.warning("stale result discarded: the claim was taken over or the check changed while it ran",
                        extra={"check": monitor.name, "status": result.status})
            return "stale"
        record_result(session, monitor, result, duration, started, trigger, settings.public_url)
        if claim is not None:
            rerun = (monitor.run_requests or 0) > claim.run_requests
            monitor.next_run_at = func.now() if rerun else func.now() + timedelta(seconds=monitor.interval_seconds)
            monitor.lease_owner, monitor.lease_token, monitor.lease_expires_at = None, None, None
        session.commit()
        log.info("check finished", extra={"check": monitor.name, "status": result.status, "duration_ms": duration})
        return result.status


def heartbeat(session, worker_id: str, ran: int) -> None:
    session.execute(insert(WorkerHeartbeat)
                    .values(worker_id=worker_id, started_at=func.now(), heartbeat_at=func.now(), checks_run=ran)
                    .on_conflict_do_update(index_elements=["worker_id"],
                                           set_={"heartbeat_at": func.now(),
                                                 "checks_run": WorkerHeartbeat.checks_run + ran}))
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


def ping_deadman(client: httpx.Client, url: str, state: dict, healthy: bool) -> bool:
    """Outbound dead man's switch, at most once a minute, and only while the whole deployment is healthy (the same
    test as /watchdog: workers alive, nothing overdue, checks executing, alerts flowing). When it is not, the pings
    stop and the external service alerts through its own channel. Returns True if a ping was sent."""
    now = time.monotonic()
    last = state.get("last")  # None until the first ping; monotonic time can be < 60 s just after boot
    if not url or not healthy or (last is not None and now - last < 60):
        return False
    state["last"] = now
    try:
        client.get(url, timeout=5.0)
        return True
    except httpx.HTTPError as exc:
        log.warning("dead man's switch ping failed", extra={"error": exc.__class__.__name__})
        return False


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
                    claims = claim_due(session, settings.worker_batch_size, worker_id, settings.lease_seconds)
                    heartbeat(session, worker_id, 0)
                watchdog.progress()
                for claim in claims:
                    if stopping:
                        break
                    with factory() as session:
                        if not renew_lease(session, claim.monitor_id, claim.token, settings.lease_seconds):
                            continue
                    execute(claim.monitor_id, settings, claim=claim)
                    ran += 1
                    with factory() as session:
                        heartbeat(session, worker_id, 1)
                    watchdog.progress()
                if stopping and claims:
                    with factory() as session:  # hand back what we did not start, so it runs promptly elsewhere
                        session.execute(update(Monitor).where(Monitor.lease_owner == worker_id)
                                        .values(lease_owner=None, lease_token=None, lease_expires_at=None))
                        session.commit()
                with factory() as session:
                    deliver_due(session, client)
                    session.commit()
                    if time.monotonic() - last_prune > 3600:
                        removed = prune_history(session, settings.retention_days)
                        session.execute(delete(WorkerHeartbeat).where(
                            WorkerHeartbeat.heartbeat_at < func.now() - timedelta(days=1)))
                        session.commit()
                        last_prune = time.monotonic()
                        if removed:
                            log.info("pruned old check runs", extra={"removed": removed})
                    healthy = (not settings.deadman_url) or monitoring_status(session, settings)["ok"]
                ping_deadman(client, settings.deadman_url, ping_state, healthy)
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
