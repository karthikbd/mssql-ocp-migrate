"""Every SQL Server column type maps to a bundled-driver type that can declare itself in
INSERT BULK and serialize a representative value (and NULL) into TDS bytes."""
import datetime as dt
import decimal
import uuid

import pytest

from mssql_ocp_migrate.model import Column
from mssql_ocp_migrate.sqlserver import SqlServer, pytds_type
from mssql_ocp_migrate._vendor.pytds import tds_base, tds_types
from mssql_ocp_migrate._vendor.pytds.collate import raw_collation
from mssql_ocp_migrate._vendor.pytds.tds_writer import _TdsWriter

UTC = dt.timezone.utc
CASES = [
    (Column("a", "bit"), True, "BIT"),
    (Column("a", "tinyint"), 200, "TINYINT"),
    (Column("a", "smallint"), -3, "SMALLINT"),
    (Column("a", "int"), 2 ** 31 - 1, "INT"),
    (Column("a", "bigint"), -2 ** 63, "BIGINT"),
    (Column("a", "real", precision=24), 1.5, "REAL"),
    (Column("a", "float", precision=53), 1e308, "FLOAT"),
    (Column("a", "money"), decimal.Decimal("922337203685477.5807"), "MONEY"),
    (Column("a", "smallmoney"), decimal.Decimal("-214748.3648"), "SMALLMONEY"),
    (Column("a", "decimal", precision=38, scale=10), decimal.Decimal("1234567890123456789012345678.0123456789"),
     "DECIMAL(38,10)"),
    (Column("a", "numeric", precision=5, scale=2), decimal.Decimal("-999.99"), "DECIMAL(5,2)"),
    (Column("a", "char", max_length=10), "abc", "CHAR(10)"),
    (Column("a", "varchar", max_length=50), "héllo", "VARCHAR(50)"),
    (Column("a", "varchar", max_length=-1), "x" * 100_000, "VARCHAR(MAX)"),
    (Column("a", "nchar", max_length=20), "漢字", "NCHAR(10)"),
    (Column("a", "nvarchar", max_length=400), "ü€😀", "NVARCHAR(200)"),
    (Column("a", "nvarchar", max_length=-1), "ü" * 50_000, "NVARCHAR(MAX)"),
    # binary(n) is declared BINARY(n) and written by our own BINARY serializer (see below)
    (Column("a", "binary", max_length=4), b"\x00\x01\x02\x03", "BINARY(4)"),
    # hierarchyid travels as its string form ('/1/3/'); varbinary metadata is rejected for it
    (Column("a", "hierarchyid", max_length=892), "/1/3/", "NVARCHAR(4000)"),
    (Column("a", "varbinary", max_length=16), b"\xff" * 16, "VARBINARY(16)"),
    (Column("a", "varbinary", max_length=-1), b"\x01" * 200_000, "VARBINARY(MAX)"),
    (Column("a", "uniqueidentifier"), uuid.uuid4(), "UNIQUEIDENTIFIER"),
    (Column("a", "date"), dt.date(1, 1, 1), "DATE"),
    (Column("a", "smalldatetime"), dt.datetime(2079, 6, 6, 23, 59), "SMALLDATETIME"),
    (Column("a", "datetime2", scale=3), dt.datetime(9999, 12, 31, 23, 59, 59, 999000), "DATETIME2(3)"),
    (Column("a", "time", scale=4), dt.time(23, 59, 59, 999900), "TIME(4)"),
    (Column("a", "datetimeoffset", scale=2), dt.datetime(2024, 5, 1, 10, 0, 0, 120000, UTC), "DATETIMEOFFSET(2)"),
    # these travel as ISO text and SQL Server converts them back exactly (100 ns / 1/300 s ticks)
    (Column("a", "datetime2", scale=7), "2024-05-01 10:00:00.1234567", "NVARCHAR(40)"),
    (Column("a", "datetimeoffset", scale=7), "2024-05-01 10:00:00.1234567 +05:30", "NVARCHAR(40)"),
    (Column("a", "time", scale=7), "10:00:00.1234567", "NVARCHAR(40)"),
    (Column("a", "datetime"), "2024-05-01T10:00:00.003", "NVARCHAR(40)"),
    (Column("a", "geography"), b"\xe6\x10\x00\x00\x01\x0c" + b"\x00" * 16, "VARBINARY(MAX)"),
    # INSERT BULK rejects XML metadata; nvarchar(max) is parsed into the (typed) xml column
    (Column("a", "xml"), "<a b='1'/>", "NVARCHAR(MAX)"),
]


class _Sink:
    def __init__(self):
        self.data = bytearray()

    def sendall(self, b, flags=0):
        self.data += bytes(b)


class _Login:
    bytes_to_unicode = True


class _Conn:
    _login = _Login()


class _Session:
    tds_version = tds_base.TDS74
    _tds = _Conn()


@pytest.mark.parametrize("col,value,decl", CASES, ids=[f"{c.type_name}{c.max_length or c.scale or ''}"
                                                       for c, _, _ in CASES])
def test_type_declares_and_serializes(col, value, decl):
    typ = pytds_type(col)
    assert typ.get_declaration().upper().replace(" ", "") == decl
    factory = tds_types.SerializerFactory(tds_base.TDS74)
    meta = SqlServer._bulk_metadata(None, "t", None, [col])[0]
    ser = meta.choose_serializer(type_factory=factory, collation=raw_collation)
    sink = _Sink()
    w = _TdsWriter(sink, 4096, _Session())
    w.begin_packet(tds_base.PacketType.BULK)
    ser.write_info(w)
    ser.write(w, value)
    ser.write(w, None)
    w.flush()
    assert len(sink.data) > 8


def test_binary_metadata_is_binary_not_varbinary():
    meta = SqlServer._bulk_metadata(None, "t", None, [Column("h", "binary", max_length=16, nullable=False)])[0]
    ser = meta.choose_serializer(type_factory=tds_types.SerializerFactory(tds_base.TDS74), collation=raw_collation)
    assert ser.type == tds_base.XSYBBINARY == 0xAD and ser.size == 16 and meta.flags == 0


def test_column_set_replaces_sparse_columns_in_the_load():
    from mssql_ocp_migrate.model import Table
    t = Table("dbo", "C", columns=[Column("Id", "int"), Column("Fax", "varchar", 20, is_sparse=True),
                                   Column("All", "xml", -1, is_column_set=True), Column("Rv", "timestamp")])
    assert [c.name for c in t.insert_columns] == ["Id", "All"]
    assert [c.name for c in t.compare_columns] == ["Id", "Fax"]
    plain = Table("dbo", "P", columns=[Column("Id", "int"), Column("Fax", "varchar", 20, is_sparse=True)])
    assert [c.name for c in plain.insert_columns] == ["Id", "Fax"]


def test_size_capped_clr_types_use_row_inserts():
    from mssql_ocp_migrate.model import Table
    from mssql_ocp_migrate.sqlserver import _fixed_size_clr
    udt = Column("P", "Point", 16, clr_udt=True, user_type="Point", user_type_schema="geo")
    assert _fixed_size_clr(udt) and not _fixed_size_clr(Column("g", "geography", -1))
    db = SqlServer.__new__(SqlServer)
    db.driver, calls = "pytds", []
    db._load_rows_pytds = lambda t, cols, b, ident: calls.append("rows") or 0
    db._insert_bulk_pytds = lambda *a: calls.append("bulk") or 0
    db.bulk_load(Table("geo", "T", columns=[Column("Id", "int"), udt]), [], False)
    db.bulk_load(Table("geo", "U", columns=[Column("Id", "int"), Column("g", "geography", -1)]), [], False)
    assert calls == ["rows", "bulk"]


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        pytds_type(Column("a", "cursor"))


def test_select_expressions_keep_full_precision():
    db = SqlServer.__new__(SqlServer)
    db.driver = "pytds"
    cols = [Column("d2", "datetime2", scale=7), Column("d", "datetime"), Column("g", "geometry"),
            Column("t3", "time", scale=3), Column("n", "nvarchar", max_length=20),
            Column("h", "hierarchyid", max_length=892)]
    exprs = SqlServer.select_exprs(db, None, cols)
    assert exprs == ["CAST([d2] AS nvarchar(40))", "CONVERT(nvarchar(30), [d], 126)",
                     "CAST([g] AS varbinary(max))", "[t3]", "[n]", "CAST([h] AS nvarchar(4000))"]
