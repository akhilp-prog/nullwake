"""HTTP surface: auth, RBAC, CSRF/origin checks, throttling, API tokens, health and headers."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nullwake import engine
from nullwake.app import create_app
from nullwake.contracts import load_bundle
from nullwake.models import Incident, Monitor, utcnow
from nullwake.monitors import Result
from nullwake.security import create_api_token, upsert_user

from .conftest import CONNECTION_YAML, contract_yaml

ORIGIN = {"origin": "http://testserver"}
PASSWORD = "correct-horse-battery"


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def seeded(db, tmp_path):
    for email, role in (("viewer@x.io", "viewer"), ("editor@x.io", "editor"), ("admin@x.io", "admin")):
        upsert_user(db, email, role, PASSWORD)
    path = tmp_path / "b.yaml"
    path.write_text(CONNECTION_YAML + "---" + contract_yaml("freshness: {column: loaded_at, maxAgeMinutes: 30}"))
    engine.apply_bundle(db, load_bundle([path]), "t")
    db.commit()
    mon = db.scalar(select(Monitor))
    engine.record_result(db, mon, Result("fail", "Data is 2 h old"), 5, utcnow())
    db.commit()
    return db.scalar(select(Incident))


def login(client, email):
    r = client.post("/login", data={"email": email, "password": PASSWORD}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = client.get("/").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_health_ready_metrics_and_headers(client, seeded):
    assert client.get("/healthz").text == "ok\n"
    ready = client.get("/readyz").json()
    assert ready["ready"] is True and ready["migration"] == "0002"
    metrics = client.get("/metrics").text
    assert 'nullwake_incidents_active{severity="critical"} 1.0' in metrics
    assert 'nullwake_checks{status="fail"} 1.0' in metrics
    r = client.get("/login")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    # "no-referrer" would make browsers send `Origin: null` on form POSTs and break the origin check.
    assert r.headers["referrer-policy"] == "same-origin"


def test_login_failures_and_throttle(client, seeded):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    bad = client.post("/login", data={"email": "viewer@x.io", "password": "wrong-password-123"}, headers=ORIGIN)
    assert bad.status_code == 401 and "incorrect" in bad.text
    no_origin = client.post("/login", data={"email": "viewer@x.io", "password": PASSWORD})
    assert no_origin.status_code == 403
    for _ in range(9):
        client.post("/login", data={"email": "viewer@x.io", "password": "wrong-password-123"}, headers=ORIGIN)
    blocked = client.post("/login", data={"email": "viewer@x.io", "password": PASSWORD}, headers=ORIGIN)
    assert blocked.status_code == 429


def test_session_cookie_flags_and_pages_render(client, seeded):
    r = client.post("/login", data={"email": "admin@x.io", "password": PASSWORD}, headers=ORIGIN, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    for path in ("/", "/incidents", f"/incidents/{seeded.id}", "/contracts", "/contracts/orders",
                 f"/checks/{seeded.monitor_id}", "/audit", "/settings", "/incidents?status=all"):
        page = client.get(path)
        assert page.status_code == 200, path
    assert "orders.freshness" in client.get("/").text
    assert "Data is 2 h old" in client.get(f"/incidents/{seeded.id}").text


def test_viewer_is_read_only(client, seeded):
    csrf = login(client, "viewer@x.io")
    r = client.post(f"/incidents/{seeded.id}/acknowledge", data={"csrf": csrf}, headers=ORIGIN)
    assert r.status_code == 403
    assert client.get("/settings").status_code == 403
    assert client.get("/audit").status_code == 403


def test_editor_acknowledges_with_csrf_and_origin(client, seeded, db):
    csrf = login(client, "editor@x.io")
    assert client.post(f"/incidents/{seeded.id}/acknowledge", data={"csrf": "wrong"}, headers=ORIGIN).status_code == 403
    assert client.post(f"/incidents/{seeded.id}/acknowledge", data={"csrf": csrf},
                       headers={"origin": "https://evil.example"}).status_code == 403
    ok = client.post(f"/incidents/{seeded.id}/acknowledge", data={"csrf": csrf, "note": "on it"}, headers=ORIGIN,
                     follow_redirects=False)
    assert ok.status_code == 303
    assert client.post(f"/incidents/{seeded.id}/resolve", data={"csrf": csrf}, headers=ORIGIN).status_code == 400
    client.post(f"/incidents/{seeded.id}/resolve", data={"csrf": csrf, "note": "backfilled"}, headers=ORIGIN)
    db.expire_all()
    incident = db.get(Incident, seeded.id)
    assert incident.status == "resolved" and incident.acknowledged_by == "editor@x.io"
    assert [e.kind for e in incident.events][-2:] == ["acknowledged", "resolved"]


def test_logout_revokes_session(client, seeded):
    csrf = login(client, "viewer@x.io")
    client.post("/logout", data={"csrf": csrf}, headers=ORIGIN)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_api_tokens_and_rbac(client, seeded, db):
    _, viewer = create_api_token(db, "ro", "viewer", "t")
    _, editor = create_api_token(db, "ci", "editor", "t")
    db.commit()
    v = {"authorization": f"Bearer {viewer}"}
    e = {"authorization": f"Bearer {editor}"}
    assert client.get("/api/v1/incidents").status_code == 401
    assert client.get("/api/v1/incidents", headers={"authorization": "Bearer nw_deadbeef_nope-nope-nope-nope"}).status_code == 401
    incidents = client.get("/api/v1/incidents", headers=v).json()
    assert incidents[0]["check"] == "orders.freshness"
    assert client.get(f"/api/v1/incidents/{seeded.id}", headers=v).json()["events"][0]["kind"] == "opened"
    assert client.post(f"/api/v1/incidents/{seeded.id}/acknowledge", headers=v, json={}).status_code == 403
    ack = client.post(f"/api/v1/incidents/{seeded.id}/acknowledge", headers=e, json={"note": "via api"})
    assert ack.status_code == 200 and ack.json()["acknowledgedBy"] == "token:ci"
    assert client.post(f"/api/v1/incidents/{seeded.id}/acknowledge", headers=e, json={}).status_code == 409
    assert client.get("/api/v1/status", headers=v).json()["activeIncidents"] == 1
    assert client.post(f"/api/v1/checks/{seeded.monitor_id}/run", headers=e).status_code == 202
    runs = client.get(f"/api/v1/checks/{seeded.monitor_id}/runs", headers=v).json()
    assert runs[0]["status"] == "fail"


def test_api_config_apply(client, seeded, db):
    _, editor = create_api_token(db, "ci", "editor", "t")
    db.commit()
    h = {"authorization": f"Bearer {editor}", "content-type": "application/yaml"}
    good = contract_yaml("checks: [{name: c1, sql: 'select 0'}]", name="refunds")
    dry = client.post("/api/v1/config/apply?dry_run=true", content=good, headers=h)
    assert dry.status_code == 200 and dry.json() == {"valid": True, "contracts": ["refunds"], "checks": 1}
    assert client.post("/api/v1/config/apply", content=good, headers=h).json()["created"] == ["contract/refunds"]
    bad = client.post("/api/v1/config/apply", content=good.replace("select 0", "drop table x"), headers=h)
    assert bad.status_code == 422 and "single SELECT" in bad.json()["error"]
    conn = client.post("/api/v1/config/apply", content=CONNECTION_YAML, headers=h)
    assert conn.status_code == 403  # editors cannot change connections


def test_admin_creates_and_revokes_tokens(client, seeded):
    csrf = login(client, "admin@x.io")
    page = client.post("/settings/tokens", data={"csrf": csrf, "name": "grafana", "role": "viewer"}, headers=ORIGIN)
    token = re.search(r"(nw_[0-9a-f]{8}_[A-Za-z0-9_-]+)", page.text).group(1)
    assert client.get("/api/v1/status", headers={"authorization": f"Bearer {token}"}).status_code == 200
    token_id = re.search(r'/settings/tokens/(\d+)/revoke', page.text).group(1)
    client.post(f"/settings/tokens/{token_id}/revoke", data={"csrf": csrf}, headers=ORIGIN)
    assert client.get("/api/v1/status", headers={"authorization": f"Bearer {token}"}).status_code == 401
    assert "token.revoke" in client.get("/audit").text


def test_unknown_pages_and_bad_input(client, seeded):
    login(client, "viewer@x.io")
    assert client.get("/incidents/999999").status_code == 404
    assert client.get("/contracts/nope").status_code == 404
    assert client.get("/api/v1/incidents?status=bogus").status_code == 422
