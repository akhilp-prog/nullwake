# Nullwake

**Data reliability that runs inside your network.** Nullwake turns data contracts written in YAML into
scheduled freshness, volume, schema, null, uniqueness and custom-SQL checks against your warehouse. When a
contract breaks it opens an incident, tells the owning team, and closes the incident automatically once the
data is healthy again.

![Overview](docs/screenshots/overview.png)

## Why

Most data incidents are discovered by the person reading the dashboard. A loader stalls overnight, an upstream
team renames a column, a backfill doubles a day of payments, and revenue numbers are quietly wrong until
someone notices. Nullwake makes the producer's promises explicit and checks them continuously:

| Contract says | Nullwake checks | Typical cause it catches |
| --- | --- | --- |
| `freshness: {maxAgeMinutes: 90}` | age of `MAX(loaded_at)` | stalled or failed loads |
| `volume: {windowHours: 24}` | rows in the latest window vs. a robust baseline (median ± MAD) of previous windows | partial loads, duplicated backfills |
| `schema.columns` | `information_schema` vs. the contract | renamed, dropped or retyped columns |
| `nullable: false`, `unique: true`, `uniqueKey` | null counts and duplicate keys | broken joins, replayed events |
| `checks: [{sql: ...}]` | any single-`SELECT` business rule | negative amounts, orphan rows, bad codes |

## What it does

- **Contracts as code.** `Connection`, `DataContract` and `AlertChannel` YAML live in your repository, are reviewed
  in pull requests and are applied with `nullwake apply` or `POST /api/v1/config/apply`.
- **Read-only by construction.** Postgres checks run in `READ ONLY` transactions with a statement timeout;
  identifiers are validated, never interpolated from free text; custom SQL must be a single `SELECT`.
  Built-in checks read back counts and aggregates only; a custom SQL check returns one number chosen by its author, so
  review assertion SQL like any other code. No result rows are copied, and warehouse error text is reduced to its
  error class and SQLSTATE before it is stored or logged. If you enable Slack or webhook alerts, those messages
  carry check, dataset and owner names and the aggregate values; see [what leaves the network](docs/security.md#what-leaves-the-network).
- **Credentials stay in your environment.** Connections name environment variables (`dsnEnv: WAREHOUSE_DSN`) instead
  of holding credentials, and connection fields that look like inline secrets are rejected at validation time.
- **Incidents, not alert spam.** One active incident per failing check (enforced by a partial unique index),
  deduplicated repeats, a timeline of every change, acknowledgement and notes, and automatic resolution.
- **Reliable alerting.** Slack and signed webhooks (HMAC-SHA256) are queued in a transactional outbox and
  retried with exponential backoff, so an alert is never lost to a crash between "incident opened" and "sent".
- **Horizontally scalable worker.** A due check is claimed with an expiring, renewed lease and a fencing token
  (`FOR UPDATE SKIP LOCKED` keeps two workers off the same row). A crashed worker's checks are reclaimed when its
  lease expires, and a late result from a reclaimed run is discarded, so it can never resolve a newer incident.
- **Watches itself.** `/watchdog` returns 503 when no worker has reported in, checks are overdue or alert delivery
  is stuck, or checks cannot execute, for an external uptime monitor; an optional outbound ping
  (`NULLWAKE_DEADMAN_URL`) works as a dead man's switch and stops when any of those is unhealthy. A worker whose loop stalls exits and is restarted.
- **CI gate.** `nullwake check contracts/` runs every contract directly against the warehouse and exits
  non-zero on failure. No server is needed, so it drops into GitHub Actions after `dbt build`.
- **Operable.** `/healthz`, `/readyz` (database + migration state), Prometheus `/metrics`, JSON logs,
  Alembic migrations, audit log of every sign-in, config change, acknowledgement and token change.
- **Access control.** Viewer / editor / admin roles, Argon2id passwords, hashed session and API tokens,
  CSRF and origin checks, sign-in throttling, strict Content-Security-Policy (the UI ships no JavaScript).

## Architecture

```mermaid
flowchart LR
  subgraph customer["Customer network"]
    WH[(Warehouse<br/>Postgres · Snowflake · BigQuery)]
    subgraph nw["Nullwake (Docker Compose / any container platform)"]
      P[Caddy<br/>TLS] --> W[Web + REST API<br/>FastAPI]
      K[Worker replicas<br/>leased scheduler] -->|read-only role,<br/>aggregates only| WH
      W --> DB[(Metadata Postgres)]
      K --> DB
    end
    Git[Contracts repo] -->|nullwake apply / CI| W
  end
  K -->|outbox: Slack, signed webhooks| A[On-call]
  U[Data team] --> P
```

More detail: [docs/architecture.md](docs/architecture.md) · [docs/security.md](docs/security.md) ·
[docs/operations.md](docs/operations.md) · [docs/pilot.md](docs/pilot.md) · [decision records](docs/adr).

## Quickstart (live demo on your machine)

The demo profile runs a simulated retail warehouse that loads orders every minute and breaks on a schedule:
stalled loader, renamed column, duplicate load, null burst, volume drop, negative amounts. It then heals, so you can
watch incidents open, alert and resolve.

```sh
cp .env.example .env            # set POSTGRES_PASSWORD, DEMO_READER_PASSWORD, NULLWAKE_DOMAIN=localhost
sed -i 's/^NULLWAKE_DEMO_MODE=.*/NULLWAKE_DEMO_MODE=true/' .env
docker compose --profile demo up -d --build
docker compose exec web nullwake user you@example.com --role admin
open https://localhost          # Caddy issues a local certificate for "localhost"
```

## Start from your existing tables

`nullwake init` reads your table definitions and drafts one contract per table, so you start from something that
already runs instead of a blank file:

```sh
nullwake init connection.yaml --schema analytics --owner data-team@acme.com   # writes contracts/*.yaml
nullwake check connection.yaml contracts/                                       # try them against the warehouse
```

Columns, types, NOT NULL and declared primary keys come from the warehouse. A load-time column (`_loaded_at`,
`ingested_at`, `created_at`, ...) becomes the freshness and volume check. Limits only your team knows, such as how late
is too late, are marked TODO with conservative defaults.

Already on dbt? Point `init` at your manifest and the rules you wrote in dbt carry over:

```sh
dbt parse                                                                       # writes target/manifest.json
nullwake init connection.yaml --dbt target/manifest.json --select tag:finance
```

`unique` and `not_null` tests become column rules, `unique_combination_of_columns` and an incremental model's
`unique_key` become the contract's key, a source's `loaded_at_field` and `freshness.error_after` become the freshness
check (no TODO), and `meta.owner` or the model's group owner becomes the contract owner. Columns and types still come
from the warehouse, so undocumented columns are covered too. `--select` takes a model or source table name,
`tag:NAME` or `source:NAME`; filtered tests (`where:`) are left out because a contract checks the whole table.

## A data contract

```yaml
apiVersion: nullwake/v1
kind: DataContract
metadata:
  name: orders
  owner: commerce-data@example.com
spec:
  connection: warehouse
  dataset: analytics.fct_orders
  schedule: {intervalMinutes: 15}
  schema:
    allowAdditionalColumns: true
    columns:
      - {name: order_id, type: bigint, nullable: false}
      - {name: amount, type: numeric}
      - {name: loaded_at, type: timestamptz}
  uniqueKey: [order_id]
  freshness: {column: loaded_at, maxAgeMinutes: 90}
  volume: {column: loaded_at, windowHours: 24, tolerancePct: 50}
  checks:
    - name: no_negative_amounts
      sql: select count(*) from analytics.fct_orders where amount < 0
```

See [`demo/contracts`](demo/contracts) and [`examples/`](examples) (Snowflake, BigQuery, Slack, signed webhooks,
GitHub Actions).

## Use it in CI

```yaml
- run: dbt build
- run: nullwake check contracts/ --format json   # exits 1 if any contract fails
  env: {WAREHOUSE_DSN: "${{ secrets.WAREHOUSE_DSN }}"}
```

## Deploying into a customer environment

1. Create a read-only warehouse role ([SQL for Postgres, Snowflake and BigQuery](docs/security.md#read-only-warehouse-access)).
2. Put the DSN or key path in the environment, never in YAML.
3. `docker compose up -d --build` on a VM inside the customer's network (or run the image on their container
   platform; web and worker are stateless, and Postgres is the only state).
4. `nullwake apply contracts/` from their repository's CI; connect Slack or their incident tool via webhook.

Day-2 operations (upgrades, backups, restore drill, secret rotation, scaling workers) are in
[docs/operations.md](docs/operations.md).

## API

OpenAPI at `/api/openapi.json`. Authenticate with `Authorization: Bearer nw_…` tokens created by an admin.

| Method | Path | Role |
| --- | --- | --- |
| `GET` | `/api/v1/status`, `/contracts`, `/contracts/{name}`, `/checks`, `/checks/{id}/runs`, `/incidents`, `/incidents/{id}` | viewer |
| `POST` | `/api/v1/incidents/{id}/acknowledge`, `/checks/{id}/run` | editor |
| `POST` | `/api/v1/config/apply?dry_run=true` (YAML body) | editor (admin for connections/channels) |

## Development

```sh
pip install -e ".[dev]"
# PostgreSQL 14+ with two databases: one for Nullwake's tests, one acting as the warehouse
export NULLWAKE_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/nullwake_test
export NULLWAKE_TEST_WAREHOUSE_DSN="host=localhost dbname=warehouse user=postgres password=postgres"
pytest
```

The suite (98 tests) runs against real Postgres: checks against live tables, read-only and timeout enforcement,
incident lifecycle, `SKIP LOCKED` claiming under contention, signed webhook delivery and retry, RBAC, CSRF/origin
checks, throttling, and the CLI's CI exit codes.

## Status

Version 0.6, running in production at app.nullwake.si (product site: [nullwake.si](https://nullwake.si)). Postgres is fully tested end to end. Snowflake is verified
against a live Snowflake account (November 2026): all six check types pass on healthy data, and a planted duplicate
order and negative amount are both caught, using a key-pair service user and a read-only role. Snowflake checks also
run end to end in CI against the [fakesnow](https://github.com/tekumara/fakesnow) emulator. BigQuery is verified against a live project
(November 2026) with a read-only service account (`bigquery.dataViewer` + `bigquery.jobUser`): 6/6 checks pass on
healthy data and a planted duplicate and negative amount are both caught. Planned next: OIDC single sign-on, PagerDuty/Opsgenie channels, dbt `manifest.json` import to generate
contracts, a Helm chart, and per-team routing.

Built by [Akhil Pettem](https://www.linkedin.com/in/akhil-p-8bbab842a/).
