# Changelog

## 0.8.1 (2026-11-27)

- Demo mode: `GET /demo` opens the demo dashboard directly from a link (for example the product site's "Live demo"
  button). It signs in only on a navigation the person made (`Sec-Fetch-User: ?1`, `Sec-Fetch-Dest: document`), so
  link previews, crawlers and embedded requests get the sign-in page instead (307), and it never replaces a session
  someone already has. Only the read-only demo account can be reached this way. The Visitors page counts these entries.

## 0.8.0 (2026-11-25)

- OpenTelemetry: optional traces and metrics over OTLP/HTTP (SigNoz, Grafana, Honeycomb, Datadog, any Collector).
  Each check run is a `nullwake.check` span with children for the warehouse query and the result write; alert
  deliveries and web requests are traced; counters for runs, incidents and alert deliveries and a run-duration
  histogram. Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set; `NULLWAKE_OTEL=false` forces it off. Spans carry names,
  statuses and durations only, never check messages, values, SQL or credentials. New `otel` extra, included in the
  Docker image. See docs/operations.md, "Traces and metrics".

## 0.7.1 (2026-11-25)

- Demo mode: one-click "Enter the live demo" button on the sign-in page (`POST /demo`). It signs in only to the
  configured demo account, refuses unless that account is a viewer, and keeps the same origin check and throttle as
  `/login`. Demo sessions last at most 8 hours. The demo password is no longer shown on the page.
- Visitors page counts one-click demo entries as demo sign-ins.

## 0.7.0 (2026-11-20)

Least privilege, after an external security review. Breaking for existing deployments: see docs/operations.md,
"Upgrading to 0.7".

- **Custom SQL is its own permission.** New or changed custom SQL checks (or moving one to another connection)
  through the API need the admin role by default (`NULLWAKE_SQL_AUTHOR_ROLE`). Editors can still re-apply approved
  SQL, so CI with an editor token keeps working. Dry runs list the SQL changes.
- **`prune` needs admin**, and a dry run lists exactly which contracts it would retire.
- **Per-service credentials.** web and worker connect as `nullwake_app` (rows only: no DDL, no superuser, no other
  databases), provisioned by `nullwake migrate`; only migrate uses the owner account. No container receives `.env`;
  warehouse and alert credentials live in `worker.env`, which only the worker gets.
- **API tokens expire** (30, 90 or 365 days; existing tokens get 90 days) and are revoked when their creator is
  demoted below the token's role.
- One-row checks fetch at most two rows (a server-side cursor on Postgres, bounded fetches on Snowflake and BigQuery),
  so a query returning millions of rows no longer loads them into the worker.
- Only well-formed SQLSTATE codes are copied into stored error text.
- The worker warns about remote Postgres DSNs without `sslmode=verify-full`.
- `docs/security.md`: what each service receives, warehouse transport, and what is stored, who sees it and for how long.

## 0.6.6 (2026-11-20)

Security review (access matrix, bandit, pip-audit, semgrep, abuse cases). Fixed:

- API tokens are accepted on `/api/v1` only. Before, an admin token could also use the web UI, so a leaked token
  could mint a replacement token that survived revoking the original.
- Contracts can no longer reference `NULLWAKE_*` environment variables, and Postgres connections refuse to run
  inside Nullwake's own metadata database. Before, someone allowed to apply YAML, with an environment variable
  holding a DSN for that database, could run counting queries against users and sessions.
- `nullwake init --dbt` validates table names from the manifest before writing them into SQL checks, and quotes
  BigQuery names (project IDs with hyphens previously produced invalid SQL).

Added `tests/test_access_matrix.py`: every route against anonymous, forged-token, viewer, editor and admin callers.

## 0.6.5 (2026-11-20)

- `nullwake init --dbt` now carries over `accepted_values` (as a SQL check listing the allowed values, quotes escaped)
  and `relationships` (a left-join orphan check against the referenced model), and gives seeds no freshness or volume
  check. Verified on dbt Labs' jaffle_shop built with dbt-postgres: 8 contracts, all dbt tests carried over, and an
  orphaned, wrongly-statused order is caught.

## 0.6.4 (2026-11-20)

- `nullwake init --dbt target/manifest.json`: drafts contracts for dbt models, snapshots, seeds and sources. dbt's
  `unique` / `not_null` tests, `dbt_utils.unique_combination_of_columns`, incremental `unique_key`, source
  `loaded_at_field` + `freshness` and `meta.owner` / group owners carry over; columns and types still come from the
  warehouse. `--select` narrows by name, `tag:` or `source:`. Ephemeral and disabled models and filtered tests are
  skipped; tests on columns the table lacks, and models not yet built, are reported.

## 0.6.3 (2026-11-20)

- `nullwake init`: drafts one contract per table from the warehouse's own definitions (columns, types, NOT NULL,
  declared primary keys) for a whole schema (`--schema`) or chosen tables (`--table`), on Postgres, Snowflake and
  BigQuery. It picks a load-time column for freshness and volume (preferring `_loaded_at`-style columns over
  `created_at`, and skipping volume for `updated_at`), marks limits only the team can set as TODO with conservative
  defaults, and never overwrites a file without `--force`. Drafted contracts pass `nullwake check` unchanged.
- Fix: the dead man's switch skipped its first ping on a host that had been up for under a minute.
- BigQuery verified against a live project (read-only service account); test script in `examples/`.

## 0.6.2 (2026-11-19)

- Product website in `site/`: a static page with no JavaScript, cookies or third-party requests, served by the bundled
  Caddy at `NULLWAKE_SITE_DOMAIN` under a strict Content-Security-Policy, with `NULLWAKE_SITE_REDIRECT_FROM` for
  `www`. The Caddy access-log filter is now a shared snippet, so the site is logged with the same privacy filtering.
- Visitors page: pages from other hostnames in the access log (the website) are listed with their host, site assets
  are not counted as page views, and only the app's own sign-ins count as demo sign-ins.

## 0.6.1 (2026-11-19)

Fixes from a second external review (of 0.6.0), each with a regression test.

- A check that is removed and then re-added no longer shows its old result for up to a full interval: it shows
  pending and runs at once.
- `freshness.maxFutureSkewMinutes` is now read from contract YAML (it was silently dropped).
- BigQuery contracts accept three-part datasets with a hyphenated project (`acme-analytics.sales.orders`).
- "Run now" on a retired or disabled check returns 409 instead of falsely confirming a run.
- Bare Postgres table names in a mixed-case (quoted) schema resolve to the right columns.

## 0.6.0 (2026-11-19)

Fixes from an external review of 0.5.0, each with a regression test. Theme: no false green.

- The Docker image now includes the Snowflake and BigQuery drivers (previously the stock image could only run Postgres).
- BigQuery project IDs with hyphens (all real ones, e.g. `acme-analytics`) are accepted; schema lookups now carry the
  same billing cap and label as check queries.
- A custom check that returns NULL or a non-finite number is an error, never a pass that resolves an incident.
- Freshness fails when the newest timestamp is in the future beyond `maxFutureSkewMinutes` (default 5), so one
  bad row cannot hide a stopped load; the current volume window now ends at the current time.
- Removing a check or contract retires it instead of deleting it: runs, incidents and queued alerts are kept, open
  incidents are closed with a note, and re-adding the check revives it with its history (migration 0004).
- Changing a connection's settings under the same name discards any run in flight against the old settings and
  reruns its checks at once.
- Schema baselines now detect the loss of an observed NOT NULL constraint. Bare Postgres table names are looked up
  through the session's `search_path`, the same table the checks query.
- Visitors page: a malformed `Referer` no longer breaks the report; referrer query strings and forwarded-IP headers
  are dropped from the access log before it reaches disk.
- Raw warehouse error text is no longer logged by default (`NULLWAKE_LOG_WAREHOUSE_ERRORS=1` to opt in while
  debugging); README and security docs now describe exactly what custom SQL can return and who can write it.

## 0.5.0 (2026-11-19)

- Snowflake connector hardened for pilots: sessions now run in UTC (Snowflake defaults to America/Los_Angeles, which
  shifted freshness ages on `TIMESTAMP_NTZ` columns by the UTC offset), connect with login and network timeouts,
  support encrypted private keys (`privateKeyPassphraseEnv`) and a default `schema`, and redact errors from schema
  lookups as well as checks.
- Fixed: a bare table name on Snowflake looked up columns with the database name as the schema.
- Snowflake checks now run end to end in the test suite against the fakesnow emulator, and were verified against a
  live Snowflake account: 6/6 pass on healthy data; a planted duplicate and a negative amount both fail as expected.

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
