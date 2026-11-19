"""pyodbc load path: typed placeholders (fast_executemany sizes buffers from the parameter type) and
IDENTITY_INSERT switched off even when a batch fails. No server or pyodbc needed."""
import pytest

from mssql_ocp_migrate.model import Column, Table
from mssql_ocp_migrate.sqlserver import SqlServer, _pyodbc_placeholder


def test_placeholders_carry_the_transport_type():
    cols = [Column("h", "hierarchyid"), Column("g", "geography"), Column("t", "text"), Column("n", "ntext"),
            Column("i", "image"), Column("d", "datetime"), Column("j", "json"), Column("v", "int"),
            Column("s", "nvarchar", max_length=-1)]
    assert [_pyodbc_placeholder(c) for c in cols] == [
        "CAST(? AS nvarchar(4000))", "CAST(? AS varbinary(max))", "CAST(? AS varchar(max))",
        "CAST(? AS nvarchar(max))", "CAST(? AS varbinary(max))", "CAST(? AS nvarchar(30))",
        "CAST(? AS nvarchar(max))", "?", "?"]


class _Cursor:
    def __init__(self, log, fail):
        self.log, self.fail, self.fast_executemany = log, fail, False

    def execute(self, sql):
        self.log.append(sql)

    def executemany(self, sql, rows):
        self.log.append(sql)
        if self.fail:
            raise MemoryError

    def close(self):
        self.log.append("close")


class _Raw:
    def __init__(self, log, fail):
        self.log, self.fail = log, fail

    def cursor(self):
        return _Cursor(self.log, self.fail)


class _Conn:
    def __init__(self, log, fail):
        self.raw = _Raw(log, fail)


@pytest.mark.parametrize("fail", [False, True])
def test_identity_insert_is_switched_off_even_after_a_failure(fail):
    log = []
    db = SqlServer.__new__(SqlServer)
    db.driver = "pyodbc"
    db.conn = _Conn(log, fail)
    t = Table("dbo", "L", columns=[Column("id", "int", is_identity=True), Column("t", "text")])
    if fail:
        with pytest.raises(MemoryError):
            db._load_pyodbc(t, t.columns, [[(1, "x")]], identity=True)
    else:
        assert db._load_pyodbc(t, t.columns, [[(1, "x")]], identity=True) == 1
    assert log[0] == "SET IDENTITY_INSERT [dbo].[L] ON"
    assert log[1] == "INSERT INTO [dbo].[L] ([id], [t]) VALUES (?, CAST(? AS varchar(max)))"
    assert log[-2:] == ["SET IDENTITY_INSERT [dbo].[L] OFF", "close"]
