"""Simulated retail warehouse for the Nullwake demo.

Loads orders and payments continuously into Postgres and injects realistic failures
(stalled loader, schema change, duplicate load, NULL burst, volume drop, bad amounts) on a
schedule, then heals them. All data is synthetic.

    python demo/simulator.py init         # create schema, read-only role, 14 days of history
    python demo/simulator.py run          # load every minute and cycle failure scenarios
    python demo/simulator.py break NAME   # force a scenario now
    python demo/simulator.py heal         # end the current scenario
    python demo/simulator.py status       # exit non-zero if not initialised

Environment: DEMO_WAREHOUSE_ADMIN_DSN (owner), DEMO_READER_PASSWORD (for the nullwake_reader role).
"""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql

ORDERS_PER_HOUR = 80
SCENARIOS = ("stalled_loader", "schema_change", "duplicate_load", "null_burst", "volume_drop", "negative_amounts")
HEALTHY_MINUTES = 40
# How long each failure lasts before the simulated on-call engineer fixes it. Freshness and volume
# need longer windows to cross their thresholds.
INCIDENT_MINUTES = {"stalled_loader": 35, "volume_drop": 40}
DEFAULT_INCIDENT_MINUTES = 15
COUNTRIES = ("IN", "SI", "DE", "US", "GB", "SG", "NL")
METHODS = ("card", "upi", "bank_transfer", "wallet")


def now() -> datetime:
    return datetime.now(timezone.utc)


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DEMO_WAREHOUSE_ADMIN_DSN"], autocommit=True)


def init(conn: psycopg.Connection) -> None:
    reader_pw = os.environ.get("DEMO_READER_PASSWORD")
    conn.execute("""
        CREATE SCHEMA IF NOT EXISTS analytics;
        DROP TABLE IF EXISTS analytics.fct_orders, analytics.stg_payments, analytics.dim_customers, analytics.sim_state;
        CREATE TABLE analytics.dim_customers (
            customer_id bigint PRIMARY KEY, email text, country text NOT NULL, segment text NOT NULL,
            updated_at timestamptz NOT NULL);
        CREATE TABLE analytics.fct_orders (
            order_id bigint, customer_id bigint, amount numeric(12,2), currency text, status text,
            created_at timestamptz, loaded_at timestamptz);
        CREATE TABLE analytics.stg_payments (
            payment_id bigint, order_id bigint, amount numeric(12,2), method text, paid_at timestamptz,
            loaded_at timestamptz);
        CREATE TABLE analytics.sim_state (id int PRIMARY KEY, scenario text, since timestamptz,
            next_order_id bigint, last_load timestamptz);
    """)
    rng = random.Random(42)
    with conn.cursor() as cur:
        with cur.copy("COPY analytics.dim_customers FROM STDIN") as copy:
            for cid in range(1, 5001):
                copy.write_row((cid, f"customer{cid}@example.com", rng.choice(COUNTRIES),
                                rng.choice(("smb", "mid", "enterprise")), now() - timedelta(days=rng.randint(1, 400))))
    start = now() - timedelta(days=14)
    next_id = load(conn, start, now(), 1, rng)
    conn.execute("INSERT INTO analytics.sim_state VALUES (1, 'healthy', now(), %s, now())", (next_id,))
    if reader_pw:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nullwake_reader'").fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(f"{verb} ROLE nullwake_reader LOGIN PASSWORD {sql.Literal(reader_pw).as_string(conn)}")
        conn.execute("GRANT USAGE ON SCHEMA analytics TO nullwake_reader; "
                     "GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO nullwake_reader; "
                     "ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO nullwake_reader; "
                     "REVOKE SELECT ON analytics.sim_state FROM nullwake_reader;")
    print(f"initialised demo warehouse with {next_id - 1:,} orders")


def load(conn, start: datetime, end: datetime, next_id: int, rng: random.Random, rate: float = 1.0,
         scenario: str = "healthy") -> int:
    """Insert orders and payments with created_at between start and end at ORDERS_PER_HOUR * rate."""
    seconds = (end - start).total_seconds()
    expected = ORDERS_PER_HOUR * rate * seconds / 3600
    n = max(int(rng.gauss(expected, max(expected, 1) ** 0.5 * 0.5)), 0) if expected >= 1 else int(rng.random() < expected)
    if n == 0:
        return next_id
    stamps = sorted(start + timedelta(seconds=rng.uniform(0, seconds)) for _ in range(n))
    loaded = now()
    orders, payments = [], []
    for ts in stamps:
        amount = round(rng.lognormvariate(3.6, 0.7), 2)
        customer = rng.randint(1, 5000)
        if scenario == "null_burst" and rng.random() < 0.4:
            customer = None
        if scenario == "negative_amounts" and rng.random() < 0.2:
            amount = -amount
        orders.append((next_id, customer, amount, "EUR", rng.choice(("paid", "paid", "paid", "refunded")), ts,
                       min(loaded, ts + timedelta(minutes=rng.uniform(1, 6)))))
        payments.append((next_id * 7, next_id, amount, rng.choice(METHODS), ts, loaded))
        if scenario == "duplicate_load" and rng.random() < 0.3:
            payments.append((next_id * 7, next_id, amount, rng.choice(METHODS), ts, loaded))
        next_id += 1
    with conn.cursor() as cur:
        with cur.copy("COPY analytics.fct_orders FROM STDIN") as copy:
            for row in orders:
                copy.write_row(row)
        pay_cols = "payment_id, order_id, amount_eur, method, paid_at, loaded_at" if _renamed(conn) else \
            "payment_id, order_id, amount, method, paid_at, loaded_at"
        with cur.copy(f"COPY analytics.stg_payments ({pay_cols}) FROM STDIN") as copy:
            for row in payments:
                copy.write_row(row)
    return next_id


def _renamed(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='analytics' "
                             "AND table_name='stg_payments' AND column_name='amount_eur'").fetchone())


def state(conn) -> tuple[str, datetime, int, datetime]:
    return conn.execute("SELECT scenario, since, next_order_id, last_load FROM analytics.sim_state WHERE id = 1").fetchone()


def set_scenario(conn, name: str) -> None:
    current = state(conn)[0]
    if current == "schema_change" and name != "schema_change" and _renamed(conn):
        conn.execute("ALTER TABLE analytics.stg_payments RENAME COLUMN amount_eur TO amount")
    if name == "schema_change" and not _renamed(conn):
        conn.execute("ALTER TABLE analytics.stg_payments RENAME COLUMN amount TO amount_eur")
    if name == "healthy":
        # Repair data damage the way an on-call engineer would after the incident.
        conn.execute("DELETE FROM analytics.fct_orders WHERE customer_id IS NULL OR amount < 0")
        conn.execute("DELETE FROM analytics.stg_payments a USING analytics.stg_payments b "
                     "WHERE a.ctid < b.ctid AND a.payment_id = b.payment_id")
    conn.execute("UPDATE analytics.sim_state SET scenario = %s, since = now() WHERE id = 1", (name,))
    print(f"{now():%H:%M:%S} scenario -> {name}", flush=True)


def tick(conn, rng: random.Random) -> None:
    scenario, since, next_id, last_load = state(conn)
    end = now()
    rate = {"stalled_loader": 0.0, "volume_drop": 0.1}.get(scenario, 1.0)
    if rate > 0:
        next_id = load(conn, last_load, end, next_id, rng, rate, scenario)
    conn.execute("UPDATE analytics.sim_state SET next_order_id = %s, last_load = %s WHERE id = 1", (next_id, end))
    # Keep the demo database small: two weeks of history.
    conn.execute("DELETE FROM analytics.fct_orders WHERE created_at < now() - interval '15 days'")
    conn.execute("DELETE FROM analytics.stg_payments WHERE paid_at < now() - interval '15 days'")


def run() -> None:
    rng = random.Random()
    while True:
        try:
            with connect() as conn:
                tick(conn, rng)
                scenario, since, *_ = state(conn)
                elapsed = (now() - since).total_seconds() / 60
                if scenario == "healthy" and elapsed >= HEALTHY_MINUTES:
                    set_scenario(conn, rng.choice(SCENARIOS))
                elif scenario != "healthy" and elapsed >= INCIDENT_MINUTES.get(scenario, DEFAULT_INCIDENT_MINUTES):
                    set_scenario(conn, "healthy")
        except psycopg.Error as exc:
            print(f"simulator error: {exc}", file=sys.stderr, flush=True)
        time.sleep(60)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("init", "run", "tick", "break", "heal", "status"):
        print(__doc__)
        return 2
    if argv[0] == "run":
        run()
        return 0
    with connect() as conn:
        if argv[0] == "init":
            init(conn)
        elif argv[0] == "status":
            scenario, since, next_id, _ = state(conn)
            print(f"scenario={scenario} since={since:%Y-%m-%d %H:%M} orders={next_id - 1}")
        elif argv[0] == "tick":
            tick(conn, random.Random())
        elif argv[0] == "break":
            if len(argv) < 2 or argv[1] not in SCENARIOS:
                print("scenarios: " + ", ".join(SCENARIOS))
                return 2
            set_scenario(conn, argv[1])
        else:
            set_scenario(conn, "healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
