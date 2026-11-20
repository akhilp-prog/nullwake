"""Every route, every caller: anonymous, each role by session, each role by API token.

The table below is the access policy. A route added to the app without an entry here fails the test, so nobody can
ship an endpoint without deciding who may call it.
"""
import re

import pytest
from fastapi.routing import APIRoute

from nullwake.app import create_app
from nullwake.security import create_api_token

from .test_web import ORIGIN, client, login, seeded  # noqa: F401  (fixtures)

RANK = {"public": -1, "viewer": 0, "editor": 1, "admin": 2}
POLICY = {
    ("GET", "/healthz"): "public", ("GET", "/readyz"): "public", ("GET", "/watchdog"): "public",
    ("GET", "/metrics"): "public",  # counts only; blocked at the bundled proxy (docs/security.md)
    ("GET", "/login"): "public", ("POST", "/login"): "public",
    ("POST", "/logout"): "viewer",
    ("GET", "/"): "viewer", ("GET", "/incidents"): "viewer", ("GET", "/incidents/{incident_id}"): "viewer",
    ("GET", "/contracts"): "viewer", ("GET", "/contracts/{name}"): "viewer", ("GET", "/checks/{check_id}"): "viewer",
    ("POST", "/incidents/{incident_id}/{action}"): "editor", ("POST", "/checks/{check_id}/{action}"): "editor",
    ("GET", "/audit"): "admin", ("GET", "/settings"): "admin", ("GET", "/visitors"): "admin",
    ("POST", "/settings/tokens"): "admin", ("POST", "/settings/tokens/{token_id}/revoke"): "admin",
    ("GET", "/api/v1/status"): "viewer", ("GET", "/api/v1/contracts"): "viewer",
    ("GET", "/api/v1/contracts/{name}"): "viewer", ("GET", "/api/v1/checks"): "viewer",
    ("GET", "/api/v1/checks/{check_id}/runs"): "viewer", ("GET", "/api/v1/incidents"): "viewer",
    ("GET", "/api/v1/incidents/{incident_id}"): "viewer",
    ("POST", "/api/v1/checks/{check_id}/run"): "editor", ("POST", "/api/v1/incidents/{incident_id}/acknowledge"): "editor",
    ("POST", "/api/v1/config/apply"): "editor",
}
DENIED = {401, 403}


def routes():
    """Every method+path the app serves, including routes from included routers."""
    out = []

    def walk(items, prefix=""):
        for r in items:
            if isinstance(r, APIRoute):
                out.extend((m, prefix + r.path) for m in r.methods if m in {"GET", "POST", "PUT", "PATCH", "DELETE"})
            elif getattr(r, "original_router", None) is not None:  # FastAPI wraps include_router() results
                walk(r.original_router.routes, prefix + (getattr(r, "include_context", None) and
                                                         getattr(r.include_context, "prefix", "") or ""))
    walk(create_app().routes)
    return sorted(set(out))


def test_every_route_has_a_policy():
    missing = [r for r in routes() if r not in POLICY]
    assert not missing, f"routes with no access policy: {missing}"
    stale = [r for r in POLICY if r not in routes()]
    assert not stale, f"policy entries for routes that no longer exist: {stale}"


def _url(path, ids):
    return re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)


def _call(client, method, url, headers=None, data=None):
    return client.request(method, url, headers={**ORIGIN, **(headers or {})}, data=data, follow_redirects=False)


@pytest.fixture
def ids(seeded, db):
    from sqlalchemy import select

    from nullwake.models import ApiToken, Contract, Monitor
    row, _ = create_api_token(db, "victim", "viewer", "t")
    db.commit()
    return {"incident_id": seeded.id, "check_id": db.scalar(select(Monitor.id)), "name": db.scalar(select(Contract.name)),
            "token_id": db.scalar(select(ApiToken.id).where(ApiToken.name == "victim")), "action": "acknowledge"}


def _is_denied(resp):
    # Pages answer an anonymous visitor with a redirect to the sign-in page rather than a bare 401.
    return resp.status_code in DENIED or (resp.status_code in (302, 303, 307) and "/login" in resp.headers.get("location", ""))


@pytest.mark.parametrize("method,path", sorted(k for k, v in POLICY.items() if v != "public"))
def test_anonymous_is_denied(client, ids, method, path):
    action = "run" if path.startswith("/checks/") else "acknowledge"
    resp = _call(client, method, _url(path, {**ids, "action": action}))
    assert _is_denied(resp), (method, path, resp.status_code)


@pytest.mark.parametrize("method,path", sorted(k for k, v in POLICY.items() if v != "public"))
def test_invalid_token_is_denied(client, ids, method, path):
    action = "run" if path.startswith("/checks/") else "acknowledge"
    resp = _call(client, method, _url(path, {**ids, "action": action}),
                 headers={"authorization": "Bearer nw_deadbeef_forged-token-value"})
    assert _is_denied(resp), (method, path, resp.status_code)


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_session_roles(client, ids, role):
    csrf = login(client, f"{role}@x.io")
    for (method, path), need in POLICY.items():
        if need == "public" or path in ("/logout", "/login"):
            continue
        action = "run" if path.startswith("/checks/") else "acknowledge"
        data = {"csrf": csrf, "name": "probe", "role": "viewer"} if method == "POST" else None
        headers = {"x-csrf-token": csrf} if method == "POST" and path.startswith("/api/") else None
        resp = _call(client, method, _url(path, {**ids, "action": action}), headers=headers,
                     data=None if path.startswith("/api/") else data)
        if RANK[role] < RANK[need]:
            assert resp.status_code == 403, (role, method, path, resp.status_code)
        else:
            assert resp.status_code not in DENIED, (role, method, path, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_token_roles_on_the_api(client, ids, db, role):
    _, raw = create_api_token(db, f"tok-{role}", role, "t")
    db.commit()
    for (method, path), need in POLICY.items():
        if not path.startswith("/api/"):
            continue
        resp = _call(client, method, _url(path, ids), headers={"authorization": f"Bearer {raw}"})
        if RANK[role] < RANK[need]:
            assert resp.status_code == 403, (role, method, path, resp.status_code)
        else:
            assert resp.status_code not in DENIED, (role, method, path, resp.status_code, resp.text[:200])


def test_tokens_cannot_drive_the_web_ui(client, ids, db):
    """A leaked API token must not reach admin pages or form actions; those need a signed-in session."""
    _, raw = create_api_token(db, "tok-admin-web", "admin", "t")
    db.commit()
    bearer = {"authorization": f"Bearer {raw}"}
    for (method, path), need in POLICY.items():
        if need == "public" or path.startswith("/api/"):
            continue
        action = "run" if path.startswith("/checks/") else "acknowledge"
        r = _call(client, method, _url(path, {**ids, "action": action}), headers=bearer,
                  data={"name": "persist", "role": "admin"} if method == "POST" else None)
        assert r.status_code == 403, (method, path, r.status_code)


def test_cross_site_writes_are_refused(client, ids):
    """A signed-in admin tricked into a cross-site form post: wrong Origin or missing CSRF token is refused."""
    csrf = login(client, "admin@x.io")
    url = f"/incidents/{ids['incident_id']}/acknowledge"
    evil = client.post(url, data={"csrf": csrf}, headers={"origin": "https://evil.example"}, follow_redirects=False)
    no_csrf = client.post(url, data={}, headers=ORIGIN, follow_redirects=False)
    no_origin = client.post(url, data={"csrf": csrf}, follow_redirects=False)
    tok = client.post("/settings/tokens", data={"csrf": "wrong", "name": "x", "role": "admin"}, headers=ORIGIN)
    assert [evil.status_code, no_csrf.status_code, no_origin.status_code, tok.status_code] == [403] * 4


def test_admin_cannot_mint_unknown_roles(client, ids):
    csrf = login(client, "admin@x.io")
    r = client.post("/settings/tokens", data={"csrf": csrf, "name": "x", "role": "superadmin"}, headers=ORIGIN)
    assert r.status_code == 400


def test_contracts_cannot_borrow_nullwakes_own_settings(tmp_path):
    from nullwake.contracts import ContractError, load_bundle
    path = tmp_path / "c.yaml"
    path.write_text("apiVersion: nullwake/v1\nkind: Connection\nmetadata: {name: c}\n"
                    "spec: {type: postgres, dsnEnv: NULLWAKE_DATABASE_URL}\n")
    with pytest.raises(ContractError, match="Nullwake's own settings"):
        load_bundle([path])


def test_connections_into_nullwakes_own_database_are_refused(monkeypatch, migrated):
    """Whatever the variable is called, a DSN that lands in the metadata database (users, sessions, tokens) fails."""
    from nullwake.connectors import ConnectorError, PostgresConnector

    from .conftest import TEST_DB
    from sqlalchemy.engine import make_url
    url = make_url(TEST_DB)
    monkeypatch.setenv("SNEAKY_DSN", f"host={url.query.get('host', 'localhost')} dbname={url.database} "
                                     f"user={url.username}")
    with pytest.raises(ConnectorError, match="Nullwake's own database"):
        PostgresConnector("sneaky", {"dsnEnv": "SNEAKY_DSN"}, 10)
