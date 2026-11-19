# Nullwake pilot guide

A pilot runs Nullwake for 30 days inside one team's network, on their most important tables, so both sides can see
whether it catches problems that matter. This page covers the terms, what to prepare, the setup call, and how the
pilot ends. Security reviewers should start with [security.md](security.md) or https://nullwake.si/security.

## Pilot terms

Plain-language terms for a design-partner pilot. They are a starting point; if your company needs its own paperwork,
send it over.

1. **Length and cost.** 30 days from the setup call, free of charge.
2. **Where it runs.** On the customer's infrastructure, under the customer's control. Nullwake (the vendor) gets no
   access to the warehouse, the deployment or its data unless the customer grants it, for example by sharing a screen.
3. **Data.** Nullwake reads aggregates only, as described in [security.md](security.md). No customer data is sent to
   the vendor. Any information shared during the pilot (table names, incidents, screenshots) is kept confidential and
   used only to run and improve the pilot.
4. **Software.** Open source under Apache 2.0. The customer may keep running it after the pilot at no cost.
5. **Support.** A setup call, a 30-minute weekly review, and email support on working days (India time, with overlap
   for European and US mornings).
6. **No obligation.** Either side can stop at any time. Before day 30 the vendor sends a written summary and, if the
   customer wants to continue, a quote for a yearly subscription (setup help, support and priority fixes). There is no
   commitment to buy.
7. **Feedback.** The customer agrees to share honest feedback in the weekly reviews. Nothing about the customer is
   published (no logo, name or quote) without written permission.
8. **As is.** Nullwake is early software provided as is, without warranty. It reads only; it cannot change customer
   data.

## Before the setup call (customer checklist)

- [ ] A Linux VM inside the network (2 vCPU, 4 GB RAM, 20 GB disk is enough for a pilot) with Docker and Docker
      Compose, or a container platform where you can run three containers and a Postgres database.
- [ ] Network access from that VM to the warehouse, and to Slack or your webhook endpoint for alerts.
- [ ] A read-only warehouse role created with the SQL in [security.md](security.md#read-only-warehouse-access),
      limited to the tables below.
- [ ] A list of the 5 to 10 tables that matter most, each with: owner (team or email), how often it loads, and how
      late is "too late".
- [ ] An alert destination: a Slack channel (incoming webhook URL) or an HTTPS webhook.
- [ ] Optional: a DNS name and TLS for the web interface, or plan to reach it over your VPN.

## Setup call (about 60 minutes)

| Step | Who | Time |
| --- | --- | --- |
| 1. Deploy: `cp .env.example .env`, set secrets, `docker compose up -d` | Customer engineer, vendor guiding | 15 min |
| 2. Create the admin user: `docker compose exec web nullwake user you@company.com --role admin` | Customer | 2 min |
| 3. Add the connection (credentials as environment variables, never in YAML) | Customer | 5 min |
| 4. Write contracts for the first 3 tables together, then `nullwake check contracts/` to see them run | Both | 20 min |
| 5. Apply contracts: `nullwake apply contracts/` | Customer | 2 min |
| 6. Connect alerts and send a test by temporarily tightening one freshness limit | Both | 10 min |
| 7. Set up a watcher for Nullwake itself: an uptime check on `/watchdog` | Customer | 5 min |

Contracts for the remaining tables are added during week 1, ideally in the customer's Git repository and applied from
CI ([example](../examples/github-actions-contract-check.yml)).

## During the pilot

**Weekly 30-minute review**, using the incidents page:

- What fired? For each incident: real problem, expected change, or noise.
- What did it miss? Any data problem found another way in the past week.
- Tune: thresholds, schedules, which columns are checked.
- Measure: check durations and warehouse cost (Snowflake `QUERY_TAG = 'nullwake'`, BigQuery label `tool=nullwake`).

**Success criteria**, agreed on the setup call. Suggested:

- At least one real data problem caught before a person noticed it, or confidence that none occurred.
- Fewer than one false alarm per table per week after week 1 tuning.
- Warehouse cost of checks acceptable to the customer.
- Setup and contract writing feel reasonable for the customer's team to own.

## Ending the pilot

- **Continue:** keep the deployment as is; discuss the subscription quote.
- **Stop:** `docker compose down -v` removes Nullwake and its database; drop the warehouse role and Slack webhook.
  Nothing remains outside the customer's network, because nothing was ever sent there.
- Either way the vendor sends a short written summary: what was caught, what was tuned, open issues.

Contact: hello@nullwake.si
