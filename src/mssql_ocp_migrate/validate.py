"""End-to-end validation: schema parity, row counts, chunk checksums, exact row-hash diffs.

Levels (validation.mode):
  count     exact COUNT_BIG per table on both sides
  checksum  + server-side per-chunk COUNT/SUM/CHECKSUM_AGG of BINARY_CHECKSUM (cheap: no data
            leaves the servers) + an exact row-by-row SHA-256 comparison of a random sample of
            chunks (sample_full_pct). Any chunk whose checksum differs is drilled into
            automatically, so the report lists the exact keys that differ.
  full      exact row-by-row SHA-256 comparison of every chunk (reads all data from both sides)
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .adapter import Database
from .config import Config
from .model import Chunk, Table
from .rowhash import RowHasher

log = logging.getLogger(__name__)


@dataclass
class ChunkCheck:
    chunk_id: str
    method: str                       # checksum | full
    ok: bool
    source_rows: int = 0
    target_rows: int = 0
    missing_in_target: List[str] = field(default_factory=list)
    extra_in_target: List[str] = field(default_factory=list)
    different: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class TableCheck:
    table: str
    source_rows: int = 0
    target_rows: int = 0
    schema_diffs: List[str] = field(default_factory=list)
    chunks: List[ChunkCheck] = field(default_factory=list)
    chunks_full: int = 0
    chunks_checksum: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return (self.source_rows == self.target_rows and not self.schema_diffs
                and all(c.ok for c in self.chunks))


@dataclass
class ValidationResult:
    mode: str
    tables: Dict[str, TableCheck] = field(default_factory=dict)
    object_diffs: List[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables.values()) and not self.object_diffs

    def summary(self) -> Dict[str, Any]:
        bad = [t for t in self.tables.values() if not t.ok]
        return {"mode": self.mode, "ok": self.ok, "tables": len(self.tables), "tables_failed": len(bad),
                "rows_source": sum(t.source_rows for t in self.tables.values()),
                "rows_target": sum(t.target_rows for t in self.tables.values()),
                "chunks_checksummed": sum(t.chunks_checksum for t in self.tables.values()),
                "chunks_row_compared": sum(t.chunks_full for t in self.tables.values()),
                "object_diffs": len(self.object_diffs), "seconds": round(self.seconds, 1)}


# ------------------------------------------------------------------ comparisons
def _schema_diff(s: Table, t: Optional[Table]) -> List[str]:
    if t is None:
        return ["table missing on target"]
    out = []
    tcols = {c.name: c for c in t.columns}
    for c in s.columns:
        tc = tcols.pop(c.name, None)
        if tc is None:
            out.append(f"column {c.name} missing on target")
            continue
        for attr in ("type_name", "max_length", "precision", "scale", "nullable", "is_identity",
                     "is_computed", "collation"):
            if getattr(c, attr) != getattr(tc, attr):
                out.append(f"column {c.name}: {attr} {getattr(c, attr)!r} -> {getattr(tc, attr)!r}")
    out.extend(f"column {n} only on target" for n in tcols)
    spk = s.primary_key.columns if s.primary_key else None
    tpk = t.primary_key.columns if t.primary_key else None
    if spk != tpk:
        out.append(f"primary key {spk} -> {tpk}")
    for kind, sa, ta in (("index", {i.name for i in s.indexes}, {i.name for i in t.indexes}),
                         ("foreign key", {f.name for f in s.foreign_keys}, {f.name for f in t.foreign_keys}),
                         ("check", {c.name for c in s.checks}, {c.name for c in t.checks})):
        out.extend(f"{kind} {n} missing on target" for n in sorted(sa - ta))
    return out


def _full_compare(src: Database, tgt: Database, table: Table, chunk: Chunk, batch: int,
                  max_diff: int) -> ChunkCheck:
    cols = table.compare_columns
    keys = table.key_columns
    h = RowHasher(cols, keys)
    cc = ChunkCheck(chunk.id, "full", True)
    if keys:
        side: Dict[bytes, tuple] = {}
        for rows in src.read_rows(table, chunk, batch, ordered=False, columns=cols):
            for r in rows:
                side[h.key(r)] = (h.digest(r), h.display_key(r))
        cc.source_rows = len(side)
        for rows in tgt.read_rows(table, chunk, batch, ordered=False, columns=cols):
            for r in rows:
                cc.target_rows += 1
                k = h.key(r)
                got = side.pop(k, None)
                if got is None:
                    if len(cc.extra_in_target) < max_diff:
                        cc.extra_in_target.append(h.display_key(r))
                    cc.ok = False
                elif got[0] != h.digest(r):
                    if len(cc.different) < max_diff:
                        cc.different.append(got[1])
                    cc.ok = False
        if side:
            cc.ok = False
            cc.missing_in_target = [v[1] for _, v in zip(range(max_diff), side.values())]
    else:   # no key: order-independent multiset digest
        digests = []
        for db in (src, tgt):
            n, acc = 0, 0
            for rows in db.read_rows(table, chunk, batch, ordered=False, columns=cols):
                for r in rows:
                    n += 1
                    acc = (acc + int.from_bytes(hashlib.sha256(h.encode(r)).digest(), "big")) % (1 << 256)
            digests.append((n, acc))
        cc.source_rows, cc.target_rows = digests[0][0], digests[1][0]
        cc.ok = digests[0] == digests[1]
        if not cc.ok:
            cc.different.append("table has no key: aggregate row digest differs")
    return cc


def _check_chunk(cfg: Config, factory, table: Table, chunk: Chunk, full: bool) -> ChunkCheck:
    from .engine import _db, _drop_connections
    try:
        src, tgt = _db(factory, "source"), _db(factory, "target")
        if not full:
            sc, ss = src.checksum(table, chunk)
            tc, ts = tgt.checksum(table, chunk)
            cc = ChunkCheck(chunk.id, "checksum", sc == tc and ss == ts, sc, tc)
            if cc.ok:
                return cc
            full = True   # drill down to exact keys
        return _full_compare(src, tgt, table, chunk, cfg.migration.batch_rows, cfg.validation.max_diff_rows)
    except Exception as exc:
        _drop_connections()
        return ChunkCheck(chunk.id, "full" if full else "checksum", False, error=f"{type(exc).__name__}: {exc}")


class Validator:
    def __init__(self, cfg: Config, factory, progress: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.factory = factory
        self.progress = progress or (lambda m: log.info(m))

    def run(self, tables: List[Table], chunks: Dict[str, List[Chunk]],
            mode: Optional[str] = None) -> ValidationResult:
        """Validate ``tables`` using the given chunk plan; ``mode`` overrides ``validation.mode``."""
        mode = mode or self.cfg.validation.mode
        v = self.cfg.validation
        t0 = time.perf_counter()
        res = ValidationResult(mode=mode)
        src, tgt = self.factory("source", True), self.factory("target", True)
        try:
            tgt_tables = {t.fqn: t for t in tgt.list_tables()} if v.compare_schema else {}
            for t in tables:
                tc = TableCheck(t.fqn)
                tc.source_rows = src.count_rows(t)
                tc.target_rows = tgt.count_rows(t) if tgt.table_exists(t) else -1
                if v.compare_schema:
                    tc.schema_diffs = _schema_diff(t, tgt_tables.get(t.fqn))
                res.tables[t.fqn] = tc
            if v.compare_schema and self.cfg.migration.copy_programmable_objects:
                smods = {(m.kind, m.fqn) for m in src.list_modules()}
                tmods = {(m.kind, m.fqn) for m in tgt.list_modules()}
                res.object_diffs = [f"{k} {n} missing on target" for k, n in sorted(smods - tmods)]
                for t in tables:
                    a, b = src.identity_current(t), tgt.identity_current(t)
                    if a is not None and b is not None and a != b:
                        res.object_diffs.append(f"identity {t.fqn}: current value {a} -> {b}")
        finally:
            src.close()
            tgt.close()
        self.progress("row counts: " + ("all match" if all(
            c.source_rows == c.target_rows for c in res.tables.values()) else "MISMATCHES found"))

        if mode != "count":
            rnd = random.Random(20240601)
            jobs = []
            for t in tables:
                if res.tables[t.fqn].target_rows < 0:
                    continue
                for c in chunks.get(t.fqn) or [Chunk(t.fqn, 0)]:
                    full = mode == "full" or rnd.random() * 100 < v.sample_full_pct
                    jobs.append((t, c, full))
            self.progress(f"validating {len(jobs)} chunks ({mode} mode)")
            m = self.cfg.migration
            with cf.ThreadPoolExecutor(max_workers=m.workers) as ex:
                futs = {ex.submit(_check_chunk, self.cfg, self.factory, t, c, full): t for t, c, full in jobs}
                for fut in cf.as_completed(futs):
                    t = futs[fut]
                    cc = fut.result()
                    tc = res.tables[t.fqn]
                    tc.chunks.append(cc)
                    if cc.method == "full":
                        tc.chunks_full += 1
                    else:
                        tc.chunks_checksum += 1
                    if not cc.ok:
                        log.error("validation mismatch %s: %s", cc.chunk_id, cc.error or
                                  f"missing={cc.missing_in_target[:3]} extra={cc.extra_in_target[:3]} "
                                  f"different={cc.different[:3]}")
            for tc in res.tables.values():
                tc.chunks.sort(key=lambda c: int(c.chunk_id.rsplit("#", 1)[1]))
        res.seconds = time.perf_counter() - t0
        s = res.summary()
        self.progress(f"validation {'PASSED' if s['ok'] else 'FAILED'}: {s['tables'] - s['tables_failed']}/"
                      f"{s['tables']} tables ok, {s['chunks_checksummed']} chunks checksummed, "
                      f"{s['chunks_row_compared']} chunks row-compared, {s['object_diffs']} object diffs")
        return res
