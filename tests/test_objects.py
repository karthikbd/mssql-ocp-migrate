"""0.3.0: everything else inside a database (and the server objects it depends on).

DDL builders, catalog parsing for each object kind, the engine's ordering and preflight checks,
validation, and the data-path rules for memory-optimized and Always Encrypted tables."""
import pytest

from mssql_ocp_migrate import ddl
from mssql_ocp_migrate.adapter import ModuleDef
from mssql_ocp_migrate.catalog import variant_literal
from mssql_ocp_migrate.config import Config, ConnectionConfig
from mssql_ocp_migrate.engine import MigrationError, Migrator, RunResult, scoped_extras
from mssql_ocp_migrate.model import (CdcTable, Certificate, Column, DatabaseExtras, DatabaseSettings,
                                     FullTextIndex, Index, Permission, Principal, SecurityPolicy, Signature,
                                     SpatialIndex, Table, Temporal, XmlIndex)
from mssql_ocp_migrate.sqlserver import SqlServer, pytds_type
from mssql_ocp_migrate.validate import Validator, _schema_diff, extras_diff

from test_extras import FakeConn, fake_server


def cfg(**migration) -> Config:
    c = Config(source=ConnectionConfig(database="src"), target=ConnectionConfig(database="tgt"))
    for k, v in migration.items():
        setattr(c.migration, k, v)
    c.check()
    return c


def tbl(name="T", **kw) -> Table:
    t = Table("dbo", name, columns=[Column("Id", "int", nullable=False), Column("Year", "int", nullable=False)])
    for k, v in kw.items():
        setattr(t, k, v)
    return t


# ================================================================== DDL: columns and tables
def test_column_options():
    assert ddl.column_sql(Column("Fax", "varchar", 20, is_sparse=True)) == "[Fax] varchar(20) SPARSE NULL"
    assert ddl.column_sql(Column("All", "xml", -1, is_column_set=True)) == \
        "[All] xml COLUMN_SET FOR ALL_SPARSE_COLUMNS"
    masked = Column("Email", "nvarchar", 200, masking_function="partial(1, \"X\", 0)'")
    assert "MASKED WITH (FUNCTION = 'partial(1, \"X\", 0)''')" in ddl.column_sql(masked)
    assert "MASKED" not in ddl.column_sql(masked, masking=False)
    enc = Column("SSN", "char", 11, nullable=False, collation="Latin1_General_BIN2", encryption_key="CEK1",
                 encryption_type="DETERMINISTIC", encryption_algorithm="AEAD_AES_256_CBC_HMAC_SHA_256")
    assert ddl.column_sql(enc) == ("[SSN] char(11) COLLATE Latin1_General_BIN2 ENCRYPTED WITH (COLUMN_ENCRYPTION_KEY "
                                   "= [CEK1], ENCRYPTION_TYPE = DETERMINISTIC, ALGORITHM = "
                                   "'AEAD_AES_256_CBC_HMAC_SHA_256') NOT NULL")
    assert ddl.type_sql(Column("d", "xml", -1, xml_schema="sales.OrderSchema", xml_document=True)) == \
        "xml(DOCUMENT [sales].[OrderSchema])"
    assert ddl.type_sql(Column("d", "xml", -1, xml_schema="dbo.S")) == "xml(CONTENT [dbo].[S])"
    clr = Column("P", "Point", -1, clr_udt=True, user_type="Point", user_type_schema="geo")
    assert ddl.type_sql(clr) == "[geo].[Point]" and clr.is_clr
    d = Column("x", "int", default_name="DF_x", default_definition="((0))")
    assert ddl.column_sql(d, named_default=False).endswith("NULL DEFAULT ((0))")


def test_memory_optimized_table_has_inline_indexes():
    pk = Index("PK_M", [("Id", False)], unique=True, is_primary_key=True, is_hash=True, bucket_count=131072)
    ix = Index("IX_Year", [("Year", True)])
    uq = Index("UQ_M", [("Year", False)], unique=True, is_unique_constraint=True)
    t = tbl("Mem", primary_key=pk, indexes=[ix, uq], memory_optimized=True, durability="SCHEMA_ONLY")
    sql = ddl.create_table_sql(t)
    assert "CONSTRAINT [PK_M] PRIMARY KEY NONCLUSTERED HASH ([Id]) WITH (BUCKET_COUNT = 131072)" in sql
    assert "INDEX [IX_Year] NONCLUSTERED ([Year] DESC)" in sql
    assert "CONSTRAINT [UQ_M] UNIQUE NONCLUSTERED ([Year])" in sql
    assert sql.endswith("WITH (MEMORY_OPTIMIZED = ON, DURABILITY = SCHEMA_ONLY)")
    assert ddl.post_load_sql(t) == []                       # indexes are already in the table
    disk = ddl.create_table_sql(t, memory=False)
    assert "MEMORY_OPTIMIZED" not in disk and "PRIMARY KEY NONCLUSTERED ([Id])" in disk
    assert len(ddl.post_load_sql(t, memory=False)) == 2


def test_partitioned_tables_and_indexes():
    pk = Index("PK_P", [("Id", False), ("Year", False)], unique=True, clustered=True, is_primary_key=True,
               partition_scheme="ps_year", partition_column="Year", partition_compression={1: "PAGE", 2: "ROW", 3: "PAGE"})
    sql = ddl.create_table_sql(tbl(primary_key=pk))
    assert ("PRIMARY KEY CLUSTERED ([Id], [Year]) WITH (DATA_COMPRESSION = PAGE ON PARTITIONS (1, 3), "
            "DATA_COMPRESSION = ROW ON PARTITIONS (2)) ON [ps_year]([Year])") in sql
    heap = ddl.create_table_sql(tbl(partition_scheme="ps_year", partition_column="Year",
                                    heap_partition_compression={1: "ROW", 2: "NONE"}))
    assert heap.endswith(") ON [ps_year]([Year]) WITH (DATA_COMPRESSION = ROW ON PARTITIONS (1), "
                         "DATA_COMPRESSION = NONE ON PARTITIONS (2))")
    ix = Index("IX_P", [("Year", False)], partition_scheme="ps_year", partition_column="Year")
    assert ddl.index_sql(tbl(), ix).endswith("([Year]) ON [ps_year]([Year])")
    assert ddl.index_sql(tbl(), ix, partitions=False).endswith("([Year])")
    assert ddl.partition_scheme_sql("ps", "pf", ["FG1", "FG2"]) == \
        "CREATE PARTITION SCHEME [ps] AS PARTITION [pf] ALL TO ([PRIMARY])"
    assert ddl.partition_scheme_sql("ps", "pf", ["FG1", "FG2"], "source") == \
        "CREATE PARTITION SCHEME [ps] AS PARTITION [pf] TO ([FG1], [FG2])"


def test_xml_spatial_and_fulltext_indexes():
    t = tbl(xml_indexes=[XmlIndex("SX_Path", "Doc", primary=False, using="PX", secondary_type="PATH"),
                         XmlIndex("PX", "Doc")],
            spatial_indexes=[SpatialIndex("SI_Geo", "Loc", "GEOGRAPHY_AUTO_GRID", cells_per_object=12),
                             SpatialIndex("SI_Shape", "Shape", "GEOMETRY_GRID", (0, 0, 500, 200),
                                          ("LOW", "MEDIUM", "HIGH", "HIGH"), 64)],
            fulltext=FullTextIndex("ftc", "PK_T", [("Body", 1033, None, False), ("File", None, "Ext", True)],
                                   "MANUAL", "MyStops"))
    sql = ddl.post_load_sql(t)
    assert sql[0] == "CREATE PRIMARY XML INDEX [PX] ON [dbo].[T] ([Doc])"
    assert sql[1] == "CREATE XML INDEX [SX_Path] ON [dbo].[T] ([Doc]) USING XML INDEX [PX] FOR PATH"
    assert sql[2] == "CREATE SPATIAL INDEX [SI_Geo] ON [dbo].[T] ([Loc]) USING GEOGRAPHY_AUTO_GRID WITH (CELLS_PER_OBJECT = 12)"
    assert sql[3] == ("CREATE SPATIAL INDEX [SI_Shape] ON [dbo].[T] ([Shape]) USING GEOMETRY_GRID WITH (BOUNDING_BOX = "
                      "(0.0, 0.0, 500.0, 200.0), GRIDS = (LEVEL_1 = LOW, LEVEL_2 = MEDIUM, LEVEL_3 = HIGH, LEVEL_4 = HIGH), "
                      "CELLS_PER_OBJECT = 64)")
    assert ddl.fulltext_index_sql(t) == (
        "CREATE FULLTEXT INDEX ON [dbo].[T] ([Body] LANGUAGE 1033, [File] TYPE COLUMN [Ext] STATISTICAL_SEMANTICS) "
        "KEY INDEX [PK_T] ON [ftc] WITH (CHANGE_TRACKING = MANUAL, STOPLIST = [MyStops])")
    t.fulltext.stoplist = None
    assert ddl.fulltext_index_sql(t).endswith("STOPLIST = OFF)")


def test_temporal_change_tracking_and_table_types():
    t = tbl(columns=[Column("Id", "int"), Column("ValidFrom", "datetime2", scale=7, is_hidden=True),
                     Column("ValidTo", "datetime2", scale=7, is_hidden=True)],
            temporal=Temporal("ValidFrom", "ValidTo", "hist", "T_History", "6 MONTHS"), change_tracking=True)
    assert ddl.temporal_sql(t) == [
        "ALTER TABLE [dbo].[T] ADD PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [ValidFrom] ADD HIDDEN",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [ValidTo] ADD HIDDEN",
        "ALTER TABLE [dbo].[T] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [hist].[T_History], "
        "DATA_CONSISTENCY_CHECK = ON, HISTORY_RETENTION_PERIOD = 6 MONTHS))"]
    assert ddl.temporal_sql(tbl()) == []
    assert ddl.change_tracking_sql(t) == "ALTER TABLE [dbo].[T] ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = ON)"
    assert ddl.change_tracking_sql(tbl()) is None
    tt = Table("dbo", "OrderLines", columns=[Column("Id", "int", nullable=False),
                                             Column("Qty", "int", default_name="DF__x", default_definition="((1))")],
               primary_key=Index("PK__x", [("Id", False)], unique=True, clustered=True, is_primary_key=True),
               indexes=[Index("IX_Qty", [("Qty", False)])])
    tt.checks = [type("C", (), {"definition": "([Qty]>(0))"})()]
    assert ddl.table_type_sql(tt) == ("CREATE TYPE [dbo].[OrderLines] AS TABLE (\n    [Id] int NOT NULL,\n    "
                                      "[Qty] int NULL DEFAULT ((1)),\n    PRIMARY KEY CLUSTERED ([Id]),\n    "
                                      "INDEX [IX_Qty] NONCLUSTERED ([Qty]),\n    CHECK ([Qty]>(0))\n)")
    assert ddl.table_type_sql(Table("dbo", "M", columns=[Column("Id", "int", nullable=False)]), True).endswith(
        "WITH (MEMORY_OPTIMIZED = ON)")


# ================================================================== DDL: database level
def test_security_ddl():
    assert ddl.principal_sql(Principal("signer", "CERTIFICATE_USER", certificate="SignCert")) == \
        "CREATE USER [signer] FROM CERTIFICATE [SignCert]"
    assert ddl.principal_sql(Principal("k", "ASYMMETRIC_KEY_USER", certificate="AK")) == \
        "CREATE USER [k] FROM ASYMMETRIC KEY [AK]"
    for p, sql in [
        (Permission("GRANT", "SEND", "app", "SERVICE", None, "//shop/Orders"), "GRANT SEND ON SERVICE::[//shop/Orders] TO [app]"),
        (Permission("GRANT", "EXECUTE", "app", "XML SCHEMA COLLECTION", "sales", "S"),
         "GRANT EXECUTE ON XML SCHEMA COLLECTION::[sales].[S] TO [app]"),
        (Permission("GRANT", "REFERENCES", "app", "ASSEMBLY", None, "Geo"), "GRANT REFERENCES ON ASSEMBLY::[Geo] TO [app]"),
        (Permission("GRANT", "CONTROL", "app", "CERTIFICATE", None, "C1"), "GRANT CONTROL ON CERTIFICATE::[C1] TO [app]"),
    ]:
        assert ddl.permission_sql(p) == sql
    pol = SecurityPolicy("rls", "pol", True, True, [
        ("FILTER", "([rls].[fn]([Rep]))", "sales", "Order", None),
        ("BLOCK", "([rls].[fn]([Rep]))", "sales", "Order", "AFTER INSERT"),
        ("FILTER", "([rls].[fn]([Rep]))", "sales", "Other", None)])
    assert ddl.security_policy_sql(pol, {("sales", "Order")}) == (
        "CREATE SECURITY POLICY [rls].[pol] ADD FILTER PREDICATE [rls].[fn]([Rep]) ON [sales].[Order], "
        "ADD BLOCK PREDICATE [rls].[fn]([Rep]) ON [sales].[Order] AFTER INSERT "
        "WITH (STATE = OFF, SCHEMABINDING = ON)")
    assert ddl.security_policy_sql(pol, {("x", "y")}) is None
    assert ddl.strip_outer_parens("(([s].[f]([a])))") == "[s].[f]([a])"
    assert ddl.strip_outer_parens("([s].[f]([a]) )") == "[s].[f]([a])"
    assert ddl.strip_outer_parens("([a])+([b])") == "([a])+([b])"
    assert ddl.strip_outer_parens("([s].[f](N')('))") == "[s].[f](N')(')"
    assert ddl.policy_state_sql(pol) == "ALTER SECURITY POLICY [rls].[pol] WITH (STATE = ON)"
    assert ddl.certificate_sql(Certificate("C", None, "0x30", "0x1F", "pw'd")) == (
        "CREATE CERTIFICATE [C] FROM BINARY = 0x30 WITH PRIVATE KEY (BINARY = 0x1F, DECRYPTION BY PASSWORD = N'pw''d')")
    assert ddl.certificate_sql(Certificate("C", None, "0x30")) == "CREATE CERTIFICATE [C] FROM BINARY = 0x30"
    assert ddl.signature_sql(Signature("dbo", "usp", "C", "0xAB", counter=True)) == \
        "ADD COUNTER SIGNATURE TO [dbo].[usp] BY CERTIFICATE [C] WITH SIGNATURE = 0xAB"
    assert ddl.master_key_sql("p") == "CREATE MASTER KEY ENCRYPTION BY PASSWORD = N'p'"
    assert "QUOTENAME(DB_NAME())" in ddl.owner_sql("app_owner") and "[app_owner]" in ddl.owner_sql("app_owner")


def test_settings_cdc_ddl():
    src = DatabaseSettings(150, False, False, "x", options={"ANSI_NULLS": "ON", "BROKER": "ENABLE_BROKER",
                                                           "PARAMETERIZATION": "FORCED"},
                           scoped_configurations={"MAXDOP": "4", "IDENTITY_CACHE": "0", "NEW_ONE": "1"},
                           change_tracking="CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON", query_store="READ_WRITE")
    tgt = DatabaseSettings(150, False, False, "x", options={"ANSI_NULLS": "ON", "BROKER": "DISABLE_BROKER",
                                                           "PARAMETERIZATION": "SIMPLE"},
                           scoped_configurations={"MAXDOP": "0", "IDENTITY_CACHE": "1"})
    assert ddl.database_settings_sql(src, tgt, "source") == [
        "ALTER DATABASE CURRENT SET ENABLE_BROKER WITH ROLLBACK IMMEDIATE",
        "ALTER DATABASE CURRENT SET PARAMETERIZATION FORCED",
        "ALTER DATABASE SCOPED CONFIGURATION SET MAXDOP = 4",
        "ALTER DATABASE SCOPED CONFIGURATION SET IDENTITY_CACHE = OFF",
        "ALTER DATABASE CURRENT SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON)",
        "ALTER DATABASE CURRENT SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE)"]
    sql = ddl.cdc_sql([CdcTable("sales", "Order", "sales_Order", None, True, ["Id", "Amount"], "PK_Order")])
    assert sql[0].endswith("EXEC sys.sp_cdc_enable_db")
    assert sql[1] == ("EXEC sys.sp_cdc_enable_table @source_schema = N'sales', @source_name = N'Order', "
                      "@capture_instance = N'sales_Order', @role_name = NULL, @supports_net_changes = 1, "
                      "@index_name = N'PK_Order', @captured_column_list = N'[Id], [Amount]'")


# ================================================================== catalog
def test_variant_literals():
    assert variant_literal("int", "42") == "42"
    assert variant_literal("decimal", "10.50") == "10.50"
    assert variant_literal("varbinary", "0x0A") == "0x0A"
    assert variant_literal("datetime2", "2024-01-01T00:00:00") == "N'2024-01-01T00:00:00'"
    assert variant_literal("nvarchar", "O'Hara") == "N'O''Hara'"
    assert variant_literal(None, None) == "NULL"


def crow(oid, name, utype, stype=None, max_len=4, nullable=1, *, is_udt=0, is_asm=0, uschema="sys", prec=0, scale=0,
         ident=0, ddef=None, dname=None, sparse=0, colset=0, mask=None, enc=None, cek=None, alg=None, gen=None,
         hidden=0, xsch=None, xname=None, xdoc=0, comp=None):
    return (oid, name, utype, is_udt, is_asm, uschema, stype or utype, max_len, prec, scale, nullable, ident,
            1 if ident else None, 1 if ident else None, 1 if comp else 0, comp, 0, None, dname, ddef, sparse, colset,
            mask, enc, cek, alg, gen, hidden, xsch, xname, xdoc)


def test_column_from_row_kinds():
    c = SqlServer.column_from_row
    clr = c(crow(1, "P", "Point", "varbinary", -1, is_asm=1, uschema="geo"))
    assert clr.clr_udt and clr.user_type == "Point" and clr.user_type_schema == "geo"
    geo = c(crow(1, "G", "geography", "varbinary", -1, is_asm=1))
    assert not geo.clr_udt and geo.is_clr and geo.user_type is None
    assert c(crow(1, "F", "varchar", max_len=20, sparse=1)).is_sparse
    cs = c(crow(1, "S", "xml", max_len=-1, colset=1))
    assert cs.is_column_set and not cs.insertable and not cs.comparable
    m = c(crow(1, "E", "nvarchar", max_len=100, mask="email()"))
    assert m.masking_function == "email()"
    e = c(crow(1, "X", "char", max_len=11, enc="DETERMINISTIC", cek="CEK1", alg="AEAD_AES_256_CBC_HMAC_SHA_256"))
    assert e.encrypted and e.encryption_type == "DETERMINISTIC"
    g = c(crow(1, "VF", "datetime2", max_len=8, scale=7, gen="AS_ROW_START", hidden=1))
    assert g.generated_always == "ROW START" and g.is_hidden
    x = c(crow(1, "D", "xml", max_len=-1, xsch="sales", xname="S", xdoc=1))
    assert x.xml_schema == "sales.S" and x.xml_document


def irow(oid, iid, name, tdesc, col, *, uniq=0, pk=0, uc=0, key=1, desc=0, incl=0, bucket=None, disabled=0):
    return (oid, iid, name, tdesc, uniq, pk, uc, None, key, desc, incl, col, 0, 0, 0, 1, 1, "NONE", None, bucket, 0,
            disabled)


MODULE_ANSWERS = [
    ("SELECT a.assembly_id, a.name, a.permission_set_desc", [(65537, "GeoLib", "SAFE_ACCESS"),
                                                             (65536, "Base", "UNSAFE_ACCESS")]),
    ("FROM sys.assembly_files", [(65536, 1, "base", "0x4D5A01"), (65537, 1, "geo", "0x4D5A02"),
                                 (65537, 2, "geo.pdb", "0x0102")]),
    ("FROM sys.assembly_references", [(65537, 65536)]),
    ("FROM sys.assembly_types t", [("geo", "Point", "GeoLib", "Geo.Point")]),
    ("SELECT SCHEMA_NAME(schema_id), name FROM sys.xml_schema_collections", [("sales", "OrderSchema")]),
    ("XML_SCHEMA_NAMESPACE", [("<xsd:schema xmlns:xsd='x'/>",)]),
    ("t.is_table_type = 0", [("dbo", "Phone", "varchar", 20, 0, 0, 1)]),
    ("FROM sys.table_types tt WHERE tt.is_user_defined", [(900, "dbo", "IdList", 0)]),
    ("tt.type_table_object_id = c.object_id", [crow(900, "Id", "int", nullable=0)]),
    ("tt.type_table_object_id = i.object_id", [irow(900, 1, "PK__IdList", "CLUSTERED", "Id", uniq=1, pk=1)]),
    ("HAVING COUNT(DISTINCT p2.data_compression)", []),
    ("FROM sys.check_constraints", []),
    ("FROM sys.sequences s", []),
    ("SELECT pf.function_id, pf.name, pf.boundary_value_on_right", [(65536, "pf_year", 1)]),
    ("FROM sys.partition_parameters pp", [(65536, "date", 0, "sys", 3, 10, 0, None)]),
    ("FROM sys.partition_range_values rv", [(65536, "date", "2024-01-01T00:00:00"),
                                            (65536, "date", "2025-01-01T00:00:00")]),
    ("FROM sys.destination_data_spaces dds", [(65601, "FG2024"), (65601, "FG2025"), (65601, "PRIMARY")]),
    ("SELECT ps.data_space_id, ps.name, pf.name", [(65601, "ps_year", "pf_year")]),
    ("SELECT name, key_store_provider_name", [("CMK1", "MSSQL_CERTIFICATE_STORE", "CurrentUser/My/AB12", 0, None)]),
    ("FROM sys.column_encryption_keys k", [("CEK1", "CMK1", "RSA_OAEP", "0x016E00")]),
    ("FROM sys.fulltext_catalogs", [("ftc", 1, 0)]),
    ("FROM sys.fulltext_stopwords", [(5, "foo", 1033)]),
    ("FROM sys.fulltext_stoplists", [(5, "MyStops")]),
    ("SELECT is_cdc_enabled FROM sys.databases", [(0,)]),
    ("te.is_first = 1 OR te.is_last = 1", [(2001, 1, "INSERT"), (2001, 0, "DELETE"), (3001, 1, "CREATE_TABLE")]),
    ("LEFT JOIN sys.triggers tr ON tr.object_id = o.object_id", [
        (1, "V ", "sales", "vOrders", "CREATE VIEW sales.vOrders AS SELECT 1 AS x", None, 0, 1, 1),
        (2, "R ", "dbo", "rPositive", "CREATE RULE dbo.rPositive AS @v > 0", None, 0, 1, 1),
        (3, "P ", "dbo", "usp_Legacy", "CREATE PROCEDURE dbo.usp_Legacy AS SELECT \"x\" WHERE NULL = NULL", None, 0, 0, 0),
        (2001, "TR", "dbo", "trg_First", "CREATE TRIGGER dbo.trg_First ON dbo.Place AFTER INSERT, DELETE AS SET NOCOUNT ON",
         "dbo.Place", 1, 1, 1)]),
    ("FROM sys.assembly_modules am", [
        (1001, "PC", "geo", "usp_Near", "GeoLib", "Geo.Procs", "Near", 0, None, None, None),
        (1002, "FS", "geo", "fnDist", "GeoLib", "Geo.Funcs", "Dist", 1, -2, None, None),
        (1003, "FT", "geo", "fnSplit", "GeoLib", "Geo.Funcs", "Split", 0, None, None, None),
        (1004, "TA", "dbo", "trg_clr", "GeoLib", "Geo.Trg", "Fire", 0, None, "dbo", "Place"),
        (1005, "AF", "geo", "Concat", "GeoLib", "Geo.Concat", None, 0, None, None, None)]),
    ("FROM sys.parameters p", [
        (1001, 1, "@lat", "float", 0, 0, "sys", "float", 8, 53, 0, 0, 1, "float", "1.5E+001"),
        (1001, 2, "@n", "int", 0, 0, "sys", "int", 4, 10, 0, 1, 0, None, None),
        (1002, 0, "", "float", 0, 0, "sys", "float", 8, 53, 0, 0, 0, None, None),
        (1002, 1, "@a", "Point", 0, 1, "geo", "varbinary", -1, 0, 0, 0, 0, None, None),
        (1003, 1, "@s", "nvarchar", 0, 0, "sys", "nvarchar", -1, 0, 0, 0, 0, None, None),
        (1005, 0, "", "nvarchar", 0, 0, "sys", "nvarchar", -1, 0, 0, 0, 0, None, None),
        (1005, 1, "@v", "nvarchar", 0, 0, "sys", "nvarchar", 8000, 0, 0, 0, 0, None, None)]),
    ("WHERE c.object_id IN (", [(1003, "item", "nvarchar", 0, 0, "sys", "nvarchar", 200, 0, 0)]),
    ("FROM sys.trigger_events te", [(1004, "INSERT", 0), (1004, "UPDATE", 0)]),
    ("FROM sys.synonyms", []),
    ("FROM sys.service_message_types mt", [("//shop/Order", "XML", "sales", "OrderSchema"),
                                           ("//shop/Ack", "EMPTY", None, None)]),
    ("FROM sys.service_contracts c\n", [("//shop/OrderContract", "//shop/Order", 1, 0),
                                        ("//shop/OrderContract", "//shop/Ack", 0, 1)]),
    ("q.is_receive_enabled", [("dbo", "OrderQ", 1, 1, 0, 1, "[src_db].[dbo].[usp_Process]", 4, -2, 1)]),
    ("FROM sys.services s JOIN sys.service_queues", [("//shop/OrderService", "dbo", "OrderQ", "//shop/OrderContract")]),
    ("FROM sys.routes", [("ToHQ", "//hq/Svc", None, "TCP://hq:4022", None)]),
    ("FROM sys.remote_service_bindings b", []),
    ("service_broker_guid", [("ABC-123",)]),
    ("FROM sys.event_notifications en", [(77, "LogDDL", "//shop/Audit", "abc-123", "CREATE_TABLE"),
                                         (77, "LogDDL", "//shop/Audit", "abc-123", "ALTER_TABLE")]),
    ("WHERE t.parent_class = 0", [(3001, "ddl_audit", "CREATE TRIGGER ddl_audit ON DATABASE FOR CREATE_TABLE AS PRINT 1",
                                   1, 1, 1)]),
    ("FROM sys.plan_guides pg", [("pg1", 1, "SELECT 1", "OBJECT", "sales", "usp", None, None, "OPTION (MAXDOP 1)")]),
]


def test_list_modules_every_kind_in_dependency_order():
    mods = fake_server(MODULE_ANSWERS).list_modules()
    kinds = [(m.kind, m.name) for m in mods]
    assert kinds[:2] == [("ASSEMBLY", "Base"), ("ASSEMBLY", "GeoLib")]        # referenced assembly first
    by = {(m.kind, m.name): m for m in mods}
    geo = by[("ASSEMBLY", "GeoLib")]
    assert geo.definition == ("CREATE ASSEMBLY [GeoLib] FROM 0x4D5A02 WITH PERMISSION_SET = SAFE;\n"
                              "ALTER ASSEMBLY [GeoLib] ADD FILE FROM 0x0102 AS N'geo.pdb'")
    import hashlib
    assert geo.meta["sha512"] == hashlib.sha512(bytes.fromhex("4D5A02")).hexdigest()
    assert by[("ASSEMBLY", "Base")].definition.endswith("PERMISSION_SET = UNSAFE")
    assert by[("TYPE", "Point")].definition == "CREATE TYPE [geo].[Point] EXTERNAL NAME [GeoLib].[Geo.Point]"
    assert by[("XML SCHEMA COLLECTION", "OrderSchema")].definition.startswith(
        "CREATE XML SCHEMA COLLECTION [sales].[OrderSchema] AS N'<xsd:schema xmlns:xsd=''x''/>'")
    assert by[("TYPE", "IdList")].definition == ("CREATE TYPE [dbo].[IdList] AS TABLE (\n    [Id] int NOT NULL,\n"
                                                 "    PRIMARY KEY CLUSTERED ([Id])\n)")
    assert by[("PARTITION FUNCTION", "pf_year")].definition == (
        "CREATE PARTITION FUNCTION [pf_year] (date) AS RANGE RIGHT FOR VALUES (N'2024-01-01T00:00:00', "
        "N'2025-01-01T00:00:00')")
    assert by[("PARTITION SCHEME", "ps_year")].meta == {"function": "pf_year",
                                                        "filegroups": ["FG2024", "FG2025", "PRIMARY"]}
    assert by[("COLUMN MASTER KEY", "CMK1")].definition == (
        "CREATE COLUMN MASTER KEY [CMK1] WITH (KEY_STORE_PROVIDER_NAME = N'MSSQL_CERTIFICATE_STORE', "
        "KEY_PATH = N'CurrentUser/My/AB12')")
    assert by[("COLUMN ENCRYPTION KEY", "CEK1")].definition == (
        "CREATE COLUMN ENCRYPTION KEY [CEK1] WITH VALUES (COLUMN_MASTER_KEY = [CMK1], ALGORITHM = 'RSA_OAEP', "
        "ENCRYPTED_VALUE = 0x016E00)")
    assert by[("FULLTEXT CATALOG", "ftc")].definition == \
        "CREATE FULLTEXT CATALOG [ftc] WITH ACCENT_SENSITIVITY = OFF AS DEFAULT"
    assert by[("FULLTEXT STOPLIST", "MyStops")].definition == (
        "CREATE FULLTEXT STOPLIST [MyStops];\nALTER FULLTEXT STOPLIST [MyStops] ADD N'foo' LANGUAGE 1033")
    assert by[("RULE", "rPositive")].definition.startswith("CREATE RULE")
    assert by[("PROCEDURE", "usp_Near")].definition == (
        "CREATE PROCEDURE [geo].[usp_Near] @lat float = 1.5E+001, @n int OUTPUT AS EXTERNAL NAME "
        "[GeoLib].[Geo.Procs].[Near]")
    assert by[("FUNCTION", "fnDist")].definition == (
        "CREATE FUNCTION [geo].[fnDist] (@a [geo].[Point]) RETURNS float WITH RETURNS NULL ON NULL INPUT, "
        "EXECUTE AS OWNER AS EXTERNAL NAME [GeoLib].[Geo.Funcs].[Dist]")
    assert by[("FUNCTION", "fnSplit")].definition == (
        "CREATE FUNCTION [geo].[fnSplit] (@s nvarchar(max)) RETURNS TABLE ([item] nvarchar(100)) "
        "AS EXTERNAL NAME [GeoLib].[Geo.Funcs].[Split]")
    assert by[("TRIGGER", "trg_clr")].definition == (
        "CREATE TRIGGER [dbo].[trg_clr] ON [dbo].[Place] AFTER INSERT, UPDATE AS EXTERNAL NAME "
        "[GeoLib].[Geo.Trg].[Fire]")
    assert by[("AGGREGATE", "Concat")].definition == (
        "CREATE AGGREGATE [geo].[Concat] (@v nvarchar(4000)) RETURNS nvarchar(max) EXTERNAL NAME "
        "[GeoLib].[Geo.Concat]")
    assert by[("MESSAGE TYPE", "//shop/Order")].definition == (
        "CREATE MESSAGE TYPE [//shop/Order] VALIDATION = VALID_XML WITH SCHEMA COLLECTION [sales].[OrderSchema]")
    assert by[("MESSAGE TYPE", "//shop/Ack")].definition.endswith("VALIDATION = EMPTY")
    assert by[("CONTRACT", "//shop/OrderContract")].definition == (
        "CREATE CONTRACT [//shop/OrderContract] ([//shop/Order] SENT BY INITIATOR, [//shop/Ack] SENT BY TARGET)")
    assert by[("QUEUE", "OrderQ")].definition == (
        "CREATE QUEUE [dbo].[OrderQ] WITH STATUS = ON, RETENTION = OFF, ACTIVATION (STATUS = ON, PROCEDURE_NAME = "
        "[dbo].[usp_Process], MAX_QUEUE_READERS = 4, EXECUTE AS OWNER), POISON_MESSAGE_HANDLING (STATUS = ON)")
    assert by[("SERVICE", "//shop/OrderService")].definition == \
        "CREATE SERVICE [//shop/OrderService] ON QUEUE [dbo].[OrderQ] ([//shop/OrderContract])"
    assert by[("ROUTE", "ToHQ")].definition == \
        "CREATE ROUTE [ToHQ] WITH SERVICE_NAME = N'//hq/Svc', ADDRESS = N'TCP://hq:4022'"
    assert by[("EVENT NOTIFICATION", "LogDDL")].definition == (
        "CREATE EVENT NOTIFICATION [LogDDL] ON DATABASE FOR CREATE_TABLE, ALTER_TABLE TO SERVICE N'//shop/Audit', "
        "N'current database'")
    ddl_trg = by[("DDL TRIGGER", "ddl_audit")]
    assert ddl_trg.disabled and ddl_trg.schema == ""
    assert ddl.module_statements(ddl_trg)[1:] == [
        "DISABLE TRIGGER [ddl_audit] ON DATABASE",
        "EXEC sys.sp_settriggerorder @triggername = N'[ddl_audit]', @order = N'First', @stmttype = N'CREATE_TABLE', "
        "@namespace = N'DATABASE'"]
    legacy = by[("PROCEDURE", "usp_Legacy")]
    assert ddl.module_statements(legacy) == ["SET ANSI_NULLS OFF", "SET QUOTED_IDENTIFIER OFF",
                                             legacy.definition, "SET ANSI_NULLS ON", "SET QUOTED_IDENTIFIER ON"]
    trg = by[("TRIGGER", "trg_First")]
    assert ddl.module_statements(trg) == [
        trg.definition, "DISABLE TRIGGER [dbo].[trg_First] ON [dbo].[Place]",
        "EXEC sys.sp_settriggerorder @triggername = N'[dbo].[trg_First]', @order = N'First', @stmttype = N'INSERT'",
        "EXEC sys.sp_settriggerorder @triggername = N'[dbo].[trg_First]', @order = N'Last', @stmttype = N'DELETE'"]
    assert ddl.module_statements(by[("VIEW", "vOrders")]) == [by[("VIEW", "vOrders")].definition]
    assert by[("PLAN GUIDE", "pg1")].definition == (
        "EXEC sys.sp_create_plan_guide @name = N'pg1', @stmt = N'SELECT 1', @type = N'OBJECT', "
        "@module_or_batch = N'[sales].[usp]', @params = NULL, @hints = N'OPTION (MAXDOP 1)';\n"
        "EXEC sys.sp_control_plan_guide N'DISABLE', N'pg1'")
    order = [m.kind for m in mods]
    assert order.index("XML SCHEMA COLLECTION") < order.index("PARTITION FUNCTION") < order.index("VIEW") \
        < order.index("MESSAGE TYPE") < order.index("DDL TRIGGER")


def test_server_level_catalog():
    s = fake_server([
        ("WHERE sp.name = ?", [("S", "0x01", "0x0200AB", "master", 1, 0, 1, "British")]),
        ("sys.server_role_members", [("dbcreator", 1), ("monitoring", 0)]),
        ("FROM sys.server_permissions p", [("GRANT", "VIEW SERVER STATE", 100, None),
                                           ("GRANT_WITH_GRANT_OPTION", "IMPERSONATE", 101, "etl_login")]),
    ])
    logins, errors = s.server_logins([Principal("app", "SQL_USER", login="app_login"),
                                      Principal("svc", "NO_LOGIN")])
    assert errors == [] and logins[0][0] == "app_login"
    assert logins[0][1].split(";\n") == [
        "CREATE LOGIN [app_login] WITH PASSWORD = 0x0200AB HASHED, SID = 0x01, DEFAULT_DATABASE = [master], "
        "CHECK_POLICY = ON, CHECK_EXPIRATION = OFF",
        "ALTER LOGIN [app_login] WITH DEFAULT_LANGUAGE = [British]",
        "ALTER LOGIN [app_login] DISABLE",
        "ALTER SERVER ROLE [dbcreator] ADD MEMBER [app_login]",
        "IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'monitoring' AND type = 'R') "
        "CREATE SERVER ROLE [monitoring]",
        "EXEC master.sys.sp_executesql N'GRANT VIEW SERVER STATE TO [monitoring]'",
        "EXEC master.sys.sp_executesql N'GRANT IMPERSONATE ON LOGIN::[etl_login] TO [monitoring] WITH GRANT OPTION'",
        "ALTER SERVER ROLE [monitoring] ADD MEMBER [app_login]",
        "EXEC master.sys.sp_executesql N'GRANT VIEW SERVER STATE TO [app_login]'",
        "EXEC master.sys.sp_executesql N'GRANT IMPERSONATE ON LOGIN::[etl_login] TO [app_login] WITH GRANT OPTION'"]
    no_hash = fake_server([("WHERE sp.name = ?", [("S", "0x01", None, "master", 1, 0, 0)])])
    assert "CONTROL SERVER" in no_hash.server_logins([Principal("a", "SQL_USER", login="a")])[1][0]
    answers = [
        ("FROM msdb.dbo.sysjobs j", [("J1", "Nightly", "rebuild", "Database Maintenance", "sa", 1, 1, 3)]),
        ("FROM msdb.dbo.sysoperators", [("DBA team", 1, "dba@example.com", None)]),
        ("FROM msdb.dbo.sysjobsteps WHERE", [(1, "rebuild", "TSQL", "EXEC dbo.usp", "src", None, 1, 0, 2, 0, 0, 0,
                                              None, 0, 5)]),
        ("FROM msdb.dbo.sysjobschedules js", [("daily", 1, 4, 1, 1, 0, 0, 0, 20240101, 99991231, 10000, 235959)]),
    ]
    name, stmts, warns = fake_server(answers).agent_jobs("src", "tgt")[0]
    assert name == "Nightly" and stmts[0].endswith(
        "EXEC msdb.dbo.sp_add_operator @name = N'DBA team', @enabled = 1, @email_address = N'dba@example.com'")
    assert "@enabled = 0" in stmts[2] and "@database_name = N'tgt'" in stmts[3]
    assert any("@notify_email_operator_name = N'DBA team'" in x and "@notify_level_email = 1" in x for x in stmts)
    assert stmts[-1].endswith("@server_name = N'(local)'") and any("@freq_type = 4" in x for x in stmts)
    assert len(warns) == 2                       # Database Mail for the operator + proxy
    answers[2] = ("FROM msdb.dbo.sysjobsteps WHERE", [(1, "copy", "CmdExec", "robocopy", None, None, 1, 0, 2, 0, 0, 0,
                                                       None, 0, None)])
    name, stmts, warns = fake_server(answers).agent_jobs("src", "tgt", linux_target=True)[0]
    assert stmts == [] and "CmdExec" in warns[0]
    assert fake_server(answers).agent_jobs("src", "tgt")[0][1]          # a Windows target still gets it
    msgs = fake_server([
        ("FROM sys.sql_modules WHERE definition", [("RAISERROR(50010, 16, 1); SELECT FORMATMESSAGE(50011)",),
                                                   ("RAISERROR('plain', 16, 1)",)]),
        ("FROM sys.messages m", [(50010, 1031, "Deutsch", 16, 0, "Fehler %d"), (50010, 1033, "us_english", 16, 1,
                                                                                 "Error %d")]),
    ]).custom_messages()
    assert msgs == [(50010, [
        ("us_english", "EXEC sys.sp_addmessage @msgnum = 50010, @severity = 16, @msgtext = N'Error %d', "
                       "@lang = N'us_english', @with_log = TRUE"),
        ("Deutsch", "EXEC sys.sp_addmessage @msgnum = 50010, @severity = 16, @msgtext = N'Fehler %d', "
                    "@lang = N'Deutsch'")])]
    links = fake_server([
        ("FROM sys.servers WHERE is_linked = 1", [(1, "HQ", "SQL Server", "SQLNCLI", "hq", None, None, None,
                                                   1, 1, 1, 0, 1, None, 0, 0)]),
        ("FROM sys.linked_logins ll", [(None, 1, None), ("app_login", 0, "remote_app")]),
    ]).linked_servers()
    lname, lstmts, lwarns = links[0]
    assert lstmts[0] == "EXEC master.dbo.sp_addlinkedserver @server = N'HQ', @srvproduct = N'SQL Server'"
    assert "N'rpc out', N'true'" in lstmts[2] and "@useself = N'True', @locallogin = NULL" in lstmts[-1]
    assert "remote_app" in lwarns[0]


def test_preflight_helpers():
    s = fake_server([("security_predicates sp JOIN", [("sales", "Order")]),
                     ("SELECT COUNT_BIG(*) FROM [sales].[Order]", [(10,)]),
                     ("FROM sys.partitions WHERE object_id = OBJECT_ID", [(25,)]),
                     ("IS_SRVROLEMEMBER", [(0,)]),
                     ("FROM sys.database_files", [("/var/opt/mssql/data/app.mdf",)])])
    t = Table("sales", "Order")
    assert s.rls_hidden_rows([t, Table("dbo", "Other")]) == [("sales.Order", 10, 25)]
    assert not s.can_unmask()
    assert s.primary_data_dir() == "/var/opt/mssql/data/"
    w = fake_server([("FROM sys.database_files", [("D:\\SQL\\app.mdf",)])])
    assert w.primary_data_dir() == "D:\\SQL\\"


# ================================================================== engine
class FakeSrc:
    """A source Database for preflight and schema-script tests."""

    def __init__(self, tables, modules=(), extras=None, unmask=True, hidden=(), jobs=(), links=()):
        self.tables, self.modules, self.extras = tables, list(modules), extras
        self.unmask, self.hidden, self.jobs, self.links = unmask, list(hidden), list(jobs), list(links)

    def list_tables(self):
        return self.tables

    def list_modules(self):
        return self.modules

    def list_schemas(self):
        return ["dbo", "sales", "audit"]

    def database_extras(self):
        return self.extras

    def rule_default_bindings(self):
        return ["EXEC sys.sp_bindrule N'[dbo].[r]', N'dbo.T.Id'"]

    def can_unmask(self):
        return self.unmask

    def rls_hidden_rows(self, tables):
        return self.hidden

    def agent_jobs(self, a, b, linux=False):
        return self.jobs

    def linked_servers(self):
        return self.links

    def close(self):
        pass


def test_preflight_refuses_masked_and_filtered_reads():
    m = Migrator(cfg(), factory=lambda s, a: None, progress=lambda _: None)
    masked = tbl(columns=[Column("Email", "nvarchar", 100, masking_function="email()")])
    with pytest.raises(MigrationError, match="UNMASK"):
        m._preflight_source(FakeSrc([masked], unmask=False), [masked], RunResult(0))
    m._preflight_source(FakeSrc([masked]), [masked], RunResult(0))
    with pytest.raises(MigrationError, match=r"row-level security hides rows.*10 of 25"):
        m._preflight_source(FakeSrc([], hidden=[("sales.Order", 10, 25)]), [], RunResult(0))


def test_preflight_temporal_history_and_server_object_warnings():
    m = Migrator(cfg(), factory=lambda s, a: None, progress=lambda _: None)
    t = tbl(temporal=Temporal("F", "T", "hist", "T_History"))
    res = RunResult(0)
    m._preflight_source(FakeSrc([t], jobs=[("Nightly", [], [])], links=[("HQ", [], [])]), [t], res)
    assert t.temporal is None
    joined = " | ".join(res.warnings)
    assert "hist.T_History isn't selected" in joined and "copy_agent_jobs" in joined and "copy_linked_servers" in joined


def test_skipped_tables_are_errors_not_silence():
    src = FakeSrc([tbl("Graph", warnings=["graph table: not supported by migrate; skipped, use lift"]), tbl("Ok")])
    m = Migrator(cfg(), factory=lambda s, a: src, progress=lambda _: None)
    assert [t.name for t in m.source_tables()] == ["Ok"]
    assert m.skipped_tables[0][0] == "dbo.Graph"


def test_schema_module_sql_trust_and_partition_modes():
    asm = ModuleDef("ASSEMBLY", "", "Geo", "CREATE ASSEMBLY [Geo] FROM 0x00", meta={"sha512": "ab"})
    plain = Migrator(cfg(), factory=lambda s, a: None)._schema_module_sql(asm)
    assert plain == ["CREATE ASSEMBLY [Geo] FROM 0x00"]
    trusted = Migrator(cfg(trust_clr_assemblies=True), factory=lambda s, a: None)._schema_module_sql(asm)
    assert "sp_add_trusted_assembly 0xab, N'Geo'" in trusted[0] and trusted[1].startswith("CREATE ASSEMBLY")
    ps = ModuleDef("PARTITION SCHEME", "", "ps", "", meta={"function": "pf", "filegroups": ["FG1"]})
    assert Migrator(cfg(), factory=None)._schema_module_sql(ps)[0].endswith("ALL TO ([PRIMARY])")
    assert Migrator(cfg(partition_filegroups="source"), factory=None)._schema_module_sql(ps)[0].endswith("TO ([FG1])")


def test_module_switches_filter_broker_and_fulltext():
    mods = [ModuleDef("QUEUE", "dbo", "Q", "x"), ModuleDef("FULLTEXT CATALOG", "", "c", "x"),
            ModuleDef("VIEW", "dbo", "v", "x")]
    src = FakeSrc([], modules=mods)
    assert [x.kind for x in Migrator(cfg(), factory=None)._modules(src)] == ["QUEUE", "FULLTEXT CATALOG", "VIEW"]
    off = Migrator(cfg(copy_service_broker=False, copy_full_text=False), factory=None)._modules(src)
    assert [x.kind for x in off] == ["VIEW"]


class RecordingTarget:
    def __init__(self, logins=("app_login",), master_key=False, jobs=(), servers=()):
        self.sql, self.logins, self.mk, self.jobs, self.servers = [], set(logins), master_key, set(jobs), set(servers)

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def query(self, sql, params=None):
        if "SUSER_ID" in sql:
            return [(1 if params[0] in self.logins else None,)]
        if "##MS_DatabaseMasterKey##" in sql:
            return [(1 if self.mk else 0,)]
        if "msdb.dbo.sysjobs" in sql:
            return [(1 if params[0] in self.jobs else 0,)]
        if "sys.servers" in sql:
            return [(1 if params[0] in self.servers else 0,)]
        raise AssertionError(sql)

    def commit(self):
        pass

    def rollback(self):
        pass


def full_extras():
    return DatabaseExtras(
        principals=[Principal("signer", "CERTIFICATE_USER", certificate="SignCert"),
                    Principal("app", "SQL_USER", login="app_login")],
        role_members=[("db_datareader", "app")],
        permissions=[Permission("GRANT", "EXECUTE", "app", "OBJECT", "dbo", "usp"),
                     Permission("GRANT", "SEND", "app", "SERVICE", None, "//svc")],
        certificates=[Certificate("SignCert", "app", "0x30", "0x1F", "pw")],
        signatures=[Signature("dbo", "usp", "SignCert", "0xAB")],
        security_policies=[SecurityPolicy("rls", "pol", True, True,
                                          [("FILTER", "([rls].[fn]([Rep]))", "dbo", "T", None)])],
        cdc_tables=[CdcTable("dbo", "T", "dbo_T", None, False, ["Id"])])


def test_apply_extras_certificates_signatures_and_rls(monkeypatch):
    monkeypatch.setenv("TARGET_DB_MASTER_KEY_PASSWORD", "Str0ng!")
    tgt, res = RecordingTarget(), RunResult(0)
    Migrator(cfg(), factory=None, progress=lambda _: None)._apply_extras(tgt, full_extras(), res)
    assert not res.ddl_errors, res.ddl_errors
    order = tgt.sql
    assert order[0] == "CREATE MASTER KEY ENCRYPTION BY PASSWORD = N'Str0ng!'"
    assert order[1].startswith("CREATE CERTIFICATE [SignCert] FROM BINARY = 0x30 WITH PRIVATE KEY")
    assert order.index("CREATE USER [signer] FROM CERTIFICATE [SignCert]") > 1
    assert "ALTER AUTHORIZATION ON CERTIFICATE::[SignCert] TO [app]" in order
    assert "ADD SIGNATURE TO [dbo].[usp] BY CERTIFICATE [SignCert] WITH SIGNATURE = 0xAB" in order
    assert order[-1].startswith("CREATE SECURITY POLICY [rls].[pol]") and "STATE = OFF" in order[-1]
    assert not any("ON SERVICE::" in x for x in order)      # broker permissions wait for the broker objects


def test_apply_extras_needs_master_key_password(monkeypatch):
    monkeypatch.delenv("TARGET_DB_MASTER_KEY_PASSWORD", raising=False)
    res = RunResult(0)
    Migrator(cfg(), factory=None, progress=lambda _: None)._apply_extras(RecordingTarget(), full_extras(), res)
    assert "TARGET_DB_MASTER_KEY_PASSWORD" in res.ddl_errors[0]["error"]
    tgt = RecordingTarget(master_key=True)
    Migrator(cfg(), factory=None, progress=lambda _: None)._apply_extras(tgt, full_extras(), RunResult(0))
    assert not any("MASTER KEY" in x for x in tgt.sql)


def test_after_settings_cdc_late_objects_permissions_and_jobs():
    tgt, res = RecordingTarget(jobs={"Exists"}), RunResult(0)
    src = FakeSrc([], jobs=[("Nightly", ["EXEC msdb.dbo.sp_add_job @job_name = N'Nightly'"], ["w"]),
                            ("Exists", ["never"], [])])
    t = tbl(change_tracking=False)
    late = [ModuleDef("QUEUE", "dbo", "Q", "CREATE QUEUE [dbo].[Q]"), ModuleDef("DDL TRIGGER", "", "d", "CREATE TRIGGER d",
                                                                                 disabled=True)]
    m = Migrator(cfg(copy_agent_jobs=True), factory=None, progress=lambda _: None)
    m._skipped_principals = set()
    m._after_settings(src, tgt, [t], late, full_extras(), res)
    sql = tgt.sql
    assert sql[0].startswith("ALTER TABLE [dbo].[T] ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF)")
    assert any("sp_cdc_enable_table" in x for x in sql)
    assert sql.index("CREATE QUEUE [dbo].[Q]") < sql.index("GRANT SEND ON SERVICE::[//svc] TO [app]")
    assert "DISABLE TRIGGER [d] ON DATABASE" in sql
    assert "EXEC msdb.dbo.sp_add_job @job_name = N'Nightly'" in sql and "never" not in sql
    assert "w" in res.warnings and any("DISABLED" in w for w in res.warnings)


def test_enable_policies_switches_on_after_validation():
    tgt = RecordingTarget()
    src = FakeSrc([], extras=full_extras())
    m = Migrator(cfg(), factory=lambda side, ac: src if side == "source" else tgt, progress=lambda _: None)
    tgt.close = lambda: None
    m._enable_policies([tbl()], [], RunResult(0))
    assert tgt.sql == ["ALTER SECURITY POLICY [rls].[pol] WITH (STATE = ON)"]


def test_scoped_extras_filters_policies_signatures_and_cdc():
    ex = full_extras()
    ex.security_policies[0].predicates.append(("FILTER", "x", "dbo", "Elsewhere", None))
    scoped = scoped_extras(ex, [tbl()], [ModuleDef("PROCEDURE", "dbo", "usp", "x")])
    assert [p[3] for p in scoped.security_policies[0].predicates] == ["T"]
    assert len(scoped.signatures) == 1 and len(scoped.cdc_tables) == 1
    none = scoped_extras(ex, [tbl("Other")], [])
    assert none.security_policies == [] and none.signatures == [] and none.cdc_tables == []


def test_schema_script_contains_every_phase_in_order():
    t = tbl(primary_key=Index("PK_T", [("Id", False)], unique=True, clustered=True, is_primary_key=True),
            temporal=Temporal("F", "T", "dbo", "T_History"), change_tracking=True)
    mods = [ModuleDef("ASSEMBLY", "", "Geo", "CREATE ASSEMBLY [Geo] FROM 0x00"),
            ModuleDef("PARTITION SCHEME", "", "ps", "", meta={"function": "pf", "filegroups": ["PRIMARY"]}),
            ModuleDef("VIEW", "dbo", "v", "CREATE VIEW dbo.v AS SELECT 1 AS x"),
            ModuleDef("PROCEDURE", "dbo", "usp", "CREATE PROCEDURE dbo.usp AS SELECT 1"),
            ModuleDef("QUEUE", "dbo", "Q", "CREATE QUEUE [dbo].[Q]")]
    ex = full_extras()
    ex.settings = DatabaseSettings(150, True, False, "x")
    src = FakeSrc([t, tbl("T_History")], modules=mods, extras=ex)
    script = Migrator(cfg(), factory=lambda s, a: src).schema_script()
    marks = ["CREATE ASSEMBLY", "CREATE PARTITION SCHEME", "CREATE TABLE [dbo].[T]", "-- ===== after data load",
             "CREATE VIEW", "sp_bindrule", "CREATE CERTIFICATE", "CREATE USER [signer]",
             "GRANT EXECUTE ON [dbo].[usp]", "ADD SIGNATURE",
             "CREATE SECURITY POLICY", "SYSTEM_VERSIONING = ON", "COMPATIBILITY_LEVEL = 150",
             "ENABLE CHANGE_TRACKING", "sp_cdc_enable_table", "CREATE QUEUE", "GRANT SEND ON SERVICE::[//svc]",
             "WITH (STATE = ON)"]
    pos = [script.index(x) for x in marks]
    assert pos == sorted(pos), list(zip(marks, pos))
    assert "IF SCHEMA_ID(N'audit')" in script
    # private keys stay out of a script on disk (and so does the master key they would need)
    assert "PRIVATE KEY (BINARY" not in script and "CREATE MASTER KEY" not in script
    assert "private key not written to this script" in script


# ================================================================== validation
def test_schema_diff_new_features():
    m = cfg().migration
    s = tbl(columns=[Column("Id", "int"), Column("F", "varchar", 10, is_sparse=True),
                     Column("E", "nvarchar", 100, masking_function="email()"),
                     Column("VF", "datetime2", 8, scale=7, generated_always="ROW START")],
            temporal=Temporal("VF", "VT", "dbo", "H"), memory_optimized=True,
            xml_indexes=[XmlIndex("PX", "Doc")], spatial_indexes=[SpatialIndex("SI", "Loc", "GEOGRAPHY_AUTO_GRID")],
            fulltext=FullTextIndex("c", "PK", [("Body", None, None, False)]), change_tracking=True,
            partition_scheme="ps", partition_column="Year")
    t = tbl(columns=[Column("Id", "int"), Column("F", "varchar", 10), Column("E", "nvarchar", 100),
                     Column("VF", "datetime2", 8, scale=7)])
    diffs = _schema_diff(s, t, m=m)
    for needle in ("column F: is_sparse True -> False", "column E: masking_function 'email()' -> None",
                   "column VF: generated_always 'ROW START' -> ''", "memory optimized True/SCHEMA_AND_DATA",
                   "system versioning not enabled", "heap partitioning ps(Year) -> None(None)",
                   "XML index PX missing", "spatial index SI missing", "full-text index missing",
                   "change tracking True -> None"):
        assert any(needle in d for d in diffs), (needle, diffs)
    quiet = _schema_diff(s, t, m=cfg(copy_data_masking=False, restore_system_versioning=False,
                                     memory_optimized="disk", copy_full_text=False,
                                     copy_change_tracking=False).migration)
    assert not any(x in " ".join(quiet) for x in ("masking", "memory", "versioning", "full-text", "change tracking"))


def test_extras_diff_new_parts():
    src = full_extras()
    src.settings = DatabaseSettings(150, False, False, "x", options={"ANSI_NULLS": "ON"},
                                    scoped_configurations={"MAXDOP": "4"}, change_tracking="x", query_store="READ_WRITE")
    tgt = DatabaseExtras(settings=DatabaseSettings(150, False, False, "x", options={"ANSI_NULLS": "OFF"},
                                                   scoped_configurations={"MAXDOP": "0"}),
                         principals=list(src.principals))
    diffs = extras_diff(cfg(), src, tgt)
    for needle in ("database option ANSI_NULLS ON -> OFF", "scoped configuration MAXDOP 4 -> 0",
                   "change tracking x -> None", "Query Store READ_WRITE -> None",
                   "security policy rls.pol missing", "certificate SignCert missing",
                   "signature on dbo.usp by SignCert missing", "change data capture instance dbo_T missing"):
        assert any(needle in d for d in diffs), (needle, diffs)
    assert extras_diff(cfg(), src, src) == []


def test_server_diffs():
    class Tgt:
        def query(self, sql, params=None):
            return [("Other",)] if "sysjobs" in sql else [("HQ",)]
    v = Validator(cfg(copy_agent_jobs=True, copy_linked_servers=True), factory=None)
    diffs = v._server_diffs(FakeSrc([], jobs=[("Nightly", [], [])], links=[("HQ", [], [])]), Tgt())
    assert diffs == ["SQL Agent job Nightly missing on target"]


# ================================================================== data path
def test_encrypted_columns_travel_as_ciphertext():
    from mssql_ocp_migrate._vendor.pytds import tds_types as T
    enc = Column("SSN", "char", 11, encryption_key="CEK", encryption_type="RANDOMIZED")
    assert isinstance(pytds_type(enc), T.VarBinaryType)
    t = Table("dbo", "P", columns=[Column("Id", "int", nullable=False), enc],
              primary_key=Index("PK", [("Id", False)], unique=True, clustered=True, is_primary_key=True))
    assert t.has_encrypted and t.key_columns == ["Id"]
    t2 = Table("dbo", "P", columns=[enc], primary_key=Index("PK", [("SSN", False)], is_primary_key=True))
    assert t2.key_columns == []                       # encrypted keys can't be ranged on the server
    s = SqlServer.__new__(SqlServer)
    s.driver = "pyodbc"
    with pytest.raises(RuntimeError, match="pytds"):
        s.bulk_load(t, [], False)
    seen = []
    s.conn = FakeConn([])
    s._version = 16
    s.query = lambda sql, params=None: seen.append(sql) or [(0, 0, 0)]
    s.checksum(t, __import__("mssql_ocp_migrate.model", fromlist=["Chunk"]).Chunk("dbo.P", 0))
    assert "SSN" not in seen[0] and "[Id]" in seen[0]


def test_memory_optimized_bulk_load_never_uses_tablock():
    s = SqlServer.__new__(SqlServer)
    s.driver = "pytds"
    calls = {}
    s._insert_bulk_pytds = lambda name, table, cols, batches, tablock: calls.setdefault("tablock", tablock) or 0
    t = Table("dbo", "M", columns=[Column("Id", "int", nullable=False)], memory_optimized=True)
    s.bulk_load(t, [], True)
    assert calls["tablock"] is False


def test_lift_copies_jobs_and_linked_servers():
    from mssql_ocp_migrate.physical import Lift

    class C:
        def __init__(self, answers=()):
            self.answers, self.sql = list(answers), []

        def query(self, sql, params=None):
            for k, rows in self.answers:
                if k in sql:
                    return rows
            return []

        def scalar(self, sql, params=None):
            return 1 if "COL_LENGTH" in sql else 0

        def execute(self, sql, params=None):
            self.sql.append(sql)

    src = C([("FROM msdb.dbo.sysjobs j", [("J1", "Nightly", "", "[Uncategorized (Local)]", None, 1, 0, 0)]),
             ("FROM sys.servers WHERE is_linked = 1", [(1, "HQ", "SQL Server", None, "hq", None, None, None,
                                                        0, 0, 1, 0, 1, None, 0, 0)])])
    tgt = C()
    lift = Lift.__new__(Lift)
    lift.lift = cfg().lift
    lift.lift.copy_agent_jobs = lift.lift.copy_linked_servers = True
    notes = lift.copy_server_objects(src, tgt, "Sales", "Sales")
    assert any("sp_add_job @job_name = N'Nightly', @enabled = 0" in x for x in tgt.sql)
    assert any("sp_addlinkedserver @server = N'HQ'" in x for x in tgt.sql)
    assert "created SQL Agent job Nightly (disabled: enable it at cut-over)" in notes
