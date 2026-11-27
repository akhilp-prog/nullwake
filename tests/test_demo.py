"""One-click demo sign-in: only in demo mode, only for a read-only account, same origin checks as /login."""
import pytest
from fastapi.testclient import TestClient

from nullwake.app import create_app
from nullwake.config import get_settings
from nullwake.security import upsert_user

from .test_web import ORIGIN, client  # noqa: F401  (fixture)

DEMO = "demo@nullwake.si"
DEMO_PW = "see-it-break-live"


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("NULLWAKE_DEMO_MODE", "true")
    monkeypatch.setenv("NULLWAKE_DEMO_USER", DEMO)
    monkeypatch.setenv("NULLWAKE_DEMO_PASSWORD", DEMO_PW)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_demo_button_signs_in_as_viewer(demo_client, db):
    upsert_user(db, DEMO, "viewer", DEMO_PW)
    db.commit()
    page = demo_client.get("/login").text
    assert 'action="/demo"' in page and DEMO_PW not in page
    r = demo_client.post("/demo", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert demo_client.get("/").status_code == 200
    assert demo_client.get("/settings").status_code == 403  # still read-only


def test_demo_refuses_cross_site_and_non_viewer(demo_client, db):
    upsert_user(db, DEMO, "admin", DEMO_PW)
    db.commit()
    assert demo_client.post("/demo", headers={"origin": "https://evil.example"}).status_code == 403
    r = demo_client.post("/demo", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 503 and "session" not in r.headers.get("set-cookie", "")


def test_demo_route_absent_outside_demo_mode(client, db):
    assert client.post("/demo", headers=ORIGIN).status_code == 404
    assert 'action="/demo"' not in client.get("/login").text


CLICK = {"sec-fetch-user": "?1", "sec-fetch-dest": "document", "sec-fetch-site": "cross-site"}


def test_demo_link_signs_in_on_a_real_click(demo_client, db):
    upsert_user(db, DEMO, "viewer", DEMO_PW)
    db.commit()
    r = demo_client.get("/demo", headers=CLICK, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "session" in r.headers.get("set-cookie", "")
    assert demo_client.get("/").status_code == 200


def test_demo_link_ignores_previews_and_crawlers(demo_client, db):
    upsert_user(db, DEMO, "viewer", DEMO_PW)
    db.commit()
    for headers in ({}, {"sec-fetch-user": "?1", "sec-fetch-dest": "image"}, {"user-agent": "LinkedInBot/1.0"}):
        r = demo_client.get("/demo", headers=headers, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/login", headers
        assert "set-cookie" not in r.headers


def test_demo_link_never_replaces_an_existing_session(demo_client, db):
    upsert_user(db, DEMO, "viewer", DEMO_PW)
    upsert_user(db, "admin@x.io", "admin", "correct-horse-battery")
    db.commit()
    r = demo_client.post("/login", data={"email": "admin@x.io", "password": "correct-horse-battery"}, headers=ORIGIN,
                         follow_redirects=False)
    assert r.status_code == 303
    r = demo_client.get("/demo", headers=CLICK, follow_redirects=False)
    assert r.status_code == 307 and "set-cookie" not in r.headers
    assert demo_client.get("/settings").status_code == 200  # still the admin


def test_demo_link_absent_outside_demo_mode(client, db):
    assert client.get("/demo", headers=CLICK, follow_redirects=False).status_code == 404
