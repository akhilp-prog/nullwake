"""Checks against a real Postgres warehouse."""
from nullwake.monitors import evaluate, volume_bounds


def make_orders(wh, hourly: list[int], window_hours: int = 1):
    """hourly[0] = rows in the most recent window, hourly[i] = rows i windows ago."""
    wh.execute("CREATE TABLE nwtest.orders (id bigint, customer_id bigint, amount numeric, loaded_at timestamptz)")
    rows = []
    n = 0
    for i, count in enumerate(hourly):
        for j in range(count):
            n += 1
            offset = (i * window_hours * 3600) + (j + 1) * (window_hours * 3600 / (count + 1))
            rows.append((n, n % 7, 10.0, offset))
    with wh.cursor() as cur:
        cur.executemany("INSERT INTO nwtest.orders VALUES (%s, %s, %s, now() - make_interval(secs => %s))", rows)


def test_freshness(wh, pg):
    make_orders(wh, [5, 5])
    ok = evaluate(pg, "freshness", "nwtest.orders", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert ok.status == "pass" and ok.value < 30
    wh.execute("UPDATE nwtest.orders SET loaded_at = loaded_at - interval '3 hours'")
    stale = evaluate(pg, "freshness", "nwtest.orders", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert stale.status == "fail" and "old" in stale.message and stale.value > 180
    wh.execute("TRUNCATE nwtest.orders")
    empty = evaluate(pg, "freshness", "nwtest.orders", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert empty.status == "fail" and "no rows" in empty.message


VOL = {"column": "loaded_at", "windowHours": 1, "lookbackWindows": 6, "tolerancePct": 30, "minRows": 1}


def test_volume_normal_drop_and_spike(wh, pg):
    make_orders(wh, [50, 48, 52, 50, 49, 51, 50])
    ok = evaluate(pg, "volume", "nwtest.orders", VOL)
    assert ok.status == "pass", ok.message
    assert ok.observed["current"] == 50 and ok.observed["history"] == [48, 52, 50, 49, 51, 50]
    wh.execute("DROP TABLE nwtest.orders")
    make_orders(wh, [12, 48, 52, 50, 49, 51, 50])
    drop = evaluate(pg, "volume", "nwtest.orders", VOL)
    assert drop.status == "fail" and "below" in drop.message
    wh.execute("DROP TABLE nwtest.orders")
    make_orders(wh, [140, 48, 52, 50, 49, 51, 50])
    assert "above" in evaluate(pg, "volume", "nwtest.orders", VOL).message


def test_volume_min_rows_and_learning(wh, pg):
    make_orders(wh, [0, 0, 0, 0, 0, 0, 5])
    assert evaluate(pg, "volume", "nwtest.orders", VOL).status == "fail"
    wh.execute("DROP TABLE nwtest.orders")
    make_orders(wh, [3, 0, 0, 0, 0, 0, 4])
    learning = evaluate(pg, "volume", "nwtest.orders", VOL)
    assert learning.status == "pass" and "not enough history" in learning.message


def test_volume_bounds_use_robust_spread():
    median, low, high = volume_bounds([100, 100, 100, 100, 5000], 10)
    assert median == 100 and low == 90 and high == 110  # one outlier does not widen the band


EXPECTED = [{"name": "id", "type": "bigint", "nullable": False},
            {"name": "amount", "type": "numeric", "nullable": True}]


def test_schema_contract_mode(wh, pg):
    make_orders(wh, [1])
    params = {"expected": EXPECTED, "allowAdditionalColumns": True}
    ok = evaluate(pg, "schema", "nwtest.orders", params)
    assert ok.status == "pass" and "new column(s): customer_id, loaded_at" in ok.message
    strict = evaluate(pg, "schema", "nwtest.orders", {**params, "allowAdditionalColumns": False})
    assert strict.status == "fail"
    wh.execute("ALTER TABLE nwtest.orders ALTER COLUMN amount TYPE text")
    changed = evaluate(pg, "schema", "nwtest.orders", params)
    assert changed.status == "fail" and "amount is text, expected numeric" in changed.message
    wh.execute("ALTER TABLE nwtest.orders DROP COLUMN id")
    assert "missing column id" in evaluate(pg, "schema", "nwtest.orders", params).message
    missing = evaluate(pg, "schema", "nwtest.nope", params)
    assert missing.status == "error" and "not found" in missing.message


def test_schema_baseline_mode(wh, pg):
    make_orders(wh, [1])
    first = evaluate(pg, "schema", "nwtest.orders", {})
    assert first.status == "pass" and first.state["baseline"]
    assert evaluate(pg, "schema", "nwtest.orders", {}, first.state).status == "pass"
    wh.execute("ALTER TABLE nwtest.orders ADD COLUMN coupon text")
    drift = evaluate(pg, "schema", "nwtest.orders", {}, first.state)
    assert drift.status == "fail" and "coupon" in drift.message


def test_not_null_unique_and_assertion(wh, pg):
    make_orders(wh, [10])
    assert evaluate(pg, "not_null", "nwtest.orders", {"column": "customer_id", "maxNullPct": 0}).status == "pass"
    wh.execute("UPDATE nwtest.orders SET customer_id = NULL WHERE id <= 2")
    nulls = evaluate(pg, "not_null", "nwtest.orders", {"column": "customer_id", "maxNullPct": 0})
    assert nulls.status == "fail" and nulls.observed["nulls"] == 2
    assert evaluate(pg, "not_null", "nwtest.orders", {"column": "customer_id", "maxNullPct": 25}).status == "pass"
    assert evaluate(pg, "unique", "nwtest.orders", {"columns": ["id"]}).status == "pass"
    wh.execute("INSERT INTO nwtest.orders SELECT * FROM nwtest.orders WHERE id IN (3, 4)")
    dupes = evaluate(pg, "unique", "nwtest.orders", {"columns": ["id"]})
    assert dupes.status == "fail" and dupes.value == 2
    sql = "select count(*) from nwtest.orders where amount > 5"
    assert evaluate(pg, "assertion", "", {"sql": sql, "maxValue": 100}).status == "pass"
    assert evaluate(pg, "assertion", "", {"sql": sql, "maxValue": 3}).status == "fail"


def test_sessions_are_read_only(wh, pg):
    wh.execute("CREATE SEQUENCE nwtest.s")
    result = evaluate(pg, "assertion", "", {"sql": "select nextval('nwtest.s')", "maxValue": 0})
    assert result.status == "error" and "read-only transaction" in result.message


def test_statement_timeout(wh):
    from nullwake.connectors import PostgresConnector
    with PostgresConnector("t", {"dsnEnv": "TEST_WAREHOUSE_DSN"}, 1) as conn:
        result = evaluate(conn, "assertion", "", {"sql": "select count(*) from pg_sleep(3)", "maxValue": 0})
    assert result.status == "error" and "statement timeout" in result.message


def test_warehouse_error_text_is_redacted(wh, pg):
    """Postgres quotes the offending value in its error text; it must not reach messages, incidents or alerts."""
    result = evaluate(pg, "assertion", "", {"sql": "select 'card-4111-1111'::int", "maxValue": 0})
    assert result.status == "error" and "data exception" in result.message and "22P02" in result.message
    assert "4111" not in result.message


def test_missing_env_is_reported():
    from nullwake.connectors import ConnectorError, PostgresConnector
    try:
        PostgresConnector("x", {"dsnEnv": "NOT_SET_ANYWHERE"}, 5)
    except ConnectorError as exc:
        assert "NOT_SET_ANYWHERE is not set" in str(exc)
    else:
        raise AssertionError("expected ConnectorError")


def test_schema_catches_not_null_becoming_nullable(wh, pg):
    wh.execute("CREATE TABLE nwtest.accounts (id bigint NOT NULL, email text)")
    expected = [{"name": "id", "type": "bigint", "nullable": False}, {"name": "email", "type": "text", "nullable": True}]
    first = evaluate(pg, "schema", "nwtest.accounts", {})
    wh.execute("ALTER TABLE nwtest.accounts ALTER COLUMN id DROP NOT NULL")  # every row still has an id
    drift = evaluate(pg, "schema", "nwtest.accounts", {}, first.state)
    assert drift.status == "fail" and "id now allows NULLs" in drift.message
    # contract mode: nullable: false is a rule about values (its not_null check), not about the DDL
    assert evaluate(pg, "schema", "nwtest.accounts", {"expected": expected}).status == "pass"


def test_bare_postgres_name_resolves_through_search_path(wh, pg):
    wh.execute("CREATE TABLE public.nw_bare (a int)")
    wh.execute("CREATE TABLE nwtest.nw_bare (a int, b text)")
    try:
        pg.conn.execute("SET search_path TO nwtest, public")
        pg.conn.commit()
        cols = [c.name for c in pg.columns("nw_bare")]
        assert cols == ["a", "b"]  # the same table that freshness/volume/null queries on "nw_bare" would read
    finally:
        wh.execute("DROP TABLE public.nw_bare")
