import json

from nullwake.cli import main

from .conftest import CONNECTION_YAML, contract_yaml


def test_check_exit_codes_for_ci(wh, tmp_path, capsys):
    wh.execute("CREATE TABLE nwtest.orders (id bigint, loaded_at timestamptz)")
    wh.execute("INSERT INTO nwtest.orders SELECT g, now() FROM generate_series(1, 20) g")
    path = tmp_path / "c.yaml"
    path.write_text(CONNECTION_YAML + "---" + contract_yaml(
        "schema: {columns: [{name: id, type: bigint, nullable: false, unique: true}]}\n"
        "freshness: {column: loaded_at, maxAgeMinutes: 10}"))
    assert main(["check", str(path)]) == 0
    assert "4/4 checks passed" in capsys.readouterr().out
    wh.execute("INSERT INTO nwtest.orders VALUES (1, now())")
    assert main(["check", str(path), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert {r["check"]: r["status"] for r in report["results"]}["orders.unique.id"] == "fail"


def test_check_reports_config_errors(tmp_path, capsys):
    path = tmp_path / "c.yaml"
    path.write_text(contract_yaml("freshness: {column: a, maxAgeMinutes: 5}"))
    assert main(["check", str(path)]) == 2
    assert "unknown connection" in capsys.readouterr().err


def test_apply_and_user_commands(tmp_path, capsys, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text(CONNECTION_YAML + "---" + contract_yaml("freshness: {column: a, maxAgeMinutes: 5}"))
    assert main(["apply", "--dry-run", str(path)]) == 0
    assert "valid: 1 contract(s), 1 check(s)" in capsys.readouterr().out
    assert main(["apply", str(path)]) == 0
    assert "created  contract/orders" in capsys.readouterr().out
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-enough-password\n"))
    assert main(["user", "Ops@Example.com", "--role", "admin", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["user", "ops@example.com", "--role", "admin", "--password-stdin"]) == 2
    assert main(["user", "ops@example.com", "--password-stdin"]) == 2  # role required to save
    assert main(["user", "OPS@example.com", "--delete"]) == 0
    assert "deleted ops@example.com" in capsys.readouterr().out
    assert main(["user", "ops@example.com", "--delete"]) == 1
