"""Snowflake checks run end to end against fakesnow (a local Snowflake emulator on DuckDB), plus the connection
settings Nullwake sends to a real account. Skipped when the optional Snowflake extras are not installed."""
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from nullwake.connectors import ConnectorError, open_connector
from nullwake.monitors import evaluate

fakesnow = pytest.importorskip("fakesnow")

SPEC = {"account": "acme-test", "user": "NULLWAKE", "passwordEnv": "SF_PASSWORD", "warehouse": "NULLWAKE_WH",
        "database": "ANALYTICS", "schema": "SALES", "role": "NULLWAKE_RO"}


@pytest.fixture
def sf(monkeypatch):
    monkeypatch.setenv("SF_PASSWORD", "not-a-real-password")
    with fakesnow.patch(create_database_on_connect=True, create_schema_on_connect=True):
        conn = open_connector("snowflake-test", "snowflake", SPEC, 30)
        yield conn
        conn.close()


def load_orders(conn, hourly: list[int]):
    """hourly[0] = rows in the latest hour, hourly[i] = rows i hours ago. Timestamps are UTC wall clock (NTZ)."""
    cur = conn.conn.cursor()
    cur.execute("CREATE OR REPLACE TABLE ANALYTICS.SALES.ORDERS "
                "(ID NUMBER, CUSTOMER_ID NUMBER, AMOUNT NUMBER(10,2), LOADED_AT TIMESTAMP_NTZ)")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n, rows = 0, []
    for i, count in enumerate(hourly):
        for j in range(count):
            n += 1
            at = now - timedelta(seconds=i * 3600 + (j + 1) * 3600 / (count + 1))
            rows.append((n, n % 7, 10.0, at.strftime("%Y-%m-%d %H:%M:%S")))
    if rows:
        cur.executemany("INSERT INTO ANALYTICS.SALES.ORDERS VALUES (%s, %s, %s, %s)", rows)


def test_freshness_counts_utc_timestamps_correctly(sf):
    load_orders(sf, [5, 5])
    ok = evaluate(sf, "freshness", "SALES.ORDERS", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert ok.status == "pass" and 0 <= ok.value < 30, ok.message  # an 8-hour timezone shift would fail here
    sf.conn.cursor().execute("UPDATE ANALYTICS.SALES.ORDERS SET LOADED_AT = DATEADD('hour', -3, LOADED_AT)")
    stale = evaluate(sf, "freshness", "SALES.ORDERS", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert stale.status == "fail" and 180 < stale.value < 240


def test_volume(sf):
    load_orders(sf, [50, 48, 52, 50, 49, 51, 50])
    params = {"column": "loaded_at", "windowHours": 1, "lookbackWindows": 6, "tolerancePct": 30, "minRows": 1}
    ok = evaluate(sf, "volume", "SALES.ORDERS", params)
    assert ok.status == "pass", ok.message
    assert ok.observed["current"] == 50 and ok.observed["history"] == [48, 52, 50, 49, 51, 50]
    load_orders(sf, [10, 48, 52, 50, 49, 51, 50])
    assert evaluate(sf, "volume", "SALES.ORDERS", params).status == "fail"


def test_nulls_duplicates_and_assertion(sf):
    load_orders(sf, [4])
    cur = sf.conn.cursor()
    cur.execute("INSERT INTO ANALYTICS.SALES.ORDERS VALUES (1, NULL, 5, CURRENT_TIMESTAMP())")
    nulls = evaluate(sf, "not_null", "SALES.ORDERS", {"column": "customer_id", "maxNullPct": 0})
    assert nulls.status == "fail" and nulls.observed["nulls"] == 1
    dupes = evaluate(sf, "unique", "SALES.ORDERS", {"columns": ["id"]})
    assert dupes.status == "fail" and dupes.value == 1
    check = evaluate(sf, "assertion", "SALES.ORDERS",
                     {"sql": "SELECT COUNT(*) FROM ANALYTICS.SALES.ORDERS WHERE AMOUNT < 0", "maxValue": 0})
    assert check.status == "pass"


def test_schema_matches_contract_types(sf):
    load_orders(sf, [1])
    contract = {"id": "integer", "customer_id": "bigint", "amount": "decimal", "loaded_at": "timestamp"}
    # the contract compiler normalizes declared types with the connection's dialect, as here
    expected = [{"name": n, "type": sf.dialect.normalize_type(t)} for n, t in contract.items()]
    for dataset in ("ORDERS", "SALES.ORDERS", "ANALYTICS.SALES.ORDERS"):  # bare names use the spec's schema
        ok = evaluate(sf, "schema", dataset, {"expected": expected, "allowAdditionalColumns": False})
        assert ok.status == "pass", (dataset, ok.message)
    sf.conn.cursor().execute("ALTER TABLE ANALYTICS.SALES.ORDERS DROP COLUMN CUSTOMER_ID")
    drift = evaluate(sf, "schema", "SALES.ORDERS", {"expected": expected})
    assert drift.status == "fail" and "customer_id" in drift.message


def test_errors_are_redacted(sf):
    with pytest.raises(ConnectorError) as err:
        sf.row("SELECT CAST('4111-1111-1111-1111' AS NUMBER)")
    assert "4111" not in str(err.value) and "query failed" in str(err.value)


def fake_module(captured: dict):
    connector = types.ModuleType("snowflake.connector")

    def connect(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(close=lambda: None)
    connector.connect = connect
    package = types.ModuleType("snowflake")
    package.connector = connector
    return {"snowflake": package, "snowflake.connector": connector}


def test_session_settings_sent_to_snowflake(monkeypatch, tmp_path):
    captured: dict = {}
    for mod, value in fake_module(captured).items():
        monkeypatch.setitem(sys.modules, mod, value)
    key = tmp_path / "key.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    monkeypatch.setenv("SF_KEY", str(key))
    monkeypatch.setenv("SF_KEY_PASS", "pass-phrase")
    spec = {k: v for k, v in SPEC.items() if k != "passwordEnv"}
    spec.update(privateKeyFileEnv="SF_KEY", privateKeyPassphraseEnv="SF_KEY_PASS")
    open_connector("sf", "snowflake", spec, 45).close()
    assert captured["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 45, "QUERY_TAG": "nullwake",
                                              "TIMEZONE": "UTC"}
    assert captured["private_key_file"] == str(key) and captured["private_key_file_pwd"] == "pass-phrase"
    assert "password" not in captured and captured["login_timeout"] == 30 and captured["role"] == "NULLWAKE_RO"


def test_missing_account_is_a_config_error(monkeypatch):
    for mod, value in fake_module({}).items():
        monkeypatch.setitem(sys.modules, mod, value)
    with pytest.raises(ConnectorError, match="missing account"):
        open_connector("sf", "snowflake", {"user": "X", "passwordEnv": "SF_PASSWORD"}, 30)
