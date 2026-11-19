"""End-to-end test against two real SQL Server instances (source and target).

    docker compose -f tests/e2e/docker-compose.yml up -d      # or point at your own servers
    MSSQL_E2E=1 pytest tests/e2e -v

Environment (defaults match docker-compose.yml):
    E2E_SRC_HOST=127.0.0.1 E2E_SRC_PORT=14331  E2E_TGT_HOST=127.0.0.1 E2E_TGT_PORT=14332
    E2E_USER=sa  E2E_PASSWORD=Str0ng!Passw0rd  E2E_ENCRYPT=optional  E2E_DRIVER=auto
"""
import os
import re
import time
import traceback
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


def _diagnose(database: str, side: str = "target") -> None:
    """Session state and a few probe queries on a fresh connection, printed for CI logs."""
    for autocommit in (True,):        # a manual-commit probe would hold locks the rest of the suite waits on
        c = connect(conn_cfg(side, database), autocommit=autocommit)
        try:
            for sql in ("SELECT @@TRANCOUNT, XACT_STATE(), @@OPTIONS, @@VERSION",
                        "SELECT t.name, t.transaction_type, t.transaction_begin_time FROM sys.dm_tran_session_transactions s "
                        "JOIN sys.dm_tran_active_transactions t ON t.transaction_id = s.transaction_id "
                        "WHERE s.session_id = @@SPID",
                        "SELECT name, CAST(value AS int), CAST(value_in_use AS int) FROM sys.configurations "
                        "WHERE name IN ('user options', 'remote proc trans', 'cursor threshold')",
                        "SELECT is_read_committed_snapshot_on, snapshot_isolation_state, is_memory_optimized_elevate_to_snapshot_on, "
                        "is_cursor_close_on_commit_on, is_local_cursor_default, delayed_durability_desc, is_cdc_enabled "
                        "FROM sys.databases WHERE name = DB_NAME()",
                        "SELECT COUNT_BIG(*) FROM dbo.[Session] WITH (SNAPSHOT)",
                        "SELECT COUNT_BIG(*) FROM dbo.[Session]",
                        "SELECT COUNT_BIG(*) FROM sales.Customer"):
                try:
                    print(f"diag autocommit={autocommit}: {sql} -> {c.query(sql)}", flush=True)
                except Exception as exc:
                    print(f"diag autocommit={autocommit}: {sql} -> {type(exc).__name__}: {exc}", flush=True)
        finally:
            c.close()


def run_script(side: str, path: Path) -> None:
    c = connect(conn_cfg(side, "master"), autocommit=True)
    try:
        for batch in re.split(r"^\s*GO\s*$", path.read_text(encoding="utf-8"), flags=re.M | re.I):
            if batch.strip():
                t0 = time.monotonic()
                try:
                    c.execute(batch)
                    if time.monotonic() - t0 > 10:
                        print(f"  slow seed batch ({time.monotonic() - t0:.0f}s): {batch.strip()[:80]!r}", flush=True)
                except Exception as exc:
                    raise RuntimeError(f"{path.name}: {exc}\n--- batch ---\n{batch.strip()[:600]}") from exc
    finally:
        c.close()


def source_major() -> int:
    c = connect(conn_cfg("source", "master"))
    try:
        return int(c.scalar("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)"))
    finally:
        c.close()


def target_major() -> int:
    c = connect(conn_cfg("target", "master"))
    try:
        return int(c.scalar("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)"))
    finally:
        c.close()


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    t0 = time.monotonic()
    run_script("source", HERE / "seed_source.sql")
    if source_major() >= 17:
        run_script("source", HERE / "seed_2025.sql")
    print(f"seeded source in {time.monotonic() - t0:.0f}s", flush=True)
    c = connect(conn_cfg("target", "master"), autocommit=True)
    c.execute("IF DB_ID('e2e_tgt') IS NOT NULL BEGIN ALTER DATABASE e2e_tgt SET SINGLE_USER WITH ROLLBACK "
              "IMMEDIATE; DROP DATABASE e2e_tgt; END")
    c.execute("CREATE DATABASE e2e_tgt")
    c.close()
    cfg = Config(source=conn_cfg("source", "e2e_src"), target=conn_cfg("target", "e2e_tgt"))
    cfg.migration.workers = int(E("E2E_WORKERS", "6"))
    cfg.migration.copy_agent_jobs = True
    cfg.migration.sync_enable_change_tracking = True       # record a baseline so test_sync can catch up
    cfg.migration.copy_linked_servers = True
    os.environ.setdefault("TARGET_DB_MASTER_KEY_PASSWORD", "E2e!TargetMasterKey1")
    cfg.migration.chunk_rows = 50_000
    cfg.migration.state_file = str(tmp / "state.json")
    cfg.validation.sample_full_pct = 10
    cfg.report_dir = str(tmp / "report")
    t0 = time.monotonic()
    try:
        run = Migrator(cfg, progress=lambda m: print(f"[{time.monotonic() - t0:7.1f}s] {m}", flush=True)).migrate()
    except Exception:
        traceback.print_exc()        # now, not in the summary: a later test may hang and lose it
        _diagnose("e2e_tgt")
        raise
    print("run warnings:", *run.warnings, sep="\n  ", flush=True)
    print("ddl errors:", *run.ddl_errors, sep="\n  ", flush=True)
    print("failed chunks:", *[f"{n}: {c} {r.errors[:3]}" for n, r in run.tables.items() for c in r.failed_chunks],
          sep="\n  ", flush=True)
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


def test_index_options_compression_and_statistics(migrated):
    a, b = both(migrated, """
        SELECT OBJECT_SCHEMA_NAME(i.object_id) + '.' + OBJECT_NAME(i.object_id), ISNULL(i.name, '(heap)'),
               i.fill_factor, i.is_padded, i.ignore_dup_key, i.allow_row_locks, i.allow_page_locks,
               p.data_compression_desc, p.partition_number
        FROM sys.indexes i JOIN sys.partitions p ON p.object_id = i.object_id AND p.index_id = i.index_id
        JOIN sys.tables t ON t.object_id = i.object_id AND t.is_ms_shipped = 0
        ORDER BY 1, 2, p.partition_number""")
    assert a == b
    assert ("sales.Order", "IX_Order_Date", 80, True, False, True, False, "PAGE", 1) in [tuple(r) for r in b]
    assert ("dbo.EventLog", "(heap)", 0, False, False, True, True, "ROW", 1) in [tuple(r) for r in b]
    a, b = both(migrated, """
        SELECT s.name, s.no_recompute, s.has_filter, s.filter_definition,
               STRING_AGG(c.name, ',') WITHIN GROUP (ORDER BY sc.stats_column_id)
        FROM sys.stats s JOIN sys.stats_columns sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
        JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
        WHERE s.user_created = 1 GROUP BY s.name, s.no_recompute, s.has_filter, s.filter_definition""")
    assert a == b and len(a) == 1


def test_extended_properties_security_and_settings(migrated):
    a, b = both(migrated, """
        SELECT ep.class, ep.name, CAST(ep.value AS nvarchar(4000)),
               CASE ep.class WHEN 3 THEN SCHEMA_NAME(ep.major_id) WHEN 0 THEN NULL ELSE OBJECT_NAME(ep.major_id) END,
               CASE ep.class WHEN 1 THEN COL_NAME(ep.major_id, ep.minor_id)
                             WHEN 7 THEN (SELECT i.name FROM sys.indexes i
                                          WHERE i.object_id = ep.major_id AND i.index_id = ep.minor_id) END
        FROM sys.extended_properties ep
        WHERE ep.class IN (0, 3) OR (ep.class IN (1, 7) AND ep.major_id IN (
            SELECT object_id FROM sys.objects WHERE is_ms_shipped = 0))
        ORDER BY 1, 2, 4, 5""")
    assert a == b and len(a) == 7
    a, b = both(migrated, """
        SELECT dp.name, dp.type, dp.default_schema_name FROM sys.database_principals dp
        WHERE dp.name IN ('reporting', 'etl_svc') ORDER BY 1""")
    assert a == b and len(a) == 2
    a, b = both(migrated, """
        SELECT r.name, m.name FROM sys.database_role_members rm
        JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
        JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
        WHERE m.name = 'etl_svc' ORDER BY 1""")
    assert a == b and len(a) == 2
    a, b = both(migrated, """
        SELECT p.class_desc, p.permission_name, p.state_desc, g.name,
               CASE p.class WHEN 1 THEN OBJECT_NAME(p.major_id) WHEN 3 THEN SCHEMA_NAME(p.major_id) END,
               CASE WHEN p.class = 1 THEN COL_NAME(p.major_id, p.minor_id) END
        FROM sys.database_permissions p JOIN sys.database_principals g ON g.principal_id = p.grantee_principal_id
        WHERE g.name IN ('reporting', 'etl_svc') AND p.permission_name <> 'CONNECT' ORDER BY 1, 2, 4, 5""")
    assert rows(a) == rows(b)
    assert len(a) == 6 and ("SERVICE", "SEND", "GRANT", "reporting", None, None) in rows(b)  # broker grant
    a, b = both(migrated, "SELECT p.name FROM sys.schemas s JOIN sys.database_principals p "
                          "ON p.principal_id = s.principal_id WHERE s.name = 'audit'")
    assert [tuple(r) for r in a] == [tuple(r) for r in b] == [("etl_svc",)]
    a, b = both(migrated, "SELECT compatibility_level, is_read_committed_snapshot_on, snapshot_isolation_state "
                          "FROM sys.databases WHERE name = DB_NAME()")
    assert [tuple(r) for r in a] == [tuple(r) for r in b] == [(150, True, 1)]


def rows(r):
    return [tuple(x) for x in r]


def test_masking_sparse_column_set_and_typed_xml(migrated):
    a, b = both(migrated, """
        SELECT c.name, c.is_sparse, c.is_column_set, mc.masking_function, xsc.name, c.is_xml_document
        FROM sys.columns c LEFT JOIN sys.masked_columns mc ON mc.object_id = c.object_id AND mc.column_id = c.column_id
        LEFT JOIN sys.xml_schema_collections xsc ON xsc.xml_collection_id = c.xml_collection_id AND c.xml_collection_id > 1
        WHERE c.object_id = OBJECT_ID('dbo.Contact') ORDER BY c.column_id""")
    assert rows(a) == rows(b) and len(a) == 8
    a, b = both(migrated, "SELECT COUNT(*), COUNT(Fax), SUM(CASE WHEN Note IS NULL THEN 0 ELSE 1 END), "
                          "CHECKSUM_AGG(CHECKSUM(Email, Phone, Rep)) FROM dbo.Contact")
    assert rows(a) == rows(b) and a[0][0] == 3000


def test_row_level_security_on_after_load(migrated):
    q = """SELECT p.name, p.is_enabled, p.is_schema_bound, sp.predicate_type_desc, sp.operation_desc,
                  OBJECT_NAME(sp.target_object_id)
           FROM sys.security_policies p JOIN sys.security_predicates sp ON sp.object_id = p.object_id ORDER BY 4"""
    a, b = both(migrated, q)
    assert rows(a) == rows(b) and len(a) == 2 and all(r[1] for r in b)


def test_table_type_partitioning_and_compression(migrated):
    a, b = both(migrated, "SELECT TYPE_NAME(user_type_id), is_table_type FROM sys.types WHERE name = 'IdList'")
    assert rows(a) == rows(b) == [("IdList", True)]
    a, b = both(migrated, """
        SELECT i.name, p.partition_number, p.data_compression_desc, p.rows
        FROM sys.partitions p JOIN sys.indexes i ON i.object_id = p.object_id AND i.index_id = p.index_id
        WHERE p.object_id = OBJECT_ID('dbo.Ledger') ORDER BY 1, 2""")
    assert rows(a) == rows(b) and len(a) == 8
    a, b = both(migrated, "SELECT pf.name, pf.boundary_value_on_right, CAST(rv.value AS date) FROM sys.partition_functions pf "
                          "JOIN sys.partition_range_values rv ON rv.function_id = pf.function_id ORDER BY 3")
    assert rows(a) == rows(b) and len(a) == 3


def test_xml_spatial_temporal_memory_optimized(migrated):
    a, b = both(migrated, "SELECT name, type_desc FROM sys.indexes WHERE type IN (3, 4) ORDER BY 1")
    assert rows(a) == rows(b) and len(a) == 3
    a, b = both(migrated, """
        SELECT t.temporal_type_desc, OBJECT_NAME(t.history_table_id), t.history_retention_period,
               t.history_retention_period_unit_desc FROM sys.tables t WHERE t.name = 'Price'""")
    assert rows(a) == rows(b) and a[0][0] == "SYSTEM_VERSIONED_TEMPORAL_TABLE"
    a, b = both(migrated, "SELECT COUNT(*) FROM dbo.PriceHistory")
    assert rows(a) == rows(b) and a[0][0] > 0
    a, b = both(migrated, "SELECT is_memory_optimized, durability_desc FROM sys.tables WHERE name = 'Session'")
    assert rows(a) == rows(b) == [(True, "SCHEMA_AND_DATA")]
    a, b = both(migrated, "SELECT COUNT(*), SUM(CAST(UserId AS bigint)) FROM dbo.Session")
    assert rows(a) == rows(b) and a[0][0] == 5000
    a, b = both(migrated, "SELECT name, type_desc, bucket_count FROM sys.hash_indexes "
                          "WHERE object_id = OBJECT_ID('dbo.Session')")
    assert rows(a) == rows(b) == [("PK_Session", "NONCLUSTERED HASH", 16384)]


def test_broker_certificates_signatures_ddl_triggers_and_plan_guides(migrated):
    for q in ["SELECT name FROM sys.service_message_types WHERE name LIKE '//e2e/%'",
              "SELECT name FROM sys.service_contracts WHERE name LIKE '//e2e/%'",
              "SELECT name, is_receive_enabled FROM sys.service_queues WHERE is_ms_shipped = 0",
              "SELECT name FROM sys.services WHERE name LIKE '//e2e/%'",
              "SELECT name, is_disabled FROM sys.triggers WHERE parent_class = 0 ORDER BY name",
              "SELECT name, is_disabled, query_text FROM sys.plan_guides",
              "SELECT is_broker_enabled FROM sys.databases WHERE name = DB_NAME()"]:
        a, b = both(migrated, q)
        assert rows(a) == rows(b) and a, q
    a, b = both(migrated, "SELECT c.name, OBJECT_NAME(cp.major_id) FROM sys.crypt_properties cp "
                          "JOIN sys.certificates c ON c.thumbprint = cp.thumbprint")
    assert rows(a) == rows(b) == [("SignCert", "usp_Signed")]
    a, b = both(migrated, "SELECT dp.name, dp.type_desc FROM sys.database_principals dp WHERE dp.name = 'signer'")
    assert rows(a) == rows(b)
    a, b = both(migrated, "EXEC dbo.usp_Count")  # the table-type parameter defaults to an empty table
    assert rows(a) == rows(b)


def test_change_tracking_cdc_and_scoped_configuration(migrated):
    for q in ["SELECT retention_period, retention_period_units_desc, is_auto_cleanup_on "
              "FROM sys.change_tracking_databases WHERE database_id = DB_ID()",
              "SELECT OBJECT_NAME(object_id), is_track_columns_updated_on FROM sys.change_tracking_tables ORDER BY 1",
              "SELECT CAST(value AS int) FROM sys.database_scoped_configurations WHERE name = 'MAXDOP'",
              "SELECT is_cdc_enabled FROM sys.databases WHERE name = DB_NAME()",
              "SELECT capture_instance, supports_net_changes FROM cdc.change_tables"]:
        a, b = both(migrated, q)
        assert rows(a) == rows(b) and a, q
    a, b = both(migrated, "SELECT column_name FROM cdc.captured_columns ORDER BY column_ordinal")
    assert rows(a) == rows(b) and len(a) == 3


def test_login_job_and_linked_server(migrated):
    _, run, src, tgt = migrated
    q = "SELECT sp.name, sp.sid, sp.is_disabled FROM sys.server_principals sp WHERE sp.name = 'e2e_app'"
    a, b = src.query(q), tgt.query(q)
    assert a[0][1] == b[0][1]                       # same SID: the user maps without remapping
    assert tgt.query("SELECT COUNT(*) FROM msdb.dbo.sysjobs WHERE name = N'e2e nightly' AND enabled = 0")[0][0] == 1
    assert tgt.query("SELECT database_name FROM msdb.dbo.sysjobsteps s JOIN msdb.dbo.sysjobs j ON j.job_id = s.job_id "
                     "WHERE j.name = N'e2e nightly'")[0][0] == "e2e_tgt"
    assert rows(tgt.query("SELECT name, is_rpc_out_enabled FROM sys.servers WHERE name = N'E2E_LOOP'")) == \
        [("E2E_LOOP", True)]


def test_module_settings_trigger_order_owners_and_disabled_indexes(migrated):
    for q in ["SELECT m.uses_ansi_nulls, m.uses_quoted_identifier FROM sys.sql_modules m "
              "WHERE m.object_id = OBJECT_ID('dbo.usp_Legacy')",
              "SELECT OBJECT_NAME(te.object_id), te.type_desc, te.is_first, te.is_last FROM sys.trigger_events te "
              "WHERE te.object_id = OBJECT_ID('sales.trg_Customer_First')",
              "SELECT USER_NAME(principal_id) FROM sys.objects WHERE object_id = OBJECT_ID('dbo.usp_Count')",
              "SELECT name, is_disabled FROM sys.indexes WHERE name = 'IX_Order_Status_Off'"]:
        a, b = both(migrated, q)
        assert rows(a) == rows(b) and a, q
    assert rows(both(migrated, "SELECT uses_ansi_nulls FROM sys.sql_modules WHERE object_id = "
                               "OBJECT_ID('dbo.usp_Legacy')")[1]) == [(False,)]
    a, b = both(migrated, "EXEC dbo.usp_Legacy")            # ANSI_NULLS OFF: NULL = NULL is true
    assert rows(a) == rows(b) == [("legacy", 1)]
    assert rows(both(migrated, "SELECT is_disabled FROM sys.indexes WHERE name = 'IX_Order_Status_Off'")[1]) == [(True,)]


def test_sql_variant_keeps_base_types(migrated):
    q = """SELECT Id, CAST(SQL_VARIANT_PROPERTY(V, 'BaseType') AS nvarchar(30)),
                  CAST(SQL_VARIANT_PROPERTY(V, 'Precision') AS int), CAST(SQL_VARIANT_PROPERTY(V, 'Scale') AS int),
                  CAST(SQL_VARIANT_PROPERTY(V, 'MaxLength') AS int),
                  CAST(SQL_VARIANT_PROPERTY(V, 'Collation') AS nvarchar(128)),
                  CONVERT(varchar(max), CAST(V AS varbinary(8000)), 1)
           FROM dbo.Variant ORDER BY Id"""
    a, b = both(migrated, q)
    assert rows(a) == rows(b) and len(a) == 20
    assert rows(a)[1][1] == "bigint" and rows(a)[8][1] == "datetime2"


def test_rowguidcol_not_for_replication_utf8_and_legacy_types(migrated):
    a, b = both(migrated, """SELECT c.name, c.is_rowguidcol, ic.is_not_for_replication, c.collation_name
                             FROM sys.columns c LEFT JOIN sys.identity_columns ic
                               ON ic.object_id = c.object_id AND ic.column_id = c.column_id
                             WHERE c.object_id = OBJECT_ID('dbo.Legacy') ORDER BY c.column_id""")
    assert rows(a) == rows(b) and len(a) == 6
    a, b = both(migrated, """SELECT Id, CONVERT(varchar(64), HASHBYTES('SHA2_256', CAST(Utf8 AS varbinary(max))), 2),
                                    CONVERT(varchar(64), HASHBYTES('SHA2_256', CAST(CAST(Notes AS varchar(max)) AS varbinary(max))), 2),
                                    CONVERT(varchar(64), HASHBYTES('SHA2_256', CAST(CAST(UNotes AS nvarchar(max)) AS varbinary(max))), 2),
                                    CONVERT(varchar(64), HASHBYTES('SHA2_256', CAST(Blob AS varbinary(max))), 2), RowGuid
                             FROM dbo.Legacy ORDER BY Id""")
    assert rows(a) == rows(b) and len(a) == 3


def test_server_objects_messages_language_roles_operator(migrated):
    _, _, src, tgt = migrated
    assert tgt.query("SELECT text FROM sys.messages WHERE message_id = 50010 AND language_id = 1033")[0][0] == \
        "Order %d is on hold"
    assert tgt.query("SELECT default_language_name FROM sys.server_principals WHERE name = 'e2e_app'")[0][0] == "British"
    assert tgt.query("""SELECT COUNT(*) FROM sys.server_role_members m
                        JOIN sys.server_principals r ON r.principal_id = m.role_principal_id
                        JOIN sys.server_principals l ON l.principal_id = m.member_principal_id
                        WHERE r.name = 'e2e_monitor' AND l.name = 'e2e_app'""")[0][0] == 1
    perms = rows(tgt.query("""SELECT g.name, p.permission_name FROM sys.server_permissions p
                              JOIN sys.server_principals g ON g.principal_id = p.grantee_principal_id
                              WHERE g.name IN ('e2e_monitor', 'e2e_app') AND p.permission_name <> 'CONNECT SQL'
                              ORDER BY 1, 2"""))
    assert perms == [("e2e_app", "VIEW ANY DATABASE"), ("e2e_monitor", "VIEW SERVER STATE")]
    assert rows(tgt.query("""SELECT o.name, j.notify_level_email FROM msdb.dbo.sysjobs j
                             JOIN msdb.dbo.sysoperators o ON o.id = j.notify_email_operator_id
                             WHERE j.name = N'e2e nightly'""")) == [("e2e dba", 2)]


def test_assessment_ran_and_found_the_cross_database_reference(migrated):
    _, run, _, _ = migrated
    assert run.assessment is not None
    items = {(f.area, f.item) for f in run.assessment.findings}
    assert ("Code", "dbo.usp_CrossDb") in items
    assert ("Server", "user-defined messages") in items


@pytest.mark.skipif(os.environ.get("MSSQL_E2E") != "1" or source_major() < 17 or target_major() < 17,
                    reason="SQL Server 2025 on both sides")
def test_json_and_vector(migrated):
    a, b = both(migrated, "SELECT Id, CAST(Doc AS nvarchar(max)), CAST(Vec AS nvarchar(max)) FROM dbo.Embedding ORDER BY Id")
    assert rows(a) == rows(b) and len(a) == 3
    a, b = both(migrated, "SELECT c.name, TYPE_NAME(c.system_type_id), c.vector_dimensions FROM sys.columns c "
                          "WHERE c.object_id = OBJECT_ID('dbo.Embedding') ORDER BY c.column_id")
    assert rows(a) == rows(b)


def test_validation_detects_tampering(migrated):
    cfg, _, _, tgt = migrated
    tgt.execute("UPDATE sales.OrderLine SET Qty = Qty + 1 WHERE OrderId = 5 AND [LineNo] = 2")
    try:
        from mssql_ocp_migrate.cli import main
        assert main(["validate", "-c", _write_cfg(cfg)]) == 1
    finally:
        tgt.execute("UPDATE sales.OrderLine SET Qty = Qty - 1 WHERE OrderId = 5 AND [LineNo] = 2")


def _write_cfg(cfg) -> str:
    import json
    p = Path(cfg.report_dir) / "cfg.json"
    d = cfg.to_dict(redact=False)
    p.write_text(json.dumps(d))
    return str(p)


def test_sync_catches_up_with_changes_made_after_the_copy(migrated):
    """Online cut-over: change the source after migrate, sync, then a final sync and validation."""
    cfg, _, src, tgt = migrated
    from mssql_ocp_migrate.sync import Syncer
    src.execute("INSERT sales.Customer (Code, Name) VALUES ('SYNC0001', N'after the copy')")
    src.execute("UPDATE sales.Customer SET Name = N'renamed' WHERE CustomerId = 100")
    src.execute("DELETE FROM sales.OrderLine WHERE OrderId = 7")
    src.execute("UPDATE sales.OrderLine SET Qty = Qty + 10 WHERE OrderId = 9")
    src.execute("INSERT dbo.Variant VALUES (21, CAST(7 AS smallint))")
    res = Syncer(cfg, progress=print).run()
    assert res.rows >= 5 and not [t for t in res.tables if "OrderLine" in t and res.tables[t]["upserted"] == 0]
    for q in ["SELECT CustomerId, Code, Name FROM sales.Customer WHERE Code = 'SYNC0001' OR CustomerId = 100 ORDER BY 1",
              "SELECT COUNT(*), SUM(CAST(Qty AS bigint)) FROM sales.OrderLine WHERE OrderId IN (7, 9)",
              "SELECT Id, CAST(SQL_VARIANT_PROPERTY(V, 'BaseType') AS nvarchar(30)) FROM dbo.Variant WHERE Id = 21"]:
        assert rows(src.query(q)) == rows(tgt.query(q)), q
    assert tgt.query("SELECT COUNT(*) FROM sys.foreign_keys WHERE is_disabled = 1")[0][0] == 0
    src.execute("DELETE FROM sales.Customer WHERE Code = 'SYNC0001'")
    final = Syncer(cfg, progress=print).run(final=True)
    assert final.from_version == res.to_version
    assert tgt.query("SELECT COUNT(*) FROM sys.foreign_keys WHERE is_not_trusted = 1 AND is_disabled = 0 "
                     "AND name NOT IN (SELECT name FROM sys.foreign_keys WHERE 1 = 0)")[0][0] == \
        src.query("SELECT COUNT(*) FROM sys.foreign_keys WHERE is_not_trusted = 1 AND is_disabled = 0")[0][0]
    from mssql_ocp_migrate.cli import main
    assert main(["validate", "-c", _write_cfg(cfg)]) == 0
