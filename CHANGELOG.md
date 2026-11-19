# Changelog

## 0.5.0 (2026-11-19)

- Snowflake connector hardened for pilots: sessions now run in UTC (Snowflake defaults to America/Los_Angeles, which
  shifted freshness ages on `TIMESTAMP_NTZ` columns by the UTC offset), connect with login and network timeouts,
  support encrypted private keys (`privateKeyPassphraseEnv`) and a default `schema`, and redact errors from schema
  lookups as well as checks.
- Fixed: a bare table name on Snowflake looked up columns with the database name as the schema.
- Snowflake checks now run end to end in the test suite against the fakesnow emulator.

## 0.4.0 (2026-11-19)

- Visitor statistics for admins at `/visitors`: visitors, page views, demo sign-ins, referrers and top pages over 7 or 30 days, read from Caddy's access log. No cookies, no JavaScript, no third-party analytics.
- The access log is privacy-filtered at write time: client IPs masked to /24 (IPv4) and /48 (IPv6), query strings stripped, cookie, authorization and CSRF headers dropped, and files deleted after 30 days.
- Contact details on the sign-in page and footer, set with `NULLWAKE_CONTACT_NAME`, `_EMAIL`, `_LINKEDIN` and `_GITHUB`.

## 0.3.0 (2026-11-18)

Fixes from a second external review.

- **Fencing tokens.** Each claim carries a random token; a result is recorded only if the row still carries it. A
  worker that lost its lease, or whose check definition changed mid-run, has its result discarded before it can touch
  check state, history, incidents or alerts, so a late "pass" can no longer resolve a newer incident. Migration
  `0003` adds `monitors.lease_token` and `run_requests`.
- **Leases.** Renewed in the background while a check runs; renewal needs the same token and a live lease; all lease
  and schedule times use the database clock. "Run now" is a counter: clicks during a run coalesce into one rerun.
- **No open metadata transaction during warehouse I/O.** The check definition is copied out and the session closed
  before the warehouse is contacted.
- **Redacted warehouse errors.** Stored and alerted messages carry the error class, a fixed description and the
  SQLSTATE only; the full text goes to the local log. BigQuery jobs are cancelled when they time out.
- **Stricter self-monitoring.** `/watchdog` also fails when no check has completed recently or every check on a
  connection fails to execute; the dead man's switch pings only while all `/watchdog` conditions are healthy.
- **Docs.** Outbound connections listed in full; custom assertions must return an aggregate; timeouts, transaction
  boundaries and overlap guarantees corrected.

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
