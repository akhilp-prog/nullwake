import pytest

from nullwake.connectors import DIALECTS, ConnectorError, guard_select, split_dataset
from nullwake.contracts import ContractError, load_bundle

from .conftest import CONNECTION_YAML, contract_yaml


def write(tmp_path, text):
    path = tmp_path / "bundle.yaml"
    path.write_text(text)
    return path


FULL = contract_yaml("""
schedule: {intervalMinutes: 5}
schema:
  allowAdditionalColumns: false
  columns:
    - {name: id, type: int8, nullable: false, unique: true}
    - {name: loaded_at, type: timestamptz}
uniqueKey: [id, loaded_at]
freshness: {column: loaded_at, maxAgeMinutes: 60}
volume: {column: loaded_at}
checks:
  - {name: positive, sql: "select count(*) from nwtest.orders where id < 0", severity: critical}
""")


def test_full_contract_compiles_to_checks(tmp_path):
    bundle = load_bundle([write(tmp_path, CONNECTION_YAML + "---" + FULL)])
    (contract,) = bundle.contracts
    names = sorted(m.name for m in contract.monitors)
    assert names == ["orders.check.positive", "orders.freshness", "orders.not_null.id", "orders.schema",
                     "orders.unique.id", "orders.unique_key", "orders.volume"]
    schema = next(m for m in contract.monitors if m.kind == "schema")
    assert schema.params["expected"][0] == {"name": "id", "type": "bigint", "nullable": False}
    assert schema.params["expected"][1]["type"] == "timestamp with time zone"
    assert all(m.interval_seconds == 300 for m in contract.monitors)
    volume = next(m for m in contract.monitors if m.kind == "volume")
    assert volume.params == {"column": "loaded_at", "windowHours": 24, "lookbackWindows": 7,
                             "tolerancePct": 50.0, "minRows": 1}
    assert volume.severity == "warning"
    assert len(contract.checksum) == 64


@pytest.mark.parametrize("body,error", [
    ("freshness: {column: loaded_at}", "maxAgeMinutes"),
    ("freshness: {column: 'x; drop table y', maxAgeMinutes: 5}", "invalid identifier"),
    ("checks: [{name: bad, sql: 'delete from nwtest.orders'}]", "single SELECT"),
    ("checks: [{name: bad, sql: 'select 1; drop table nwtest.orders'}]", "single SELECT"),
    ("checks: [{name: Bad Name, sql: 'select 1'}]", "names use lowercase"),
    ("schema: {columns: [{name: a}, {name: A}]}", "duplicate column"),
    ("volume: {column: c, windowHours: 0}", "between 1"),
    ("schedule: {intervalMinutes: 5}", "at least one of"),
])
def test_contract_validation_errors(tmp_path, body, error):
    with pytest.raises(ContractError, match=error):
        load_bundle([write(tmp_path, CONNECTION_YAML + "---" + contract_yaml(body))])


def test_secrets_must_be_env_references(tmp_path):
    bad = CONNECTION_YAML.replace("dsnEnv: TEST_WAREHOUSE_DSN", "dsn: postgres://user:pw@host/db")
    with pytest.raises(ContractError, match="never inline"):
        load_bundle([write(tmp_path, bad)])


def test_unknown_connection_and_api_version(tmp_path):
    with pytest.raises(ContractError, match="unknown connection"):
        load_bundle([write(tmp_path, contract_yaml("freshness: {column: a, maxAgeMinutes: 5}"))])
    with pytest.raises(ContractError, match="apiVersion"):
        load_bundle([write(tmp_path, CONNECTION_YAML.replace("nullwake/v1", "v2"))])


def test_duplicate_names_rejected(tmp_path):
    doc = contract_yaml("freshness: {column: a, maxAgeMinutes: 5}")
    with pytest.raises(ContractError, match="duplicate contract"):
        load_bundle([write(tmp_path, CONNECTION_YAML + "---" + doc + "---" + doc)])


def test_dialect_sql_generation():
    pg, sf, bq = DIALECTS["postgres"], DIALECTS["snowflake"], DIALECTS["bigquery"]
    assert "EXTRACT(EPOCH FROM (now() - MAX(loaded_at)))" in pg.freshness_sql("a.b", "loaded_at")
    assert "DATEDIFF('second', MAX(loaded_at), CURRENT_TIMESTAMP())" in sf.freshness_sql("db.a.b", "loaded_at")
    assert bq.freshness_sql("my-proj.d.t", "ts").endswith("FROM `my-proj.d.t`")
    sql = pg.volume_sql("a.b", "ts", 24, 3)
    assert sql.count("SUM(CASE") == 4 and "interval '345600 seconds'" in sql
    assert "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 86400 SECOND)" in bq.volume_sql("d.t", "ts", 24, 3)
    assert "GROUP BY a, b HAVING COUNT(*) > 1" in sf.duplicate_sql("x.y", ["a", "b"])
    assert sf.normalize_type("VARCHAR(16777216)") == "text"
    assert pg.normalize_type("character varying(20)") == "character varying"


@pytest.mark.parametrize("bad", ["a.b.c.d", "a..b", "a.b;--", "1abc", "a b"])
def test_identifiers_are_validated(bad):
    with pytest.raises(ConnectorError):
        split_dataset(bad)


def test_guard_select():
    assert guard_select("  WITH x AS (select 1) select * from x;  ") == "WITH x AS (select 1) select * from x"
    for bad in ("update t set a=1", "select 1; select 2", "copy t to '/tmp/x'"):
        with pytest.raises(ConnectorError):
            guard_select(bad)


BQ_CONNECTION = """
apiVersion: nullwake/v1
kind: Connection
metadata: {name: bq}
spec: {type: bigquery, project: acme-analytics}
"""


def test_bigquery_three_part_dataset_with_hyphenated_project_compiles(tmp_path):
    doc = contract_yaml("freshness: {column: loaded_at, maxAgeMinutes: 30}", dataset="acme-analytics.sales.orders")
    doc = doc.replace("connection: wh", "connection: bq")
    bundle = load_bundle([write(tmp_path, BQ_CONNECTION + "---" + doc)])
    assert bundle.contracts[0].monitors[0].dataset == "acme-analytics.sales.orders"
    with pytest.raises(ContractError):  # still rejected on a Postgres connection
        load_bundle([write(tmp_path, CONNECTION_YAML + "---" + contract_yaml("", dataset="acme-analytics.sales.orders"))])


def test_future_skew_setting_is_compiled_from_yaml(tmp_path):
    body = "freshness: {column: loaded_at, maxAgeMinutes: 30, maxFutureSkewMinutes: 0}"
    params = load_bundle([write(tmp_path, CONNECTION_YAML + "---" + contract_yaml(body))]).contracts[0].monitors[0].params
    assert params["maxFutureSkewMinutes"] == 0
    default = load_bundle([write(tmp_path, CONNECTION_YAML + "---" + contract_yaml(
        "freshness: {column: loaded_at, maxAgeMinutes: 30}"))]).contracts[0].monitors[0].params
    assert default["maxFutureSkewMinutes"] == 5
