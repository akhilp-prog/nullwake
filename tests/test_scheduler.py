"""Scheduler leases, fencing and the "is Nullwake still watching?" signals.

These cover the failure modes from the reviews of v0.1 and v0.2: a worker that dies mid-check, a check slower than its
interval, a late result from a worker that lost its claim (it must not resolve a newer incident), a check whose
definition changes mid-run, "Run now" during a run, and monitoring that stops without anyone being told.
"""
import os
from datetime import timedelta

import httpx
from sqlalchemy import func, select, update

from nullwake import engine
from nullwake.config import get_settings
from nullwake.db import session_factory
from nullwake.models import AlertDelivery, CheckRun, Incident, Monitor, WorkerHeartbeat, utcnow
from nullwake.monitors import Result
from nullwake.watchdog import connections_not_executing, monitoring_status, overdue_checks, stalled_deliveries
from nullwake.worker import claim_due, execute, heartbeat, ping_deadman, renew_lease

from nullwake.contracts import load_bundle

from .conftest import CONNECTION_YAML
from .test_engine import bundle, setup_monitor

LEASE = 300


def table(wh, fresh: bool):
    wh.execute("DROP TABLE IF EXISTS nwtest.orders; CREATE TABLE nwtest.orders (loaded_at timestamptz)")
    wh.execute("INSERT INTO nwtest.orders VALUES (now() - interval '%s')" % ("1 minute" if fresh else "3 hours"))


def expire_leases(session):
    session.execute(update(Monitor).values(lease_expires_at=func.now() - timedelta(seconds=1)))
    session.commit()


def monitor(session):
    session.expire_all()
    return session.scalar(select(Monitor))


def claim(worker):
    with session_factory()() as s:
        return claim_due(s, 10, worker, LEASE)


def test_worker_that_dies_mid_check_has_its_check_reclaimed(db, tmp_path, wh):
    table(wh, fresh=True)
    mon = setup_monitor(db, tmp_path)
    assert [c.monitor_id for c in claim("worker-a")] == [mon.id]
    # worker-a crashes here: it never runs the check and never releases the lease.
    assert claim("worker-b") == []  # the lease is still live: no second run
    expire_leases(db)
    (c,) = claim("worker-b")  # lease expired: the run is reclaimed, not lost
    assert monitor(db).lease_owner == "worker-b"
    assert execute(c.monitor_id, get_settings(), claim=c) == "pass"
    m = monitor(db)
    assert m.lease_owner is None and m.lease_token is None and m.lease_expires_at is None
    assert m.next_run_at > utcnow() + timedelta(seconds=m.interval_seconds - 30)


def test_check_slower_than_its_interval_is_not_started_twice(db, tmp_path, wh):
    table(wh, fresh=True)
    mon = setup_monitor(db, tmp_path)
    (c,) = claim("worker-a")
    # Still running past its interval: next_run_at has not moved, so it looks due again ...
    db.execute(update(Monitor).values(next_run_at=func.now() - timedelta(seconds=mon.interval_seconds)))
    db.commit()
    assert claim("worker-b") == []  # ... but the live lease keeps worker-b off it
    assert execute(c.monitor_id, get_settings(), claim=c) == "pass"
    assert claim("worker-b") == []  # finished: the next run is a full interval away


def test_late_result_from_a_superseded_claim_cannot_resolve_a_newer_incident(db, tmp_path, wh):
    """The v0.2 review's reproduction: A starts, A's lease expires, B records a failure and opens an incident, then
    A returns an older pass. A's result must be discarded: no resolution, no 'resolved' alert."""
    mon = setup_monitor(db, tmp_path)
    (a,) = claim("worker-a")
    expire_leases(db)
    (b,) = claim("worker-b")
    table(wh, fresh=False)
    assert execute(mon.id, get_settings(), claim=b) == "fail"
    (incident,) = db.scalars(select(Incident)).unique().all()
    sent_before = sorted(d.event for d in db.scalars(select(AlertDelivery)))
    table(wh, fresh=True)  # A's view of the world: it "saw" fresh data
    assert execute(mon.id, get_settings(), claim=a) == "stale"
    db.expire_all()
    assert db.get(Incident, incident.id).status == "open"
    assert sorted(d.event for d in db.scalars(select(AlertDelivery))) == sent_before  # no false recovery alert
    assert db.scalar(select(func.count()).select_from(CheckRun)) == 1  # A's run left no trace in history either
    assert monitor(db).last_status == "fail"


def test_result_of_a_changed_check_definition_is_discarded(db, tmp_path, wh):
    table(wh, fresh=True)
    mon = setup_monitor(db, tmp_path)
    (c,) = claim("worker-a")
    engine.apply_bundle(db, bundle(tmp_path, "freshness: {column: loaded_at, maxAgeMinutes: 5}"), "editor")
    db.commit()  # the definition changed while worker-a was running the old one
    assert execute(mon.id, get_settings(), claim=c) == "stale"
    assert db.scalar(select(func.count()).select_from(CheckRun)) == 0


def test_result_from_before_a_same_name_connection_change_is_discarded(db, tmp_path, wh, monkeypatch):
    table(wh, fresh=True)
    mon = setup_monitor(db, tmp_path)
    (c,) = claim("worker-a")
    monkeypatch.setenv("NEW_WAREHOUSE_DSN", os.environ["TEST_WAREHOUSE_DSN"])
    moved = tmp_path / "moved.yaml"
    moved.write_text(CONNECTION_YAML.replace("TEST_WAREHOUSE_DSN", "NEW_WAREHOUSE_DSN"))
    engine.apply_bundle(db, load_bundle([moved]), "editor")  # connection "wh" now points somewhere else
    db.commit()
    assert execute(mon.id, get_settings(), claim=c) == "stale"
    assert db.scalar(select(func.count()).select_from(CheckRun)) == 0
    assert len(claim("worker-b")) == 1  # rerun right away against the new settings


def test_renewal_needs_the_same_claim_and_a_live_lease(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    (c,) = claim("worker-a")
    assert renew_lease(db, mon.id, c.token, LEASE) is True
    assert renew_lease(db, mon.id, "not-the-token", LEASE) is False
    expire_leases(db)
    assert renew_lease(db, mon.id, c.token, LEASE) is False  # an expired lease is not revived, even if unclaimed


def test_run_now_during_a_run_is_kept_and_repeated_clicks_coalesce(db, tmp_path, wh):
    table(wh, fresh=True)
    mon = setup_monitor(db, tmp_path)
    (c,) = claim("worker-a")
    for _ in range(3):  # three clicks of "Run now" while the check runs
        engine.request_run(db.get(Monitor, mon.id))
        db.commit()
    assert claim("worker-b") == []  # still running under worker-a's lease
    execute(mon.id, get_settings(), claim=c)
    assert len(claim("worker-b")) == 1  # exactly one follow-up run, right away
    assert claim("worker-c") == []


def test_watchdog_reports_a_stopped_worker_and_overdue_checks(db, tmp_path):
    setup_monitor(db, tmp_path)
    settings = get_settings()
    status = monitoring_status(db, settings)
    assert status["ok"] is False and "no worker heartbeat" in status["problems"]
    heartbeat(db, "host:1", 0)
    db.execute(update(Monitor).values(next_run_at=func.now() - timedelta(seconds=settings.watchdog_grace_seconds + 60)))
    db.commit()
    assert overdue_checks(db, settings.watchdog_grace_seconds) == 1
    assert monitoring_status(db, settings)["problems"] == ["checks overdue"]
    db.execute(update(Monitor).values(lease_owner="host:1", lease_expires_at=func.now() + timedelta(minutes=5)))
    db.commit()
    assert overdue_checks(db, settings.watchdog_grace_seconds) == 0  # running under a live lease is not overdue
    db.execute(update(WorkerHeartbeat).values(heartbeat_at=func.now() - timedelta(hours=1)))
    db.commit()
    assert "no worker heartbeat" in monitoring_status(db, settings)["problems"]


def test_watchdog_reports_checks_that_cannot_execute(db, tmp_path):
    """A live worker loop that cannot reach the warehouse is not healthy, even though it heartbeats and reschedules."""
    setup_monitor(db, tmp_path)
    heartbeat(db, "host:1", 0)
    db.execute(update(Monitor).values(last_status="error"))
    db.commit()
    assert connections_not_executing(db) == 1
    assert monitoring_status(db, get_settings())["problems"] == ["checks cannot execute"]
    db.execute(update(Monitor).values(last_status="fail"))  # failing data is an incident, not a monitoring outage
    db.commit()
    assert monitoring_status(db, get_settings())["ok"] is True


def test_watchdog_reports_when_no_checks_complete(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    heartbeat(db, "host:1", 0)
    settings = get_settings()
    gap = mon.interval_seconds + settings.watchdog_grace_seconds + settings.lease_seconds + 60
    db.add(CheckRun(monitor_id=mon.id, started_at=utcnow() - timedelta(seconds=gap), duration_ms=1, status="pass"))
    db.commit()
    assert "no checks completing" in monitoring_status(db, settings)["problems"]


def test_watchdog_reports_stalled_alert_delivery(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    engine.record_result(db, mon, Result("fail", "stale"), 1, utcnow())
    db.commit()
    assert stalled_deliveries(db) == 0  # just written: not stalled yet
    db.execute(update(AlertDelivery).values(created_at=func.now() - timedelta(minutes=20)))
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


def test_dead_mans_switch_pings_only_while_healthy():
    hits = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: hits.append(req) or httpx.Response(200)))
    state: dict = {}
    assert ping_deadman(client, "https://hc.example/ping", state, healthy=False) is False
    assert ping_deadman(client, "https://hc.example/ping", state, healthy=True) is True
    assert ping_deadman(client, "https://hc.example/ping", state, healthy=True) is False  # at most once a minute
    assert len(hits) == 1


def test_dead_mans_switch_pings_right_after_boot(monkeypatch):
    """The monotonic clock can be under 60 s on a freshly booted host; the first ping must still go out."""
    import nullwake.worker as worker
    monkeypatch.setattr(worker.time, "monotonic", lambda: 5.0)
    hits = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: hits.append(req) or httpx.Response(200)))
    assert ping_deadman(client, "https://hc.example/ping", {}, healthy=True) is True
    assert len(hits) == 1


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
    db.execute(update(WorkerHeartbeat).values(heartbeat_at=func.now() - timedelta(hours=1)))
    db.commit()
    assert main(["worker-health"]) == 1
    assert "unhealthy" in capsys.readouterr().out
