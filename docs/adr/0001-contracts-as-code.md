# ADR 0001: Contracts are configuration-as-code, not UI forms

**Status:** accepted · **Date:** 2026-11-18

**Context.** Data contracts are agreements between producing and consuming teams. In customer environments they need
review, history and rollback, and they must be deployable by the customer's own CI without a person in the loop.

**Decision.** Connections, contracts and alert channels are YAML documents (`apiVersion: nullwake/v1`) applied with
`nullwake apply` or `POST /api/v1/config/apply`. The UI is read-mostly: it shows contracts, their source and history,
and handles incident response. The stored source and SHA-256 make every applied version auditable.

**Consequences.** Changes go through pull requests; `--dry-run` validates in CI; `nullwake check` reuses the same
files to gate deployments. Editing a contract in the UI is intentionally not supported.
