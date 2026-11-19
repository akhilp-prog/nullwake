"""Visitor statistics from the proxy's privacy-filtered access log, and the contact links."""
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from nullwake import visitors
from nullwake.config import get_settings

from .test_web import client, login, seeded  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130"


def line(ts, uri, method="GET", status=200, ip="203.0.113.0", agent=BROWSER, referer=None, host=None):
    headers = {"User-Agent": [agent]} if agent else {}
    if referer:
        headers["Referer"] = [referer]
    req = {"method": method, "uri": uri, "client_ip": ip, "headers": headers}
    if host:
        req["host"] = host
    return json.dumps({"ts": ts.timestamp(), "status": status, "request": req}) + "\n"


def test_counts_visitors_views_sign_ins_and_referrers(tmp_path):
    log = tmp_path / "access.log"
    today, yesterday = NOW - timedelta(hours=1), NOW - timedelta(days=1)
    log.write_text("".join([
        line(today, "/login", referer="https://www.linkedin.com/feed/"),
        line(today, "/login", method="POST", status=303),                          # a demo sign-in
        line(today, "/incidents/42"), line(today, "/incidents/7"),                  # ids collapse to :id
        line(today, "/static/style.css"), line(today, "/watchdog"),                  # not page views
        line(today, "/", agent="GoogleStackdriverMonitoring-UptimeChecks"),          # uptime checker
        line(today, "/", agent="LinkedInBot/1.0"),                                    # link preview bot
        line(today, "/missing", status=404),
        line(today, "/", ip="198.51.100.0", referer="https://lnkd.in/abc"),         # second visitor
        line(today, "/", referer="https://app.nullwake.si/incidents"),               # own site: not a referrer
        line(NOW - timedelta(days=40), "/"),                                          # outside 30 days
    ]))
    rotated = tmp_path / "access-2026-11-18T00-00-00.000.log.gz"
    with gzip.open(rotated, "wt") as fh:
        fh.write(line(yesterday, "/contracts", ip="192.0.2.0"))
    s = visitors.compute(str(log), "app.nullwake.si", days=30, now=NOW)
    assert s.log_found and s.visitors == 3 and s.page_views == 6 and s.demo_sign_ins == 1
    assert dict(s.pages)["/incidents/:id"] == 2
    assert dict(s.referrers) == {"linkedin.com": 2}
    assert s.bots_ignored == 2
    assert s.daily[0] == {"day": "2026-11-19", "visitors": 2, "views": 5}
    assert s.daily[1] == {"day": "2026-11-18", "visitors": 1, "views": 1}
    assert len(s.daily) == 30


def test_missing_log_is_reported_not_an_error(tmp_path):
    s = visitors.compute(str(tmp_path / "nope.log"), "x", now=NOW)
    assert s.log_found is False and s.visitors == 0


@pytest.fixture
def contact_settings(monkeypatch):
    monkeypatch.setenv("NULLWAKE_CONTACT_NAME", "Akhil Pettem")
    monkeypatch.setenv("NULLWAKE_CONTACT_EMAIL", "hello@nullwake.si")
    monkeypatch.setenv("NULLWAKE_CONTACT_LINKEDIN", "https://www.linkedin.com/in/akhil-p-8bbab842a/")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_contact_links_on_sign_in_page_and_footer(contact_settings):
    from fastapi.testclient import TestClient

    from nullwake.app import create_app
    with TestClient(create_app()) as client:
        page = client.get("/login").text
    assert "Built by Akhil Pettem" in page and 'href="mailto:hello@nullwake.si"' in page
    assert "linkedin.com/in/akhil-p-8bbab842a" in page


def test_visitors_page_is_admin_only(client, seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("NULLWAKE_ACCESS_LOG", str(tmp_path / "access.log"))
    get_settings.cache_clear()
    login(client, "viewer@x.io")
    assert client.get("/visitors").status_code == 403
    client.cookies.clear()
    login(client, "admin@x.io")
    r = client.get("/visitors")
    assert r.status_code == 200 and "No access log found" in r.text
    (tmp_path / "access.log").write_text(line(datetime.now(timezone.utc), "/login"))
    r = client.get("/visitors")
    assert r.status_code == 200 and "<strong>1</strong>" in r.text and "/login" in r.text
    get_settings.cache_clear()


def test_malformed_referrer_does_not_break_the_report(tmp_path):
    log = tmp_path / "access.log"
    log.write_text(line(NOW - timedelta(hours=1), "/login", referer="http://[not-a-host/x") +
                   line(NOW - timedelta(hours=1), "/login", ip="198.51.100.0", referer="https://lnkd.in/abc"))
    stats = visitors.compute(str(log), "app.example", days=7, now=NOW)
    assert stats.visitors == 2 and dict(stats.referrers) == {"linkedin.com": 1}


def test_marketing_site_pages_are_kept_apart_from_app_pages(tmp_path):
    log = tmp_path / "access.log"
    t = NOW - timedelta(hours=1)
    log.write_text("".join([
        line(t, "/", host="nullwake.si", referer="https://www.linkedin.com/feed/"),
        line(t, "/site.css", host="nullwake.si"), line(t, "/fonts/x.woff2", host="nullwake.si"),  # assets
        line(t, "/img/overview.webp", host="nullwake.si"),
        line(t, "/", host="app.nullwake.si:443", referer="https://nullwake.si/"),   # site -> demo click-through
        line(t, "/login", method="POST", status=303, host="nullwake.si"),          # not an app sign-in
    ]))
    s = visitors.compute(str(log), "app.nullwake.si", days=30, now=NOW)
    assert dict(s.pages) == {"nullwake.si/": 1, "/": 1}
    assert s.page_views == 2 and s.demo_sign_ins == 0
    assert dict(s.referrers) == {"linkedin.com": 1, "nullwake.si": 1}
