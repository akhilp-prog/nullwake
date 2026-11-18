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

- **Lease, run, then reschedule.** A worker commits a lease on the check (owner + expiry, by default
  3 × the query timeout + 60 s) before contacting the warehouse, so no metadata transaction is held open during slow
  warehouse queries. `next_run_at` moves only when the run finishes, so a run that takes longer than the check's
  interval is not started again while its lease is live, and a worker that dies mid-run delays the check until its
  lease expires instead of skipping it. The lease is renewed right before each check in a batch starts.
- **What the lease does not guarantee.** A run that outlives its lease (every warehouse query has a statement
  timeout, so this needs a stalled connection) can overlap with a reclaimed run. The worker that lost its lease still
  records its result but leaves the schedule to the new owner, and the one-active-incident index below keeps the
  overlap from opening duplicate incidents. Runs are therefore at-least-once, not exactly-once.
- **Watching the watcher.** Each worker writes a heartbeat after every check and every idle pass. A loop that makes
  no progress for the lease + 120 s exits so the container restarts it. `/watchdog` and the
  `nullwake_checks_overdue` / `nullwake_alert_deliveries_stalled` metrics report a stopped worker, overdue checks and
  stuck alerts without needing a worker to be alive (see operations.md, "Watching the watcher").
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
