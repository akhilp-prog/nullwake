"""Config apply, incident lifecycle, outbox, worker claiming and alert delivery."""
import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from nullwake import engine
from nullwake.alerts import MAX_ATTEMPTS, deliver_due, sign
from nullwake.config import get_settings
from nullwake.contracts import load_bundle
from nullwake.db import session_factory
from nullwake.models import AlertDelivery, AuditEntry, CheckRun, Contract, Incident, IncidentEvent, Monitor, utcnow
from nullwake.monitors import Result
from nullwake.worker import claim_due, execute

from .conftest import CONNECTION_YAML, contract_yaml

CHANNELS = """
apiVersion: nullwake/v1
kind: AlertChannel
metadata: {name: oncall}
spec: {type: webhook, urlEnv: TEST_HOOK_URL, signingSecretEnv: TEST_HOOK_SECRET, minSeverity: critical}
---
apiVersion: nullwake/v1
kind: AlertChannel
metadata: {name: team-slack}
spec: {type: slack, urlEnv: TEST_SLACK_URL}
"""


def bundle(tmp_path, body="freshness: {column: loaded_at, maxAgeMinutes: 30}", extra=""):
    path = tmp_path / "b.yaml"
    path.write_text(CONNECTION_YAML + "---" + CHANNELS + "---" + contract_yaml(body) + extra)
    return load_bundle([path])


def test_apply_is_idempotent_and_versioned(db, tmp_path):
    first = engine.apply_bundle(db, bundle(tmp_path), "alice")
    assert "contract/orders" in first.created and "connection/wh" in first.created
    db.commit()
    again = engine.apply_bundle(db, bundle(tmp_path), "alice")
    assert "contract/orders" in again.unchanged and not again.created
    changed = bundle(tmp_path, "freshness: {column: loaded_at, maxAgeMinutes: 10}\nchecks: [{name: c1, sql: 'select 0'}]")
    summary = engine.apply_bundle(db, changed, "bob")
    db.commit()
    assert "contract/orders" in summary.updated
    contract = db.scalar(select(Contract))
    assert contract.version == 2
    assert {m.name for m in contract.monitors} == {"orders.freshness", "orders.check.c1"}
    removed = engine.apply_bundle(db, bundle(tmp_path), "bob")
    assert "check/orders.check.c1" in removed.deleted
    assert db.scalar(select(AuditEntry).where(AuditEntry.actor == "bob")).action == "config.apply"


def setup_monitor(db, tmp_path):
    engine.apply_bundle(db, bundle(tmp_path), "t")
    db.commit()
    return db.scalar(select(Monitor))


def test_incident_lifecycle_with_dedupe_and_outbox(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    now = utcnow()
    engine.record_result(db, mon, Result("fail", "Data is 2 h old"), 10, now, public_url="https://nw.example")
    engine.record_result(db, mon, Result("fail", "Data is 2 h old"), 10, now)
    engine.record_result(db, mon, Result("fail", "Data is 3 h old"), 10, now)
    db.commit()
    (incident,) = db.scalars(select(Incident)).unique().all()
    assert incident.status == "open" and incident.failure_count == 3
    kinds = [e.kind for e in incident.events]
    assert kinds == ["opened", "changed"]
    deliveries = db.scalars(select(AlertDelivery)).all()
    assert sorted(d.channel.name for d in deliveries) == ["oncall", "team-slack"]  # critical reaches both
    payload = deliveries[0].payload["incident"]
    assert payload["url"] == f"https://nw.example/incidents/{incident.id}" and payload["owner"] == "team@example.com"
    assert engine.acknowledge(db, incident, "alice", "looking") is True
    assert engine.acknowledge(db, incident, "alice") is False
    engine.record_result(db, mon, Result("pass", "fresh again"), 10, now)
    db.commit()
    db.refresh(incident)
    assert incident.status == "resolved" and incident.resolved_by == "nullwake"
    assert [d.event for d in db.scalars(select(AlertDelivery).order_by(AlertDelivery.id))].count("resolved") == 2
    engine.record_result(db, mon, Result("fail", "stale"), 10, now)
    db.commit()
    assert len(db.scalars(select(Incident)).unique().all()) == 2  # a new failure opens a new incident


def test_warning_respects_channel_min_severity(db, tmp_path):
    engine.apply_bundle(db, bundle(tmp_path, "volume: {column: loaded_at}"), "t")
    db.commit()
    mon = db.scalar(select(Monitor))
    assert mon.severity == "warning"
    engine.record_result(db, mon, Result("fail", "low volume"), 1, utcnow())
    db.commit()
    assert [d.channel.name for d in db.scalars(select(AlertDelivery))] == ["team-slack"]


def test_only_one_active_incident_per_check_is_enforced_by_db(db, tmp_path):
    mon = setup_monitor(db, tmp_path)
    db.add(Incident(monitor_id=mon.id, severity="critical", title="a"))
    db.commit()
    db.add(Incident(monitor_id=mon.id, severity="critical", title="b"))
    with pytest.raises(Exception, match="uq_incident_active_monitor"):
        db.commit()


def test_claim_due_skips_rows_locked_by_another_worker(db, tmp_path):
    setup_monitor(db, tmp_path)
    factory = session_factory()
    holder = factory()
    locked = holder.scalars(select(Monitor).with_for_update()).all()
    assert len(locked) == 1
    with factory() as other:
        assert claim_due(other, 10) == []  # SKIP LOCKED: no waiting, no double run
    holder.rollback()
    holder.close()
    with factory() as other:
        assert len(claim_due(other, 10)) == 1
    with factory() as other:
        assert claim_due(other, 10) == []  # the first claim holds a live lease


def test_execute_runs_against_warehouse_and_records(db, wh, tmp_path):
    wh.execute("CREATE TABLE nwtest.orders (loaded_at timestamptz)")
    wh.execute("INSERT INTO nwtest.orders VALUES (now() - interval '2 hours')")
    mon = setup_monitor(db, tmp_path)
    assert execute(mon.id, get_settings(), "manual") == "fail"
    wh.execute("INSERT INTO nwtest.orders VALUES (now())")
    assert execute(mon.id, get_settings()) == "pass"
    runs = db.scalars(select(CheckRun).order_by(CheckRun.id)).all()
    assert [r.status for r in runs] == ["fail", "pass"] and runs[0].trigger == "manual"
    assert db.scalar(select(Incident)).status == "resolved"


def test_accept_schema_baseline(db, wh, tmp_path):
    wh.execute("CREATE TABLE nwtest.orders (id int)")
    engine.apply_bundle(db, bundle(tmp_path), "t")
    contract = db.scalar(select(Contract))
    mon = Monitor(name="raw.schema", connection_name="wh", kind="schema", dataset="nwtest.orders", params={},
                  severity="warning", interval_seconds=60, state={}, contract_id=contract.id)
    db.add(mon)
    db.commit()
    assert execute(mon.id, get_settings()) == "pass"
    wh.execute("ALTER TABLE nwtest.orders ADD COLUMN extra text")
    assert execute(mon.id, get_settings()) == "fail"
    db.expire_all()
    mon = db.get(Monitor, mon.id)
    assert engine.accept_schema(db, mon, "alice")
    db.commit()
    assert execute(mon.id, get_settings()) == "pass"


class Hook:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        return httpx.Response(self.status)


def test_signed_webhook_and_slack_delivery(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HOOK_URL", "https://hooks.example/nullwake")
    monkeypatch.setenv("TEST_HOOK_SECRET", "s3cret")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.example/T/B/X")
    mon = setup_monitor(db, tmp_path)
    engine.record_result(db, mon, Result("fail", "Data is 2 h old"), 1, utcnow())
    db.commit()
    hook = Hook()
    with httpx.Client(transport=httpx.MockTransport(hook)) as client:
        assert deliver_due(db, client) == (2, 0)
    db.commit()
    webhook = next(r for r in hook.requests if r.url.host == "hooks.example")
    ts = webhook.headers["x-nullwake-timestamp"]
    expected = "sha256=" + hmac.new(b"s3cret", ts.encode() + b"." + webhook.content, hashlib.sha256).hexdigest()
    assert webhook.headers["x-nullwake-signature"] == expected == sign("s3cret", ts, webhook.content)
    assert json.loads(webhook.content)["incident"]["check"] == "orders.freshness"
    slack = next(r for r in hook.requests if "slack" in r.url.host)
    assert "Critical incident" in json.loads(slack.content)["text"]
    assert all(d.status == "sent" for d in db.scalars(select(AlertDelivery)))
    assert db.scalar(select(IncidentEvent).where(IncidentEvent.kind == "alert_sent")) is not None


def test_failed_delivery_backs_off_then_gives_up(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("TEST_HOOK_SECRET", "s")
    monkeypatch.setenv("TEST_SLACK_URL", "https://hooks.slack.example/x")
    mon = setup_monitor(db, tmp_path)
    engine.record_result(db, mon, Result("fail", "x"), 1, utcnow())
    db.commit()
    with httpx.Client(transport=httpx.MockTransport(Hook(503))) as client:
        assert deliver_due(db, client) == (0, 0)
        db.commit()
        first = db.scalars(select(AlertDelivery)).first()
        assert first.attempts == 1 and first.status == "pending" and first.next_attempt_at > utcnow()
        assert first.last_error == "HTTP 503"
        for _ in range(MAX_ATTEMPTS):
            for d in db.scalars(select(AlertDelivery)):
                d.next_attempt_at = utcnow()
            db.commit()
            deliver_due(db, client)
            db.commit()
    assert {d.status for d in db.scalars(select(AlertDelivery))} == {"failed"}
    assert db.scalar(select(IncidentEvent).where(IncidentEvent.kind == "alert_failed")) is not None
