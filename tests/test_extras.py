"""0.3.0: index options, statistics, extended properties, security and database settings."""
from mssql_ocp_migrate import ddl
from mssql_ocp_migrate.adapter import ModuleDef
from mssql_ocp_migrate.config import Config, ConfigError, ConnectionConfig
from mssql_ocp_migrate.engine import Migrator, RunResult, scoped_extras
from mssql_ocp_migrate.model import (Column, DatabaseExtras, DatabaseSettings, ExtendedProperty, Index,
                                     Permission, Principal, Statistic, Table)
from mssql_ocp_migrate.sqlserver import SqlServer
from mssql_ocp_migrate.validate import _schema_diff, extras_diff

import pytest


def table(**kw) -> Table:
    t = Table(schema="sales", name="Order", columns=[Column("Id", "int", nullable=False),
                                                     Column("Status", "tinyint")])
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def cfg(**migration) -> Config:
    c = Config(source=ConnectionConfig(database="a"), target=ConnectionConfig(database="b"))
    for k, v in migration.items():
        setattr(c.migration, k, v)
    c.check()
    return c


# ------------------------------------------------------------------ index options
def test_storage_options_only_emit_non_defaults():
    assert ddl.storage_options(Index("IX", [("a", False)])) == []
    assert ddl.storage_options(Index("IX", [("a", False)], fill_factor=100)) == []
    ix = Index("IX", [("a", False)], fill_factor=80, is_padded=True, ignore_dup_key=True,
               allow_row_locks=False, allow_page_locks=False, data_compression="PAGE")
    assert ddl.storage_options(ix) == ["PAD_INDEX = ON", "FILLFACTOR = 80", "IGNORE_DUP_KEY = ON",
                                       "ALLOW_ROW_LOCKS = OFF", "ALLOW_PAGE_LOCKS = OFF",
                                       "DATA_COMPRESSION = PAGE"]


def test_primary_key_and_heap_compression_in_create_table():
    pk = Index("PK_Order", [("Id", False)], unique=True, clustered=True, is_primary_key=True,
               fill_factor=90, data_compression="ROW")
    sql = ddl.create_table_sql(table(primary_key=pk))
    assert "PRIMARY KEY CLUSTERED ([Id]) WITH (FILLFACTOR = 90, DATA_COMPRESSION = ROW)" in sql
    assert ") WITH (DATA_COMPRESSION" not in sql
    heap = ddl.create_table_sql(table(heap_compression="PAGE"))
    assert heap.endswith(") WITH (DATA_COMPRESSION = PAGE)")
    assert "WITH" not in ddl.create_table_sql(table(heap_compression="PAGE", primary_key=pk), copy_options=False)
    # a heap that gets a clustered index after the load is compressed by that index, not the table
    ci = Index("CIX", [("Id", False)], clustered=True)
    assert "DATA_COMPRESSION" not in ddl.create_table_sql(table(heap_compression="PAGE", indexes=[ci]))


def test_index_build_options_per_index_kind():
    b = ddl.BuildOptions(sort_in_tempdb=True, maxdop=8, online=True)
    t = table()
    ix = Index("IX_Status", [("Status", True)], included=["Id"], filter_definition="([Status]<(3))",
               fill_factor=70, data_compression="PAGE")
    assert ddl.index_sql(t, ix, b) == (
        "CREATE NONCLUSTERED INDEX [IX_Status] ON [sales].[Order] ([Status] DESC) INCLUDE ([Id]) "
        "WHERE ([Status]<(3)) WITH (FILLFACTOR = 70, DATA_COMPRESSION = PAGE, SORT_IN_TEMPDB = ON, "
        "MAXDOP = 8, ONLINE = ON)")
    uq = Index("UQ_x", [("Id", False)], unique=True, is_unique_constraint=True, ignore_dup_key=True)
    assert ddl.index_sql(t, uq, ddl.BuildOptions(sort_in_tempdb=True)).endswith(
        "UNIQUE NONCLUSTERED ([Id]) WITH (IGNORE_DUP_KEY = ON, SORT_IN_TEMPDB = ON)")
    cs = Index("CCI", [], clustered=True, type_desc="CLUSTERED COLUMNSTORE", data_compression="COLUMNSTORE_ARCHIVE")
    assert ddl.index_sql(t, cs, b) == ("CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [sales].[Order] "
                                       "WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE, MAXDOP = 8)")
    # options off: plain index, old behaviour
    assert ddl.index_sql(t, ix, ddl.BuildOptions(copy_options=False)).endswith("WHERE ([Status]<(3))")


def test_statistics_sql():
    st = Statistic("st_status", ["Status", "Id"], filter_definition="([Status]>(0))", no_recompute=True)
    assert ddl.statistics_sql(table(), st) == ("CREATE STATISTICS [st_status] ON [sales].[Order] ([Status], [Id]) "
                                               "WHERE ([Status]>(0)) WITH NORECOMPUTE")
    assert ddl.statistics_sql(table(), Statistic("s", ["Id"]), fullscan=True).endswith("([Id]) WITH FULLSCAN")


# ------------------------------------------------------------------ database objects
def test_extended_property_sql_escapes_and_levels():
    ep = ExtendedProperty("MS_Description", "Customer's order", (("SCHEMA", "sales"), ("TABLE", "Order"),
                                                                 ("COLUMN", "Id")))
    assert ddl.extended_property_sql(ep) == (
        "EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'Customer''s order', "
        "@level0type = N'SCHEMA', @level0name = N'sales', @level1type = N'TABLE', @level1name = N'Order', "
        "@level2type = N'COLUMN', @level2name = N'Id'")
    assert ddl.extended_property_sql(ExtendedProperty("v", None)).endswith("@value = NULL")


def test_principal_and_permission_sql():
    assert ddl.principal_sql(Principal("app", "SQL_USER", login="app_login", default_schema="sales")) == \
        "CREATE USER [app] FOR LOGIN [app_login] WITH DEFAULT_SCHEMA = [sales]"
    assert ddl.principal_sql(Principal("svc", "NO_LOGIN")) == "CREATE USER [svc] WITHOUT LOGIN"
    assert ddl.principal_sql(Principal("readers", "ROLE")) == "CREATE ROLE [readers]"
    assert ddl.principal_sql(Principal("DOM\\grp", "WINDOWS_GROUP", login="DOM\\grp")) == \
        "CREATE USER [DOM\\grp] FOR LOGIN [DOM\\grp]"
    for kind in ("CONTAINED", "ORPHANED", "OTHER"):
        assert ddl.principal_sql(Principal("x", kind)) is None
    assert ddl.role_owner_sql(Principal("r", "ROLE", owner="app")) == "ALTER AUTHORIZATION ON ROLE::[r] TO [app]"
    assert ddl.role_owner_sql(Principal("r", "ROLE", owner="dbo")) is None
    perms = [
        (Permission("GRANT", "SELECT", "readers", "OBJECT", "sales", "Order", "Amount"),
         "GRANT SELECT ON [sales].[Order] ([Amount]) TO [readers]"),
        (Permission("DENY", "DELETE", "app", "SCHEMA", "sales"), "DENY DELETE ON SCHEMA::[sales] TO [app]"),
        (Permission("GRANT_WITH_GRANT_OPTION", "EXECUTE", "app", "OBJECT", "dbo", "usp"),
         "GRANT EXECUTE ON [dbo].[usp] TO [app] WITH GRANT OPTION"),
        (Permission("GRANT", "VIEW DEFINITION", "readers", "DATABASE"), "GRANT VIEW DEFINITION TO [readers]"),
        (Permission("GRANT", "REFERENCES", "app", "TYPE", "dbo", "Phone"),
         "GRANT REFERENCES ON TYPE::[dbo].[Phone] TO [app]"),
    ]
    for p, sql in perms:
        assert ddl.permission_sql(p) == sql


def test_database_settings_sql():
    src = DatabaseSettings(150, True, True, "Latin1_General_CI_AS")
    tgt = DatabaseSettings(160, False, False, "SQL_Latin1_General_CP1_CI_AS")
    assert ddl.database_settings_sql(src, tgt, "source") == [
        "ALTER DATABASE CURRENT SET COMPATIBILITY_LEVEL = 150",
        "ALTER DATABASE CURRENT SET ALLOW_SNAPSHOT_ISOLATION ON",
        "ALTER DATABASE CURRENT SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE"]
    assert ddl.database_settings_sql(src, tgt, "target")[0].startswith("ALTER DATABASE CURRENT SET ALLOW")
    assert ddl.database_settings_sql(src, tgt, "140")[0].endswith("= 140")
    assert ddl.database_settings_sql(src, src, "source") == []


def test_compatibility_level_config_values():
    assert cfg(compatibility_level=150).migration.compatibility_level == "150"
    assert cfg(compatibility_level="Target").migration.compatibility_level == "target"
    with pytest.raises(ConfigError):
        cfg(compatibility_level="latest")
    with pytest.raises(ConfigError):
        cfg(index_maxdop=-1)


# ------------------------------------------------------------------ catalog parsing (SQL Server)
class FakeConn:
    """Answers catalog queries by keyword, the way SqlServer issues them."""

    def __init__(self, answers):
        self.answers = answers

    def query(self, sql, params=None):
        for key, rows in self.answers:
            if key in sql:
                return rows
        raise AssertionError("unexpected query: " + sql[:120])

    def scalar(self, sql, params=None):
        return 1     # every column exists (COL_LENGTH)


def fake_server(answers) -> SqlServer:
    s = SqlServer.__new__(SqlServer)
    s.conn = FakeConn(answers)
    s.driver = "pytds"
    s._version = 16
    return s


def settings_row(owner="app_login"):
    # compatibility, RCSI, snapshot, collation, owner, 20 ON/OFF options, then PARAMETERIZATION,
    # CURSOR_DEFAULT, PAGE_VERIFY, BROKER, TARGET_RECOVERY_TIME, DELAYED_DURABILITY
    return [(150, True, 1, "Latin1_General_CI_AS", owner) + (1,) * 20 + (0, 1, "CHECKSUM", 1, 60, "DISABLED")]


EXTRAS_ANSWERS = [
    ("SELECT is_cdc_enabled FROM sys.databases", [(0,)]),
    ("USER_NAME(o.principal_id) FROM sys.objects o", [("sales", "usp_Owned", "app"), ("cdc", "x", "app")]),
    ("SELECT compatibility_level, is_read_committed_snapshot_on", settings_row()),
    ("sys.database_scoped_configurations", [("MAXDOP", "4"), ("LEGACY_CARDINALITY_ESTIMATION", "0")]),
    ("sys.change_tracking_databases", [(2, "DAYS", 1)]),
    ("sys.database_query_store_options", [("READ_WRITE",)]),
    ("dp.owning_principal_id", [
        (5, "readers", "R", None, None, 0, 6, None, None),
        (6, "app", "S", "sales", "app_login", 1, None, None, None),
        (7, "svc", "S", None, None, 0, None, None, None),
        (8, "contained", "S", None, None, 2, None, None, None),
        (9, "orphan", "S", None, None, 1, None, None, None),
        (10, "DOM\\ops", "G", None, "DOM\\ops", 3, None, None, None),
        (11, "signer", "C", None, None, 0, None, "SignCert", None)]),
    ("SELECT principal_id, name FROM sys.database_principals", [(1, "dbo"), (5, "readers"), (6, "app")]),
    ("sys.database_role_members", [("readers", "svc"), ("db_datareader", "app")]),
    ("FROM sys.schemas s JOIN", [("sales", "app")]),
    ("sys.database_permissions", [
        (1, "SELECT", "GRANT", "readers", "sales", "Order", "Amount"),
        (3, "DELETE", "DENY", "app", "sales", None, None),
        (0, "VIEW DEFINITION", "GRANT", "readers", None, None, None),
        (6, "REFERENCES", "GRANT_WITH_GRANT_OPTION", "app", "dbo", "Phone", None),
        (17, "SEND", "GRANT", "app", None, "//svc", None)]),
    ("sys.extended_properties", [
        (0, "Owner", "Data team", None, None, None, None, None, None, None),
        (3, "MS_Description", "Sales schema", "sales", None, None, None, None, None, None),
        (1, "MS_Description", "Orders", "sales", "Order", "U ", None, None, None, None),
        (1, "MS_Description", "Amount", "sales", "Order", "U ", None, None, "Amount", None),
        (1, "MS_Description", "pk", "sales", "PK_Order", "PK", "Order", "U ", None, None),
        (1, "MS_Description", "trg", "sales", "trg_x", "TR", "Order", "U ", None, None),
        (7, "MS_Description", "ix", "sales", "Order", "U ", None, None, None, "IX_Order"),
        (1, "MS_Description", "fn", "dbo", "fnNet", "FN", None, None, None, None),
        (1, "MS_Description", "param", "dbo", "fnNet", "FN", None, None, "col", None)]),
    ("FROM sys.security_policies", [(50, "rls", "pol", 1, 1)]),
    ("FROM sys.security_predicates sp ORDER BY", [
        (50, "FILTER", "([rls].[fn]([Rep]))", "sales", "Order", None),
        (50, "BLOCK", "([rls].[fn]([Rep]))", "sales", "Order", "AFTER_INSERT")]),
    ("##MS_DatabaseMasterKey##", [(1,)]),
    ("FROM sys.certificates c WHERE c.name NOT LIKE", [("SignCert", "dbo", "0x3082", "MK", "0x1F2E"),
                                                       ("PwCert", "app", "0x3083", "PW", None)]),
    ("cp.crypt_type IN ('SPVC', 'CPVC')", [("dbo", "usp", "SignCert", "SPVC", "0xABCD")]),
    ("crypt_type IN ('SPVA', 'CPVA')", [(2,)]),
    ("FROM sys.symmetric_keys WHERE name NOT LIKE", [("CardKey",)]),
    ("FROM sys.asymmetric_keys", []),
    ("FROM sys.database_scoped_credentials", [("blobcred",)]),
    ("FROM sys.external_data_sources", []),
    ("SELECT is_encrypted FROM sys.databases", [(1,)]),
    ("FROM sys.service_queues q", [("dbo", "TargetQ")]),
    ("FROM [dbo].[TargetQ]", [(3,)]),
]


def test_database_extras_maps_everything():
    s = fake_server(EXTRAS_ANSWERS)
    ex = s.database_extras()
    assert ("sales", "usp_Owned", "app") in ex.object_owners
    st = ex.settings
    assert (st.compatibility_level, st.read_committed_snapshot, st.snapshot_isolation, st.owner) == \
        (150, True, True, "app_login")
    assert st.options["ANSI_NULLS"] == "ON" and st.options["MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT"] == "= ON"
    assert st.options["PARAMETERIZATION"] == "SIMPLE" and st.options["CURSOR_DEFAULT"] == "LOCAL"
    assert st.options["BROKER"] == "ENABLE_BROKER" and st.options["TARGET_RECOVERY_TIME"] == "= 60 SECONDS"
    assert st.options["DELAYED_DURABILITY"] == "= DISABLED" and st.options["PAGE_VERIFY"] == "CHECKSUM"
    assert st.scoped_configurations == {"MAXDOP": "4", "LEGACY_CARDINALITY_ESTIMATION": "0"}
    assert st.change_tracking == "CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON" and st.query_store == "READ_WRITE"
    kinds = {p.name: (p.kind, p.login, p.default_schema, p.owner, p.certificate) for p in ex.principals}
    assert kinds == {"readers": ("ROLE", None, None, "app", None),
                     "app": ("SQL_USER", "app_login", "sales", None, None),
                     "svc": ("NO_LOGIN", None, None, None, None), "contained": ("CONTAINED", None, None, None, None),
                     "orphan": ("ORPHANED", None, None, None, None),
                     "DOM\\ops": ("WINDOWS_GROUP", "DOM\\ops", None, None, None),
                     "signer": ("CERTIFICATE_USER", None, None, None, "SignCert")}
    assert ex.role_members == [("readers", "svc"), ("db_datareader", "app")]
    assert ex.schema_owners == {"sales": "app"}
    assert [p.securable_class for p in ex.permissions] == ["OBJECT", "SCHEMA", "DATABASE", "TYPE", "SERVICE"]
    assert ex.permissions[0].column == "Amount" and ex.permissions[1].schema == "sales"
    levels = [e.level for e in ex.extended_properties]
    assert levels == [
        (), (("SCHEMA", "sales"),),
        (("SCHEMA", "sales"), ("TABLE", "Order")),
        (("SCHEMA", "sales"), ("TABLE", "Order"), ("COLUMN", "Amount")),
        (("SCHEMA", "sales"), ("TABLE", "Order"), ("CONSTRAINT", "PK_Order")),
        (("SCHEMA", "sales"), ("TABLE", "Order"), ("TRIGGER", "trg_x")),
        (("SCHEMA", "sales"), ("TABLE", "Order"), ("INDEX", "IX_Order")),
        (("SCHEMA", "dbo"), ("FUNCTION", "fnNet"))]          # function parameter/column skipped
    pol = ex.security_policies[0]
    assert (pol.schema, pol.name, pol.enabled, pol.schemabinding) == ("rls", "pol", True, True)
    assert pol.predicates[1] == ("BLOCK", "([rls].[fn]([Rep]))", "sales", "Order", "AFTER INSERT")
    assert [(c.name, c.owner, bool(c.private_key)) for c in ex.certificates] == [
        ("SignCert", None, True), ("PwCert", "app", False)]
    assert ex.certificates[0].password and ex.has_master_key
    assert [(sg.schema, sg.name, sg.certificate, sg.counter) for sg in ex.signatures] == [
        ("dbo", "usp", "SignCert", False)]
    joined = " | ".join(ex.warnings)
    for needle in ("PwCert", "asymmetric keys can't be copied", "symmetric key CardKey", "blobcred", "TDE",
                   "dbo.TargetQ holds 3 messages"):
        assert needle in joined, needle


def test_index_and_statistics_catalog_parsing():
    s = fake_server([
        ("FROM sys.indexes i\n            JOIN sys.tables", [
            (1, 1, "PK_T", "CLUSTERED", 1, 1, 0, None, 1, 0, 0, "Id", 90, 1, 0, 1, 1, "PAGE", "ps_year", None, 0, 0),
            (1, 1, "PK_T", "CLUSTERED", 1, 1, 0, None, 0, 0, 0, "Year", 90, 1, 0, 1, 1, "PAGE", "ps_year", None, 1, 0),
            (1, 2, "IX_T", "NONCLUSTERED", 0, 0, 0, "([S]>(0))", 1, 1, 0, "S", 0, 0, 1, 0, 1, "NONE", None, None, 0, 0),
            (1, 2, "IX_T", "NONCLUSTERED", 0, 0, 0, "([S]>(0))", 0, 0, 1, "Id", 0, 0, 1, 0, 1, "NONE", None, None, 0, 0),
            (3, 1, "PK_M", "NONCLUSTERED HASH", 1, 1, 0, None, 1, 0, 0, "Id", 0, 0, 0, 1, 1, "NONE", None, 1024, 0, 0)]),
        ("HAVING COUNT(DISTINCT p2.data_compression) > 1", [(1, 1, 1, "PAGE"), (1, 1, 2, "ROW"), (2, 0, 1, "ROW"),
                                                         (2, 0, 2, "NONE")]),
        ("FROM sys.indexes i LEFT JOIN sys.partition_schemes", [(2, "ROW", "ps_heap", "Year")]),
        ("FROM sys.stats s", [(1, 3, "st_s", None, 1, "S"), (1, 3, "st_s", None, 1, "Id")]),
    ])
    t1, t2, t3 = Table("dbo", "T"), Table("dbo", "Heap"), Table("dbo", "Mem")
    tables = {1: t1, 2: t2, 3: t3}
    s._load_indexes(tables)
    s._load_storage(tables)
    s._load_statistics(tables)
    pk = t1.primary_key
    assert pk.columns == [("Id", False)]            # the partitioning column isn't a key column
    assert (pk.partition_scheme, pk.partition_column) == ("ps_year", "Year")
    assert pk.options()["fill_factor"] == 90 and pk.options()["pad_index"]
    assert pk.partition_compression == {1: "PAGE", 2: "ROW"}
    ix = t1.indexes[0]
    assert ix.ignore_dup_key and not ix.allow_row_locks and ix.columns == [("S", True)] and ix.included == ["Id"]
    assert (t2.heap_compression, t2.partition_scheme, t2.partition_column) == ("ROW", "ps_heap", "Year")
    assert t2.heap_partition_compression == {1: "ROW", 2: "NONE"}
    assert t3.primary_key.is_hash and t3.primary_key.bucket_count == 1024 and not t3.primary_key.clustered
    assert [(st.name, st.columns, st.no_recompute) for st in t1.statistics] == [("st_s", ["S", "Id"], True)]


# ------------------------------------------------------------------ scope, apply, validate
def extras() -> DatabaseExtras:
    return DatabaseExtras(
        settings=DatabaseSettings(150, True, False, "x"),
        principals=[Principal("readers", "ROLE", owner="app"), Principal("app", "SQL_USER", login="app_login"),
                    Principal("ghost", "SQL_USER", login="missing_login"), Principal("c", "CONTAINED")],
        role_members=[("readers", "app"), ("readers", "ghost"), ("db_datareader", "app")],
        schema_owners={"sales": "app", "audit": "ghost"},
        permissions=[Permission("GRANT", "SELECT", "readers", "OBJECT", "sales", "Order"),
                     Permission("GRANT", "SELECT", "readers", "OBJECT", "sales", "NotCopied"),
                     Permission("GRANT", "EXECUTE", "app", "OBJECT", "dbo", "usp"),
                     Permission("GRANT", "CONNECT", "ghost", "DATABASE")],
        extended_properties=[ExtendedProperty("d", "db"), ExtendedProperty("d", "s", (("SCHEMA", "sales"),)),
                             ExtendedProperty("d", "t", (("SCHEMA", "sales"), ("TABLE", "Order"))),
                             ExtendedProperty("d", "x", (("SCHEMA", "sales"), ("TABLE", "NotCopied")))])


def test_scope_keeps_database_items_and_copied_objects_only():
    ex = scoped_extras(extras(), [table()], [ModuleDef("PROCEDURE", "dbo", "usp", "x")])
    assert [(p.schema, p.securable) for p in ex.permissions] == [("sales", "Order"), ("dbo", "usp"), (None, None)]
    assert [e.value for e in ex.extended_properties] == ["db", "s", "t"]
    assert len(ex.principals) == 4 and ex.schema_owners == {"sales": "app", "audit": "ghost"}


class RecordingTarget:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def query(self, sql, params=None):
        return [(1 if params == ["app_login"] else None,)]

    def commit(self):
        pass

    def rollback(self):
        pass


def test_apply_extras_order_and_missing_logins():
    tgt, res = RecordingTarget(), RunResult(started=0)
    m = Migrator(cfg(), factory=lambda side, ac: None, progress=lambda _: None)
    m._apply_extras(tgt, extras(), res)
    assert tgt.sql[:2] == ["CREATE ROLE [readers]", "CREATE USER [app] FOR LOGIN [app_login]"]
    assert "ALTER AUTHORIZATION ON ROLE::[readers] TO [app]" in tgt.sql
    assert "ALTER ROLE [readers] ADD MEMBER [app]" in tgt.sql
    assert "ALTER AUTHORIZATION ON SCHEMA::[sales] TO [app]" in tgt.sql
    joined = "\n".join(tgt.sql)
    assert "ghost" not in joined                     # no user, so no membership/owner/grant either
    assert "CREATE USER [c]" not in joined
    errors = {e["object"]: e["error"] for e in res.ddl_errors}
    assert "missing_login" in errors["user ghost"] and "password" in errors["user c"]
    assert sum(s.startswith("EXEC sys.sp_addextendedproperty") for s in tgt.sql) == 4
    # order: principals < memberships < permissions < extended properties
    pos = {k: next(i for i, s in enumerate(tgt.sql) if s.startswith(k))
           for k in ("CREATE ROLE", "ALTER ROLE", "GRANT", "EXEC sys.sp_addextendedproperty")}
    assert pos["CREATE ROLE"] < pos["ALTER ROLE"] < pos["GRANT"] < pos["EXEC sys.sp_addextendedproperty"]


def test_apply_extras_respects_switches():
    tgt, res = RecordingTarget(), RunResult(started=0)
    Migrator(cfg(copy_security=False), factory=lambda s, a: None, progress=lambda _: None)._apply_extras(
        tgt, extras(), res)
    assert all(s.startswith("EXEC sys.sp_addextendedproperty") for s in tgt.sql) and not res.ddl_errors


def test_schema_diff_reports_options_and_statistics():
    s = table(primary_key=Index("PK", [("Id", False)], clustered=True, is_primary_key=True, data_compression="PAGE"),
              indexes=[Index("IX", [("Status", False)], fill_factor=80, ignore_dup_key=True)],
              statistics=[Statistic("st", ["Status"]), Statistic("st2", ["Id"])])
    t = table(primary_key=Index("PK", [("Id", False)], clustered=True, is_primary_key=True),
              indexes=[Index("IX", [("Status", False)], fill_factor=100)],
              statistics=[Statistic("st", ["Id"])])
    diffs = _schema_diff(s, t, options=True, statistics=True)
    assert "index PK: data_compression 'PAGE' -> 'NONE'" in diffs
    assert "index IX: fill_factor 80 -> 0" in diffs and "index IX: ignore_dup_key True -> False" in diffs
    assert "statistics st: columns ['Status'] -> ['Id']" in diffs and "statistics st2 missing on target" in diffs
    assert _schema_diff(s, t) == []                           # switches off: old behaviour
    heap_s, heap_t = table(heap_compression="ROW"), table()
    assert _schema_diff(heap_s, heap_t, options=True) == ["heap compression ROW -> NONE"]


def test_extras_diff():
    src = scoped_extras(extras(), [table()], [ModuleDef("PROCEDURE", "dbo", "usp", "x")])
    same = extras_diff(cfg(), src, src)
    assert same == []
    tgt = DatabaseExtras(settings=DatabaseSettings(160, False, False, "x"),
                         principals=[Principal("readers", "ROLE"), Principal("app", "SQL_USER")],
                         role_members=[("readers", "app")], schema_owners={},
                         permissions=[Permission("GRANT", "SELECT", "readers", "OBJECT", "sales", "Order")],
                         extended_properties=[ExtendedProperty("d", "changed")])
    diffs = extras_diff(cfg(), src, tgt)
    for expected in ("compatibility level 150 -> 160", "READ_COMMITTED_SNAPSHOT True -> False",
                     "user ghost missing on target", "user c missing on target",
                     "app is not a member of role db_datareader on target",
                     "schema sales: owner app -> dbo",
                     "GRANT EXECUTE on object dbo.usp to app missing on target",
                     "extended property d on database: value differs",
                     "extended property d on sales.Order missing on target"):
        assert expected in diffs, (expected, diffs)
    assert not any("ghost" in d and "CONNECT" in d for d in diffs)     # grants of a missing user aren't repeated
    assert extras_diff(cfg(compatibility_level="target", copy_security=False, copy_extended_properties=False),
                       src, tgt) == ["READ_COMMITTED_SNAPSHOT True -> False"]
