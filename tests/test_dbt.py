"""`nullwake init --dbt`: dbt's own tests, source freshness and owners become contract checks."""
import json

from nullwake.cli import main
from nullwake.contracts import load_bundle
from nullwake.dbt import load_manifest, select

from .conftest import CONNECTION_YAML


def _test(name, model, column=None, **kwargs):
    node = {"resource_type": "test", "attached_node": model, "config": {"enabled": True},
            "test_metadata": {"name": name, "kwargs": {**kwargs, **({"column_name": column} if column else {})}},
            "depends_on": {"nodes": [model]}}
    if column:
        node["column_name"] = column
    return node


MANIFEST = {
    "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
    "nodes": {
        "model.shop.fct_orders": {
            "resource_type": "model", "name": "fct_orders", "alias": "fct_orders", "schema": "nwtest",
            "database": "warehouse", "package_name": "shop", "tags": ["finance"], "group": "finance",
            "config": {"enabled": True, "materialized": "table", "meta": {}}},
        "model.shop.int_order_lines": {
            "resource_type": "model", "name": "int_order_lines", "alias": "order_lines", "schema": "nwtest",
            "database": "warehouse", "package_name": "shop", "tags": [],
            "config": {"enabled": True, "materialized": "incremental", "unique_key": ["order_id", "line_no"],
                       "meta": {"owner": "lines@acme.test"}}},
        "model.shop.stg_tmp": {
            "resource_type": "model", "name": "stg_tmp", "schema": "nwtest", "database": "warehouse",
            "config": {"enabled": True, "materialized": "ephemeral"}},
        "model.shop.not_built": {
            "resource_type": "model", "name": "not_built", "schema": "nwtest", "database": "warehouse",
            "config": {"enabled": True, "materialized": "view"}},
        "test.shop.u1": _test("unique", "model.shop.fct_orders", "order_id"),
        "test.shop.n1": _test("not_null", "model.shop.fct_orders", "order_id"),
        "test.shop.n2": _test("not_null", "model.shop.fct_orders", "customer_id"),
        "test.shop.n3": _test("not_null", "model.shop.fct_orders", "ghost_col"),
        "test.shop.f1": {**_test("not_null", "model.shop.fct_orders", "amount"),
                         "config": {"enabled": True, "where": "amount > 0"}},
        "seed.shop.country_codes": {
            "resource_type": "seed", "name": "country_codes", "alias": "country_codes", "schema": "nwtest",
            "database": "warehouse", "config": {"enabled": True, "materialized": "seed"}},
        "test.shop.r1": {**_test("relationships", "model.shop.fct_orders", "customer_id", to="ref('int_order_lines')",
                                 field="order_id"),
                         "depends_on": {"nodes": ["model.shop.int_order_lines", "model.shop.fct_orders"]}},
        "test.shop.a1": _test("accepted_values", "model.shop.fct_orders", "status", values=["new", "it's done"]),
        "test.shop.a2": _test("accepted_values", "model.shop.fct_orders", "status", values=[{"x": 1}]),
    },
    "sources": {
        "source.shop.stripe.charges": {
            "resource_type": "source", "name": "charges", "identifier": "raw_charges", "source_name": "stripe",
            "schema": "nwtest", "database": "warehouse", "loaded_at_field": "_synced_at",
            "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 1, "period": "day"}},
            "config": {"enabled": True}, "meta": {"owner": "payments@acme.test"}, "tags": []},
    },
    "groups": {"group.shop.finance": {"name": "finance", "owner": {"email": "finance@acme.test"}}},
}


def test_dbt_manifest_drafts_contracts_that_pass(wh, tmp_path, capsys):
    wh.execute("CREATE TABLE nwtest.fct_orders (order_id bigint, customer_id bigint, amount numeric, "
               "created_at timestamptz)")
    wh.execute("INSERT INTO nwtest.fct_orders SELECT g, g % 5, g, now() - (g || ' minutes')::interval "
               "FROM generate_series(1, 30) g")
    wh.execute("ALTER TABLE nwtest.fct_orders ADD COLUMN status text DEFAULT 'new'")
    wh.execute("CREATE TABLE nwtest.country_codes (code text, updated_at timestamptz)")
    wh.execute("INSERT INTO nwtest.country_codes VALUES ('SI', now() - interval '400 days')")
    wh.execute("CREATE TABLE nwtest.order_lines (order_id bigint, line_no int, sku text)")
    wh.execute("INSERT INTO nwtest.order_lines SELECT g / 3, g % 3, 'x' FROM generate_series(0, 20) g")
    wh.execute("CREATE TABLE nwtest.raw_charges (id text, _synced_at timestamptz, updated_at timestamptz)")
    wh.execute("INSERT INTO nwtest.raw_charges VALUES ('ch_1', now(), now())")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))
    conn_file = tmp_path / "conn.yaml"
    conn_file.write_text(CONNECTION_YAML)
    out = tmp_path / "contracts"

    assert main(["init", str(conn_file), "--dbt", str(manifest), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert sorted(p.name for p in out.glob("*.yaml")) == ["charges.yaml", "country_codes.yaml",
                                                         "fct_orders.yaml", "int_order_lines.yaml"]
    assert "not_built: table not found" in printed and "has the dbt model been built?" in printed
    assert "ghost_col, which the warehouse table does not have" in printed

    orders = (out / "fct_orders.yaml").read_text()
    assert "dataset: nwtest.fct_orders" in orders                      # Postgres: two-part names
    assert "owner: finance@acme.test  # from dbt meta" in orders        # group owner
    assert "{name: order_id, type: bigint, nullable: false, unique: true}  # dbt test: not_null, unique" in orders
    assert "{name: customer_id, type: bigint, nullable: false}" in orders
    assert "{name: amount, type: numeric}" in orders                    # filtered test is not a whole-table rule
    assert "# uniqueKey" not in orders                                  # unique test already covers it
    assert "not in ('new', 'it''s done')" in orders                     # values quoted, quotes escaped
    assert "left join nwtest.order_lines p on c.customer_id = p.order_id" in orders
    assert orders.count("\n    - {name: ") == 2                               # the dict-valued test is skipped
    seed = (out / "country_codes.yaml").read_text()
    assert "  freshness: {" not in seed and "volume:" not in seed and "dbt seed" in seed
    lines = (out / "int_order_lines.yaml").read_text()
    assert "dataset: nwtest.order_lines" in lines and "owner: lines@acme.test" in lines
    assert "uniqueKey: [order_id, line_no]  # from dbt" in lines
    charges = (out / "charges.yaml").read_text()
    assert "freshness: {column: _synced_at, maxAgeMinutes: 1440}  # dbt freshness error_after" in charges
    assert "volume: {column: _synced_at" in charges and "TODO: how late" not in charges

    bundle = load_bundle([str(conn_file), str(out)])
    assert len(bundle.contracts) == 4
    assert main(["check", str(conn_file), str(out)]) == 0

    # The carried-over dbt tests catch what dbt would have caught.
    wh.execute("INSERT INTO nwtest.fct_orders VALUES (999, 77777, 1, now(), 'lost')")
    assert main(["check", str(conn_file), str(out), "--contract", "fct_orders"]) == 1
    printed = capsys.readouterr().out
    assert "FAIL  fct_orders.check.relationships_customer_id" in printed
    assert "FAIL  fct_orders.check.accepted_values_status" in printed


def test_dbt_selection_and_errors(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))
    snow = load_manifest(manifest, three_part=True)
    assert {t.dataset for t in snow} >= {"warehouse.nwtest.fct_orders", "warehouse.nwtest.raw_charges"}
    assert [t.name for t in select(snow, ["tag:finance"])] == ["fct_orders"]
    assert [t.name for t in select(snow, ["source:stripe"])] == ["charges"]
    assert select(snow, ["int_order_lines"])[0].hints.unique_key == ["order_id", "line_no"]
    assert [t.hints.max_age_minutes for t in select(snow, ["charges"])] == [1440]

    conn_file = tmp_path / "conn.yaml"
    conn_file.write_text(CONNECTION_YAML)
    bad = tmp_path / "bad.json"
    bad.write_text('{"x": 1}')
    assert main(["init", str(conn_file), "--dbt", str(bad)]) == 2
    assert "not a dbt manifest" in capsys.readouterr().err
    assert main(["init", str(conn_file), "--dbt", str(manifest), "--select", "tag:nope"]) == 2
    assert main(["init", str(conn_file), "--schema", "x", "--select", "tag:x"]) == 2
