"""The command line end to end on the SQLite stand-in: migrate, validate, estimate, check, finalize."""
import json

import pytest

import mssql_ocp_migrate.engine as engine
from mssql_ocp_migrate.cli import main
from sqlite_adapter import SqliteFactory
from test_engine import make_dbs, rows


@pytest.fixture
def setup(tmp_path, monkeypatch):
    src, tgt = make_dbs(tmp_path)
    monkeypatch.setattr(engine, "SqlServerFactory", lambda cfg: SqliteFactory(src, tgt))
    conf = {"source": {"database": "src"}, "target": {"database": "tgt"},
            "migration": {"workers": 2, "chunk_rows": 700, "batch_rows": 250, "schema_mode": "skip",
                          "state_file": str(tmp_path / "state.json"), "max_retries": 0, "executor": "thread",
                          "assess": False},
            "validation": {"mode": "checksum", "sample_full_pct": 20},
            "report_dir": str(tmp_path / "report")}
    path = tmp_path / "migrate.json"
    path.write_text(json.dumps(conf))
    return src, tgt, str(path), tmp_path


def test_migrate_then_validate_from_the_command_line(setup, capsys):
    src, tgt, c, tmp = setup
    assert main(["migrate", "-c", c, "--workers", "3"]) == 0
    out = capsys.readouterr().out
    assert "SUCCESS" in out and (tmp / "report" / "report.html").exists()
    assert rows(src, "orders") == rows(tgt, "orders")
    assert main(["validate", "-c", c, "--mode", "full"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_validate_reports_tampering(setup, capsys):
    import sqlite3
    src, tgt, c, _ = setup
    assert main(["migrate", "-c", c, "--skip-validation"]) == 0
    db = sqlite3.connect(tgt)
    db.execute("UPDATE customers SET name = 'changed' WHERE code = 'C0001'")
    db.commit()
    db.close()
    assert main(["validate", "-c", c, "--mode", "full"]) == 1


def test_estimate_and_check_from_the_command_line(setup, capsys):
    _, _, c, tmp = setup
    assert main(["estimate", "-c", c, "--sample-rows", "300", "--no-write-probe"]) == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out and "report:" in out
    # the SQLite stand-in can't report a SQL Server version: check says so and fails
    assert main(["check", "-c", c]) == 2
    assert "[FAIL] source" in capsys.readouterr().out


def test_option_overrides_reach_the_config(setup, monkeypatch):
    from mssql_ocp_migrate.cli import _load, build_parser
    _, _, c, _ = setup
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "2")
    args = build_parser().parse_args(["migrate", "-c", c, "--include", "main.orders", "--exclude", "main.x",
                                      "--truncate", "--shard", "auto/4"])
    cfg = _load(args)
    m = cfg.migration
    assert m.include == ["main.orders"] and m.exclude == ["main.x"] and m.if_target_not_empty == "truncate"
    assert (m.shard_index, m.shard_count) == (2, 4)
    args = build_parser().parse_args(["migrate", "-c", c, "--resume", "--shard", "2/3"])
    m = _load(args).migration
    assert m.if_target_not_empty == "resume" and (m.shard_index, m.shard_count) == (1, 3)


def test_unexpected_errors_exit_with_code_1(setup, monkeypatch, capsys):
    _, _, c, _ = setup

    def boom(*a, **k):
        raise RuntimeError("server went away")
    monkeypatch.setattr(engine.Migrator, "migrate", boom)
    assert main(["migrate", "-c", c]) == 1
    assert "server went away" in capsys.readouterr().err


def test_schema_script_never_contains_private_keys(setup, monkeypatch, tmp_path):
    """A written script must not hold a certificate's private key together with its password."""
    import sqlite_adapter
    from mssql_ocp_migrate.model import Certificate, DatabaseExtras
    extras = DatabaseExtras(certificates=[Certificate("SignCert", "dbo", "0xAB", private_key="0xDEADBEEF",
                                                      password="Mom!secret-one-time"),
                                          Certificate("PublicOnly", None, "0xCD")])
    monkeypatch.setattr(sqlite_adapter.SqliteDB, "database_extras", lambda self: extras, raising=False)
    monkeypatch.setattr(sqlite_adapter.SqliteDB, "list_schemas", lambda self: ["main"], raising=False)
    _, _, c, _ = setup
    out = tmp_path / "schema.sql"
    assert main(["schema", "-c", c, "-o", str(out)]) == 0
    text = out.read_text()
    assert "CREATE CERTIFICATE [SignCert] FROM BINARY = 0xAB" in text
    assert "0xDEADBEEF" not in text and "secret-one-time" not in text and "CREATE MASTER KEY" not in text
    assert "certificate SignCert: private key not written to this script" in text
    assert extras.certificates[0].private_key == "0xDEADBEEF"     # migrate's own copy is untouched
