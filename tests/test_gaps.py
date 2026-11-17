"""0.4.0: gaps found by comparing with other migration tools (dbatools, DMA/Azure migration, DACPAC)
and with what SQL Server on Linux doesn't support."""
import json

import pytest

from mssql_ocp_migrate import ddl
from mssql_ocp_migrate.adapter import ModuleDef
from mssql_ocp_migrate.assess import (BLOCKER, INFO, WARNING, Assessor, Finding, render, scan_code)
from mssql_ocp_migrate.engine import MigrationError, Migrator, RunResult
from mssql_ocp_migrate.model import Column, DatabaseExtras, Index, Principal, Table
from mssql_ocp_migrate.sqlserver import SqlServer, variant_read_expr, variant_write_sql
from mssql_ocp_migrate.sync import Syncer, changes_sql, load_version, save_version, sync_skip_reason, SyncResult
from mssql_ocp_migrate.validate import extras_diff, module_diffs

from test_objects import RecordingTarget, cfg, tbl


# ================================================================== modules
class FailingTarget(RecordingTarget):
    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("CREATE PROCEDURE"):
            raise RuntimeError("Invalid column name 'x'")


def test_session_settings_are_restored_even_when_a_module_fails():
    legacy = ModuleDef("PROCEDURE", "dbo", "p", "CREATE PROCEDURE dbo.p AS SELECT \"x\"",
                       meta={"ansi_nulls": False, "quoted_identifier": False})
    tgt, res = FailingTarget(), RunResult(0)
    Migrator(cfg(), factory=None, progress=lambda _: None)._create_modules(tgt, [legacy], res)
    assert tgt.sql[:3] == ["SET ANSI_NULLS OFF", "SET QUOTED_IDENTIFIER OFF", legacy.definition]
    assert tgt.sql[-2:] == ["SET ANSI_NULLS ON", "SET QUOTED_IDENTIFIER ON"]
    assert res.ddl_errors and "Invalid column" in res.ddl_errors[0]["error"]


def test_schema_script_keeps_module_settings():
    src_mods = [ModuleDef("PROCEDURE", "dbo", "p", "CREATE PROCEDURE dbo.p AS SELECT 1", meta={"ansi_nulls": False})]

    class Src:
        def list_tables(self):
            return [tbl()]

        def list_modules(self):
            return src_mods

        def list_schemas(self):
            return ["dbo"]

        def database_extras(self):
            return None

        def rule_default_bindings(self):
            return []

        def close(self):
            pass

    script = Migrator(cfg(), factory=lambda s, a: Src()).schema_script()
    assert "SET ANSI_NULLS OFF;\nGO\nCREATE PROCEDURE dbo.p AS SELECT 1\nGO\nSET ANSI_NULLS ON;\nGO" in script


def test_module_diffs_compare_text_settings_state_and_order():
    s = [ModuleDef("VIEW", "dbo", "v", "CREATE VIEW dbo.v AS SELECT 1 AS x\r\n", meta={"ansi_nulls": True}),
         ModuleDef("PROCEDURE", "dbo", "p", "CREATE PROCEDURE dbo.p AS SELECT 1", meta={"quoted_identifier": False}),
         ModuleDef("TRIGGER", "dbo", "t", "CREATE TRIGGER t", "dbo.T", True, {"trigger_order": [("FIRST", "INSERT")]})]
    t = [ModuleDef("VIEW", "dbo", "v", "CREATE VIEW dbo.v AS SELECT 1 AS x   ", meta={"ansi_nulls": True}),
         ModuleDef("PROCEDURE", "dbo", "p", "CREATE PROCEDURE dbo.p AS SELECT 2", meta={"quoted_identifier": True}),
         ModuleDef("TRIGGER", "dbo", "t", "CREATE TRIGGER t", "dbo.T", False, {})]
    assert module_diffs(s, t) == [
        "procedure dbo.p: definition differs", "procedure dbo.p: QUOTED_IDENTIFIER OFF -> ON",
        "trigger dbo.t: disabled True -> False", "trigger dbo.t: trigger order [('FIRST', 'INSERT')] -> []"]


# ================================================================== tables, columns, indexes, owners
def test_disabled_indexes_are_created_disabled_before_the_load():
    t = tbl(indexes=[Index("IX_on", [("Year", False)]), Index("IX_off", [("Year", True)], is_disabled=True)])
    assert ddl.disabled_indexes_sql(t) == [
        "CREATE NONCLUSTERED INDEX [IX_off] ON [dbo].[T] ([Year] DESC)",
        "ALTER INDEX [IX_off] ON [dbo].[T] DISABLE"]
    assert ddl.post_load_sql(t) == ["CREATE NONCLUSTERED INDEX [IX_on] ON [dbo].[T] ([Year])"]
    assert Index("x", [], is_disabled=True).options()["disabled"] is True


def test_column_options_rowguidcol_not_for_replication_vector():
    c = Column("Id", "int", nullable=False, is_identity=True, identity_seed=1, identity_increment=1,
               identity_not_for_replication=True)
    assert ddl.column_sql(c) == "[Id] int IDENTITY(1,1) NOT FOR REPLICATION NOT NULL"
    g = Column("G", "uniqueidentifier", nullable=False, is_rowguidcol=True)
    assert ddl.column_sql(g) == "[G] uniqueidentifier ROWGUIDCOL NOT NULL"
    assert ddl.type_sql(Column("E", "vector", vector_dimensions=1536)) == "vector(1536)"
    assert ddl.type_sql(Column("E", "vector", vector_dimensions=3, vector_base_type="float16")) == "vector(3, float16)"
    assert ddl.type_sql(Column("J", "json", -1)) == "json"


def test_filestream_and_ansi_padding_columns_are_reported():
    row = (1, "Doc", "varbinary", 0, 0, "sys", "varbinary", -1, 0, 0, 1, 0, None, None, 0, None, 0, None, None,
           None, 0, 0, None, None, None, None, None, 0, None, None, 0, 1, 0, 0, 1, None, None)
    c = SqlServer.column_from_row(row)
    assert c.is_filestream and ddl.column_sql(c) == "[Doc] varbinary(max) NULL"     # no FILESTREAM on Linux
    old = row[:31]                                                                    # 0.3.0-shaped row
    assert not SqlServer.column_from_row(old).is_filestream


def test_object_owners_are_applied_after_principals_and_validated():
    ex = DatabaseExtras(principals=[Principal("app", "NO_LOGIN")],
                        object_owners=[("dbo", "usp_Owned", "app"), ("dbo", "usp_Other", "ghost")])
    tgt = RecordingTarget()
    Migrator(cfg(), factory=None, progress=lambda _: None)._apply_extras(tgt, ex, RunResult(0))
    assert "ALTER AUTHORIZATION ON OBJECT::[dbo].[usp_Owned] TO [app]" in tgt.sql
    assert tgt.sql.index("CREATE USER [app] WITHOUT LOGIN") < tgt.sql.index(
        "ALTER AUTHORIZATION ON OBJECT::[dbo].[usp_Owned] TO [app]")
    diffs = extras_diff(cfg(), ex, DatabaseExtras(principals=[Principal("app", "NO_LOGIN")]))
    assert "object dbo.usp_Owned: owner app -> schema owner" in diffs


# ================================================================== sql_variant
def test_sql_variant_values_keep_their_base_type():
    assert variant_write_sql("int|10|0|4||0x0000002A") == "CAST(CAST(0x0000002A AS int) AS sql_variant)"
    assert variant_write_sql("datetime2|27|7|8||0x00") == "CAST(CAST(0x00 AS datetime2(7)) AS sql_variant)"
    assert variant_write_sql("decimal|10|2|9||0x0A02") == "CAST(CAST(0x0A02 AS decimal(10,2)) AS sql_variant)"
    assert variant_write_sql("nvarchar|0|0|20||it's|ok") == "CAST(CAST(N'it''s|ok' AS nvarchar(10)) AS sql_variant)"
    assert variant_write_sql("varchar|0|0|5|Greek_CI_AS|x") == \
        "CAST(CAST(N'x' COLLATE Greek_CI_AS AS varchar(5)) AS sql_variant)"
    assert variant_write_sql(None) == "NULL"
    assert variant_write_sql("float|53|0|8||1.0000000000000001E-300") == \
        "CAST(CAST(CAST(N'1.0000000000000001E-300' AS float) AS float) AS sql_variant)"
    assert variant_write_sql("real|24|0|4||-2.5000000000000000E+000") == \
        "CAST(CAST(CAST(N'-2.5000000000000000E+000' AS float) AS real) AS sql_variant)"
    with pytest.raises(ValueError):
        variant_write_sql("float|53|0|8||1);DROP TABLE x--")
    for bad in ("int|10|0|4||0x00;DROP TABLE t", "xml|0|0|0||x", "varchar|0|0|5|a b|x", "int|1x|0|4||0x00"):
        with pytest.raises(ValueError):
            variant_write_sql(bad)
    assert "SQL_VARIANT_PROPERTY([v], 'BaseType')" in variant_read_expr("[v]")


def test_tables_with_sql_variant_load_with_per_value_casts():
    db = SqlServer.__new__(SqlServer)
    db.driver = "pytds"
    sent = []
    db.execute = lambda sql, params=None: sent.append((sql, params))
    t = Table("dbo", "V", columns=[Column("Id", "int", nullable=False), Column("V", "sql_variant", 8016)])
    n = db.bulk_load(t, [[(1, "int|10|0|4||0x0000002A"), (2, None)]], True)
    assert n == 2
    assert sent == [("INSERT INTO [dbo].[V] ([Id], [V]) VALUES (?, CAST(CAST(0x0000002A AS int) AS sql_variant)), "
                     "(?, NULL)", [1, 2])]


# ================================================================== assessment
def test_code_rules():
    found = scan_code([
        ("dbo.a", "EXEC master..xp_cmdshell 'dir'"),
        ("dbo.b", "-- EXEC xp_cmdshell 'commented out'\nSELECT 1"),
        ("dbo.c", "BULK INSERT dbo.t FROM 'C:\\data\\in.csv'"),
        ("dbo.d", "SELECT * FROM OPENROWSET('Microsoft.ACE.OLEDB.12.0', 'Excel', 'SELECT 1')"),
        ("dbo.e", "SELECT * FROM OPENROWSET('MSOLEDBSQL', 'Server=x;', 'SELECT 1')"),
        ("dbo.f", "EXEC sp_OACreate 'Scripting.FileSystemObject', @o OUT"),
        ("dbo.g", "RAISERROR 50001 'old style'"),
        ("dbo.h", "EXEC msdb.dbo.sp_send_dbmail @recipients = 'x'"),
    ])
    by = {(f.item, f.severity) for f in found}
    assert ("dbo.a", BLOCKER) in by and not any(f.item == "dbo.b" for f in found)
    assert ("dbo.c", WARNING) in by and ("dbo.d", BLOCKER) in by and not any(f.item == "dbo.e" for f in found)
    assert ("dbo.f", BLOCKER) in by and ("dbo.g", BLOCKER) in by and ("dbo.h", WARNING) in by


class FakeDb:
    def __init__(self, answers):
        self.answers = answers

    def query(self, sql, params=None):
        for key, rows in self.answers:
            if key in sql:
                return rows
        raise AssertionError("unexpected query: " + sql[:150])


def facts(major=16, edition="Developer Edition", engine=3, coll="SQL_Latin1_General_CP1_CI_AS", offset=0,
          platform="Linux"):
    return [("SERVERPROPERTY('ProductMajorVersion')", [(major, edition, engine, coll, offset)]),
            ("host_platform", [(platform,)])]


SOURCE = facts(15, "Enterprise Edition", 3, "Latin1_General_CI_AS", -300, "Windows") + [
    ("FROM sys.database_files\"\"\"", []),
    ("CASE WHEN type = 0 THEN size", [(2_000_000 // 8 * 1024 // 1024, 1024)]),
    ("SELECT compatibility_level FROM sys.databases", [(150,)]),
    ("FROM sys.sql_modules m JOIN sys.objects o", [("dbo", "usp_Export", "EXEC xp_cmdshell 'bcp ...'")]),
    ("WHERE t.parent_class = 0", []),
    ("FROM sys.assemblies", [("Geo", "SAFE_ACCESS"), ("FileIO", "UNSAFE_ACCESS")]),
    ("type IN ('U', 'G')", [("CORP\\app", "WINDOWS_USER")]),
    ("FROM sys.servers s WHERE s.is_linked = 1", [("XL", "Microsoft.ACE.OLEDB.12.0", "Excel", 0),
                                                  ("HQ", "SQLNCLI", "SQL Server", 1)]),
    ("JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id\n                WHERE j.job_id IN",
     [("Nightly export", "copy", "CmdExec")]),
    ("FROM msdb.dbo.sysalerts", [("Severity 17", 0, 17)]),
    ("WHERE s.database_name = ? AND s.subsystem = 'TSQL'", []),
    ("sys.filegroups WHERE type = 'FD'", [(1,)]),
    ("WHERE is_filestream = 1", [("dbo.Docs.Body",)]),
    ("is_published, is_merge_published", [(0, 1, 0, 0)]),
    ("FROM sys.database_mirroring", [(0,)]),
    ("log_shipping_primary_databases", [(0,)]),
    ("is_remote_data_archive_enabled", [(0,)]),
    ("FROM sys.cryptographic_providers", []),
    ("allow_enclave_computations", []),
    ("SELECT is_encrypted FROM sys.databases", [(0,)]),
    ("##MS_DatabaseMasterKey##", [(0,)]),
    ("is_auto_close_on", [(0, 0, "CHECKSUM", "FULL", "NONE")]),
    ("WHERE ty.name IN ('text', 'ntext', 'image')", []),
    ("sys.database_audit_specifications", []),
    ("LEFT JOIN sys.server_principals sp ON sp.sid = dp.sid", []),
    ("FROM sys.routes", []),
    ("sys.sql_expression_dependencies d", [("dbo.usp_Report", "Archive", None)]),
    ("FROM sys.synonyms", [("dbo", "Old", "[Archive].[dbo].[Orders]")]),
    ("SELECT DB_NAME()", [("Sales",)]),
    ("SELECT definition FROM sys.sql_modules", [("RAISERROR(50010, 16, 1)",)]),
    ("FROM sys.messages", [(50010,)]),
    ("sys.server_triggers", [("audit_logon",)]),
    ("ExecIsStartup", []),
    ("sys.server_event_sessions", []),
    ("resource_governor_configuration", [(0,)]),
    ("sys.credentials", []),
    ("WHERE definition IS NULL", [("dbo.usp_Secret",)]),
    ("dm_db_persisted_sku_features", [("Partitioning",)]),
    ("FROM sys.configurations", [("clr enabled", 1), ("max degree of parallelism", 4), ("xp_cmdshell", 1)]),
    ("c.system_type_id", []),
]
TARGET = facts(16, "Standard Edition", 2, "SQL_Latin1_General_CP1_CI_AS", 0, "Linux") + [
    ("FROM sys.configurations", [("clr enabled", 0), ("max degree of parallelism", 0), ("xp_cmdshell", 0)]),
    ("dm_os_volume_stats", [(100,)]),
]


def test_assessment_for_openshift():
    a = Assessor(FakeDb(SOURCE), FakeDb(TARGET), "Sales").run()
    text = "\n".join(f.text() for f in a.findings)
    sev = {(f.area, f.item): f.severity for f in a.findings}
    assert sev[("Linux", "assembly FileIO")] == BLOCKER and sev[("Code", "assembly Geo")] == INFO
    assert sev[("Code", "dbo.usp_Export")] == BLOCKER                         # xp_cmdshell
    assert sev[("Linux", "linked server XL")] == BLOCKER and sev[("Linux", "linked server HQ")] == WARNING
    assert sev[("Agent", "job Nightly export")] == WARNING and sev[("Agent", "alert Severity 17")] == WARNING
    assert sev[("Security", "user CORP\\app")] == WARNING
    assert sev[("Linux", "FILESTREAM")] == WARNING                            # migrate converts it
    assert sev[("Linux", "merge replication")] == BLOCKER
    assert sev[("Code", "dbo.usp_Report")] == WARNING and "Archive" in text  # cross-database reference
    assert sev[("Code", "synonym dbo.Old")] == WARNING
    assert sev[("Server", "server trigger audit_logon")] == WARNING
    assert sev[("Code", "dbo.usp_Secret")] == BLOCKER                         # WITH ENCRYPTION, migrate
    assert sev[("Server", "server collation")] == WARNING and sev[("Server", "time zone")] == WARNING
    assert sev[("Server", "sp_configure 'clr enabled'")] == WARNING
    assert sev[("Server", "sp_configure 'max degree of parallelism'")] == INFO
    assert sev[("Storage", "target volume")] == BLOCKER                       # 100 MB free
    assert "user-defined messages" in text and "50010" in text
    assert a.findings[0].severity == BLOCKER                                  # sorted, blockers first
    assert "blockers" in render(a) and json.dumps(a.to_dict())


def test_lift_assessment_blocks_filestream_restore_on_linux():
    a = Assessor(FakeDb(SOURCE), FakeDb(TARGET), "Sales", lift=True).run()
    sev = {(f.area, f.item): f.severity for f in a.findings}
    assert sev[("Restore", "FILESTREAM filegroup")] == BLOCKER
    assert sev[("Code", "dbo.usp_Secret")] == INFO                            # lift copies encrypted modules


def test_version_and_express_checks():
    src = SOURCE + []
    src = [(k, v) if k != "SELECT compatibility_level FROM sys.databases" else (k, [(160,)]) for k, v in src]
    tgt = facts(15, "Express Edition", 4, "SQL_Latin1_General_CP1_CI_AS", 0) + TARGET[2:]
    a = Assessor(FakeDb(src), FakeDb(tgt), "Sales").run()
    sev = {(f.area, f.item): f.severity for f in a.findings}
    assert sev[("Version", "compatibility level")] == BLOCKER
    assert sev[("Edition", "database size")] == BLOCKER


def test_checks_that_fail_are_reported_not_raised():
    a = Assessor(FakeDb(facts()), None, "x").run()
    assert all(f.severity == INFO for f in a.findings) and any("check skipped" in f.message for f in a.findings)


def test_migrate_stops_only_on_blockers_for_the_copy(monkeypatch):
    import mssql_ocp_migrate.assess as A
    findings = [Finding(BLOCKER, "Storage", "target volume", "too small"),
                Finding(BLOCKER, "Code", "dbo.p", "xp_cmdshell"),
                Finding(BLOCKER, "Version", "compatibility level", "160 > 150"),
                Finding(WARNING, "Server", "time zone", "differs")]
    monkeypatch.setattr(A, "assess", lambda *a, **k: A.Assessment(list(findings), {}, {}))
    q = type("Q", (), {"query": lambda self, *a: []})()
    res = RunResult(0)
    with pytest.raises(MigrationError, match="target volume"):
        Migrator(cfg(), factory=None, progress=lambda _: None)._assess(q, q, res)
    res = RunResult(0)
    Migrator(cfg(ignore_blockers=True), factory=None, progress=lambda _: None)._assess(q, q, res)
    assert any("xp_cmdshell" in w for w in res.warnings) and any("time zone" in w for w in res.warnings)
    res = RunResult(0)
    with pytest.raises(MigrationError) as e:
        Migrator(cfg(compatibility_level="150"), factory=None, progress=lambda _: None)._assess(q, q, res)
    assert "compatibility" not in str(e.value)


# ================================================================== server objects
class ServerTarget(RecordingTarget):
    def __init__(self, existing_messages=None, config=None):
        super().__init__()
        self.messages = existing_messages or {}
        self.config = config or {}

    def query(self, sql, params=None):
        if "FROM sys.messages" in sql:
            return [(self.messages[params[0]],)] if params[0] in self.messages else []
        return super().query(sql, params)

    def configuration(self):
        return dict(self.config)


def test_custom_messages_are_added_unless_the_target_has_that_number():
    class Src:
        def custom_messages(self):
            return [(50010, [("us_english", "EXEC sys.sp_addmessage @msgnum = 50010, @msgtext = N'Order %d'")]),
                    (50011, [("us_english", "EXEC sys.sp_addmessage @msgnum = 50011, @msgtext = N'Same'")]),
                    (50012, [("us_english", "EXEC sys.sp_addmessage @msgnum = 50012, @msgtext = N'Mine'")])]
    tgt, res = ServerTarget({50011: "Same", 50012: "Somebody else's"}), RunResult(0)
    Migrator(cfg(), factory=None, progress=lambda _: None)._copy_messages(Src(), tgt, res)
    assert tgt.sql == ["EXEC sys.sp_addmessage @msgnum = 50010, @msgtext = N'Order %d'"]
    assert res.warnings == ["user-defined message 50012 already exists on the target with a different text; left as is"]


def test_server_configuration_applies_only_the_curated_list():
    class Src:
        def configuration(self):
            return {"clr enabled": 1, "xp_cmdshell": 1, "max degree of parallelism": 4, "show advanced options": 0}
    tgt = ServerTarget(config={"clr enabled": 0, "xp_cmdshell": 0, "max degree of parallelism": 4})
    Migrator(cfg(), factory=None, progress=lambda _: None)._copy_configuration(Src(), tgt, RunResult(0))
    assert tgt.sql == ["EXEC sys.sp_configure 'show advanced options', 1; RECONFIGURE",
                       "EXEC sys.sp_configure N'clr enabled', 1; RECONFIGURE",
                       "EXEC sys.sp_configure 'show advanced options', 0; RECONFIGURE"]   # xp_cmdshell never


# ================================================================== sync
def test_sync_skips_what_change_tracking_cant_follow():
    pk = Index("PK", [("Id", False)], unique=True, clustered=True, is_primary_key=True)
    assert sync_skip_reason(tbl(primary_key=pk)) is None
    assert "primary key" in sync_skip_reason(tbl())
    assert "memory-optimized" in sync_skip_reason(tbl(primary_key=pk, memory_optimized=True))


def test_changes_query_joins_the_current_row():
    db = SqlServer.__new__(SqlServer)
    db.driver = "pytds"
    t = tbl(primary_key=Index("PK", [("Id", False), ("Year", False)], unique=True, clustered=True, is_primary_key=True))
    sql = changes_sql(db, t)
    assert sql.startswith("SELECT ct.[Id], ct.[Year], CASE WHEN t.[__k0] IS NULL THEN 0 ELSE 1 END, t.[__c0], t.[__c1] ")
    assert "FROM CHANGETABLE(CHANGES [dbo].[T], ?) AS ct LEFT JOIN (SELECT [Id] AS [__k0], [Year] AS [__k1], " in sql
    assert sql.endswith("ON t.[__k0] = ct.[Id] AND t.[__k1] = ct.[Year]")


class SyncDb:
    """One fake for both sides: source answers change-tracking queries, target records statements."""

    def __init__(self, side, changes=()):
        self.side, self.changes, self.sql, self.loaded = side, list(changes), [], []

    def query(self, sql, params=None):
        if "CHANGE_TRACKING_CURRENT_VERSION" in sql:
            return [(42,)]
        if "sys.change_tracking_tables" in sql:
            return [("dbo", "T", 5)]
        if "CHANGETABLE" in sql:
            assert params == [10]
            return self.changes
        if "sys.foreign_keys" in sql:
            return [("dbo", "Child", "FK_Child_T", "dbo", "T")]
        if "sys.triggers" in sql:
            return [("dbo", "T", "trg_audit")]
        if "sys.sequences" in sql:
            return [("dbo", "Seq", 99, 1)]
        raise AssertionError(sql)

    def select_exprs(self, t, cols):
        return [ddl.q(c.name) for c in cols]

    def qualified(self, t):
        return f"{ddl.q(t.schema)}.{ddl.q(t.name)}"

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def bulk_load(self, t, batches, tablock):
        self.loaded += [r for b in batches for r in b]
        return len(self.loaded)

    def identity_current(self, t):
        return 7

    def reseed_identity(self, t, v):
        self.sql.append((f"RESEED {v}", None))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_sync_pass_applies_deletes_and_upserts_with_triggers_and_fks_off(tmp_path, monkeypatch):
    c = cfg(state_file=str(tmp_path / "state.json"))
    (tmp_path / "state.json").write_text(json.dumps({"change_tracking_version": 10}))
    pk = Index("PK", [("Id", False)], unique=True, clustered=True, is_primary_key=True)
    src = SyncDb("source", changes=[(1, 1, 1, 2024), (2, 0, None, None)])     # key 1 upserted, key 2 deleted
    tgt = SyncDb("target")
    monkeypatch.setattr("mssql_ocp_migrate.engine.Migrator.source_tables", lambda self, src=None: [tbl(primary_key=pk)])
    res = Syncer(c, factory=lambda side, ac: src if side == "source" else tgt, progress=lambda _: None).run(final=True)
    assert (res.from_version, res.to_version, res.rows) == (10, 42, 2)
    assert res.tables["dbo.T"] == {"deleted": 1, "upserted": 1}
    stmts = [s for s, _ in tgt.sql]
    assert stmts[0] == "ALTER TABLE [dbo].[Child] NOCHECK CONSTRAINT [FK_Child_T]"
    assert stmts[1] == "DISABLE TRIGGER [dbo].[trg_audit] ON [dbo].[T]"
    assert stmts[2] == "DELETE FROM [dbo].[T] WHERE ([Id] = CAST(? AS int)) OR ([Id] = CAST(? AS int))"
    assert tgt.sql[2][1] == [1, 2] and tgt.loaded == [(1, 2024)]
    assert "ENABLE TRIGGER [dbo].[trg_audit] ON [dbo].[T]" in stmts
    assert "ALTER TABLE [dbo].[Child] WITH CHECK CHECK CONSTRAINT [FK_Child_T]" in stmts       # final: trusted
    assert "RESEED 7" in stmts and "ALTER SEQUENCE [dbo].[Seq] RESTART WITH 100" in stmts
    assert load_version(c) == 42                                             # next pass starts here


def test_sync_needs_a_recorded_version(tmp_path):
    from mssql_ocp_migrate.sync import SyncError
    c = cfg(state_file=str(tmp_path / "none.json"))
    with pytest.raises(SyncError, match="change tracking"):
        Syncer(c, factory=lambda s, a: None).run()
    save_version(c, 7, SyncResult(from_version=3))
    assert load_version(c) == 7


def test_vector_keeps_its_own_type_name():
    from mssql_ocp_migrate.catalog import CatalogMixin as Catalog
    row = (1, "Vec", "vector", 0, 0, "sys", "varbinary", 20, 0, 0, 1, 0, None, None, 0, None, 0, None, None, None,
           0, 0, None, None, None, None, None, 0, None, None, 0, 0, 0, 0, 1, 3, "float32")
    c = Catalog.column_from_row(row)
    assert c.type_name == "vector" and ddl.type_sql(c) == "vector(3)"
    row = (1, "Name", "sysname", 0, 0, "sys", "nvarchar", 256, 0, 0, 0) + row[11:]
    assert Catalog.column_from_row(row).type_name == "nvarchar"
