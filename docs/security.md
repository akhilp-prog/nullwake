# Security model

## Data handling

- Nullwake stores **metadata and aggregates only**: row counts, ages, null counts, duplicate counts, column names and
  types, assertion results. Built-in checks never read rows back; a custom assertion returns one number its author
  chooses (see the limits below). Warehouse error text is reduced to its error class and SQLSTATE before it is stored,
  alerted on or logged; set `NULLWAKE_LOG_WAREHOUSE_ERRORS=1` only while debugging, because raw driver messages can
  quote values.
- **Who can write custom SQL.** Anyone with the editor role can apply a contract, and so can run any single `SELECT`
  that the connection's warehouse role allows. Treat editors as trusted warehouse readers and keep the role's grants
  to the contracted tables.
- Query load: checks are read-only but still use warehouse resources. Grant `SELECT` on the contracted tables only
  (not whole schemas) where you can, give Nullwake its own small warehouse or resource group, and measure check
  durations (`check_runs.duration_ms`) against a representative workload before widening the rollout.
- Custom assertions must be a single `SELECT`/`WITH` statement returning one number. This lexical guard is defense
  in depth; the security boundary is the warehouse role below.
- **Access logs.** Caddy keeps an access log for visitor statistics. It is filtered before it reaches disk: client IPs are masked to /24 (IPv4) or /48 (IPv6), query strings are removed from the request and from the `Referer`, forwarded-IP headers and `Cookie`, `Authorization`,
CSRF and `Set-Cookie` headers are dropped. The log keeps paths, timestamps and user agents. Rotated log files are
deleted after 30 days; the active file rotates at 10 MiB, so on a quiet instance it can hold older entries until then.

## What leaves the network

Nullwake makes these outbound connections, and no others:

| Connection | Sends | When |
| --- | --- | --- |
| Your warehouse | Generated read-only queries; receives aggregates and column metadata | Always (a hosted warehouse such as Snowflake or BigQuery is outside your network) |
| Each alert channel (Slack or your webhook URL) | The fields below | Only if you configure channels |
| `NULLWAKE_DEADMAN_URL` | A plain GET with no body | Only if you set it |

Each alert channel receives:

| Field | Example |
| --- | --- |
| Check, contract and dataset names | `orders.freshness`, `analytics.fct_orders` |
| Contract owner | `commerce-data@retail.example` |
| Severity, status, timestamps | `critical`, `opened`, `2026-11-18T09:29:00Z` |
| Check message with aggregate values | `Data is 3 h old (limit 20 min)`, `412 rows, below the expected 6,500–13,500` |
| Link back to the incident | `https://nullwake.internal/incidents/42` |

No warehouse rows or credentials are sent. Two limits on that guarantee:

- **Warehouse error text is redacted.** Databases often quote the offending value in an error (for example
  `invalid input syntax for type integer: "4111-…"`). Nullwake stores and alerts only the error class, a fixed
  description and the SQLSTATE (e.g. `query failed: data exception (SQLSTATE 22P02)`); the full text is written only
  to the worker's local log.
- **Custom assertions are trusted to return an aggregate.** An assertion's single number appears in its message. A
  query such as `SELECT max(card_number) …` would put a real value in an alert, so review assertion SQL in pull
  requests like any other code: it must return a count or other approved aggregate.

If dataset or owner names are themselves sensitive, use a webhook to a receiver inside your network instead of Slack.
The `/watchdog` endpoint exposes counts only.

## Read-only warehouse access

Grant Nullwake the least privilege that lets it read the contracted tables.

**Postgres**

```sql
CREATE ROLE nullwake_reader LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA analytics TO nullwake_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO nullwake_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO nullwake_reader;
ALTER ROLE nullwake_reader SET default_transaction_read_only = on;
```

Nullwake additionally opens every session `READ ONLY` with `SET LOCAL statement_timeout`.

**Snowflake** (key-pair authentication recommended)

```sql
CREATE ROLE NULLWAKE_READER;
GRANT USAGE ON WAREHOUSE MONITORING_XS TO ROLE NULLWAKE_READER;
GRANT USAGE ON DATABASE ANALYTICS TO ROLE NULLWAKE_READER;
GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE NULLWAKE_READER;
GRANT SELECT ON ALL TABLES IN DATABASE ANALYTICS TO ROLE NULLWAKE_READER;
GRANT SELECT ON FUTURE TABLES IN DATABASE ANALYTICS TO ROLE NULLWAKE_READER;
CREATE USER NULLWAKE_SVC TYPE = SERVICE DEFAULT_ROLE = NULLWAKE_READER RSA_PUBLIC_KEY = '…';
GRANT ROLE NULLWAKE_READER TO USER NULLWAKE_SVC;
```

Use a dedicated extra-small warehouse with auto-suspend so monitoring cost is visible and bounded. Sessions set
`STATEMENT_TIMEOUT_IN_SECONDS`, `QUERY_TAG = 'nullwake'` and `TIMEZONE = 'UTC'`, and connect with a 30-second login
timeout. `TIMESTAMP_NTZ` columns are therefore read as UTC; Snowflake's own default (America/Los_Angeles) would shift
every freshness age by the UTC offset. If a table stores local wall-clock time in `TIMESTAMP_NTZ`, use a
`TIMESTAMP_TZ` column or an assertion instead. Snowflake has no read-only session mode, so the role above is the
boundary: never point Nullwake at a role that owns or can write to the tables.

**BigQuery**: a service account with `roles/bigquery.dataViewer` on the datasets and `roles/bigquery.jobUser` on the
project. Every query job, including schema lookups, carries `maximum_bytes_billed` (default 10 GiB; `maximumBytesBilled` in the connection spec) and
the label `tool=nullwake`.

## Secrets

- Connection and channel YAML may only *reference* environment variables (`dsnEnv`, `passwordEnv`,
  `privateKeyFileEnv`, `urlEnv`, `signingSecretEnv`). Keys that look like inline secrets are rejected.
- Rotate by updating the environment and restarting the worker; no database change is needed.

## Application security

| Control | Implementation |
| --- | --- |
| Passwords | Argon2id (`argon2-cffi`), 14–256 characters, constant-time verification with a dummy hash for unknown users |
| Sessions | 256-bit random tokens, stored as SHA-256 hashes, `HttpOnly; SameSite=Lax; Secure` (when served over HTTPS), 8 h lifetime, revoked on password/role change |
| CSRF | per-session token on every form + exact `Origin` match on every cookie-authenticated write |
| API tokens | `nw_<prefix>_<secret>`, stored hashed, role-scoped, revocable, last-used tracking |
| Authorization | viewer (read) · editor (acknowledge, resolve, run, apply contracts) · admin (connections, channels, tokens, audit) |
| Brute force | 10 failed sign-ins per IP and per account per 15 minutes |
| Headers | strict CSP (`default-src 'none'`, no scripts), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, HSTS over HTTPS, `no-store` |
| Webhooks | `X-Nullwake-Signature: sha256=HMAC(secret, "<timestamp>.<body>")` with `X-Nullwake-Timestamp`; receivers should reject timestamps older than 5 minutes |
| Audit | sign-ins, config applies, acknowledgements, resolutions, notes, schema acceptance, token create/revoke |
| Containers | non-root user, read-only root filesystem, all capabilities dropped, `no-new-privileges` |

### Client IP for throttling

Throttling uses the TCP peer address unless `NULLWAKE_CLIENT_IP_HEADER` is set. The bundled Caddy config sets
`X-Nullwake-Client` from the real peer address. Only set this variable when Nullwake is reachable *exclusively*
through a proxy that overwrites the header.

## Known limitations (v0.1)

- Local accounts only; OIDC SSO is on the roadmap. Put Nullwake behind your identity-aware proxy if SSO is required today.
- Single organization per deployment (no multi-tenancy).
- `/metrics` and `/readyz` are unauthenticated and blocked at the bundled proxy; expose them only on internal networks.
- Single host as bundled: no high availability. Agree recovery targets with each customer, and test an encrypted
  off-host restore and a failed-upgrade rollback before production.
