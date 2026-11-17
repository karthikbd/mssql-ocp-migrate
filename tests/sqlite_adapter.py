"""SQLite implementation of the Database interface — lets the whole engine (chunking, parallel
copy, resume, validation, reporting) run in tests without a SQL Server instance."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

from mssql_ocp_migrate import chunking
from mssql_ocp_migrate.adapter import Database
from mssql_ocp_migrate.model import Chunk, Column, Table
from mssql_ocp_migrate.rowhash import RowHasher

_TYPES = {"INTEGER": "bigint", "INT": "int", "TEXT": "nvarchar", "REAL": "float", "BLOB": "varbinary",
          "NUMERIC": "decimal"}


class SqliteDB(Database):
    def __init__(self, path: str, label: str, autocommit: bool = True):
        self.path = path
        self.label = label
        self.conn = sqlite3.connect(path, timeout=60, isolation_level=None if autocommit else "DEFERRED",
                                    check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.autocommit = autocommit

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def list_tables(self) -> List[Table]:
        out = []
        for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            t = Table(schema="main", name=name)
            pk = []
            for cid, cname, ctype, notnull, dflt, pkpos in self.conn.execute(f"PRAGMA table_info({self.quote(name)})"):
                t.columns.append(Column(name=cname, type_name=_TYPES.get(ctype.upper(), "nvarchar"),
                                        max_length=-1 if ctype.upper() in ("TEXT", "BLOB") else 0,
                                        nullable=not notnull and not pkpos))
                if pkpos:
                    pk.append((pkpos, cname))
            if pk:
                from mssql_ocp_migrate.model import Index
                t.primary_key = Index(name=f"PK_{name}", columns=[(c, False) for _, c in sorted(pk)],
                                      unique=True, clustered=True, is_primary_key=True)
            t.row_count = self.conn.execute(f"SELECT COUNT(*) FROM {self.quote(name)}").fetchone()[0]
            t.reserved_mb = t.row_count * 100 / 1024 / 1024
            out.append(t)
        return out

    def table_exists(self, table: Table) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (table.name,)).fetchone() is not None

    def boundaries(self, table: Table, chunk_rows: int) -> List[Tuple[Any, ...]]:
        keys = table.key_columns
        if not keys or table.row_count <= chunk_rows:
            return []
        kl = ", ".join(self.quote(k) for k in keys)
        sql = (f"SELECT {kl} FROM (SELECT {kl}, ROW_NUMBER() OVER (ORDER BY {kl}) AS rn__ FROM "
               f"{self.qualified(table)}) WHERE rn__ > 1 AND (rn__ - 1) % ? = 0 ORDER BY {kl}")
        return [tuple(r) for r in self.conn.execute(sql, (chunk_rows,))]

    def _where(self, table, chunk):
        return chunking.predicate(chunk, table.key_columns, self.quote)

    def read_rows(self, table: Table, chunk: Chunk, batch: int, ordered: bool = True,
                  columns: Optional[Sequence[Column]] = None) -> Iterator[List[tuple]]:
        cols = list(columns) if columns is not None else table.insert_columns
        where, params = self._where(table, chunk)
        order = (" ORDER BY " + ", ".join(self.quote(k) for k in table.key_columns)) \
            if ordered and table.key_columns else ""
        cur = self.conn.execute(f"SELECT {', '.join(self.quote(c.name) for c in cols)} FROM "
                                f"{self.qualified(table)} WHERE {where}{order}", params)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield [tuple(r) for r in rows]

    def count_rows(self, table: Table, chunk: Optional[Chunk] = None) -> int:
        if chunk is None:
            return self.conn.execute(f"SELECT COUNT(*) FROM {self.qualified(table)}").fetchone()[0]
        where, params = self._where(table, chunk)
        return self.conn.execute(f"SELECT COUNT(*) FROM {self.qualified(table)} WHERE {where}", params).fetchone()[0]

    def checksum(self, table: Table, chunk: Chunk):
        h = RowHasher(table.compare_columns, table.key_columns)
        n, acc = 0, 0
        for rows in self.read_rows(table, chunk, 5000, ordered=False, columns=table.compare_columns):
            for r in rows:
                n += 1
                acc = (acc + int.from_bytes(hashlib.sha256(h.encode(r)).digest()[:8], "big")) % (1 << 64)
        return n, acc

    def delete_range(self, table: Table, chunk: Chunk) -> None:
        where, params = self._where(table, chunk)
        self._begin()
        self.conn.execute(f"DELETE FROM {self.qualified(table)} WHERE {where}", params)

    def truncate(self, table: Table) -> None:
        self._begin()
        self.conn.execute(f"DELETE FROM {self.qualified(table)}")

    def bulk_load(self, table: Table, batches: Iterable[Sequence[tuple]], tablock: bool) -> int:
        cols = table.insert_columns
        sql = (f"INSERT INTO {self.qualified(table)} ({', '.join(self.quote(c.name) for c in cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        self._begin()
        n = 0
        for b in batches:
            self.conn.executemany(sql, b)
            n += len(b)
        return n

    def _begin(self) -> None:
        if not self.autocommit and not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        self.conn.execute(sql, params or ())

    def commit(self) -> None:
        if self.conn.in_transaction:
            self.conn.commit()

    def rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


# Fault injection shared across threads (tests that inject faults use thread workers;
# process workers are spawned and don't share this dict)
FAULTS: dict = {}
_lock = threading.Lock()


class FaultyTarget(SqliteDB):
    """Fails bulk_load for chunks listed in FAULTS[(table, lower)] = remaining_failures."""

    def bulk_load(self, table, batches, tablock):
        batches = list(batches)
        first = batches[0][0][0] if batches and batches[0] else None
        with _lock:
            left = FAULTS.get((table.name, first), 0)
            if left:
                FAULTS[(table.name, first)] = left - 1
                raise ConnectionError(f"injected network failure at {table.name} key {first}")
        return super().bulk_load(table, batches, tablock)


class SqliteFactory:
    def __init__(self, src: str, tgt: str, faulty: bool = False):
        self.src, self.tgt, self.faulty = src, tgt, faulty

    def __call__(self, side: str, autocommit: bool = True):
        if side == "source":
            return SqliteDB(self.src, "source", True)
        cls = FaultyTarget if self.faulty else SqliteDB
        return cls(self.tgt, "target", autocommit)
