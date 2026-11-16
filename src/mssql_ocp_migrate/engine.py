"""Orchestration: preflight -> schema -> chunked parallel copy -> constraints -> objects -> validation."""
from __future__ import annotations

import concurrent.futures as cf
import logging
import threading
import time
import traceback
import zlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import ddl
from .adapter import Database, ModuleDef
from .chunking import build_chunks
from .config import Config
from .model import Chunk, ChunkResult, Table
from .state import State

log = logging.getLogger(__name__)

Factory = Callable[[str, bool], Database]   # (side, autocommit) -> Database


class MigrationError(RuntimeError):
    pass


class SqlServerFactory:
    """Picklable factory so it also works with the process executor."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def __call__(self, side: str, autocommit: bool = True) -> Database:
        from .sqlserver import SqlServer
        return SqlServer(getattr(self.cfg, side), side, autocommit=autocommit)


# ---------------------------------------------------------------- worker side
_local = threading.local()


def _db(factory: Factory, side: str) -> Database:
    cache = getattr(_local, "dbs", None)
    if cache is None:
        cache = _local.dbs = {}
    if side not in cache:
        cache[side] = factory(side, side == "source")   # target uses explicit transactions
    return cache[side]


def _drop_connections() -> None:
    for db in getattr(_local, "dbs", {}).values():
        try:
            db.close()
        except Exception:
            pass
    _local.dbs = {}


def copy_chunk(factory: Factory, table: Table, chunk: Chunk, batch_rows: int,
               tablock: bool, single_chunk: bool, max_retries: int) -> ChunkResult:
    """Copy one key range. Idempotent: the target range is cleared first in the same transaction."""
    attempt = 0
    while True:
        attempt += 1
        t0 = time.perf_counter()
        try:
            src, tgt = _db(factory, "source"), _db(factory, "target")
            tgt.prepare(table)
            if single_chunk:
                tgt.truncate(table)
            else:
                tgt.delete_range(table, chunk)
            rows = tgt.bulk_load(table, src.read_rows(table, chunk, batch_rows), tablock)
            tgt.commit()
            return ChunkResult(chunk.id, rows, time.perf_counter() - t0)
        except Exception as exc:  # retry on a fresh connection
            try:
                _db(factory, "target").rollback()
            except Exception:
                pass
            _drop_connections()
            if attempt > max_retries:
                return ChunkResult(chunk.id, 0, time.perf_counter() - t0, "failed",
                                   f"{type(exc).__name__}: {exc}")
            wait = min(30, 2 ** attempt)
            log.warning("chunk %s attempt %d failed (%s); retrying in %ss", chunk.id, attempt, exc, wait)
            time.sleep(wait)


def _boundaries(factory: Factory, table: Table, chunk_rows: int):
    return _db(factory, "source").boundaries(table, chunk_rows)


# ---------------------------------------------------------------- results
@dataclass
class TableRun:
    """Copy outcome for one table: rows and seconds summed over its chunks, plus any failed chunks."""
    table: str
    rows: int = 0
    seconds: float = 0.0
    chunks: int = 0
    failed_chunks: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class RunResult:
    """Everything a :meth:`Migrator.migrate` run produced.

    ``ok`` is True only when every chunk copied, no DDL failed and validation (if run) passed.
    ``validation`` holds a :class:`~mssql_ocp_migrate.validate.ValidationResult` or ``None``.
    """
    started: float
    finished: float = 0.0
    source_info: Dict[str, Any] = field(default_factory=dict)
    target_info: Dict[str, Any] = field(default_factory=dict)
    tables: Dict[str, TableRun] = field(default_factory=dict)
    ddl_errors: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    identity_strategy: Dict[str, str] = field(default_factory=dict)
    validation: Any = None

    @property
    def rows(self) -> int:
        return sum(t.rows for t in self.tables.values())

    @property
    def ok(self) -> bool:
        copy_ok = all(not t.failed_chunks for t in self.tables.values())
        val_ok = self.validation is None or self.validation.ok
        return copy_ok and val_ok and not self.ddl_errors


# ---------------------------------------------------------------- migrator
class Migrator:
    """Logical (row-level) migration: schema, chunked parallel copy, constraints, objects, validation.

    :param cfg: the loaded :class:`~mssql_ocp_migrate.config.Config`.
    :param factory: ``(side, autocommit) -> Database``; defaults to SQL Server connections built from
        ``cfg``. Tests pass a SQLite factory here.
    :param progress: callback receiving one human-readable line per event (default: ``logging``).
    """

    def __init__(self, cfg: Config, factory: Optional[Factory] = None,
                 progress: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.factory: Factory = factory or SqlServerFactory(cfg)
        self.progress = progress or (lambda msg: log.info(msg))
        self._tables: Optional[List[Table]] = None

    # -- discovery ---------------------------------------------------------------
    def source_tables(self, src: Optional[Database] = None) -> List[Table]:
        """Catalog metadata of the source tables selected by ``include``/``exclude`` (cached)."""
        if self._tables is None:
            own = src is None
            src = src or self.factory("source", True)
            try:
                self._tables = [t for t in src.list_tables() if self.cfg.selects(t.schema, t.name)
                                and not any("FileTable" in w for w in t.warnings)]
            finally:
                if own:
                    src.close()
        return self._tables

    def schema_script(self) -> str:
        """Full DDL script (what `migrate` would run), for review or a DBA hand-off."""
        src = self.factory("source", True)
        try:
            tables = self.source_tables(src)
            modules = src.list_modules() if self.cfg.migration.copy_programmable_objects else []
            schemas = sorted({t.schema for t in tables} | {m.schema for m in modules})
        finally:
            src.close()
        out = ["-- generated by mssql-ocp-migrate", ""]
        for s in schemas:
            if s != "dbo":
                out.append(f"IF SCHEMA_ID(N'{s}') IS NULL EXEC('CREATE SCHEMA {ddl.q(s)}');\nGO")
        for m in modules:
            if m.kind in ("TYPE", "SEQUENCE") and m.definition:
                out.append(m.definition + ";\nGO")
        for t in tables:
            out.append(ddl.create_table_sql(t) + ";\nGO")
        out.append("-- ===== after data load =====")
        for t in tables:
            out.extend(s + ";\nGO" for s in ddl.post_load_sql(t))
        for t in tables:
            out.extend(s + ";\nGO" for s in ddl.fk_sql_all(t))
        for m in modules:
            if m.kind not in ("TYPE", "SEQUENCE") and m.definition:
                out.append(m.definition.strip() + "\nGO")
        return "\n".join(out) + "\n"

    # -- main entry ------------------------------------------------------------
    def migrate(self, validate: bool = True) -> RunResult:
        """Single process, or one shard of a scale-out run (migration.shard_count > 1).

        Scale-out: shard 0 creates the schema and writes the chunk plan to ``plan_file`` (on a
        shared volume); every shard copies the chunks whose id hashes to it; `finalize` then builds
        indexes/constraints/objects and validates once all shards are done."""
        cfg, m = self.cfg, self.cfg.migration
        res = RunResult(started=time.time())
        sharded = m.shard_count > 1
        lead = m.shard_index == 0
        state_path = shard_path(m.state_file, m.shard_index, m.shard_count) if sharded else m.state_file
        resume = m.if_target_not_empty == "resume" or sharded
        state = State.load_or_new(state_path) if resume else State(state_path)
        modules: List[ModuleDef] = []
        src = self.factory("source", True)
        tgt = self.factory("target", True)
        try:
            res.source_info = getattr(src, "server_info", lambda: {})()
            res.target_info = getattr(tgt, "server_info", lambda: {})()
            tables = self.source_tables(src)
            if not tables:
                raise MigrationError("no tables matched include/exclude filters")
            for t in tables:
                res.warnings.extend(f"{t.fqn}: {w}" for w in t.warnings)
            total_rows = sum(t.row_count for t in tables)
            self.progress(f"{len(tables)} tables, ~{total_rows:,} rows, "
                          f"~{sum(t.reserved_mb for t in tables):,.0f} MB on source"
                          + (f" | shard {m.shard_index + 1}/{m.shard_count}" if sharded else ""))
            modules = src.list_modules() if m.copy_programmable_objects else []
            if lead and not state.phase_done("schema"):
                self._preflight_target(tgt, tables)
                if m.schema_mode == "create":
                    self._create_schema(tgt, tables, modules, res)
                if m.target_recovery_simple:
                    self._recovery_to_simple(tgt, state)
                state.mark_phase("schema")
        finally:
            src.close()
            tgt.close()

        if sharded:
            self._shared_plan(tables, state, lead)
        else:
            self._plan_chunks(tables, state)
        self._copy(tables, state, res)

        if sharded:
            self.progress(f"shard {m.shard_index + 1}/{m.shard_count} finished; run `mssql-migrate finalize` "
                          "after all shards complete")
            res.finished = time.time()
            return res
        self._finish(tables, modules, state, res, validate)
        res.finished = time.time()
        return res

    def finalize(self, validate: bool = True) -> RunResult:
        """After a sharded copy: check every shard finished, then indexes/FKs/objects + validation."""
        m = self.cfg.migration
        res = RunResult(started=time.time())
        plan = State.load_or_new(m.plan_file)
        done: Dict[str, Dict[str, Any]] = {}
        for i in range(m.shard_count):
            st = State.load_or_new(shard_path(m.state_file, i, m.shard_count))
            for tname, t in st.data["tables"].items():
                for idx, c in t["chunks"].items():
                    if c["status"] == "done":
                        done.setdefault(tname, {})[idx] = c
        missing = [f"{t}#{i}" for t, tt in plan.data["tables"].items() for i in tt["chunks"]
                   if i not in done.get(t, {})]
        if missing:
            raise MigrationError(f"{len(missing)} chunks not copied yet (e.g. {', '.join(missing[:5])}); "
                                 "re-run the failed shards first")
        for tname, chunks in done.items():
            for idx, c in chunks.items():
                plan.data["tables"][tname]["chunks"][idx].update(status="done", rows=c["rows"],
                                                                 seconds=c["seconds"])
        src = self.factory("source", True)
        try:
            tables = self.source_tables(src)
            modules = src.list_modules() if m.copy_programmable_objects else []
        finally:
            src.close()
        self._fill_from_state(tables, plan, res)
        self._finish(tables, modules, plan, res, validate)
        res.finished = time.time()
        return res

    def _finish(self, tables, modules, state: State, res: RunResult, validate: bool) -> None:
        m = self.cfg.migration
        tgt = self.factory("target", True)
        src = self.factory("source", True)
        try:
            if m.schema_mode == "create" and not state.phase_done("post_load"):
                failed = {n for n, r in res.tables.items() if r.failed_chunks}
                if failed:
                    res.warnings.append("indexes/constraints not created because some chunks failed; "
                                        "re-run with if_target_not_empty = \"resume\"")
                else:
                    self._post_load(src, tgt, tables, modules, res)
                    state.mark_phase("post_load")
            if m.target_recovery_simple and state.data["phases"].get("recovery_before"):
                if not any(r.failed_chunks for r in res.tables.values()):
                    model = state.data["phases"]["recovery_before"]
                    tgt.execute(f"ALTER DATABASE CURRENT SET RECOVERY {model}")
                    res.warnings.append(f"target recovery model restored to {model}: take a FULL backup now "
                                        "to start the log chain")
            res.identity_strategy = _identity_info()
        finally:
            src.close()
            tgt.close()
        if validate:
            from .validate import Validator
            res.validation = Validator(self.cfg, self.factory, self.progress).run(
                tables, {t.fqn: state.chunks(t.fqn) or [] for t in tables})

    def _recovery_to_simple(self, tgt: Database, state: State) -> None:
        """Bulk loads are minimally logged in SIMPLE/BULK_LOGGED: far less log I/O on the target."""
        query = getattr(tgt, "query", None)
        if query is None:
            return
        model = query("SELECT recovery_model_desc FROM sys.databases WHERE name = DB_NAME()")[0][0]
        if model != "SIMPLE":
            state.data["phases"]["recovery_before"] = model
            tgt.execute("ALTER DATABASE CURRENT SET RECOVERY SIMPLE")
            self.progress(f"target recovery model {model} -> SIMPLE for the load (restored at the end)")

    def _shared_plan(self, tables: List[Table], state: State, lead: bool) -> None:
        m = self.cfg.migration
        if not m.plan_file:
            raise MigrationError("sharding needs migration.plan_file on a volume shared by all shards")
        if state.data["tables"]:
            return                                    # shard restarted: keep its own progress
        plan_path = Path(m.plan_file)
        if lead and not plan_path.exists():
            plan = State(m.plan_file)
            self._plan_chunks(tables, plan)           # saves to plan_file
        waited = 0.0
        while not plan_path.exists():
            if waited > m.plan_wait_seconds:
                raise MigrationError(f"plan file {m.plan_file} did not appear (is shard 0 running?)")
            time.sleep(5)
            waited += 5
        plan = State.load_or_new(m.plan_file)
        for tname, t in plan.data["tables"].items():
            state.data["tables"][tname] = {"chunks": {i: {**c, "status": "pending", "rows": 0, "seconds": 0.0}
                                                      for i, c in t["chunks"].items()}}
        state.save()

    # -- phases ------------------------------------------------------------------
    def _preflight_target(self, tgt: Database, tables: List[Table]) -> None:
        policy = self.cfg.migration.if_target_not_empty
        existing = [t for t in tables if tgt.table_exists(t)]
        if self.cfg.migration.schema_mode == "skip":
            missing = [t.fqn for t in tables if t not in existing]
            if missing:
                raise MigrationError(f"schema_mode=skip but target is missing tables: {', '.join(missing[:10])}")
        if policy == "fail":
            nonempty = [t.fqn for t in existing if tgt.count_rows(t) > 0]
            if nonempty:
                raise MigrationError("target tables already contain data: " + ", ".join(nonempty[:10])
                                     + " (set if_target_not_empty = \"truncate\" or \"resume\")")
            if existing and self.cfg.migration.schema_mode == "create":
                raise MigrationError("target tables already exist: " + ", ".join(t.fqn for t in existing[:10])
                                     + " (drop them, use schema_mode = \"skip\", or resume)")

    def _create_schema(self, tgt: Database, tables: List[Table], modules: List[ModuleDef], res: RunResult) -> None:
        self.progress("creating schemas, types, sequences and tables on target")
        schemas = sorted({t.schema for t in tables} | {mm.schema for mm in modules})
        for s in schemas:
            if s != "dbo":
                self._ddl(tgt, f"IF SCHEMA_ID(N'{s.replace(chr(39), chr(39)*2)}') IS NULL "
                               f"EXEC('CREATE SCHEMA {ddl.q(s).replace(chr(39), chr(39)*2)}')", f"schema {s}", res)
        for mod in modules:
            if mod.kind in ("TYPE", "SEQUENCE") and mod.definition:
                self._ddl(tgt, mod.definition, f"{mod.kind} {mod.fqn}", res, ignore_exists=True)
        for t in tables:
            if not tgt.table_exists(t):
                self._ddl(tgt, ddl.create_table_sql(t), f"table {t.fqn}", res)
                if not self.cfg.migration.create_indexes_after_load:
                    for sql in ddl.post_load_sql(t):
                        self._ddl(tgt, sql, f"index/constraint on {t.fqn}", res, ignore_exists=True)

    def _post_load(self, src: Database, tgt: Database, tables: List[Table], modules: List[ModuleDef],
                   res: RunResult) -> None:
        self.progress("creating indexes and constraints")
        for t in tables:
            for sql in ddl.post_load_sql(t):
                self._ddl(tgt, sql, f"index/constraint on {t.fqn}", res, ignore_exists=True)
            cur = src.identity_current(t)
            if cur is not None:
                try:
                    tgt.reseed_identity(t, cur)
                except Exception as exc:
                    res.ddl_errors.append({"object": f"identity {t.fqn}", "error": str(exc)})
        for t in tables:
            for sql in ddl.fk_sql_all(t):
                self._ddl(tgt, sql, f"foreign key on {t.fqn}", res, ignore_exists=True)
        code = [mm for mm in modules if mm.kind not in ("TYPE", "SEQUENCE")]
        if code:
            self.progress(f"creating {len(code)} views/procedures/functions/synonyms/triggers")
            self._create_modules(tgt, code, res)

    def _create_modules(self, tgt: Database, modules: List[ModuleDef], res: RunResult) -> None:
        """Dependency order is unknown up front: retry failed objects until no progress.
        Triggers go last so they never fire during the load."""
        pending = [mm for mm in modules if mm.kind != "TRIGGER"] + [mm for mm in modules if mm.kind == "TRIGGER"]
        errors: Dict[str, str] = {}
        while pending:
            progress, nxt = False, []
            for mod in pending:
                if mod.definition is None:
                    errors[mod.fqn] = "definition not readable (WITH ENCRYPTION?)"
                    continue
                try:
                    tgt.execute(mod.definition)
                    if mod.kind == "TRIGGER" and mod.disabled and mod.parent:
                        tgt.execute(f"DISABLE TRIGGER {ddl.q(mod.schema)}.{ddl.q(mod.name)} ON "
                                    + ".".join(ddl.q(p) for p in mod.parent.split(".", 1)))
                    tgt.commit()
                    errors.pop(mod.fqn, None)
                    progress = True
                except Exception as exc:
                    tgt.rollback()
                    if "already an object named" in str(exc) or "already exists" in str(exc):
                        progress = True
                        continue
                    errors[mod.fqn] = str(exc)
                    nxt.append(mod)
            if not progress:
                break
            pending = nxt
        for name, err in errors.items():
            res.ddl_errors.append({"object": name, "error": err})

    def _ddl(self, tgt: Database, sql: str, what: str, res: RunResult, ignore_exists: bool = False) -> None:
        try:
            tgt.execute(sql)
            tgt.commit()
        except Exception as exc:
            tgt.rollback()
            msg = str(exc)
            if ignore_exists and ("already" in msg and ("exists" in msg or "object named" in msg)):
                return
            log.error("DDL failed for %s: %s", what, msg)
            res.ddl_errors.append({"object": what, "error": msg, "sql": sql[:2000]})

    def _plan_chunks(self, tables: List[Table], state: State) -> None:
        m = self.cfg.migration
        todo = [t for t in tables if state.chunks(t.fqn) is None]
        if not todo:
            return
        self.progress(f"planning chunks for {len(todo)} tables (~{m.chunk_rows:,} rows each)")
        with cf.ThreadPoolExecutor(max_workers=min(m.workers, len(todo))) as ex:
            futs = {ex.submit(_boundaries, self.factory, t, m.chunk_rows): t for t in todo}
            for fut in cf.as_completed(futs):
                t = futs[fut]
                state.set_chunks(t.fqn, build_chunks(t.fqn, fut.result()))
        _drop_connections()
        state.save()

    def _copy(self, tables: List[Table], state: State, res: RunResult) -> None:
        m = self.cfg.migration
        jobs = []
        for t in sorted(tables, key=lambda x: -x.row_count):   # biggest first = best packing
            chunks = state.chunks(t.fqn) or []
            res.tables[t.fqn] = TableRun(table=t.fqn, chunks=len(chunks))
            for c in chunks:
                if not state.chunk_done(c) and in_shard(c.id, m.shard_index, m.shard_count):
                    jobs.append((t, c, len(chunks) == 1))
        total = len(jobs)
        if not total:
            self.progress("all chunks already copied")
            self._fill_from_state(tables, state, res)
            return
        kind = self.cfg.resolved_executor()
        self.progress(f"copying {total} chunks with {m.workers} {kind} workers")
        Executor = cf.ProcessPoolExecutor if kind == "process" else cf.ThreadPoolExecutor
        t0, done_rows, done = time.perf_counter(), 0, 0
        by_id = {c.id: (t, c) for t, c, _ in jobs}
        with Executor(max_workers=m.workers) as ex:
            futs = [ex.submit(copy_chunk, self.factory, t, c, m.batch_rows,
                              m.tablock and single, single, m.max_retries)
                    for t, c, single in jobs]
            for fut in cf.as_completed(futs):
                try:
                    r = fut.result()
                except Exception as exc:  # pragma: no cover - worker crashed
                    log.error("worker crashed: %s", traceback.format_exc())
                    raise MigrationError(f"worker crashed: {exc}") from exc
                t, c = by_id[r.chunk_id]
                state.record(c, r.rows, r.seconds, r.status, r.error)
                state.save()
                done += 1
                if r.status == "done":
                    done_rows += r.rows
                else:
                    res.tables[t.fqn].failed_chunks.append(r.chunk_id)
                    res.tables[t.fqn].errors.append(r.error or "")
                    log.error("chunk %s failed: %s", r.chunk_id, r.error)
                el = time.perf_counter() - t0
                self.progress(f"[{done}/{total}] {r.chunk_id}: {r.rows:,} rows in {r.seconds:.1f}s "
                              f"| total {done_rows:,} rows, {done_rows / max(el, 1e-6):,.0f} rows/s")
        self._fill_from_state(tables, state, res)

    @staticmethod
    def _fill_from_state(tables: List[Table], state: State, res: RunResult) -> None:
        for t in tables:
            s = state.table_stats(t.fqn)
            tr = res.tables.setdefault(t.fqn, TableRun(table=t.fqn))
            tr.rows, tr.seconds, tr.chunks = s["rows"], s["seconds"], s["chunks"]


def shard_path(path: str, index: int, count: int) -> str:
    p = Path(path)
    return str(p.with_name(f"{p.stem}.shard{index + 1}of{count}{p.suffix or '.json'}"))


def in_shard(chunk_id: str, index: int, count: int) -> bool:
    """Stable assignment of chunks to shards (same on every pod, no coordination needed)."""
    return count <= 1 or zlib.crc32(chunk_id.encode("utf-8")) % count == index


def _identity_info() -> Dict[str, str]:
    try:
        from .sqlserver import identity_strategy_cache
        return identity_strategy_cache()
    except Exception:  # pragma: no cover
        return {}
