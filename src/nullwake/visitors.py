"""Privacy-preserving visitor statistics from the proxy's access log.

No cookies, no JavaScript, no third party. The bundled Caddy writes one JSON line per request with the client IP
already masked (/24 for IPv4, /48 for IPv6) and cookies, auth headers and query strings removed (deploy/Caddyfile).
A "visitor" is a masked IP + user agent on one day, so the counts are estimates: people behind one office network
and browser may count once, and one person on two networks counts twice.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

MAX_BYTES = 64 * 1024 * 1024  # never read more than this much log per request
SKIP_PREFIXES = ("/static/", "/api/", "/watchdog", "/healthz", "/readyz", "/metrics", "/favicon", "/robots.txt")
BOT = re.compile(r"bot|crawl|spider|slurp|monitor|uptime|stackdriver|curl|wget|python|go-http|httpx|java/|"
                 r"preview|facebookexternalhit|whatsapp|headless|lighthouse|scanner", re.I)
NUMERIC = re.compile(r"/\d+(?=/|$)")
ASSET = re.compile(r"\.(css|js|woff2?|png|jpe?g|webp|svg|ico|txt|xml|map)$", re.I)  # files, not pages (marketing site)


@dataclass
class VisitorStats:
    days: int
    visitors: int = 0
    page_views: int = 0
    demo_sign_ins: int = 0
    daily: list[dict] = field(default_factory=list)
    pages: list[tuple[str, int]] = field(default_factory=list)
    referrers: list[tuple[str, int]] = field(default_factory=list)
    bots_ignored: int = 0
    log_found: bool = False
    since: datetime | None = None


def _log_files(path: str) -> list[str]:
    base, ext = os.path.splitext(path)
    rotated = sorted(glob.glob(f"{base}-*{ext}") + glob.glob(f"{base}-*{ext}.gz"))
    return rotated + ([path] if os.path.exists(path) else [])


def _lines(paths: list[str]):
    budget = MAX_BYTES
    for p in reversed(paths):  # newest first, so the budget keeps the most recent days
        opener = gzip.open if p.endswith(".gz") else open
        try:
            with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    budget -= len(line)
                    if budget < 0:
                        return
                    yield line
        except OSError:
            continue


def _header(headers: dict, name: str) -> str:
    value = headers.get(name) or []
    return value[0] if isinstance(value, list) and value else (value if isinstance(value, str) else "")


def _referrer(raw: str, own_host: str) -> str | None:
    if not raw:
        return None
    try:  # a client can send any Referer; a malformed one must not break the report
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return None
    if not host or host == own_host:
        return None
    host = host.removeprefix("www.").removeprefix("m.")
    return {"lnkd.in": "linkedin.com", "t.co": "twitter.com / x.com"}.get(host, host)


def compute(path: str, own_host: str, days: int = 30, now: datetime | None = None) -> VisitorStats:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = VisitorStats(days=days, since=since)
    files = _log_files(path)
    stats.log_found = bool(files)
    per_day_visitors: dict[str, set] = defaultdict(set)
    per_day_views: Counter = Counter()
    all_visitors: set = set()
    pages: Counter = Counter()
    refs: Counter = Counter()
    for line in _lines(files):
        try:
            entry = json.loads(line)
            ts = datetime.fromtimestamp(float(entry["ts"]), timezone.utc)
            req = entry["request"]
        except (ValueError, KeyError, TypeError):
            continue
        if ts < since:
            continue
        method, uri, status = req.get("method", ""), (req.get("uri") or "/").split("?")[0], int(entry.get("status", 0))
        # The same log may hold other hostnames served by the proxy (e.g. the marketing site at the apex domain).
        # Their pages are listed with the host in front so "/" on the site and "/" in the app stay apart.
        host = str(req.get("host") or "").lower().rsplit(":", 1)[0] if req.get("host") else ""
        other_host = bool(host and own_host and host != own_host.lower())
        headers = req.get("headers") or {}
        agent = _header(headers, "User-Agent")
        if not agent or BOT.search(agent):
            stats.bots_ignored += 1
            continue
        if not other_host and method == "POST" and uri in ("/login", "/demo") and status == 303:
            stats.demo_sign_ins += 1
            continue
        if method != "GET" or status >= 400 or uri.startswith(SKIP_PREFIXES) or ASSET.search(uri):
            continue
        day = ts.strftime("%Y-%m-%d")
        who = hashlib.sha256(f"{req.get('client_ip') or req.get('remote_ip')}|{agent}".encode()).hexdigest()[:16]
        per_day_visitors[day].add(who)
        all_visitors.add(who)
        per_day_views[day] += 1
        page = NUMERIC.sub("/:id", uri) or "/"
        pages[f"{host}{page}" if other_host else page] += 1
        ref = _referrer(_header(headers, "Referer"), own_host)
        if ref:
            refs[ref] += 1
    stats.visitors = len(all_visitors)
    stats.page_views = sum(per_day_views.values())
    for i in range(days):
        d = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        stats.daily.append({"day": d, "visitors": len(per_day_visitors.get(d, ())), "views": per_day_views.get(d, 0)})
    stats.daily.reverse()  # newest first
    stats.pages = pages.most_common(10)
    stats.referrers = refs.most_common(10)
    return stats
