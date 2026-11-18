# Operations runbook

All commands run from the deployment directory on the host.

## First deployment

```sh
cp .env.example .env               # set NULLWAKE_DOMAIN, POSTGRES_PASSWORD, warehouse/alert env vars
docker compose config --quiet      # validate
docker compose up -d --build
docker compose ps                  # migrate exits 0; web is healthy
docker compose exec web nullwake user admin@example.com --role admin     # prompts for the password
docker compose exec web nullwake apply /path/in/container/contracts      # or POST /api/v1/config/apply from CI
curl -fsS https://$NULLWAKE_DOMAIN/healthz
```

DNS: an `A` record for `NULLWAKE_DOMAIN` pointing at the host; inbound TCP 80/443 open for Let's Encrypt.

## Health

| Signal | Where | Healthy |
| --- | --- | --- |
| Liveness | `GET /healthz` | `200 ok` |
| Readiness | `GET /readyz` (internal) | `ready: true`, migration at head |
| Scheduler | Overview badge, `/settings`, `nullwake_worker_heartbeat_age_seconds` | < 120 s |
| Overdue checks | `/watchdog`, `nullwake_checks_overdue` | 0 |
| Alert queue | `/watchdog`, `nullwake_alert_deliveries_stalled`, `nullwake_alert_deliveries{status="failed"}` | 0 |
| Worker container | `docker compose ps worker` (runs `nullwake worker-health`) | `healthy` |
| Logs | `docker compose logs -f web worker` | JSON, one line per event |

Suggested Prometheus alerts: worker heartbeat age > 300 s; `nullwake_checks_overdue > 0`;
`nullwake_alert_deliveries_stalled > 0`; `nullwake_incidents_active{severity="critical"} > 0` for 30 m without
acknowledgement.

### Watching the watcher

Nullwake's own alerts go out through the worker, so they cannot tell you that the worker has stopped. Set up at
least one of these, alerting through a channel that does not depend on Nullwake:

- **External uptime check on `/watchdog`.** It returns `200` with `ok: true`, or `503` with the problems: no worker
  heartbeat (older than the lease + 180 s), checks overdue by more than `NULLWAKE_WATCHDOG_GRACE_SECONDS` (default
  300) with no live lease, or alerts pending for over 15 minutes or failed in the last day. It reports counts only,
  no check, dataset or channel names, and the bundled proxy serves it. Point Google Cloud Monitoring uptime checks,
  UptimeRobot or Pingdom at it.
- **Outbound dead man's switch.** Set `NULLWAKE_DEADMAN_URL` to a ping URL (e.g. healthchecks.io); each worker GETs it
  about once a minute, and the service alerts when the pings stop. Works without exposing anything inbound.

Drill: `docker compose stop worker`, confirm the external alert arrives (within the lease + 180 s plus the monitor's
own interval), then `docker compose start worker` and confirm it clears.

## Upgrades

```sh
git pull                          # or unpack the new release
docker compose build
docker compose up -d              # migrate runs before web/worker start
docker compose exec web curl -fsS localhost:8000/readyz
```

Migrations are forward-only in production; take a backup first.

## Backups

The metadata database holds contracts, run history, incidents, users and the audit log. Contracts also live in Git, so a
lost database is recoverable but loses history; back it up daily.

```sh
umask 077
docker compose exec -T db pg_dump -U nullwake -Fc nullwake > "nullwake-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Copy dumps to encrypted off-host storage. `pg_dump` is consistent while the app is running.

## Restore (and the drill)

Run the drill quarterly on a separate host or Compose project:

```sh
docker compose stop web worker
docker compose exec -T db dropdb -U nullwake nullwake
docker compose exec -T db createdb -U nullwake nullwake
docker compose exec -T db pg_restore -U nullwake -d nullwake --no-owner < nullwake-YYYYmmddTHHMMSSZ.dump
docker compose exec -T db psql -U nullwake -d nullwake -c "DELETE FROM sessions"   # force fresh sign-ins
docker compose up -d
```

Verify: `/readyz` is ready at the expected migration, row counts for `contracts`, `monitors`, `incidents` match the
source, an admin can sign in, and incident history is visible. (Verified for v0.1: a dump of a live instance restored
into a separate database served identical counts and history.)

## Scaling

- More checks or slower warehouses: `docker compose up -d --scale worker=3`. Each run is protected by a lease, so
  replicas do not start a check that another replica is running; a check is reclaimed only after its lease expires
  (`NULLWAKE_WORKER_LEASE_SECONDS`, default 3 × query timeout + 60 s). Tune `NULLWAKE_WORKER_BATCH_SIZE` and `NULLWAKE_QUERY_TIMEOUT_SECONDS`.
- Run history grows with `checks × runs/day`; it is pruned hourly after `NULLWAKE_RETENTION_DAYS` (default 30).

## Common tasks

| Task | Command |
| --- | --- |
| Reset a password / change role | `docker compose exec web nullwake user EMAIL --role editor` (revokes sessions) |
| Validate contracts without applying | `nullwake apply contracts/ --dry-run` |
| Remove contracts deleted from Git | `nullwake apply contracts/ --prune` |
| Run one contract now from CI | `nullwake check contracts/ --contract orders` |
| Rotate a webhook signing secret | update `.env`, `docker compose up -d worker` |
| Accept an intended schema change (baseline-mode check) | check page → "Accept new schema" (audited) |

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `NULLWAKE_DATABASE_URL` | required | metadata Postgres (`postgresql+psycopg://…`) |
| `NULLWAKE_PUBLIC_URL` | `http://localhost:8000` | origin check, secure cookies, links in alerts |
| `NULLWAKE_SESSION_HOURS` | 8 | browser session lifetime |
| `NULLWAKE_WORKER_POLL_SECONDS` | 5 | idle poll interval |
| `NULLWAKE_WORKER_BATCH_SIZE` | 10 | checks claimed per pass |
| `NULLWAKE_QUERY_TIMEOUT_SECONDS` | 60 | warehouse statement timeout |
| `NULLWAKE_RETENTION_DAYS` | 30 | check-run history kept |
| `NULLWAKE_CLIENT_IP_HEADER` | unset | trusted proxy header for client IP (see security.md) |
| `NULLWAKE_DEMO_MODE` / `_USER` / `_PASSWORD` | off | show read-only demo credentials on the sign-in page |
| `NULLWAKE_WORKER_LEASE_SECONDS` | 3 × query timeout + 60 | how long a claimed run is protected before another worker may reclaim it |
| `NULLWAKE_WATCHDOG_GRACE_SECONDS` | 300 | how late a check may be before `/watchdog` reports it overdue |
| `NULLWAKE_DEADMAN_URL` | unset | URL each worker GETs about once a minute (dead man's switch) |
| `NULLWAKE_LOG_LEVEL` | INFO | log level |
