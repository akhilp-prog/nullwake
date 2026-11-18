# Changelog

## 0.2.0 (2026-11-18)

Scheduler and self-monitoring fixes from an external review of v0.1.

- **Leased scheduling.** A due check is claimed with an expiring lease, and `next_run_at` moves only when the run
  finishes. A crashed worker's check is reclaimed when the lease expires instead of being skipped for an interval,
  and a check that runs longer than its interval is not started again while it runs. "Run now" during a run is kept.
  Migration `0002` adds `monitors.lease_owner` and `lease_expires_at`.
- **Watching the watcher.** Heartbeat after every check; a stalled worker loop exits so the container restarts;
  `nullwake worker-health` as the worker container's health check; `/watchdog` for external uptime monitors (counts
  only); optional outbound dead man's switch (`NULLWAKE_DEADMAN_URL`); metrics `nullwake_checks_overdue` and
  `nullwake_alert_deliveries_stalled`.
- **Docs.** Corrected the scheduler guarantees (at-least-once, not exactly-once), documented exactly what leaves the
  network when alert channels are enabled, and added query-load and recovery guidance.
- `nullwake serve` now defaults to one Uvicorn worker.

## 0.1.0 (2026-11-18)

First release.

- Data contracts as YAML: schema, freshness, volume, not-null, uniqueness, custom SQL assertions.
- Postgres connector (read-only sessions, statement timeouts); Snowflake and BigQuery adapters (beta).
- Worker with `SKIP LOCKED` scheduling, incident lifecycle with dedupe and auto-resolve.
- Slack and HMAC-signed webhook alerts via a transactional outbox with retries.
- Web UI (no JavaScript, strict CSP), REST API with role-scoped tokens, audit log.
- `nullwake check` CI command; Docker Compose deployment with Caddy; demo warehouse simulator.
