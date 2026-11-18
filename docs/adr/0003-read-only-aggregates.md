# ADR 0003: Compute in the warehouse, keep only aggregates

**Status:** accepted · **Date:** 2026-11-18

**Context.** Security reviews of monitoring tools focus on what data leaves the warehouse and what the tool can change.

**Decision.** Every check is a generated aggregate query (or a single-`SELECT` assertion) run under a read-only role,
in a read-only transaction where the engine supports it, with a statement timeout. Only aggregates and column metadata
are stored. Secrets are referenced by environment variable name and never persisted.

**Consequences.** Nullwake cannot show sample failing rows. That is a deliberate trade for a simpler security review; users follow
the incident link to their own warehouse tools to investigate.
