"""SQL Server implementation of the Database interface (catalog, chunking, bulk load, checksums)."""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import chunking
from .adapter import Database
from .catalog import CatalogMixin
from .config import ConnectionConfig
from .ddl import q
from .drivers import Connection, connect
from .model import Chunk, Column, LOB_LEGACY_TYPES, Table

log = logging.getLogger(__name__)

# Identity strategies, chosen once per target by a runtime probe (see _identity_strategy)
ID_SET_INSERT_BULK = "set_identity_insert+insert_bulk"
ID_PLAIN_BULK = "insert_bulk"
ID_ROW_INSERT = "set_identity_insert+insert_values"
_probe_lock = threading.Lock()
_probe_cache: Dict[str, str] = {}


class SqlServer(CatalogMixin, Database):
    def __init__(self, cfg: ConnectionConfig, label: str, autocommit: bool = True):
        self.cfg = cfg
        self.label = label
        self.conn: Connection = connect(cfg, autocommit=autocommit)
        self.driver = self.conn.driver
        self._version: Optional[int] = None

    # ------------------------------------------------------------------ basics
    def quote(self, name: str) -> str:
        return q(name)

    def describe(self) -> str:
        return f"{self.label} {self.cfg.describe()} via {self.driver}"

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        self.conn.execute(sql, params)

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[tuple]:
        return self.conn.query(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    @property
    def major_version(self) -> int:
        if self._version is None:
            v = self.conn.scalar("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32))")
            self._version = int(str(v).split(".")[0])
        return self._version

    def server_info(self) -> Dict[str, Any]:
        row = self.query(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)), "
            "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), "
            "CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128)), "
            "CAST(SERVERPROPERTY('EngineEdition') AS int), DB_NAME()")[0]
        return {"version": row[0], "edition": row[1], "collation": row[2],
                "engine_edition": row[3], "database": row[4]}

    def _has_col(self, view: str, col: str) -> bool:
        return self.conn.scalar("SELECT COL_LENGTH(?, ?)", [view, col]) is not None

    # ---------------------------------------------------------------- catalog
    def table_exists(self, table: Table) -> bool:
        return self.conn.scalar("SELECT OBJECT_ID(?, 'U')", [f"{q(table.schema)}.{q(table.name)}"]) is not None

    # ----------------------------------------------------------- transport
    def select_exprs(self, table: Table, columns: Sequence[Column]) -> List[str]:
        """Expressions that move each column losslessly through Python.

        * CLR types travel as varbinary (server converts back on insert)
        * datetime2/time/datetimeoffset(7) and datetime travel as ISO strings: Python's
          datetime stops at microseconds, these types go further (100 ns / 1/300 s ticks).
        """
        out = []
        for c in columns:
            t = c.type_name.lower()
            qc = q(c.name)
            if c.is_clr:
                out.append(f"CAST({qc} AS varbinary(max))")
            elif t in ("datetime2", "datetimeoffset", "time") and (c.scale > 6 or self.driver == "pymssql"):
                # default conversion is ISO (yyyy-mm-dd hh:mi:ss.fffffff [+hh:mm]), DATEFORMAT-independent
                out.append(f"CAST({qc} AS nvarchar(40))")
            elif t == "datetime":
                out.append(f"CONVERT(nvarchar(30), {qc}, 126)")
            else:
                out.append(qc)
        return out

    # ------------------------------------------------------------------ data
    def boundaries(self, table: Table, chunk_rows: int) -> List[Tuple[Any, ...]]:
        keys = table.key_columns
        if not keys or table.row_count <= chunk_rows:
            return []
        kl = ", ".join(q(k) for k in keys)
        rows = self.query(
            f"SELECT {kl} FROM (SELECT {kl}, ROW_NUMBER() OVER (ORDER BY {kl}) AS rn__ "
            f"FROM {self.qualified(table)}{'' if table.memory_optimized else ' WITH (NOLOCK)'}) x "
            f"WHERE rn__ > 1 AND (rn__ - 1) % ? = 0 ORDER BY {kl}", [int(chunk_rows)])
        return [tuple(r) for r in rows]

    def _where(self, table: Table, chunk: Chunk) -> Tuple[str, List[Any]]:
        def ph(col: str) -> str:
            # Type the parameter exactly like the key column: a str parameter is nvarchar, and
            # comparing it to a varchar key would convert the column and turn seeks into scans.
            c = table.column(col)
            if c.is_clr or c.max_length == -1 or c.type_name.lower() in ("xml", "text", "ntext", "image",
                                                                          "sql_variant"):
                return "?"
            from .ddl import type_sql
            if self.driver == "pymssql" and c.type_name.lower() in ("datetime", "smalldatetime"):
                # pymssql sends datetimes as text with 6 decimals, which datetime cannot parse directly
                return f"CAST(CAST(? AS datetime2(7)) AS {type_sql(c)})"
            return f"CAST(? AS {type_sql(c)})"
        return chunking.predicate(chunk, table.key_columns, q, ph)

    def prepare(self, table: Table) -> None:
        """Run once per worker before its first chunk of a table (outside the chunk transaction)."""
        if table.has_identity and self.driver == "pytds":
            self._identity_strategy()

    def read_rows(self, table: Table, chunk: Chunk, batch: int,
                  ordered: bool = True, columns: Optional[Sequence[Column]] = None) -> Iterator[List[tuple]]:
        cols = list(columns) if columns is not None else table.insert_columns
        where, params = self._where(table, chunk)
        order = ""
        if ordered and table.key_columns:
            order = " ORDER BY " + ", ".join(q(k) for k in table.key_columns)
        sql = (f"SELECT {', '.join(self.select_exprs(table, cols))} FROM {self.qualified(table)} "
               f"WHERE {where}{order}")
        return self.conn.stream(sql, params, batch)

    def count_rows(self, table: Table, chunk: Optional[Chunk] = None) -> int:
        if chunk is None:
            return int(self.conn.scalar(f"SELECT COUNT_BIG(*) FROM {self.qualified(table)}"))
        where, params = self._where(table, chunk)
        return int(self.conn.scalar(f"SELECT COUNT_BIG(*) FROM {self.qualified(table)} WHERE {where}", params))

    def _checksum_expr(self, c: Column) -> str:
        qc = q(c.name)
        t = c.type_name.lower()
        big = c.max_length == -1 or t in LOB_LEGACY_TYPES or t == "xml" or c.is_clr
        if not big:
            return qc
        if t in ("ntext", "xml"):
            inner = f"CAST(CAST({qc} AS nvarchar(max)) AS varbinary(max))"
        elif t == "text":
            inner = f"CAST(CAST({qc} AS varchar(max)) AS varbinary(max))"
        else:
            inner = f"CAST({qc} AS varbinary(max))"
        if self.major_version >= 13:              # HASHBYTES has no 8000-byte limit from 2016
            return f"HASHBYTES('SHA2_256', {inner})"
        return f"DATALENGTH({qc})"

    def checksum(self, table: Table, chunk: Chunk) -> Tuple[int, Any]:
        # Always Encrypted columns can't be used in expressions on the server; validation compares
        # their ciphertext row by row instead (see validate._check_chunk)
        cols = [c for c in table.compare_columns if not c.encrypted]
        exprs = ", ".join(self._checksum_expr(c) for c in cols)
        where, params = self._where(table, chunk)
        row = self.query(
            f"SELECT COUNT_BIG(*), SUM(CAST(BINARY_CHECKSUM({exprs}) AS bigint)), "
            f"CHECKSUM_AGG(BINARY_CHECKSUM({exprs})) FROM {self.qualified(table)} WHERE {where}", params)[0]
        return int(row[0]), (int(row[1] or 0), int(row[2] or 0))

    def random_key(self, table: Table, rnd) -> Optional[Tuple[Any, ...]]:
        """A random existing key, cheaply (TABLESAMPLE reads a few pages, not the table)."""
        kl = ", ".join(q(k) for k in table.key_columns)
        pct = min(100.0, max(0.0001, 100.0 * 2000 / max(table.row_count, 1)))
        rows = self.query(f"SELECT TOP (1000) {kl} FROM {self.qualified(table)} TABLESAMPLE SYSTEM ({pct:.6f} PERCENT)")
        if not rows:
            rows = self.query(f"SELECT TOP (1) {kl} FROM {self.qualified(table)} ORDER BY {kl}")
        return tuple(rnd.choice(rows)) if rows else None

    def checksum_window(self, table: Table, start: Tuple[Any, ...], rows: int) -> Tuple[int, int, int]:
        """Checksum of the first ``rows`` rows at or after ``start`` in key order (server-side)."""
        cols = [c for c in table.compare_columns if not c.encrypted]
        where, params = self._where(table, Chunk(table.fqn, 0, lower=tuple(start)))
        kl = ", ".join(q(k) for k in table.key_columns)
        exprs = ", ".join(self._checksum_expr(c) for c in cols)
        sql = (f"SELECT COUNT_BIG(*), SUM(CAST(BINARY_CHECKSUM({exprs}) AS bigint)), "
               f"CHECKSUM_AGG(BINARY_CHECKSUM({exprs})) FROM (SELECT TOP ({int(rows)}) "
               f"{', '.join(q(c.name) for c in cols)} FROM {self.qualified(table)} WHERE {where} "
               f"ORDER BY {kl}) w")
        r = self.query(sql, params)[0]
        return int(r[0]), int(r[1] or 0), int(r[2] or 0)

    def delete_range(self, table: Table, chunk: Chunk) -> None:
        where, params = self._where(table, chunk)
        self.execute(f"DELETE FROM {self.qualified(table)} WHERE {where}", params)

    def truncate(self, table: Table) -> None:
        try:
            self.execute(f"TRUNCATE TABLE {self.qualified(table)}")
        except Exception:      # referenced by a FK -> fall back to DELETE
            self.rollback()
            self.execute(f"DELETE FROM {self.qualified(table)}")

    # -------------------------------------------------------------- identity
    def identity_current(self, table: Table) -> Optional[int]:
        if not table.has_identity:
            return None
        v = self.conn.scalar("SELECT CAST(IDENT_CURRENT(?) AS decimal(38,0))",
                             [f"{q(table.schema)}.{q(table.name)}"])
        return int(v) if v is not None else None

    def reseed_identity(self, table: Table, value: int) -> None:
        self.execute(f"DBCC CHECKIDENT ('{self.qualified(table).replace(chr(39), chr(39) * 2)}', RESEED, {int(value)})"
                     " WITH NO_INFOMSGS")

    # ------------------------------------------------------------- bulk load
    def bulk_load(self, table: Table, batches: Iterable[Sequence[tuple]], tablock: bool) -> int:
        cols = table.insert_columns
        tablock = tablock and not table.memory_optimized       # TABLOCK isn't allowed on memory-optimized tables
        if table.has_encrypted and self.driver != "pytds":
            raise RuntimeError(f"{table.fqn} has Always Encrypted columns: their ciphertext is copied with TDS "
                               "bulk insert, which needs the bundled driver on the target (driver = \"pytds\")")
        identity = table.has_identity
        strategy = self._identity_strategy() if identity else ID_PLAIN_BULK
        if self.driver == "pyodbc":
            return self._load_pyodbc(table, cols, batches, identity)
        if self.driver == "pymssql":
            return self._load_pymssql(table, cols, batches, identity, strategy, tablock)
        if strategy == ID_ROW_INSERT:
            return self._load_rows_pytds(table, cols, batches, identity)
        if strategy == ID_SET_INSERT_BULK:
            self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} ON")
        try:
            return self._insert_bulk_pytds(self.qualified(table), table, cols, batches, tablock)
        finally:
            if strategy == ID_SET_INSERT_BULK:
                self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} OFF")

    def _bulk_metadata(self, target_name: str, table: Optional[Table], cols: Sequence[Column]):
        """Column metadata for INSERT BULK, built from catalog metadata (no extra round trip)."""
        from ._vendor.pytds import tds_base
        out = []
        for c in cols:
            flags = tds_base.Column.fNullable if c.nullable else 0
            if c.type_name.lower() == "binary" and not c.encrypted:
                out.append(_BinaryColumn(c.max_length, name=c.name, flags=flags))
            else:
                out.append(tds_base.Column(name=c.name, type=pytds_type(c), flags=flags))
        return out

    def _insert_bulk_pytds(self, target_name: str, table: Optional[Table], cols: Sequence[Column],
                           batches: Iterable[Sequence[tuple]], tablock: bool) -> int:
        from ._vendor.pytds import tds_base
        meta = self._bulk_metadata(target_name, table, cols)
        coerce = _coercers(cols)
        counter = [0]

        def rows():
            for batch in batches:
                for r in batch:
                    counter[0] += 1
                    yield [f(v) for f, v in zip(coerce, r)] if coerce else r

        col_defs = ", ".join(f"{tds_base.tds_quote_id(m.column_name)} {m.type.get_declaration()}" for m in meta)
        opts = ["KEEP_NULLS"]          # NULL stays NULL instead of picking up column defaults
        if tablock:
            opts.append("TABLOCK")
        pk = table.primary_key if table is not None else None
        if pk and pk.clustered and not any(d for _, d in pk.columns):
            # rows are read ORDER BY the key, so tell the server it can skip the sort
            opts.append("ORDER(" + ", ".join(f"{q(c)} ASC" for c, _ in pk.columns) + ")")
        cur = self.conn.raw.cursor()
        try:
            cur.execute(f"INSERT BULK {target_name} ({col_defs}) WITH ({', '.join(opts)})")
            cur._session.submit_bulk(meta, rows())
            cur._session.process_simple_request()
        finally:
            cur.close()
        return counter[0]

    def _load_rows_pytds(self, table: Table, cols: Sequence[Column],
                         batches: Iterable[Sequence[tuple]], identity: bool) -> int:
        """Fallback: parameterised multi-row INSERT (always correct, slower)."""
        per_stmt = max(1, min(1000, 2000 // max(1, len(cols))))
        collist = ", ".join(q(c.name) for c in cols)
        n = 0
        coerce = _coercers(cols)
        if identity:
            self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} ON")
        try:
            for batch in batches:
                for i in range(0, len(batch), per_stmt):
                    part = batch[i:i + per_stmt]
                    ph = "(" + ", ".join("?" for _ in cols) + ")"
                    params: List[Any] = []
                    for r in part:
                        params.extend(f(v) for f, v in zip(coerce, r)) if coerce else params.extend(r)
                    self.execute(f"INSERT INTO {self.qualified(table)} ({collist}) VALUES "
                                 + ", ".join(ph for _ in part), params)
                    n += len(part)
        finally:
            if identity:
                self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} OFF")
        return n

    def _load_pyodbc(self, table: Table, cols: Sequence[Column],
                     batches: Iterable[Sequence[tuple]], identity: bool,
                     target_name: Optional[str] = None) -> int:
        collist = ", ".join(q(c.name) for c in cols)
        ph = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {target_name or self.qualified(table)} ({collist}) VALUES ({ph})"
        cur = self.conn.raw.cursor()
        cur.fast_executemany = True
        n = 0
        try:
            if identity:
                cur.execute(f"SET IDENTITY_INSERT {self.qualified(table)} ON")
            for batch in batches:
                if batch:
                    cur.executemany(sql, batch)
                    n += len(batch)
            if identity:
                cur.execute(f"SET IDENTITY_INSERT {self.qualified(table)} OFF")
        finally:
            cur.close()
        return n

    # ---------------------------------------------------------------- pymssql
    def _pymssql_bulk(self, target_name: str, column_ids: Sequence[int], cols: Sequence[Column],
                      batches: Iterable[Sequence[tuple]], tablock: bool) -> int:
        """FreeTDS bulk copy (INSERT BULK) through pymssql, one call per batch."""
        coerce = _pymssql_coercers(cols)
        n = 0
        for batch in batches:
            if not batch:
                continue
            rows = [tuple(f(v) for f, v in zip(coerce, r)) for r in batch]
            self.conn.raw.bulk_copy(target_name, rows, column_ids=list(column_ids), batch_size=len(rows),
                                    tablock=tablock)
            n += len(rows)
        return n

    def _load_pymssql(self, table: Table, cols: Sequence[Column], batches: Iterable[Sequence[tuple]],
                      identity: bool, strategy: str, tablock: bool) -> int:
        """Bulk copy when it is provably safe, else multi-row INSERT.

        FreeTDS bulk copy has no KEEP_NULLS option, so a NULL sent to a column with a DEFAULT would
        be replaced by the default: such tables, and CLR/xml/sql_variant columns, use INSERT."""
        unsafe = (any(c.nullable and c.default_definition for c in cols)
                  or any(c.is_clr or c.type_name.lower() in ("xml", "sql_variant", "datetimeoffset")
                         for c in cols)
                  or any(c.is_column_set for c in table.columns)        # bcp ordinals skip sparse columns
                  or table.fqn in _PYMSSQL_NO_BULK or (identity and strategy == ID_ROW_INSERT))
        if unsafe:
            return self._load_rows_pytds(table, cols, batches, identity)
        column_ids = [i + 1 for i, c in enumerate(table.columns) if c.insertable]
        if identity:
            self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} ON")
        try:
            return self._pymssql_bulk(self.qualified(table), column_ids, cols, batches, tablock)
        except Exception:
            _PYMSSQL_NO_BULK.add(table.fqn)        # the chunk is retried with INSERT on a new connection
            raise
        finally:
            if identity:
                try:
                    self.execute(f"SET IDENTITY_INSERT {self.qualified(table)} OFF")
                except Exception:
                    pass

    def _identity_strategy(self) -> str:
        """Find, once per target server, how INSERT BULK keeps explicit identity values.

        A tiny #temp table with IDENTITY is bulk-loaded with value 42 using each candidate
        strategy; the first one that stores 42 wins. Falls back to row INSERTs, which are
        always correct. The result is logged and written to the report.
        """
        if self.driver == "pyodbc":
            return ID_ROW_INSERT
        key = f"{self.driver}:{self.cfg.host}:{self.cfg.port}/{self.cfg.database}"
        with _probe_lock:
            if key in _probe_cache:
                return _probe_cache[key]
            chosen = ID_ROW_INSERT
            probe_col = Column(name="id", type_name="int", nullable=False, is_identity=True)
            val_col = Column(name="v", type_name="int", nullable=True)
            for strategy in (ID_SET_INSERT_BULK, ID_PLAIN_BULK):
                try:
                    self.execute("IF OBJECT_ID('tempdb..#mom_probe') IS NOT NULL DROP TABLE #mom_probe")
                    self.execute("CREATE TABLE #mom_probe (id int IDENTITY(1,1) NOT NULL, v int NULL)")
                    if strategy == ID_SET_INSERT_BULK:
                        self.execute("SET IDENTITY_INSERT #mom_probe ON")
                    if self.driver == "pymssql":
                        self._pymssql_bulk("#mom_probe", [1, 2], [probe_col, val_col], [[(42, 7)]], False)
                    else:
                        self._insert_bulk_pytds("#mom_probe", None, [probe_col, val_col], [[(42, 7)]], False)
                    if strategy == ID_SET_INSERT_BULK:
                        self.execute("SET IDENTITY_INSERT #mom_probe OFF")
                    got = self.conn.scalar("SELECT id FROM #mom_probe WHERE v = 7")
                    self.execute("DROP TABLE #mom_probe")
                    self.commit()
                    if got == 42:
                        chosen = strategy
                        break
                except Exception as exc:  # strategy not supported by this server
                    log.debug("identity strategy %s failed: %s", strategy, exc)
                    self.rollback()
            log.info("identity load strategy for %s: %s", key, chosen)
            _probe_cache[key] = chosen
            return chosen


def pytds_type(c: Column):
    """Map catalog metadata to the bundled driver's SQL type objects (used for INSERT BULK)."""
    from ._vendor.pytds import tds_types as T
    t = c.type_name.lower()
    if c.encrypted:                 # ciphertext travels as varbinary (ALLOW_ENCRYPTED_VALUE_MODIFICATIONS)
        return T.VarBinaryMaxType() if c.max_length == -1 else T.VarBinaryType(size=8000)
    if c.is_clr:            # hierarchyid and size-capped CLR types must not be sent as (max)
        return T.VarBinaryType(size=c.max_length) if 0 < c.max_length <= 8000 else T.VarBinaryMaxType()
    if (t in ("datetime2", "datetimeoffset", "time") and c.scale > 6) or t == "datetime":
        return T.NVarCharType(size=40)          # travels as ISO text, see select_exprs()
    simple = {"bit": T.BitType, "tinyint": T.TinyIntType, "smallint": T.SmallIntType, "int": T.IntType,
              "bigint": T.BigIntType, "real": T.RealType, "money": T.MoneyType,
              "smallmoney": T.SmallMoneyType, "text": T.TextType, "ntext": T.NTextType,
              "image": T.ImageType, "xml": T.XmlType, "uniqueidentifier": T.UniqueIdentifierType,
              "date": T.DateType, "smalldatetime": T.SmallDateTimeType, "sql_variant": T.VariantType}
    if t in simple:
        return simple[t]()
    if t == "float":
        return T.RealType() if 0 < c.precision <= 24 else T.FloatType()
    if t in ("decimal", "numeric"):
        return T.DecimalType(precision=c.precision, scale=c.scale)
    if t == "char":
        return T.CharType(size=c.max_length)
    if t == "binary":      # INSERT BULK uses _BinaryColumn; this type is for the declaration only
        return T.BinaryType(size=c.max_length)
    if t == "nchar":
        return T.NCharType(size=c.max_length // 2)
    if t == "varchar":
        return T.VarCharMaxType() if c.max_length == -1 else T.VarCharType(size=c.max_length)
    if t == "nvarchar":
        return T.NVarCharMaxType() if c.max_length == -1 else T.NVarCharType(size=c.max_length // 2)
    if t == "varbinary":
        return T.VarBinaryMaxType() if c.max_length == -1 else T.VarBinaryType(size=c.max_length)
    if t == "time":
        return T.TimeType(precision=c.scale)
    if t == "datetime2":
        return T.DateTime2Type(precision=c.scale)
    if t == "datetimeoffset":
        return T.DateTimeOffsetType(precision=c.scale)
    raise ValueError(f"unsupported column type {c.type_name} ({c.name})")


def _binary_serializer_class():
    from ._vendor.pytds import tds_base, tds_types

    class BinarySerializer(tds_types.VarBinarySerializer):
        """BINARY(n) for INSERT BULK: the bundled driver only ships a VARBINARY serializer,
        and SQL Server rejects VARBINARY metadata for a BINARY column ("Invalid column type")."""
        type = tds_base.XSYBBINARY

        def __repr__(self):
            return f"Binary({self.size})"

    return BinarySerializer


def _BinaryColumn(size: int, **kw):
    from ._vendor.pytds import tds_base, tds_types
    ser = _binary_serializer_class()

    class BinaryColumn(tds_base.Column):
        def choose_serializer(self, type_factory, collation):
            return ser(size)

    return BinaryColumn(type=tds_types.BinaryType(size=size), **kw)


_PYMSSQL_NO_BULK: set = set()


def _pymssql_coercers(cols: Sequence[Column]):
    """Values FreeTDS bulk copy converts reliably: GUIDs and temporal values as text."""
    from .drivers import pymssql_value
    out = []
    for c in cols:
        if c.type_name.lower() == "uniqueidentifier":
            out.append(lambda v: None if v is None else str(v))
        else:
            out.append(pymssql_value)
    return out


def _coercers(cols: Sequence[Column]):
    """Per-column value fixes when source and target drivers differ (e.g. GUID as str vs UUID)."""
    fns = []
    needed = False
    for c in cols:
        if c.type_name.lower() == "uniqueidentifier":
            fns.append(lambda v: uuid.UUID(v) if isinstance(v, str) else v)
            needed = True
        else:
            fns.append(lambda v: v)
    return fns if needed else None


def identity_strategy_cache() -> Dict[str, str]:
    return dict(_probe_cache)
