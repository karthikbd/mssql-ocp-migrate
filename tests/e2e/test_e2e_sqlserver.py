"""End-to-end test against two real SQL Server instances (source and target).

    docker compose -f tests/e2e/docker-compose.yml up -d      # or point at your own servers
    MSSQL_E2E=1 pytest tests/e2e -v

Environment (defaults match docker-compose.yml):
    E2E_SRC_HOST=127.0.0.1 E2E_SRC_PORT=14331  E2E_TGT_HOST=127.0.0.1 E2E_TGT_PORT=14332
    E2E_USER=sa  E2E_PASSWORD=Str0ng!Passw0rd  E2E_ENCRYPT=optional  E2E_DRIVER=auto
"""
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MSSQL_E2E") != "1", reason="set MSSQL_E2E=1 to run")

from mssql_ocp_migrate import Config, Migrator  # noqa: E402
from mssql_ocp_migrate.config import ConnectionConfig  # noqa: E402
from mssql_ocp_migrate.drivers import connect  # noqa: E402

HERE = Path(__file__).parent
E = os.environ.get


def conn_cfg(side: str, database: str) -> ConnectionConfig:
    pfx = "E2E_SRC" if side == "source" else "E2E_TGT"
    return ConnectionConfig(host=E(f"{pfx}_HOST", "127.0.0.1"),
                            port=int(E(f"{pfx}_PORT", "14331" if side == "source" else "14332")),
                            database=database, user=E("E2E_USER", "sa"),
                            password=E("E2E_PASSWORD", "Str0ng!Passw0rd"),
                            encrypt=E("E2E_ENCRYPT", "optional"), trust_server_certificate=True,
                            driver=E("E2E_DRIVER", "auto"), login_timeout=60)


def run_script(side: str, path: Path) -> None:
    c = connect(conn_cfg(side, "master"), autocommit=True)
    try:
        for batch in re.split(r"^\s*GO\s*$", path.read_text(encoding="utf-8"), flags=re.M | re.I):
            if batch.strip():
                c.execute(batch)
    finally:
        c.close()


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    run_script("source", HERE / "seed_source.sql")
    c = connect(conn_cfg("target", "master"), autocommit=True)
    c.execute("IF DB_ID('e2e_tgt') IS NOT NULL BEGIN ALTER DATABASE e2e_tgt SET SINGLE_USER WITH ROLLBACK "
              "IMMEDIATE; DROP DATABASE e2e_tgt; END")
    c.execute("CREATE DATABASE e2e_tgt")
    c.close()
    cfg = Config(source=conn_cfg("source", "e2e_src"), target=conn_cfg("target", "e2e_tgt"))
    cfg.migration.workers = int(E("E2E_WORKERS", "6"))
    cfg.migration.chunk_rows = 50_000
    cfg.migration.state_file = str(tmp / "state.json")
    cfg.validation.sample_full_pct = 10
    cfg.report_dir = str(tmp / "report")
    run = Migrator(cfg).migrate()
    from mssql_ocp_migrate import report
    print("report:", report.write(cfg, run=run))
    src = connect(conn_cfg("source", "e2e_src"))
    tgt = connect(conn_cfg("target", "e2e_tgt"))
    yield cfg, run, src, tgt
    src.close()
    tgt.close()


def both(m, sql):
    _, _, src, tgt = m
    return src.query(sql), tgt.query(sql)


def test_run_and_validation_pass(migrated):
    cfg, run, _, _ = migrated
    assert not run.ddl_errors, run.ddl_errors
    assert all(not t.failed_chunks for t in run.tables.values())
    assert run.validation.ok, run.validation.summary()
    assert run.tables["sales.Order"].chunks > 5            # really chunked and parallel


def test_identity_values_gaps_and_seed_preserved(migrated):
    a, b = both(migrated, "SELECT CHECKSUM_AGG(CustomerId), COUNT(*), MAX(CustomerId) FROM sales.Customer")
    assert a == b
    a, b = both(migrated, "SELECT CAST(IDENT_CURRENT('sales.Customer') AS bigint), "
                          "CAST(IDENT_INCR('sales.Customer') AS int), CAST(IDENT_SEED('sales.Customer') AS int)")
    assert a == b


def test_high_precision_temporal_values_exact(migrated):
    sql = ("SELECT TOP 500 CustomerId, CONVERT(varchar(27), CreatedAt, 121), CONVERT(varchar(23), LegacyStamp, 121) "
           "FROM sales.Customer ORDER BY CustomerId DESC")
    a, b = both(migrated, sql)
    assert a == b
    sql = ("SELECT TOP 500 OrderId, CONVERT(varchar(40), PlacedAt, 121), CONVERT(varchar(16), OrderTime) "
           "FROM sales.[Order] WHERE OrderTime IS NOT NULL ORDER BY OrderId")
    a, b = both(migrated, sql)
    assert a == b


def test_clr_types_and_lobs(migrated):
    a, b = both(migrated, "SELECT Id, Loc.STAsText(), Node.ToString() FROM dbo.Place ORDER BY Id")
    assert a == b
    a, b = both(migrated, "SELECT (SELECT SUM(CAST(DATALENGTH(Payload) AS bigint)) FROM sales.[Order]), "
                          "(SELECT SUM(CAST(DATALENGTH(Notes) AS bigint)) FROM sales.Customer), "
                          "(SELECT COUNT(*) FROM sales.[Order] WHERE Meta IS NOT NULL)")
    assert a == b


def test_heap_with_duplicate_rows(migrated):
    a, b = both(migrated, "SELECT COUNT(*), COUNT(DISTINCT CONCAT(At, Source, Message)) FROM dbo.EventLog")
    assert a == b and a[0][0] > a[0][1]


def test_rowversion_and_computed_columns(migrated):
    a, b = both(migrated, "SELECT SUM(Total) FROM sales.[Order]")
    assert a == b
    _, _, _, tgt = migrated
    assert tgt.scalar("SELECT COUNT(*) FROM sales.Customer WHERE Rv IS NULL") == 0


def test_constraints_trusted_and_objects_created(migrated):
    _, _, _, tgt = migrated
    assert tgt.scalar("SELECT COUNT(*) FROM sys.foreign_keys WHERE is_not_trusted = 1") == 0
    assert tgt.scalar("SELECT COUNT(*) FROM sys.check_constraints WHERE is_not_trusted = 1") == 0
    for obj in ("sales.vOrderTotals", "sales.vTopCustomers", "dbo.fnNet", "sales.usp_CustomerOrders",
                "sales.trg_Order_Audit", "sales.trg_Customer_Touch", "dbo.Orders", "dbo.InvoiceNo"):
        assert tgt.scalar("SELECT OBJECT_ID(?)", [obj]) is not None, obj
    assert tgt.scalar("SELECT is_disabled FROM sys.triggers WHERE name = 'trg_Order_Audit'") in (1, True)
    a, b = both(migrated, "SELECT name, is_unique, has_filter FROM sys.indexes "
                          "WHERE object_id = OBJECT_ID('sales.[Order]') ORDER BY name")
    assert a == b
    assert int(tgt.scalar("SELECT CAST(current_value AS bigint) FROM sys.sequences WHERE name='InvoiceNo'")) >= 1001


def test_validation_detects_tampering(migrated):
    cfg, _, _, tgt = migrated
    tgt.execute("UPDATE sales.OrderLine SET Qty = Qty + 1 WHERE OrderId = 5 AND LineNo = 2")
    try:
        from mssql_ocp_migrate.cli import main
        assert main(["validate", "-c", _write_cfg(cfg)]) == 1
    finally:
        tgt.execute("UPDATE sales.OrderLine SET Qty = Qty - 1 WHERE OrderId = 5 AND LineNo = 2")


def _write_cfg(cfg) -> str:
    import json
    p = Path(cfg.report_dir) / "cfg.json"
    d = cfg.to_dict(redact=False)
    p.write_text(json.dumps(d))
    return str(p)
