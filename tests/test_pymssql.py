"""pymssql as an optional driver: parameter handling, routing rules, and a wire-level login
against the fake TDS server. Skipped when pymssql is not installed."""
import datetime as dt
import uuid

import pytest

from mssql_ocp_migrate.config import ConnectionConfig
from mssql_ocp_migrate.drivers import Connection, DriverError, choose_driver, connect, pymssql_value
from mssql_ocp_migrate.model import Chunk, Column, Index, Table
from mssql_ocp_migrate.sqlserver import SqlServer, _pymssql_coercers

pymssql = pytest.importorskip("pymssql")


def test_auto_prefers_pymssql_over_bundled_driver(monkeypatch):
    import mssql_ocp_migrate.drivers as d
    monkeypatch.setattr(d, "_pyodbc_usable", lambda cfg: False)
    assert choose_driver(ConnectionConfig(database="x")) == "pymssql"
    monkeypatch.setattr(d, "_pyodbc_usable", lambda cfg: True)
    assert choose_driver(ConnectionConfig(database="x")) == "pyodbc"
    assert choose_driver(ConnectionConfig(database="x", driver="pytds")) == "pytds"


def test_parameters_keep_microseconds_and_percent_signs():
    from pymssql import _mssql
    c = Connection(raw=None, driver="pymssql", cfg=None, autocommit=True)
    sql = c._sql("SELECT 1 WHERE (rn - 1) % ? = 0 AND t >= CAST(? AS datetime2(7))", [5, 0])
    params = c._params([5, dt.datetime(2024, 1, 2, 3, 4, 5, 123456)])
    out = _mssql.substitute_params(sql, params).decode()
    assert out == "SELECT 1 WHERE (rn - 1) % 5 = 0 AND t >= CAST(N'2024-01-02 03:04:05.123456' AS datetime2(7))"


def test_datetime_keys_are_cast_through_datetime2():
    t = Table("dbo", "T", columns=[Column("ts", "datetime", nullable=False)])
    t.primary_key = Index("PK", [("ts", False)], unique=True, is_primary_key=True)
    db = SqlServer.__new__(SqlServer)
    db.driver = "pymssql"
    where, _ = db._where(t, Chunk("dbo.T", 1, lower=(dt.datetime(2024, 1, 1, 0, 0, 0, 3333),)))
    assert where == "([ts] >= CAST(CAST(? AS datetime2(7)) AS datetime))"


def test_temporal_columns_travel_as_text_with_pymssql():
    db = SqlServer.__new__(SqlServer)
    db.driver = "pymssql"
    exprs = db.select_exprs(None, [Column("a", "datetime2", scale=3), Column("b", "time", scale=0),
                                   Column("c", "date")])
    assert exprs == ["CAST([a] AS nvarchar(40))", "CAST([b] AS nvarchar(40))", "[c]"]


def test_bulk_coercion():
    f = _pymssql_coercers([Column("g", "uniqueidentifier"), Column("t", "datetime2", scale=6), Column("n", "int")])
    u = uuid.uuid4()
    assert [fn(v) for fn, v in zip(f, (u, dt.datetime(2024, 5, 1, 1, 2, 3, 4), 7))] == \
        [str(u), "2024-05-01 01:02:03.000004", 7]
    assert pymssql_value(None) is None and pymssql_value(dt.date(2024, 1, 2)) == "2024-01-02"


def _unsafe_tables():
    with_default = Table("dbo", "A", columns=[Column("v", "int", nullable=True, default_definition="((0))")])
    with_xml = Table("dbo", "B", columns=[Column("x", "xml")])
    with_max = Table("dbo", "D", columns=[Column("n", "nvarchar", max_length=-1)])   # dropped the connection
    with_datetime = Table("dbo", "E", columns=[Column("d", "datetime")])
    with_utf8 = Table("dbo", "F", columns=[Column("u", "varchar", max_length=60, collation="Latin1_General_100_CI_AS_SC_UTF8")])
    return with_default, with_xml, with_max, with_datetime, with_utf8


def test_tables_that_bulk_copy_could_corrupt_use_insert(monkeypatch):
    db = SqlServer.__new__(SqlServer)
    db.driver = "pymssql"
    calls = []
    monkeypatch.setattr(db, "_load_rows_pytds", lambda *a: calls.append("insert") or 0, raising=False)
    monkeypatch.setattr(db, "_pymssql_bulk", lambda *a: calls.append("bulk") or 0, raising=False)
    for t in _unsafe_tables():
        db._load_pymssql(t, t.insert_columns, [], False, "insert_bulk", False)
    safe = Table("dbo", "C", columns=[Column("v", "int", nullable=False, default_definition="((0))"),
                                     Column("w", "nvarchar", max_length=20)])
    db._load_pymssql(safe, safe.insert_columns, [], False, "insert_bulk", False)
    assert calls == ["insert"] * 5 + ["bulk"]


def test_utf8_varchar_is_read_as_nvarchar_with_pymssql():
    db = SqlServer.__new__(SqlServer)
    db.driver = "pymssql"
    cols = [Column("u", "varchar", max_length=60, collation="Latin1_General_100_CI_AS_SC_UTF8"),
            Column("m", "varchar", max_length=-1, collation="Latin1_General_100_CI_AS_SC_UTF8"),
            Column("l", "varchar", max_length=60, collation="Latin1_General_CI_AS")]
    assert db.select_exprs(None, cols) == ["CAST([u] AS nvarchar(60))", "CAST([m] AS nvarchar(max))", "[l]"]
    db.driver = "pyodbc"
    assert db.select_exprs(None, cols)[:2] == ["CAST([u] AS nvarchar(60))", "CAST([m] AS nvarchar(max))"]
    db.driver = "pytds"
    assert db.select_exprs(None, cols) == ["[u]", "[m]", "[l]"]


def test_pymssql_login_reaches_server():
    """FreeTDS inside the pymssql wheel speaks to our fake TDS server: credentials arrive in LOGIN7."""
    from test_tls import ENCRYPT_NOT_SUP, LOGIN7, FakeTds
    srv = FakeTds(ENCRYPT_NOT_SUP)
    srv.start()
    with pytest.raises(DriverError):
        connect(ConnectionConfig(host="127.0.0.1", port=srv.port, database="db", user="alice", password="pw",
                                 driver="pymssql", encrypt="off", login_timeout=5))
    srv.join(10)
    assert srv.logins and srv.logins[0][0] == LOGIN7
    assert "alice".encode("utf-16-le") in srv.logins[0][1] or b"alice" in srv.logins[0][1]


def test_binary_parameters_are_sent_as_binary_literals():
    """pymssql 2.4 quotes bytes as '...' (varchar), which binary columns reject; bytearray is 0x..."""
    from pymssql import _mssql
    conn = Connection(None, "pymssql", ConnectionConfig(database="d"), autocommit=True)
    params = conn._params([b"\x01\xff", memoryview(b"\x02"), bytearray(b"\x03"), "text", 5, None])
    assert [_mssql.quote_simple_value(p) for p in params[:3]] == [b"0x01ff", b"0x02", b"0x03"]
    assert params[3:] == ("text", 5, None)
