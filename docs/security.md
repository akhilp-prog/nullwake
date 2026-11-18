# Security model

## Data handling

- Nullwake stores **metadata and aggregates only**: row counts, ages, null counts, duplicate counts, column names and
  types, assertion results. No warehouse rows are copied to the metadata store, logs or alerts.
- Custom assertions must be a single `SELECT`/`WITH` statement returning one number. This lexical guard is defense
  in depth; the security boundary is the warehouse role below.

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
CREATE USER NULLWAKE_SVC DEFAULT_ROLE = NULLWAKE_READER RSA_PUBLIC_KEY = '…';
GRANT ROLE NULLWAKE_READER TO USER NULLWAKE_SVC;
```

Use a dedicated extra-small warehouse with auto-suspend so monitoring cost is visible and bounded. Sessions set
`STATEMENT_TIMEOUT_IN_SECONDS` and `QUERY_TAG = 'nullwake'`.

**BigQuery**: a service account with `roles/bigquery.dataViewer` on the datasets and `roles/bigquery.jobUser` on the
project. Every job carries `maximum_bytes_billed` (default 10 GiB; `maximumBytesBilled` in the connection spec) and
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
