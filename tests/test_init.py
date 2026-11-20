"""`nullwake init`: contracts drafted from the warehouse's own table definitions."""
from nullwake.cli import main
from nullwake.connectors import ColumnInfo
from nullwake.contracts import load_bundle
from nullwake.init import draft_contract, pick_timestamp

from .conftest import CONNECTION_YAML


def test_init_drafts_contracts_that_load_and_pass(wh, tmp_path, capsys):
    wh.execute("CREATE TABLE nwtest.orders (order_id bigint PRIMARY KEY, customer_id bigint, "
               "amount numeric(12,2) NOT NULL, created_at timestamptz, _loaded_at timestamptz)")
    wh.execute("INSERT INTO nwtest.orders SELECT g, g % 7, g * 1.5, now() - interval '1 day', "
               "now() - (g || ' minutes')::interval FROM generate_series(1, 50) g")
    wh.execute("CREATE TABLE nwtest.customers (id bigint, email text, updated_at timestamp)")
    wh.execute("INSERT INTO nwtest.customers VALUES (1, 'a@x', now())")
    wh.execute("CREATE TABLE nwtest.lookup (code text, label text)")
    wh.execute("INSERT INTO nwtest.lookup VALUES ('a', 'A')")
    conn_file = tmp_path / "conn.yaml"
    conn_file.write_text(CONNECTION_YAML)
    out = tmp_path / "contracts"

    assert main(["init", str(conn_file), "--schema", "nwtest", "--out", str(out), "--owner", "data@acme.test"]) == 0
    printed = capsys.readouterr().out
    assert sorted(p.name for p in out.glob("*.yaml")) == ["customers.yaml", "lookup.yaml", "orders.yaml"]
    assert "3 contract(s) drafted" in printed and "nullwake check" in printed

    orders = (out / "orders.yaml").read_text()
    assert "uniqueKey: [order_id]  # declared primary key" in orders
    assert "freshness: {column: _loaded_at" in orders          # load time preferred over created_at
    assert "volume: {column: _loaded_at" in orders
    assert "{name: amount, type: numeric, nullable: false}" in orders
    customers = (out / "customers.yaml").read_text()
    assert "freshness: {column: updated_at" in customers and "volume:" not in customers
    assert "# uniqueKey" in customers                           # no PK declared: suggestion only
    assert "  freshness: {" not in (out / "lookup.yaml").read_text()        # no timestamp column

    # The drafts are valid contracts and pass against the data they were drafted from.
    bundle = load_bundle([str(conn_file), str(out)])
    assert {c.name for c in bundle.contracts} == {"orders", "customers", "lookup"}
    assert main(["check", str(conn_file), str(out)]) == 0
    assert "checks passed" in capsys.readouterr().out


def test_init_never_overwrites_without_force(wh, tmp_path, capsys):
    wh.execute("CREATE TABLE nwtest.orders (id bigint)")
    conn_file = tmp_path / "conn.yaml"
    conn_file.write_text(CONNECTION_YAML)
    out = tmp_path / "c"
    out.mkdir()
    (out / "orders.yaml").write_text("# hand-edited\n")
    assert main(["init", str(conn_file), "--table", "nwtest.orders", "--out", str(out)]) == 1
    assert "already exists" in capsys.readouterr().out
    assert (out / "orders.yaml").read_text() == "# hand-edited\n"
    assert main(["init", str(conn_file), "--table", "nwtest.orders", "--out", str(out), "--force"]) == 0
    assert "kind: DataContract" in (out / "orders.yaml").read_text()


def test_init_reports_missing_tables_and_bad_input(wh, tmp_path, capsys):
    conn_file = tmp_path / "conn.yaml"
    conn_file.write_text(CONNECTION_YAML)
    assert main(["init", str(conn_file), "--table", "nwtest.nope", "--out", str(tmp_path / "c")]) == 1
    assert "table not found" in capsys.readouterr().out
    assert main(["init", str(conn_file), "--out", str(tmp_path / "c")]) == 2


def test_timestamp_choice_and_names():
    cols = [ColumnInfo("id", "bigint", False), ColumnInfo("updated_at", "timestamp", True),
            ColumnInfo("created_at", "timestamp", True), ColumnInfo("event_date", "date", True)]
    assert pick_timestamp(cols)[0].name == "created_at"
    assert pick_timestamp([ColumnInfo("x", "text", True)])[0] is None
    taken: set[str] = set()
    a = draft_contract("wh", "a.Orders-2024", cols, [], "o@x.test", taken)
    b = draft_contract("wh", "b.orders-2024", cols, [], "o@x.test", taken)
    assert (a.name, b.name) == ("orders-2024", "orders-2024_2")
