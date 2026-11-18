# ADR 0002: Postgres `SKIP LOCKED` instead of a message broker

**Status:** accepted · **Date:** 2026-11-18

**Context.** Checks run on intervals; several worker replicas must share the load without running a check twice.
Customer deployments should need as few moving parts as possible.

**Decision.** The metadata Postgres is the queue. Workers claim due checks with
`SELECT … FOR UPDATE SKIP LOCKED`, take an expiring lease on each (owner + expiry), commit, run the check, then set
`next_run_at` and release the lease in the transaction that records the result. Alert deliveries use the same
`SKIP LOCKED` pattern as a transactional outbox.

*Amended for v0.2.* v0.1 advanced `next_run_at` at claim time. A review showed that a worker crash then skipped the
claimed run until the next interval, and a run longer than its interval could be started again by another worker.
Leases close both gaps; delivery remains at-least-once (see architecture.md).

*Amended for v0.3.* A second review showed that v0.2 still recorded a result from a worker that had lost its lease,
so a late "pass" could resolve an incident opened by a newer run. Each claim now carries a random token, and a result
is recorded only if the row still carries that token (fencing). Leases are renewed during a run and use the database
clock; "Run now" is a counter instead of a timestamp comparison.

**Consequences.** No Redis/Kafka/Celery to operate or secure. Throughput is ample for thousands of checks per minute,
far beyond a single team's needs. If that ceiling is reached, the claim function is the only seam to replace.
