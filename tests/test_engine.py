"""End-to-end engine tests on SQLite: plan -> parallel chunked copy -> validate -> report,
plus failure injection, resume, corruption detection and the process executor."""
import json
import random
import sqlite3
from pathlib import Path

import pytest

from mssql_ocp_migrate import Config, Migrator
from mssql_ocp_migrate import report
from mssql_ocp_migrate.state import State

from sqlite_adapter import FAULTS, SqliteFactory

DDL = [
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, amount NUMERIC, created TEXT, payload BLOB)",
    "CREATE TABLE order_lines (order_id INT NOT NULL, line_no INT NOT NULL, sku TEXT, qty INT, "
    "PRIMARY KEY (order_id, line_no))",
    "CREATE TABLE customers (code TEXT PRIMARY KEY, name TEXT)",
    "CREATE TABLE audit_log (ts TEXT, actor TEXT, action TEXT)",
    "CREATE TABLE empty_table (id INTEGER PRIMARY KEY, v TEXT)",
]


def make_dbs(tmp_path: Path, seed: int = 7):
    rnd = random.Random(seed)
    src, tgt = tmp_path / "src.db", tmp_path / "tgt.db"
    for p in (src, tgt):
        c = sqlite3.connect(p)
        for d in DDL:
            c.execute(d)
        c.commit()
        c.close()
    c = sqlite3.connect(src)
    c.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                  [(i * 3, f"cust-{rnd.randint(1, 500)}", None if i % 97 == 0 else round(rnd.uniform(1, 9999), 2),
                    f"2024-01-{1 + i % 28:02d}T10:{i % 60:02d}:00.1234567", bytes(rnd.getrandbits(8) for _ in range(16)))
                   for i in range(1, 5001)])
    c.executemany("INSERT INTO order_lines VALUES (?,?,?,?)",
                  [(o, ln, f"SKU{rnd.randint(1, 999):04d}", rnd.randint(1, 20))
                   for o in range(1, 3001) for ln in range(1, rnd.randint(2, 6))])
    c.executemany("INSERT INTO customers VALUES (?,?)",
                  [((f"C{i:04d}" if i % 2 else f"c{i:04d}"), f"Name {i} ü€ 漢字") for i in range(700)])
    c.executemany("INSERT INTO audit_log VALUES (?,?,?)",
                  [(f"2024-02-01 00:00:{i % 60:02d}", f"user{i % 9}", "login" if i % 3 else None) for i in range(1500)])
    c.commit()
    c.close()
    return str(src), str(tgt)


def cfg(tmp_path: Path, **migration) -> Config:
    m = {"workers": 4, "chunk_rows": 700, "batch_rows": 250, "schema_mode": "skip",
         "state_file": str(tmp_path / "state.json"), "max_retries": 0}
    m.update(migration)
    return Config.from_dict({
        "source": {"database": "src"}, "target": {"database": "tgt"},
        "migration": m,
        "validation": {"mode": "checksum", "sample_full_pct": 20, "compare_schema": True},
        "report_dir": str(tmp_path / "report"),
    })


def rows(path, table):
    c = sqlite3.connect(path)
    out = sorted(c.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
    c.close()
    return out


def test_full_migration_copies_everything_and_validates(tmp_path):
    src, tgt = make_dbs(tmp_path)
    run = Migrator(cfg(tmp_path), SqliteFactory(src, tgt)).migrate()
    assert run.ok, run
    for t in ("orders", "order_lines", "customers", "audit_log", "empty_table"):
        assert rows(src, t) == rows(tgt, t), t
    assert run.rows == 5000 + len(rows(src, "order_lines")) + 700 + 1500
    # chunking actually split the keyed tables
    assert run.tables["main.orders"].chunks == 8
    assert run.tables["main.audit_log"].chunks == 1        # no key -> one chunk
    v = run.validation
    assert v.ok and v.summary()["chunks_row_compared"] > 0
    paths = report.write(run.validation and cfg(tmp_path), run=run)
    data = json.loads(Path(paths["json"]).read_text())
    assert data["run_ok"] is True
    assert "PASSED" in Path(paths["html"]).read_text()


def test_validation_pinpoints_corrupted_missing_and_extra_rows(tmp_path):
    src, tgt = make_dbs(tmp_path)
    c = cfg(tmp_path)
    Migrator(c, SqliteFactory(src, tgt)).migrate(validate=False)
    t = sqlite3.connect(tgt)
    t.execute("UPDATE orders SET amount = amount + 0.01 WHERE id = 300")
    t.execute("DELETE FROM order_lines WHERE order_id = 42 AND line_no = 1")
    t.execute("INSERT INTO customers VALUES ('ZZZ9', 'ghost')")
    t.execute("UPDATE audit_log SET actor = 'mallory' WHERE rowid = 10")
    t.commit()
    t.close()
    from mssql_ocp_migrate.validate import Validator
    st = State.load_or_new(c.migration.state_file)
    m = Migrator(c, SqliteFactory(src, tgt))
    tables = m.source_tables()
    c.validation.sample_full_pct = 0          # rely on checksum + automatic drill-down
    res = Validator(c, SqliteFactory(src, tgt)).run(tables, {x.fqn: st.chunks(x.fqn) for x in tables})
    assert not res.ok
    bad = {n: tc for n, tc in res.tables.items() if not tc.ok}
    assert set(bad) == {"main.orders", "main.order_lines", "main.customers", "main.audit_log"}
    diffs = [ch for ch in bad["main.orders"].chunks if not ch.ok]
    assert diffs[0].different == ["300"]
    assert [ch for ch in bad["main.order_lines"].chunks if not ch.ok][0].missing_in_target == ["42, 1"]
    assert [ch for ch in bad["main.customers"].chunks if not ch.ok][0].extra_in_target == ["'ZZZ9'"]
    assert bad["main.customers"].target_rows == bad["main.customers"].source_rows + 1


def test_failed_chunk_is_reported_then_resume_finishes_only_the_rest(tmp_path):
    src, tgt = make_dbs(tmp_path)
    FAULTS.clear()
    FAULTS[("orders", 2103)] = 5                   # chunk starting at id 2103 fails
    c = cfg(tmp_path, executor="thread")       # injected faults live in this process's memory
    run = Migrator(c, SqliteFactory(src, tgt, faulty=True)).migrate(validate=False)
    assert not run.ok
    assert run.tables["main.orders"].failed_chunks == ["main.orders#1"]
    assert "injected network failure" in run.tables["main.orders"].errors[0]
    state = json.loads(Path(c.migration.state_file).read_text())
    done_before = sum(1 for ch in state["tables"]["main.orders"]["chunks"].values() if ch["status"] == "done")
    assert done_before == 7

    FAULTS.clear()
    c2 = cfg(tmp_path, if_target_not_empty="resume", executor="thread")
    progress = []
    run2 = Migrator(c2, SqliteFactory(src, tgt, faulty=True), progress.append).migrate()
    assert run2.ok
    assert any("copying 1 chunks" in p for p in progress)       # only the failed chunk re-ran
    assert rows(src, "orders") == rows(tgt, "orders")


def test_retry_recovers_from_transient_failure(tmp_path, monkeypatch):
    import mssql_ocp_migrate.engine as eng
    monkeypatch.setattr(eng.time, "sleep", lambda s: None)
    src, tgt = make_dbs(tmp_path)
    FAULTS.clear()
    FAULTS[("orders", 2103)] = 2                   # fails twice, then works
    run = Migrator(cfg(tmp_path, max_retries=3, executor="thread"), SqliteFactory(src, tgt, faulty=True)).migrate()
    assert run.ok
    assert rows(src, "orders") == rows(tgt, "orders")


def test_fail_policy_refuses_non_empty_target(tmp_path):
    src, tgt = make_dbs(tmp_path)
    t = sqlite3.connect(tgt)
    t.execute("INSERT INTO customers VALUES ('X', 'pre-existing')")
    t.commit()
    t.close()
    from mssql_ocp_migrate.engine import MigrationError
    with pytest.raises(MigrationError, match="already contain data"):
        Migrator(cfg(tmp_path), SqliteFactory(src, tgt)).migrate()
    # truncate policy replaces the stale row
    run = Migrator(cfg(tmp_path, if_target_not_empty="truncate"), SqliteFactory(src, tgt)).migrate()
    assert run.ok and rows(src, "customers") == rows(tgt, "customers")


def test_include_exclude_filters(tmp_path):
    src, tgt = make_dbs(tmp_path)
    run = Migrator(cfg(tmp_path, include=["main.order*"], exclude=["*.order_lines"]),
                   SqliteFactory(src, tgt)).migrate()
    assert set(run.tables) == {"main.orders"}
    assert rows(tgt, "customers") == []


def test_process_executor(tmp_path):
    src, tgt = make_dbs(tmp_path)
    run = Migrator(cfg(tmp_path, executor="process", workers=3), SqliteFactory(src, tgt)).migrate()
    assert run.ok
    assert rows(src, "order_lines") == rows(tgt, "order_lines")


def test_sharded_scale_out_then_finalize(tmp_path):
    """3 pods each copy a third of the chunks (no coordination beyond a shared plan file)."""
    src, tgt = make_dbs(tmp_path)
    shard_rows = []
    for i in range(3):
        c = cfg(tmp_path, shard_index=i, shard_count=3, plan_file=str(tmp_path / "plan.json"))
        run = Migrator(c, SqliteFactory(src, tgt)).migrate()
        assert run.validation is None                  # validation waits for finalize
        shard_rows.append(run.rows)
    assert all(r > 0 for r in shard_rows)
    from mssql_ocp_migrate.engine import MigrationError
    c = cfg(tmp_path, shard_count=3, plan_file=str(tmp_path / "plan.json"))
    run = Migrator(c, SqliteFactory(src, tgt)).finalize()
    assert run.ok and run.validation.ok
    for t in ("orders", "order_lines", "customers", "audit_log"):
        assert rows(src, t) == rows(tgt, t)
    assert sum(shard_rows) == run.rows


def test_finalize_refuses_until_all_shards_done(tmp_path):
    src, tgt = make_dbs(tmp_path)
    c0 = cfg(tmp_path, shard_index=0, shard_count=2, plan_file=str(tmp_path / "plan.json"))
    Migrator(c0, SqliteFactory(src, tgt)).migrate()
    from mssql_ocp_migrate.engine import MigrationError
    with pytest.raises(MigrationError, match="chunks not copied yet"):
        Migrator(cfg(tmp_path, shard_count=2, plan_file=str(tmp_path / "plan.json")),
                 SqliteFactory(src, tgt)).finalize()
