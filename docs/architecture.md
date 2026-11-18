# Architecture

## Components

| Component | Process | State | Scales |
| --- | --- | --- | --- |
| Web + API | `nullwake serve` (FastAPI/Uvicorn) | none | horizontally behind the proxy |
| Worker | `nullwake worker` | none | horizontally; checks are claimed with expiring leases |
| Metadata store | PostgreSQL 14+ | contracts, checks, runs, incidents, outbox, users, audit | vertically; runs are pruned after `NULLWAKE_RETENTION_DAYS` |
| Proxy | Caddy | TLS certificates | n/a |

Warehouses are reached only by workers (and by `nullwake check` in CI).

## Check execution

```mermaid
sequenceDiagram
  participant W as Worker
  participant M as Metadata DB
  participant X as Warehouse
  participant O as Outbox
  W->>M: SELECT … FOR UPDATE SKIP LOCKED (due, no live lease); set lease; COMMIT
  W->>X: read-only txn, statement_timeout, generated SQL
  X-->>W: aggregates (counts, ages, column metadata)
  W->>M: BEGIN; insert check_run; update check; open/update/resolve incident; enqueue alerts; next_run_at; release lease; COMMIT
  W->>O: claim pending deliveries (SKIP LOCKED); POST; mark sent or back off
```

Design points:

- **Lease, run, then reschedule.** A worker claims a due check by writing a lease on its row: owner, expiry
  (by default 3 × the query timeout + 60 s) and a fresh random token. It then copies the check's definition out of the
  metadata store and **closes that session before contacting the warehouse**, so no metadata transaction is open
  during warehouse I/O. While the check runs, a background thread renews the lease every lease/3 seconds, for at most
  two lease lengths. `next_run_at` moves only when an accepted run finishes. A run that takes longer than its interval
  is not started again while its lease is live, and a worker that dies mid-run delays the check until its lease
  expires instead of skipping it. All lease and schedule times come from the database clock, not the workers' clocks.
- **Fencing: late results are discarded.** The result is recorded only if, under the row lock, the row still carries
  the claim's token. A worker whose lease expired and was reclaimed, or whose check definition or connection changed
  while it ran (`nullwake apply` clears the claim), gets its result thrown away before it touches the check's state,
  history, incidents or the alert outbox. So a late "pass" can never resolve an incident opened by a newer run.
  Renewal requires the same token and a lease that has not yet expired; an expired lease is never revived.
- **What is and is not guaranteed.** Two runs of one check can still execute in the warehouse at the same time if a
  worker is paused longer than its lease (warehouse queries are read-only, so this costs only load), but only the
  current claim's result is recorded. Execution is therefore at-least-once; recorded results are from one claim at a
  time. "Run now" increments a counter on the check; a run that finishes with a higher count than it was claimed with
  is followed by exactly one immediate rerun, however many times the button was pressed.
- **Timeouts.** Each warehouse statement carries a server-side timeout (Postgres `statement_timeout`, Snowflake
  `STATEMENT_TIMEOUT_IN_SECONDS`). BigQuery waits up to the timeout and then cancels the job. A check runs a few
  statements, so a run is bounded by roughly that many timeouts plus connection time, not by one timeout.
- **Watching the watcher.** Each worker writes a heartbeat after every check and every idle pass. A loop that makes
  no progress for the lease + 120 s exits so the container restarts it. `/watchdog` reports, without needing a worker
  to be alive: no recent heartbeat, checks overdue, no check completed recently, every check on a connection failing
  to execute, and stuck alert delivery. Heartbeats show the loop is alive; the last two show work is actually getting
  done. See operations.md, "Watching the watcher".
- **Incident invariants live in the database.** A partial unique index (`monitor_id WHERE status <> 'resolved'`)
  guarantees one active incident per check even if a manual run races the scheduler.
- **Transactional outbox.** Alert rows are written in the same transaction as the incident change. Delivery is
  at-least-once with exponential backoff (30 s × 2ⁿ, 8 attempts); webhook receivers can de-duplicate on the
  `X-Nullwake-Delivery` header.
- **Volume baselines are computed in the warehouse.** One query returns the current window and the previous *N*
  windows (`SUM(CASE …)` buckets), so a new contract has a baseline on its first run with no learning period. The band is
  `median ± max(3 × 1.4826 × MAD, tolerancePct × median)`, which ignores single outliers.
- **Contract versions.** Each apply stores the YAML source and its SHA-256; a changed checksum bumps the version, and
  checks whose definition changed get their state reset.

## Data model

`connections`, `contracts` → `monitors` (checks) → `check_runs`; `incidents` → `incident_events`;
`alert_channels` → `alert_deliveries` (outbox); `users`, `sessions`, `api_tokens`, `login_attempts`;
`audit_log`; `worker_heartbeats`. Migrations are managed by Alembic (`nullwake migrate`).

## Dialects

SQL is generated per dialect (`connectors.py`): Postgres (`now() - interval`), Snowflake (`DATEADD`, `DATEDIFF`)
and BigQuery (`TIMESTAMP_SUB`, `TIMESTAMP_DIFF`). Type names are normalised per dialect so a contract can say
`timestamptz` or `bigint` naturally.
