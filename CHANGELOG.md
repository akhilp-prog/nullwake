# Changelog

## 0.1.0 (2026-11-18)

First release.

- Data contracts as YAML: schema, freshness, volume, not-null, uniqueness, custom SQL assertions.
- Postgres connector (read-only sessions, statement timeouts); Snowflake and BigQuery adapters (beta).
- Worker with `SKIP LOCKED` scheduling, incident lifecycle with dedupe and auto-resolve.
- Slack and HMAC-signed webhook alerts via a transactional outbox with retries.
- Web UI (no JavaScript, strict CSP), REST API with role-scoped tokens, audit log.
- `nullwake check` CI command; Docker Compose deployment with Caddy; demo warehouse simulator.
