"""Measure real throughput on a sample and project how long the full migration will take."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

from . import ddl
from .config import Config
from .model import Chunk, Table

log = logging.getLogger(__name__)


@dataclass
class Estimate:
    tables: int
    rows: int
    source_mb: float
    sample_table: str = ""
    sample_rows: int = 0
    read_rows_per_s: float = 0.0
    write_rows_per_s: float = 0.0
    stream_mb_per_s: float = 0.0
    checksum_mb_per_s: float = 0.0
    workers: int = 1
    copy_minutes: float = 0.0
    index_minutes: float = 0.0
    validate_minutes: float = 0.0
    total_minutes: float = 0.0
    total_minutes_range: List[float] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parallel_factor(workers: int, executor: str) -> float:
    # Parallel streams rarely scale linearly (network, log writes, CPU): 75% efficiency per extra
    # stream. Threads with the pure-Python driver share one core (GIL), so they barely add throughput.
    if executor == "thread_gil":
        return min(workers, 1.3)
    return 1 + 0.75 * (workers - 1)


def estimate(cfg: Config, factory=None, sample_rows: int = 50_000, write_probe: bool = True) -> Estimate:
    """Measure a logical migration on a sample and project its duration.

    Reads ``sample_rows`` from the largest selected table, bulk-loads them into a ``#temp`` table on
    the target (rolled back) unless ``write_probe`` is False, times a server-side checksum, then
    scales by bytes to the whole selection for ``cfg.migration.workers``.
    """
    from .engine import Migrator, SqlServerFactory
    factory = factory or SqlServerFactory(cfg)
    mig = Migrator(cfg, factory)
    src = factory("source", True)
    try:
        tables = mig.source_tables(src)
        rows = sum(t.row_count for t in tables)
        mb = sum(t.reserved_mb for t in tables)
        est = Estimate(tables=len(tables), rows=rows, source_mb=round(mb, 1), workers=cfg.migration.workers)
        if not tables or rows == 0:
            est.notes.append("no data to move")
            return est
        # sample the table that dominates the volume (most representative of the long pole)
        big = max(tables, key=lambda t: t.reserved_mb)
        est.sample_table = big.fqn
        bytes_per_row = (big.reserved_mb * 1024 * 1024 / big.row_count) if big.row_count else 200
        sample: List[tuple] = []
        t0 = time.perf_counter()
        for batch in src.read_rows(big, Chunk(big.fqn, 0), cfg.migration.batch_rows, ordered=False):
            sample.extend(batch)
            if len(sample) >= sample_rows:
                sample = sample[:sample_rows]
                break
        read_s = max(time.perf_counter() - t0, 1e-6)
        est.sample_rows = len(sample)
        est.read_rows_per_s = round(len(sample) / read_s)
        # server-side checksum speed (validation cost)
        t0 = time.perf_counter()
        src.checksum(big, Chunk(big.fqn, 0))
        cks = max(time.perf_counter() - t0, 1e-6)
        est.checksum_mb_per_s = round(big.reserved_mb / cks, 1)
    finally:
        src.close()

    write_rps = None
    if write_probe and sample:
        tgt = factory("target", False)
        try:
            write_rps = _write_probe(tgt, big, sample, cfg.migration.batch_rows)
            est.write_rows_per_s = round(write_rps)
        except Exception as exc:
            est.notes.append(f"write probe skipped: {exc}")
            tgt.rollback()
        finally:
            tgt.close()

    per_stream_rps = min(est.read_rows_per_s, write_rps or est.read_rows_per_s)
    est.stream_mb_per_s = round(per_stream_rps * bytes_per_row / 1024 / 1024, 2)
    kind = cfg.resolved_executor()
    if kind == "thread":
        from .drivers import choose_driver
        if "pytds" in (choose_driver(cfg.source), choose_driver(cfg.target)):
            kind = "thread_gil"
            est.notes.append("thread executor with the pure-Python driver is CPU-bound on one core; "
                             "use executor = \"process\" (or \"auto\") to scale with workers")
    total_rps = per_stream_rps * _parallel_factor(cfg.migration.workers, kind)
    # rows/s varies with row width; scale by bytes so wide and narrow tables are weighed fairly
    copy_s = (mb * 1024 * 1024 / bytes_per_row) / max(total_rps, 1)
    est.copy_minutes = round(copy_s / 60, 1)
    est.index_minutes = round(0.25 * est.copy_minutes, 1)       # rebuild indexes/FKs after load
    v = cfg.validation
    val_s = 2 * mb / max(est.checksum_mb_per_s, 1)              # both sides scanned
    if v.mode == "full":
        val_s += copy_s * 0.8
    else:
        val_s += copy_s * 0.8 * v.sample_full_pct / 100
    est.validate_minutes = round(val_s / 60, 1)
    est.total_minutes = round(est.copy_minutes + est.index_minutes + est.validate_minutes, 1)
    est.total_minutes_range = [round(est.total_minutes * 0.7, 1), round(est.total_minutes * 1.6, 1)]
    if est.copy_minutes > 120:
        est.notes.append("more workers (and the process executor) usually help until the network or "
                         "target log disk saturates; re-run estimate with a higher workers value to compare")
    if any(not t.key_columns and t.row_count > 5_000_000 for t in tables):
        est.notes.append("large tables without a key cannot be split into parallel chunks")
    return est


def _write_probe(tgt, table: Table, sample: List[tuple], batch_rows: int) -> float:
    """Bulk-load the sample into a #temp copy of the table on the target, then roll back."""
    from .model import Column
    cols = table.insert_columns
    tmp = Table(schema="", name="#mom_estimate",
                columns=[Column(**{**c.__dict__, "is_identity": False, "default_definition": None,
                                   "default_name": None, "user_type": None, "user_type_schema": None})
                         for c in cols])
    body = ", ".join(ddl.column_sql(c) for c in tmp.columns)
    tgt.execute(f"CREATE TABLE #mom_estimate ({body})")
    t0 = time.perf_counter()
    batches = [sample[i:i + batch_rows] for i in range(0, len(sample), batch_rows)]
    if tgt.driver == "pytds":
        tgt._insert_bulk_pytds("#mom_estimate", None, tmp.columns, batches, True)
    else:
        tgt._load_pyodbc(tmp, tmp.columns, batches, False, target_name="#mom_estimate")
    secs = max(time.perf_counter() - t0, 1e-6)
    tgt.rollback()
    return len(sample) / secs


def format_estimate(e: Estimate) -> str:
    lines = [
        f"Tables: {e.tables}   Rows: {e.rows:,}   Source size: {e.source_mb:,.0f} MB",
        f"Sample: {e.sample_rows:,} rows from {e.sample_table}",
        f"Per stream: read {e.read_rows_per_s:,.0f} rows/s, write {e.write_rows_per_s:,.0f} rows/s "
        f"(~{e.stream_mb_per_s} MB/s); server checksum ~{e.checksum_mb_per_s} MB/s",
        f"With {e.workers} workers:",
        f"  copy        ~{e.copy_minutes:,.1f} min",
        f"  indexes/FKs ~{e.index_minutes:,.1f} min",
        f"  validation  ~{e.validate_minutes:,.1f} min",
        f"  TOTAL       ~{e.total_minutes:,.1f} min  (likely range "
        f"{e.total_minutes_range[0] if e.total_minutes_range else 0:,.1f}–"
        f"{e.total_minutes_range[1] if e.total_minutes_range else 0:,.1f} min)",
    ]
    lines += [f"note: {n}" for n in e.notes]
    return "\n".join(lines)
