# ADR 0002: Postgres `SKIP LOCKED` instead of a message broker

**Status:** accepted · **Date:** 2026-11-18

**Context.** Checks run on intervals; several worker replicas must share the load without running a check twice.
Customer deployments should need as few moving parts as possible.

**Decision.** The metadata Postgres is the queue. Workers claim due checks with
`SELECT … FOR UPDATE SKIP LOCKED`, advance `next_run_at`, commit, then run the check. Alert deliveries use the same
pattern as a transactional outbox.

**Consequences.** No Redis/Kafka/Celery to operate or secure. Throughput is ample for thousands of checks per minute,
far beyond a single team's needs. If that ceiling is reached, the claim function is the only seam to replace.
