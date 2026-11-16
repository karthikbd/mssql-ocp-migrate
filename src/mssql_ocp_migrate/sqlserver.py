"""SQL Server implementation of the Database interface (catalog, chunking, bulk load, checksums)."""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import chunking
from .adapter import Database, ModuleDef
from .config import ConnectionConfig
from .ddl import q
from .drivers import Connection, connect
from .model import (CheckConstraint, Chunk, Column, ForeignKey, Index, LOB_LEGACY_TYPES, Table)

log = logging.getLogger(__name__)

# Identity strategies, chosen once per target by a runtime probe (see _identity_strategy)
ID_SET_INSERT_BULK = "set_identity_insert+insert_bulk"
ID_PLAIN_BULK = "insert_bulk"
ID_ROW_INSERT = "set_identity_insert+insert_values"
_probe_lock = threading.Lock()
_probe_cache: Dict[str, str] = {}


class SqlServer(Database):
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
    def list_schemas(self) -> List[str]:
        rows = self.query(
            "SELECT s.name FROM sys.schemas s WHERE s.schema_id > 4 AND s.schema_id < 16384 "
            "AND s.name NOT IN ('guest','INFORMATION_SCHEMA','sys') ORDER BY s.name")
        return [r[0] for r in rows]

    def list_tables(self) -> List[Table]:
        extra = []
        if self._has_col("sys.tables", "temporal_type"):
            extra.append("t.temporal_type")
        else:
            extra.append("0")
        extra.append("t.is_memory_optimized" if self._has_col("sys.tables", "is_memory_optimized") else "0")
        extra.append("t.is_filetable" if self._has_col("sys.tables", "is_filetable") else "0")
        rows = self.query(f"""
            SELECT t.object_id, s.name, t.name,
                   ISNULL((SELECT SUM(p.rows) FROM sys.partitions p
                           WHERE p.object_id = t.object_id AND p.index_id IN (0,1)), 0),
                   ISNULL((SELECT SUM(a.total_pages) * 8 / 1024.0 FROM sys.partitions p
                           JOIN sys.allocation_units a ON a.container_id = p.partition_id
                           WHERE p.object_id = t.object_id), 0),
                   {', '.join(extra)},
                   (SELECT COUNT(DISTINCT p.partition_number) FROM sys.partitions p
                    WHERE p.object_id = t.object_id)
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.is_ms_shipped = 0
              AND NOT EXISTS (SELECT 1 FROM sys.extended_properties ep
                              WHERE ep.major_id = t.object_id AND ep.minor_id = 0
                                AND ep.name = 'microsoft_database_tools_support')
            ORDER BY s.name, t.name""")
        tables: Dict[int, Table] = {}
        for oid, sch, name, nrows, mb, temporal, inmem, filetable, partitions in rows:
            t = Table(schema=sch, name=name, row_count=int(nrows or 0), reserved_mb=float(mb or 0))
            if temporal and int(temporal) == 2:
                t.warnings.append("system-versioned temporal table: history table copied as a normal "
                                  "table; re-enable SYSTEM_VERSIONING manually after cut-over")
            if inmem:
                t.warnings.append("memory-optimized table: created as a disk-based table")
            if filetable:
                t.warnings.append("FileTable: not supported, skipped by default")
            if partitions and int(partitions) > 1:
                t.warnings.append(f"partitioned ({partitions} partitions): created unpartitioned")
            tables[oid] = t
        if not tables:
            return []
        self._load_columns(tables)
        self._load_indexes(tables)
        self._load_fks(tables)
        self._load_checks(tables)
        for t in tables.values():
            for c in t.columns:
                if c.type_name.lower() == "sql_variant":
                    t.warnings.append(f"column {c.name} is sql_variant: copied, verify base types")
            if not t.key_columns:
                t.warnings.append("no primary key / unique key: copied as one chunk (no parallelism, "
                                  "no resume inside the table, validation by aggregate only)")
        return list(tables.values())

    def _load_columns(self, tables: Dict[int, Table]) -> None:
        rows = self.query("""
            SELECT c.object_id, c.name, ut.name, ut.is_user_defined, ut.is_assembly_type,
                   SCHEMA_NAME(ut.schema_id), st.name,
                   c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity,
                   CAST(ic.seed_value AS decimal(38,0)), CAST(ic.increment_value AS decimal(38,0)),
                   c.is_computed, cc.definition, cc.is_persisted, c.collation_name,
                   dc.name, dc.definition
            FROM sys.columns c
            JOIN sys.tables t ON t.object_id = c.object_id AND t.is_ms_shipped = 0
            JOIN sys.types ut ON ut.user_type_id = c.user_type_id
            LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id
            LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
            LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
            ORDER BY c.object_id, c.column_id""")
        for (oid, name, utype, is_udt, is_asm, uschema, stype, max_len, prec, scale, nullable,
             is_ident, seed, inc, is_comp, comp_def, persisted, coll, dname, ddef) in rows:
            t = tables.get(oid)
            if t is None:
                continue
            if is_asm:                       # CLR: geography / geometry / hierarchyid / custom CLR
                base, alias = utype, None
            elif is_udt:                     # alias type, e.g. CREATE TYPE dbo.Phone FROM varchar(20)
                base, alias = stype, utype
            elif stype and utype != stype:   # built-in alias such as sysname = nvarchar(128)
                base, alias = stype, None
            else:
                base, alias = utype, None
            t.columns.append(Column(
                name=name, type_name=base, max_length=int(max_len), precision=int(prec),
                scale=int(scale), nullable=bool(nullable), is_identity=bool(is_ident),
                identity_seed=int(seed) if seed is not None else None,
                identity_increment=int(inc) if inc is not None else None,
                is_computed=bool(is_comp), computed_definition=comp_def,
                computed_persisted=bool(persisted), collation=coll,
                default_name=dname, default_definition=ddef,
                user_type=alias, user_type_schema=uschema if alias else None))

    def _load_indexes(self, tables: Dict[int, Table]) -> None:
        filt = "i.filter_definition" if self._has_col("sys.indexes", "filter_definition") else "NULL"
        rows = self.query(f"""
            SELECT i.object_id, i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key,
                   i.is_unique_constraint, {filt}, ic.key_ordinal, ic.is_descending_key,
                   ic.is_included_column, c.name
            FROM sys.indexes i
            JOIN sys.tables t ON t.object_id = i.object_id AND t.is_ms_shipped = 0
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.type IN (1, 2, 5, 6) AND i.is_hypothetical = 0
            ORDER BY i.object_id, i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id""")
        built: Dict[Tuple[int, int], Index] = {}
        for (oid, iid, name, type_desc, uniq, is_pk, is_uc, fdef, key_ord, desc, incl, col) in rows:
            t = tables.get(oid)
            if t is None:
                continue
            ix = built.get((oid, iid))
            if ix is None:
                ix = Index(name=name, columns=[], unique=bool(uniq), clustered=type_desc.startswith("CLUSTERED"),
                           is_primary_key=bool(is_pk), is_unique_constraint=bool(is_uc),
                           filter_definition=fdef, type_desc=type_desc)
                built[(oid, iid)] = ix
                if is_pk:
                    t.primary_key = ix
                else:
                    t.indexes.append(ix)
            if incl:
                ix.included.append(col)
            elif "COLUMNSTORE" in type_desc and not key_ord:
                ix.columns.append((col, False))
            else:
                ix.columns.append((col, bool(desc)))
        other = self.query("""
            SELECT OBJECT_SCHEMA_NAME(i.object_id), OBJECT_NAME(i.object_id), i.name, i.type_desc
            FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id AND t.is_ms_shipped = 0
            WHERE i.type IN (3, 4, 7)""")
        by_name = {(t.schema, t.name): t for t in tables.values()}
        for sch, tbl, iname, tdesc in other:
            if (sch, tbl) in by_name:
                by_name[(sch, tbl)].warnings.append(f"{tdesc} index {iname} not recreated (create manually)")

    def _load_fks(self, tables: Dict[int, Table]) -> None:
        rows = self.query("""
            SELECT fk.parent_object_id, fk.object_id, fk.name,
                   OBJECT_SCHEMA_NAME(fk.referenced_object_id), OBJECT_NAME(fk.referenced_object_id),
                   fk.delete_referential_action_desc, fk.update_referential_action_desc,
                   fk.is_disabled, fk.is_not_trusted, pc.name, rc.name
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
            ORDER BY fk.object_id, fkc.constraint_column_id""")
        built: Dict[int, ForeignKey] = {}
        for poid, fkid, name, rs, rt, ondel, onupd, dis, untrusted, pcol, rcol in rows:
            t = tables.get(poid)
            if t is None:
                continue
            fk = built.get(fkid)
            if fk is None:
                fk = ForeignKey(name=name, columns=[], ref_schema=rs, ref_table=rt, ref_columns=[],
                                on_delete=ondel, on_update=onupd, disabled=bool(dis),
                                not_trusted=bool(untrusted))
                built[fkid] = fk
                t.foreign_keys.append(fk)
            fk.columns.append(pcol)
            fk.ref_columns.append(rcol)

    def _load_checks(self, tables: Dict[int, Table]) -> None:
        for poid, name, definition, dis in self.query(
                "SELECT parent_object_id, name, definition, is_disabled FROM sys.check_constraints"):
            t = tables.get(poid)
            if t is not None:
                t.checks.append(CheckConstraint(name=name, definition=definition, disabled=bool(dis)))

    def list_modules(self) -> List[ModuleDef]:
        out: List[ModuleDef] = []
        # alias types
        for sch, name, base, max_len, prec, scale, nullable in self.query("""
                SELECT SCHEMA_NAME(t.schema_id), t.name, st.name, t.max_length, t.precision, t.scale, t.is_nullable
                FROM sys.types t JOIN sys.types st ON st.user_type_id = t.system_type_id
                WHERE t.is_user_defined = 1 AND t.is_assembly_type = 0 AND t.is_table_type = 0"""):
            col = Column(name="x", type_name=base, max_length=max_len, precision=prec, scale=scale)
            from .ddl import type_sql
            out.append(ModuleDef("TYPE", sch, name,
                                 f"CREATE TYPE {q(sch)}.{q(name)} FROM {type_sql(col)} "
                                 f"{'NULL' if nullable else 'NOT NULL'}"))
        if self._has_col("sys.sequences", "current_value"):
            for sch, name, tname, start, inc, mn, mx, cyc, cache, cur in self.query("""
                    SELECT SCHEMA_NAME(s.schema_id), s.name, TYPE_NAME(s.user_type_id),
                           CAST(s.start_value AS decimal(38,0)), CAST(s.increment AS decimal(38,0)),
                           CAST(s.minimum_value AS decimal(38,0)), CAST(s.maximum_value AS decimal(38,0)),
                           s.is_cycling, s.cache_size, CAST(s.current_value AS decimal(38,0))
                    FROM sys.sequences s"""):
                restart = int(cur) + int(inc)
                out.append(ModuleDef("SEQUENCE", sch, name,
                                     f"CREATE SEQUENCE {q(sch)}.{q(name)} AS {tname} START WITH {restart} "
                                     f"INCREMENT BY {int(inc)} MINVALUE {int(mn)} MAXVALUE {int(mx)} "
                                     f"{'CYCLE' if cyc else 'NO CYCLE'}"
                                     + (f" CACHE {cache}" if cache else "")))
        kinds = {"V": "VIEW", "P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION", "TF": "FUNCTION",
                 "TR": "TRIGGER"}
        for otype, sch, name, definition, parent, disabled in self.query("""
                SELECT o.type, SCHEMA_NAME(o.schema_id), o.name, m.definition,
                       CASE WHEN o.parent_object_id > 0
                            THEN OBJECT_SCHEMA_NAME(o.parent_object_id) + '.' + OBJECT_NAME(o.parent_object_id) END,
                       ISNULL(tr.is_disabled, 0)
                FROM sys.objects o
                JOIN sys.sql_modules m ON m.object_id = o.object_id
                LEFT JOIN sys.triggers tr ON tr.object_id = o.object_id
                WHERE o.is_ms_shipped = 0 AND o.type IN ('V','P','FN','IF','TF','TR')
                ORDER BY o.create_date"""):
            out.append(ModuleDef(kinds[otype.strip()], sch, name, definition, parent, bool(disabled)))
        for sch, name, base in self.query(
                "SELECT SCHEMA_NAME(schema_id), name, base_object_name FROM sys.synonyms"):
            out.append(ModuleDef("SYNONYM", sch, name, f"CREATE SYNONYM {q(sch)}.{q(name)} FOR {base}"))
        return out

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
            f"FROM {self.qualified(table)} WITH (NOLOCK)) x "
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
        cols = table.compare_columns
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
        cols = table.compare_columns
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
        return [tds_base.Column(name=c.name, type=pytds_type(c),
                                flags=tds_base.Column.fNullable if c.nullable else 0) for c in cols]

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
    if c.is_clr:
        return T.VarBinaryMaxType()
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
    if t == "binary":      # python-tds has no BINARY bulk serializer; server converts varbinary(n)
        return T.VarBinaryType(size=c.max_length)
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
