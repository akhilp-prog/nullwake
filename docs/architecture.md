# Architecture

## Components

| Component | Process | State | Scales |
| --- | --- | --- | --- |
| Web + API | `nullwake serve` (FastAPI/Uvicorn) | none | horizontally behind the proxy |
| Worker | `nullwake worker` | none | horizontally; checks are claimed with `SKIP LOCKED` |
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
  W->>M: SELECT … FOR UPDATE SKIP LOCKED (due checks); push next_run_at; COMMIT
  W->>X: read-only txn, statement_timeout, generated SQL
  X-->>W: aggregates (counts, ages, column metadata)
  W->>M: BEGIN; insert check_run; update check; open/update/resolve incident; enqueue alerts; COMMIT
  W->>O: claim pending deliveries (SKIP LOCKED); POST; mark sent or back off
```

Design points:

- **Claim then run.** A worker commits the claim (moving `next_run_at` forward) before contacting the warehouse,
  so no metadata transaction is held open during slow warehouse queries and a crashed worker delays a check by at
  most one interval rather than blocking it.
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
