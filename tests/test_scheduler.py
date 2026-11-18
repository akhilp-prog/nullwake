"""Scheduler leases and the "is Nullwake still watching?" signals.

These cover the failure modes from the v0.1 review: a worker that dies mid-check, a check that runs longer than its
interval, a "run now" that arrives during a run, and a worker that stops without anyone being told.
"""
from datetime import timedelta

from sqlalchemy import select, update

from nullwake import engine
from nullwake.config import get_settings
from nullwake.db import session_factory
from nullwake.models import AlertDelivery, Monitor, WorkerHeartbeat, utcnow
from nullwake.monitors import Result
from nullwake.watchdog import monitoring_status, overdue_checks, stalled_deliveries
from nullwake.worker import claim_due, execute, heartbeat, renew_lease

from .test_engine import setup_monitor

LEASE = 300


def fresh_table(wh):
    wh.execute("CREATE TABLE nwtest.orders (loaded_at timestamptz)")
    wh.execute("INSERT INTO nwtest.orders VALUES (now())")


def expire_leases(session):
    session.execute(update(Monitor).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    session.commit()


def monitor(session):
    session.expire_all()
    return session.scalar(select(Monitor))


def test_worker_that_dies_mid_check_has_its_check_reclaimed(db, tmp_path, wh):
    fresh_table(wh)
    mon = setup_monitor(db, tmp_path)
    factory = session_factory()
    with factory() as a:
        assert [mid for mid, _ in claim_due(a, 10, "worker-a", LEASE)] == [mon.id]
    # worker-a crashes here: it never runs the check and never releases the lease.
    with factory() as b:
        assert claim_due(b, 10, "worker-b", LEASE) == []  # the lease is still live: no double run
        expire_leases(b)
        ((mid, due),) = claim_due(b, 10, "worker-b", LEASE)  # lease expired: the run is reclaimed, not lost
    assert monitor(db).lease_owner == "worker-b"
    assert execute(mid, get_settings(), worker_id="worker-b", claimed_due=due) == "pass"
    m = monitor(db)
    assert m.lease_owner is None and m.lease_expires_at is None
    assert m.next_run_at > utcnow() + timedelta(seconds=m.interval_seconds - 30)


def test_check_slower_than_its_interval_is_not_started_twice(db, tmp_path, wh):
    fresh_table(wh)
    mon = setup_monitor(db, tmp_path)
    factory = session_factory()
    with factory() as a:
        ((mid, due),) = claim_due(a, 10, "worker-a", LEASE)
    # The check is still running past its interval: next_run_at has not moved, so it looks due again ...
    db.execute(update(Monitor).values(next_run_at=utcnow() - timedelta(seconds=mon.interval_seconds)))
    db.commit()
    with factory() as b:
        assert claim_due(b, 10, "worker-b", LEASE) == []  # ... but the live lease keeps worker-b off it
    assert execute(mid, get_settings(), worker_id="worker-a", claimed_due=due) == "pass"
    with factory() as b:
        assert claim_due(b, 10, "worker-b", LEASE) == []  # finished: next run is a full interval away
    assert monitor(db).next_run_at > utcnow()


def test_run_now_during_a_run_is_kept(db, tmp_path, wh):
    fresh_table(wh)
    setup_monitor(db, tmp_path)
    factory = session_factory()
    with factory() as a:
        ((mid, due),) = claim_due(a, 10, "worker-a", LEASE)
    db.execute(update(Monitor).values(next_run_at=utcnow()))  # someone presses "Run now" mid-run
    db.commit()
    execute(mid, get_settings(), worker_id="worker-a", claimed_due=due)
    with factory() as b:
        assert len(claim_due(b, 10, "worker-b", LEASE)) == 1  # the request was not overwritten by the reschedule


def test_worker_that_lost_its_lease_does_not_reschedule(db, tmp_path, wh):
    fresh_table(wh)
    setup_monitor(db, tmp_path)
    factory = session_factory()
    with factory() as a:
        ((mid, due),) = claim_due(a, 10, "worker-a", LEASE)
        expire_leases(a)
    with factory() as b:
        claim_due(b, 10, "worker-b", LEASE)
    with factory() as a:
        assert renew_lease(a, mid, "worker-a", LEASE) is False  # a slow worker finds out before it starts
    before = monitor(db).next_run_at
    execute(mid, get_settings(), worker_id="worker-a", claimed_due=due)  # it ran anyway; the result still counts
    m = monitor(db)
    assert m.lease_owner == "worker-b" and m.next_run_at == before


def test_watchdog_reports_a_stopped_worker_and_overdue_checks(db, tmp_path):
    setup_monitor(db, tmp_path)
    settings = get_settings()
    status = monitoring_status(db, settings)
    assert status["ok"] is False and "no worker heartbeat" in status["problems"]
    heartbeat(db, "host:1", 0)
    db.execute(update(Monitor).values(next_run_at=utcnow() - timedelta(seconds=settings.watchdog_grace_seconds + 60)))
    db.commit()
    assert overdue_checks(db, settings.watchdog_grace_seconds) == 1
    assert monitoring_status(db, settings)["problems"] == ["checks overdue"]
    db.execute(update(Monitor).values(lease_owner="host:1", lease_expires_at=utcnow() + timedelta(minutes=5)))
    db.commit()
    assert overdue_checks(db, settings.watchdog_grace_seconds) == 0  # running under a live lease is not overdue
    db.execute(update(WorkerHeartbeat).values(heartbeat_at=utcnow() - timedelta(hours=1)))
    db.commit()
    assert "no worker heartbeat" in monitoring_status(db, settings)["problems"]


def test_watchdog_reports_stalled_alert_delivery(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    engine.record_result(db, mon, Result("fail", "stale"), 1, utcnow())
    db.commit()
    assert stalled_deliveries(db) == 0  # just written: not stalled yet
    db.execute(update(AlertDelivery).values(created_at=utcnow() - timedelta(minutes=20)))
    db.commit()
    assert stalled_deliveries(db) == 2


def test_watchdog_endpoint_is_503_without_a_worker_and_names_nothing(db, tmp_path):
    from fastapi.testclient import TestClient

    from nullwake.app import create_app
    setup_monitor(db, tmp_path)
    with TestClient(create_app()) as client:
        r = client.get("/watchdog")
        assert r.status_code == 503 and r.json()["problems"] == ["no worker heartbeat"]
        assert "orders" not in r.text and "wh" not in r.json()  # counts only: no check, dataset or channel names
        heartbeat(db, "host:1", 0)
        r = client.get("/watchdog")
        assert r.status_code == 200 and r.json()["ok"] is True


def test_stuck_worker_loop_exits_so_it_is_restarted():
    import subprocess
    import sys
    code = ("import time; from nullwake.worker import StallWatchdog\n"
            "w = StallWatchdog(0.4); w.start(); time.sleep(5); print('still alive')")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 70 and "still alive" not in proc.stdout


def test_worker_health_command_follows_this_hosts_heartbeat(db, capsys):
    import socket

    from nullwake.cli import main
    assert main(["worker-health"]) == 1
    heartbeat(db, f"{socket.gethostname()}:123", 0)
    assert main(["worker-health"]) == 0
    db.execute(update(WorkerHeartbeat).values(heartbeat_at=utcnow() - timedelta(hours=1)))
    db.commit()
    assert main(["worker-health"]) == 1
    assert "unhealthy" in capsys.readouterr().out
