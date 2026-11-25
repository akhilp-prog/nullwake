"""Alert delivery from the transactional outbox, with retries and signed webhooks."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import telemetry
from .models import AlertDelivery, IncidentEvent, utcnow

log = logging.getLogger("nullwake.alerts")
MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30


class DeliveryError(Exception):
    pass


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def slack_body(payload: dict) -> dict:
    inc = payload["incident"]
    icon = ":white_check_mark:" if payload["event"] == "resolved" else (
        ":rotating_light:" if inc["severity"] == "critical" else ":warning:")
    verb = "Resolved" if payload["event"] == "resolved" else f"{inc['severity'].title()} incident"
    link = f"<{inc['url']}|#{inc['id']}>" if inc.get("url") else f"#{inc['id']}"
    lines = [f"{icon} *{verb}* {link}: {inc['check']}", inc["message"]]
    meta = [f"dataset `{inc['dataset']}`"]
    if inc.get("owner"):
        meta.append(f"owner {inc['owner']}")
    lines.append(" · ".join(meta))
    text = "\n".join(lines)
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def send(delivery: AlertDelivery, client: httpx.Client) -> None:
    spec = delivery.channel.spec
    url = os.environ.get(spec.get("urlEnv", ""), "")
    if not url:
        raise DeliveryError(f"environment variable {spec.get('urlEnv')} is not set")
    if delivery.channel.type == "slack":
        body = json.dumps(slack_body(delivery.payload)).encode()
        headers = {"Content-Type": "application/json"}
    else:
        body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "User-Agent": "nullwake",
                   "X-Nullwake-Event": delivery.event, "X-Nullwake-Delivery": str(delivery.id),
                   "X-Nullwake-Timestamp": ts}
        secret_env = spec.get("signingSecretEnv")
        if secret_env:
            secret = os.environ.get(secret_env)
            if not secret:
                raise DeliveryError(f"environment variable {secret_env} is not set")
            headers["X-Nullwake-Signature"] = sign(secret, ts, body)
    try:
        response = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise DeliveryError(f"{exc.__class__.__name__}") from None
    if response.status_code >= 300:
        raise DeliveryError(f"HTTP {response.status_code}")


def deliver_due(session: Session, client: httpx.Client, limit: int = 20) -> tuple[int, int]:
    """Send due deliveries. Rows are locked with SKIP LOCKED so several workers can share the queue."""
    due = session.scalars(
        select(AlertDelivery).where(AlertDelivery.status == "pending", AlertDelivery.next_attempt_at <= utcnow())
        .order_by(AlertDelivery.id).limit(limit).with_for_update(skip_locked=True, of=AlertDelivery)
    ).all()
    sent = failed = 0
    for d in due:
        d.attempts += 1
        try:
            with telemetry.span("nullwake.alert.deliver", **{"nullwake.channel": d.channel.name,
                                                             "nullwake.channel.type": d.channel.type,
                                                             "nullwake.alert.event": d.event,
                                                             "nullwake.alert.attempt": d.attempts}) as sp:
                try:
                    send(d, client)
                except DeliveryError as exc:
                    sp.fail(str(exc)[:120])
                    raise
        except DeliveryError as exc:
            telemetry.count("alerts", outcome="error", channel_type=d.channel.type)
            d.last_error = str(exc)[:500]
            if d.attempts >= MAX_ATTEMPTS:
                d.status = "failed"
                failed += 1
                session.add(IncidentEvent(incident_id=d.incident_id, actor="nullwake", kind="alert_failed",
                                          message=f"{d.channel.name}: {d.last_error} after {d.attempts} attempts"))
            else:
                d.next_attempt_at = utcnow() + timedelta(seconds=BASE_BACKOFF_SECONDS * 2 ** (d.attempts - 1))
            log.warning("alert delivery %s to %s failed: %s", d.id, d.channel.name, d.last_error)
            continue
        telemetry.count("alerts", outcome="sent", channel_type=d.channel.type)
        d.status, d.last_error = "sent", ""
        sent += 1
        session.add(IncidentEvent(incident_id=d.incident_id, actor="nullwake", kind="alert_sent",
                                  message=f"{d.event} notification sent to {d.channel.name}"))
    return sent, failed
