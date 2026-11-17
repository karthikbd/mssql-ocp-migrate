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
from .model import Chunk, ChunkResult, DatabaseExtras, SecurityPolicy, Table
from .state import State

log = logging.getLogger(__name__)

Factory = Callable[[str, bool], Database]   # (side, autocommit) -> Database


class MigrationError(RuntimeError):
    pass


# objects created before the tables, in the catalog's order (assemblies first, keys last)
SCHEMA_KINDS = {"ASSEMBLY", "XML SCHEMA COLLECTION", "TYPE", "SEQUENCE", "PARTITION FUNCTION", "PARTITION SCHEME",
                "COLUMN MASTER KEY", "COLUMN ENCRYPTION KEY", "RULE", "DEFAULT", "FULLTEXT CATALOG",
                "FULLTEXT STOPLIST"}
BROKER_KINDS = {"MESSAGE TYPE", "CONTRACT", "QUEUE", "SERVICE", "ROUTE", "REMOTE SERVICE BINDING",
                "EVENT NOTIFICATION"}
# objects created after security and settings (they need users, broker objects or must not fire earlier)
LATE_KINDS = BROKER_KINDS | {"DDL TRIGGER", "PLAN GUIDE"}
FULLTEXT_KINDS = {"FULLTEXT CATALOG", "FULLTEXT STOPLIST"}
LATE_PERMISSION_CLASSES = {"MESSAGE TYPE", "CONTRACT", "SERVICE"}


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
    attempt, first_error = 0, None
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
            return ChunkResult(chunk.id, rows, time.perf_counter() - t0, retried=first_error)
        except Exception as exc:  # retry on a fresh connection
            try:
                _db(factory, "target").rollback()
            except Exception:
                pass
            _drop_connections()
            first_error = first_error or f"{type(exc).__name__}: {exc}"
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
        self.skipped_tables: List[tuple] = []

    # -- discovery ---------------------------------------------------------------
    def source_tables(self, src: Optional[Database] = None) -> List[Table]:
        """Catalog metadata of the source tables selected by ``include``/``exclude`` (cached).
        FileTables, graph and external tables can't be copied row by row: they're left out and
        listed in ``skipped_tables``."""
        if self._tables is None:
            own = src is None
            src = src or self.factory("source", True)
            try:
                self._tables, self.skipped_tables = [], []
                for t in src.list_tables():
                    if not self.cfg.selects(t.schema, t.name):
                        continue
                    why = next((w for w in t.warnings if "skipped" in w), None)
                    if why:
                        self.skipped_tables.append((t.fqn, why))
                    else:
                        self._tables.append(t)
            finally:
                if own:
                    src.close()
        return self._tables

    def _modules(self, src: Database) -> List[ModuleDef]:
        """Source objects to recreate, filtered by the copy switches."""
        m = self.cfg.migration
        if not m.copy_programmable_objects:
            return []
        out = []
        for mod in src.list_modules():
            if mod.kind in BROKER_KINDS and not m.copy_service_broker:
                continue
            if mod.kind in FULLTEXT_KINDS and not m.copy_full_text:
                continue
            out.append(mod)
        return out

    def _schema_module_sql(self, mod: ModuleDef) -> List[str]:
        """Statements for an object created before the tables (assembly trust, partition schemes)."""
        m = self.cfg.migration
        if mod.kind == "PARTITION SCHEME":
            return [ddl.partition_scheme_sql(mod.name, mod.meta["function"], mod.meta["filegroups"],
                                             m.partition_filegroups)]
        out = []
        if mod.kind == "ASSEMBLY" and m.trust_clr_assemblies and mod.meta.get("sha512"):
            h = "0x" + mod.meta["sha512"]
            out.append(f"IF OBJECT_ID('sys.trusted_assemblies') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
                       f"sys.trusted_assemblies WHERE hash = {h}) EXEC sys.sp_add_trusted_assembly {h}, "
                       f"{ddl.lit(mod.name)}")
        if mod.definition:
            out.append(mod.definition)
        return out

    def schema_script(self) -> str:
        """Full DDL script (what `migrate` would run), for review or a DBA hand-off. It assumes a
        new, empty target database; the run itself checks the target (logins, settings) first."""
        mc = self.cfg.migration
        src = self.factory("source", True)
        try:
            tables = self.source_tables(src)
            modules = self._modules(src)
            schemas = {t.schema for t in tables} | {m.schema for m in modules if m.schema}
            extras = src.database_extras()
            if extras is not None and mc.copy_security:
                schemas |= set(src.list_schemas())
            bindings = getattr(src, "rule_default_bindings", lambda: [])()
        finally:
            src.close()
        extras = scoped_extras(extras, tables, modules) if extras is not None else None
        go = ";\nGO"
        out = ["-- generated by mssql-ocp-migrate", ""]
        out += [f"IF SCHEMA_ID(N'{s}') IS NULL EXEC('CREATE SCHEMA {ddl.q(s)}'){go}" for s in sorted(schemas)
                if s != "dbo"]
        for mod in modules:
            if mod.kind in SCHEMA_KINDS:
                out += [x + go for x in self._schema_module_sql(mod)]
        out += [self._create_table(t) + go for t in tables]
        out.append("-- ===== after data load =====")
        for t in tables:
            out += [x + go for x in self._table_post_sql(t)]
        for t in tables:
            out += [x + go for x in ddl.fk_sql_all(t)]
        for mod in modules:
            if mod.kind not in SCHEMA_KINDS and mod.kind not in LATE_KINDS and mod.definition:
                out.append(mod.definition.strip() + "\nGO")
        out += [x + go for x in bindings]
        if extras is not None:
            out.append("-- ===== security, extended properties, database settings =====")
            out += [x + go for x in self._extras_sql(extras)]
            out += [x + go for t in tables if mc.restore_system_versioning for x in ddl.temporal_sql(t)]
            if mc.copy_database_settings:
                out += [x + go for x in ddl.database_settings_sql(extras.settings, type(extras.settings)(),
                                                                   mc.compatibility_level)]
            out += [x + go for x in self._after_settings_sql(tables, extras)]
        for mod in modules:
            if mod.kind in LATE_KINDS and mod.definition:
                out.append(mod.definition.strip() + "\nGO")
        if extras is not None and mc.copy_security:
            out += [ddl.permission_sql(p) + go for p in extras.permissions
                    if p.securable_class in LATE_PERMISSION_CLASSES]
        if extras is not None and mc.copy_row_level_security:
            out += [ddl.policy_state_sql(p) + go for p in extras.security_policies if p.enabled]
        return "\n".join(out) + "\n"

    def _build_options(self) -> ddl.BuildOptions:
        m = self.cfg.migration
        return ddl.BuildOptions(copy_options=m.copy_index_options, sort_in_tempdb=m.index_sort_in_tempdb,
                                maxdop=m.index_maxdop, online=m.index_online)

    def _create_table(self, t: Table) -> str:
        m = self.cfg.migration
        return ddl.create_table_sql(t, m.copy_index_options, m.copy_data_masking, m.memory_optimized == "keep",
                                    True)

    def _table_post_sql(self, t: Table) -> List[str]:
        """Indexes, XML/spatial indexes, statistics, checks and the full-text index of one table."""
        m = self.cfg.migration
        out = ddl.post_load_sql(t, self._build_options(), m.memory_optimized == "keep")
        if m.copy_statistics:
            out += [ddl.statistics_sql(t, st, m.statistics_fullscan) for st in t.statistics]
        if m.copy_full_text and t.fulltext:
            out.append(ddl.fulltext_index_sql(t))
        return out

    def _extras_sql(self, ex: DatabaseExtras) -> List[str]:
        """Certificates, security, signatures, extended properties and RLS (disabled) in order."""
        m, out = self.cfg.migration, []
        if m.copy_certificates:
            if ex.certificates and any(c.private_key for c in ex.certificates):
                out.append("CREATE MASTER KEY ENCRYPTION BY PASSWORD = '<value of master_key_password_env>'")
            out += [ddl.certificate_sql(c) for c in ex.certificates]
        if m.copy_security:
            out += [x for x in (ddl.principal_sql(p) for p in sorted(ex.principals, key=lambda p: p.kind != "ROLE"))
                    if x]
            out += [x for x in (ddl.role_owner_sql(p) for p in ex.principals) if x]
            if m.copy_certificates:
                out += [ddl.owner_change_sql("CERTIFICATE", c.name, c.owner) for c in ex.certificates if c.owner]
            out += [ddl.role_member_sql(r, mem) for r, mem in ex.role_members]
            out += [ddl.schema_owner_sql(sch, own) for sch, own in ex.schema_owners.items()]
            out += [ddl.permission_sql(p) for p in ex.permissions if p.securable_class not in LATE_PERMISSION_CLASSES]
        if m.copy_certificates:
            out += [ddl.signature_sql(sg) for sg in ex.signatures]
        if m.copy_extended_properties:
            out += [ddl.extended_property_sql(e) for e in ex.extended_properties]
        if m.copy_row_level_security:
            out += [x for x in (ddl.security_policy_sql(p) for p in ex.security_policies) if x]
        return out

    def _after_settings_sql(self, tables: List[Table], ex: DatabaseExtras) -> List[str]:
        """After the database settings: table change tracking, CDC, Service Broker permissions."""
        m, out = self.cfg.migration, []
        if m.copy_change_tracking:
            out += [x for x in (ddl.change_tracking_sql(t) for t in tables) if x]
        if m.copy_cdc and ex.cdc_tables:
            out += ddl.cdc_sql(ex.cdc_tables)
        return out

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
            sc, tc = res.source_info.get("collation"), res.target_info.get("collation")
            if sc and tc and sc != tc:
                res.warnings.append(f"database collation differs: source {sc}, target {tc}. Columns keep their "
                                    "own collation, but temp tables, variables and literals follow the target's "
                                    "default; create the target database with COLLATE " + sc)
            tables = self.source_tables(src)
            if not tables:
                raise MigrationError("no tables matched include/exclude filters")
            for t in tables:
                res.warnings.extend(f"{t.fqn}: {w}" for w in t.warnings)
            for fqn, why in self.skipped_tables:
                res.ddl_errors.append({"object": f"table {fqn}", "error": f"not migrated: {why}"})
            total_rows = sum(t.row_count for t in tables)
            self.progress(f"{len(tables)} tables, ~{total_rows:,} rows, "
                          f"~{sum(t.reserved_mb for t in tables):,.0f} MB on source"
                          + (f" | shard {m.shard_index + 1}/{m.shard_count}" if sharded else ""))
            modules = self._modules(src)
            if lead and not state.phase_done("schema"):
                self._preflight_source(src, tables, res)
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
            modules = self._modules(src)
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
                    extras = self._post_load(src, tgt, tables, modules, res)
                    if m.copy_database_settings:
                        self._database_settings(src, tgt, res)
                    self._after_settings(src, tgt, tables, modules, extras, res)
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
        if m.schema_mode == "create" and m.copy_row_level_security and state.phase_done("post_load") \
                and not state.phase_done("policies"):
            self._enable_policies(tables, modules, res)
            state.mark_phase("policies")

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

    def _preflight_source(self, src: Database, tables: List[Table], res: RunResult) -> None:
        """Refuse to copy data the migration login can't fully see, and point out server-level
        objects that stay behind."""
        m = self.cfg.migration
        masked = [f"{t.fqn}.{c.name}" for t in tables for c in t.columns if c.masking_function]
        if masked and hasattr(src, "can_unmask") and not src.can_unmask():
            raise MigrationError(
                f"{len(masked)} masked columns (e.g. {', '.join(masked[:3])}) would be copied as masked values: "
                "grant UNMASK to the migration login on the source (or use a db_owner login)")
        hidden = getattr(src, "rls_hidden_rows", lambda t: [])(tables)
        if hidden:
            raise MigrationError(
                "row-level security hides rows from the migration login: "
                + ", ".join(f"{t} ({v:,} of {n:,} rows visible)" for t, v, n in hidden[:5])
                + ". Run as a login the predicate functions let through, or switch the policies off during the copy")
        selected = {(t.schema, t.name) for t in tables}
        for t in tables:
            if t.temporal and (t.temporal.history_schema, t.temporal.history_table) not in selected:
                res.warnings.append(f"{t.fqn}: history table {t.temporal.history_schema}.{t.temporal.history_table} "
                                    "isn't selected, so system versioning can't be re-enabled")
                t.temporal = None
        if not hasattr(src, "agent_jobs"):
            return
        if not m.copy_agent_jobs:
            try:
                jobs = src.agent_jobs(self.cfg.source.database, self.cfg.target.database)
                if jobs:
                    res.warnings.append(f"{len(jobs)} SQL Agent jobs run steps in this database ("
                                        + ", ".join(j[0] for j in jobs[:5])
                                        + "): set copy_agent_jobs = true to copy them (created disabled)")
            except Exception:
                pass
        if not m.copy_linked_servers:
            try:
                links = src.linked_servers()
                if links:
                    res.warnings.append(f"{len(links)} linked servers on the source (" + ", ".join(x[0] for x in links[:5])
                                        + "): set copy_linked_servers = true to copy their definitions")
            except Exception:
                pass

    def _create_schema(self, tgt: Database, tables: List[Table], modules: List[ModuleDef], res: RunResult) -> None:
        m = self.cfg.migration
        self.progress("creating schemas, types, sequences, partitioning and tables on target")
        schemas = {t.schema for t in tables} | {mm.schema for mm in modules if mm.schema}
        if m.copy_security:       # owners and GRANTs can refer to schemas with no tables
            src = self.factory("source", True)
            try:
                schemas |= set(src.list_schemas())
            finally:
                src.close()
        for s in sorted(schemas):
            if s != "dbo":
                self._ddl(tgt, f"IF SCHEMA_ID(N'{s.replace(chr(39), chr(39)*2)}') IS NULL "
                               f"EXEC('CREATE SCHEMA {ddl.q(s).replace(chr(39), chr(39)*2)}')", f"schema {s}", res)
        if m.memory_optimized == "keep" and any(t.memory_optimized for t in tables):
            self._memory_optimized_filegroup(tgt, res)
        if any(t.has_encrypted for t in tables):
            # ciphertext is copied as is: the server must accept already-encrypted values
            self._ddl(tgt, "DECLARE @s nvarchar(max) = N'ALTER USER ' + QUOTENAME(USER_NAME()) + "
                           "N' WITH ALLOW_ENCRYPTED_VALUE_MODIFICATIONS = ON'; EXEC (@s)",
                      "allow encrypted value modifications", res)
        if any(mm.kind in FULLTEXT_KINDS for mm in modules) and not self._fulltext_installed(tgt, res):
            modules[:] = [mm for mm in modules if mm.kind not in FULLTEXT_KINDS]
            for t in tables:
                t.fulltext = None
        for mod in modules:
            if mod.kind in SCHEMA_KINDS:
                for sql in self._schema_module_sql(mod):
                    self._ddl(tgt, sql, f"{mod.kind.lower()} {mod.fqn.strip('.')}", res, ignore_exists=True)
        for t in tables:
            if not tgt.table_exists(t):
                self._ddl(tgt, self._create_table(t), f"table {t.fqn}", res)
                if not m.create_indexes_after_load:
                    for sql in ddl.post_load_sql(t, self._build_options(), m.memory_optimized == "keep"):
                        self._ddl(tgt, sql, f"index/constraint on {t.fqn}", res, ignore_exists=True)

    def _memory_optimized_filegroup(self, tgt: Database, res: RunResult) -> None:
        """Memory-optimized tables need a MEMORY_OPTIMIZED_DATA filegroup, and the chunk
        transactions need snapshot isolation on them (MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT)."""
        if hasattr(tgt, "memory_optimized_filegroup") and not tgt.memory_optimized_filegroup():
            folder = tgt.primary_data_dir() + "mom_inmem"
            self.progress(f"adding a MEMORY_OPTIMIZED_DATA filegroup ({folder}) to the target")
            self._ddl(tgt, "ALTER DATABASE CURRENT ADD FILEGROUP [mom_inmem] CONTAINS MEMORY_OPTIMIZED_DATA",
                      "memory-optimized filegroup", res)
            self._ddl(tgt, "DECLARE @s nvarchar(max) = N'ALTER DATABASE ' + QUOTENAME(DB_NAME()) + "
                           f"N' ADD FILE (NAME = N''mom_inmem'', FILENAME = N''{folder}'') TO FILEGROUP [mom_inmem]'; "
                           "EXEC (@s)", "memory-optimized container", res)
        self._ddl(tgt, "ALTER DATABASE CURRENT SET MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT = ON",
                  "snapshot for memory-optimized tables", res)

    def _fulltext_installed(self, tgt: Database, res: RunResult) -> bool:
        query = getattr(tgt, "query", None)
        if query is None or query("SELECT CAST(SERVERPROPERTY('IsFullTextInstalled') AS int)")[0][0]:
            return True
        res.ddl_errors.append({"object": "full-text search",
                               "error": "the source uses full-text indexes but full-text search isn't installed on "
                                        "the target (add the mssql-server-fts package to the image), or set "
                                        "copy_full_text = false"})
        return False

    def _post_load(self, src: Database, tgt: Database, tables: List[Table], modules: List[ModuleDef],
                   res: RunResult) -> Optional[DatabaseExtras]:
        m = self.cfg.migration
        self.progress("creating indexes, statistics and constraints")
        for t in tables:
            for sql in self._table_post_sql(t):
                self._ddl(tgt, sql, f"index/statistics/constraint on {t.fqn}", res, ignore_exists=True)
            cur = src.identity_current(t)
            if cur is not None:
                try:
                    tgt.reseed_identity(t, cur)
                except Exception as exc:
                    if t.memory_optimized:      # DBCC CHECKIDENT doesn't support memory-optimized tables
                        res.warnings.append(f"{t.fqn}: identity not reseeded ({exc}); it continues after the "
                                            "highest copied value")
                    else:
                        res.ddl_errors.append({"object": f"identity {t.fqn}", "error": str(exc)})
        for t in tables:
            for sql in ddl.fk_sql_all(t):
                self._ddl(tgt, sql, f"foreign key on {t.fqn}", res, ignore_exists=True)
        code = [mm for mm in modules if mm.kind not in SCHEMA_KINDS and mm.kind not in LATE_KINDS]
        if code:
            self.progress(f"creating {len(code)} views/procedures/functions/aggregates/synonyms/triggers")
            self._create_modules(tgt, code, res)
        for sql in getattr(src, "rule_default_bindings", lambda: [])():
            self._ddl(tgt, sql, "rule/default binding", res)
        extras = src.database_extras()
        if extras is not None:
            extras = scoped_extras(extras, tables, modules)
            res.warnings.extend(extras.warnings)
            if m.copy_logins and m.copy_security:
                self._copy_logins(src, tgt, extras, res)
            self._apply_extras(tgt, extras, res)
        if m.restore_system_versioning:
            for t in tables:
                for sql in ddl.temporal_sql(t):
                    self._ddl(tgt, sql, f"system versioning on {t.fqn}", res)
        return extras

    def _copy_logins(self, src: Database, tgt: Database, ex: DatabaseExtras, res: RunResult) -> None:
        if not hasattr(src, "server_logins"):
            return
        query = getattr(tgt, "query")
        missing = [p for p in ex.principals if p.login and not query("SELECT SUSER_ID(?)", [p.login])[0][0]]
        if not missing:
            return
        logins, errors = src.server_logins(missing)
        for e in errors:
            res.ddl_errors.append({"object": "login", "error": e})
        for name, sql in logins:
            self.progress(f"creating login {name} (same SID and password)")
            self._ddl(tgt, sql, f"login {name}", res, ignore_exists=True)

    def _apply_extras(self, tgt: Database, ex: DatabaseExtras, res: RunResult) -> None:
        m = self.cfg.migration
        query = getattr(tgt, "query", None)
        if m.copy_certificates and ex.certificates:
            if any(c.private_key for c in ex.certificates) and query is not None and not query(
                    "SELECT COUNT(*) FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##'")[0][0]:
                import os
                pwd = os.environ.get(m.master_key_password_env)
                if pwd:
                    self._ddl(tgt, ddl.master_key_sql(pwd), "database master key", res)
                else:
                    res.ddl_errors.append({"object": "database master key", "error":
                                           f"certificates with private keys need a database master key on the "
                                           f"target: set the environment variable {m.master_key_password_env}"})
            for c in ex.certificates:
                self._ddl(tgt, ddl.certificate_sql(c), f"certificate {c.name}", res, ignore_exists=True)
        skipped = set()
        if m.copy_security:
            self.progress(f"creating {len(ex.principals)} users/roles, {len(ex.role_members)} role memberships "
                          f"and {len(ex.permissions)} permissions")
            for p in sorted(ex.principals, key=lambda p: p.kind != "ROLE"):     # roles first
                sql = ddl.principal_sql(p)
                why = None
                if sql is None:
                    why = {"CONTAINED": "contained database user with a password: recreate it with a new "
                                        "password (its hash can't be copied)",
                           "ORPHANED": "its login can't be found on the source: the user is orphaned, or the "
                                       "migration login lacks VIEW ANY DEFINITION",
                           "ASYMMETRIC_KEY_USER": "created from an asymmetric key, which can't be exported"}.get(
                        p.kind, f"{p.kind.lower()} principal can't be recreated from metadata")
                elif p.login and query is not None and not query("SELECT SUSER_ID(?)", [p.login])[0][0]:
                    why = (f"login {p.login} doesn't exist on the target (or isn't visible without VIEW ANY "
                           "DEFINITION): create it first (copy_logins does this with the same SID and password), "
                           "or set copy_security = false")
                if why:
                    skipped.add(p.name)
                    res.ddl_errors.append({"object": f"user {p.name}", "error": why})
                    continue
                self._ddl(tgt, sql, f"principal {p.name}", res, ignore_exists=True)
            for p in ex.principals:
                sql = ddl.role_owner_sql(p)
                if sql and p.owner not in skipped:
                    self._ddl(tgt, sql, f"owner of role {p.name}", res)
            if m.copy_certificates:
                for c in ex.certificates:
                    if c.owner and c.owner not in skipped:
                        self._ddl(tgt, ddl.owner_change_sql("CERTIFICATE", c.name, c.owner),
                                  f"owner of certificate {c.name}", res)
            for role, member in ex.role_members:
                if role not in skipped and member not in skipped:
                    self._ddl(tgt, ddl.role_member_sql(role, member), f"member {member} of {role}", res)
            for sch, owner in ex.schema_owners.items():
                if owner not in skipped:
                    self._ddl(tgt, ddl.schema_owner_sql(sch, owner), f"owner of schema {sch}", res)
            for perm in ex.permissions:
                if perm.grantee not in skipped and perm.securable_class not in LATE_PERMISSION_CLASSES:
                    self._ddl(tgt, ddl.permission_sql(perm), f"permission {perm.permission} to {perm.grantee}", res)
        self._skipped_principals = skipped
        if m.copy_certificates:
            for sg in ex.signatures:
                self._ddl(tgt, ddl.signature_sql(sg), f"signature on {sg.schema}.{sg.name}", res, ignore_exists=True)
        if m.copy_extended_properties and ex.extended_properties:
            self.progress(f"copying {len(ex.extended_properties)} extended properties")
            for ep in ex.extended_properties:
                self._ddl(tgt, ddl.extended_property_sql(ep),
                          f"extended property {ep.name} on {'.'.join(n for _, n in ep.level) or 'database'}",
                          res, ignore_exists=True)
        if m.copy_row_level_security:
            for pol in ex.security_policies:
                sql = ddl.security_policy_sql(pol)
                if sql:
                    self._ddl(tgt, sql, f"security policy {pol.schema}.{pol.name}", res, ignore_exists=True)

    def _database_settings(self, src: Database, tgt: Database, res: RunResult) -> None:
        se, te = src.database_extras(), tgt.database_extras()
        if se is None or te is None:
            return
        for sql in ddl.database_settings_sql(se.settings, te.settings, self.cfg.migration.compatibility_level):
            self.progress(sql)
            try:
                tgt.execute(sql)
                tgt.commit()
            except Exception as exc:
                tgt.rollback()
                if "COMPATIBILITY_LEVEL" in sql:
                    res.warnings.append(f"could not set the compatibility level ({exc}); the target keeps "
                                        f"level {te.settings.compatibility_level}")
                elif "SCOPED CONFIGURATION" in sql:
                    res.warnings.append(f"could not apply {sql} ({exc})")
                else:
                    res.ddl_errors.append({"object": "database settings", "error": str(exc), "sql": sql})
        owner = se.settings.owner
        if owner and owner != te.settings.owner and owner != "sa":
            query = getattr(tgt, "query")
            if not query("SELECT SUSER_ID(?)", [owner])[0][0]:
                res.warnings.append(f"database owner {owner} doesn't exist on the target; the owner stays "
                                    f"{te.settings.owner}")
            else:
                try:
                    tgt.execute(ddl.owner_sql(owner))
                except Exception as exc:
                    res.warnings.append(f"could not make {owner} the database owner ({exc})")

    def _after_settings(self, src: Database, tgt: Database, tables: List[Table], modules: List[ModuleDef],
                        extras: Optional[DatabaseExtras], res: RunResult) -> None:
        """Change tracking and CDC (need the database settings), then Service Broker, DDL triggers,
        plan guides and permissions on broker objects, then server-level objects."""
        m = self.cfg.migration
        if extras is not None:
            for sql in self._after_settings_sql(tables, extras):
                self._ddl(tgt, sql, "change tracking / change data capture", res, ignore_exists=True)
        late = [mm for mm in modules if mm.kind in LATE_KINDS]
        if late:
            self.progress(f"creating {len(late)} Service Broker objects, DDL triggers and plan guides")
            self._create_modules(tgt, late, res)
        if extras is not None and m.copy_security:
            skipped = getattr(self, "_skipped_principals", set())
            for perm in extras.permissions:
                if perm.securable_class in LATE_PERMISSION_CLASSES and perm.grantee not in skipped:
                    self._ddl(tgt, ddl.permission_sql(perm), f"permission {perm.permission} to {perm.grantee}", res)
        if any(t.has_encrypted for t in tables):
            self._ddl(tgt, "DECLARE @s nvarchar(max) = N'ALTER USER ' + QUOTENAME(USER_NAME()) + "
                           "N' WITH ALLOW_ENCRYPTED_VALUE_MODIFICATIONS = OFF'; EXEC (@s)",
                      "allow encrypted value modifications off", res)
        self._server_objects(src, tgt, res)

    def _server_objects(self, src: Database, tgt: Database, res: RunResult) -> None:
        m = self.cfg.migration
        query = getattr(tgt, "query", None)
        if m.copy_agent_jobs and hasattr(src, "agent_jobs"):
            try:
                jobs = src.agent_jobs(self.cfg.source.database, self.cfg.target.database)
            except Exception as exc:
                jobs = []
                res.ddl_errors.append({"object": "SQL Agent jobs", "error": f"msdb not readable on the source: {exc}"})
            for name, stmts, warns in jobs:
                res.warnings.extend(warns)
                if query("SELECT COUNT(*) FROM msdb.dbo.sysjobs WHERE name = ?", [name])[0][0]:
                    continue
                self.progress(f"creating SQL Agent job {name} (disabled)")
                for sql in stmts:
                    self._ddl(tgt, sql, f"agent job {name}", res)
            if jobs:
                res.warnings.append(f"{len(jobs)} SQL Agent jobs were created DISABLED on the target: enable them "
                                    "at cut-over and disable them on the source")
        if m.copy_linked_servers and hasattr(src, "linked_servers"):
            try:
                links = src.linked_servers()
            except Exception as exc:
                links = []
                res.ddl_errors.append({"object": "linked servers", "error": str(exc)})
            for name, stmts, warns in links:
                res.warnings.extend(warns)
                if query("SELECT COUNT(*) FROM sys.servers WHERE name = ?", [name])[0][0]:
                    continue
                for sql in stmts:
                    self._ddl(tgt, sql, f"linked server {name}", res)

    def _enable_policies(self, tables: List[Table], modules: List[ModuleDef], res: RunResult) -> None:
        """Row-level security policies are created switched off so they can't hide rows from the
        validation; this switches them to the source's state."""
        src, tgt = self.factory("source", True), self.factory("target", True)
        try:
            ex = src.database_extras()
            if ex is None:
                return
            for pol in scoped_extras(ex, tables, modules).security_policies:
                if pol.enabled and pol.predicates:
                    self.progress(f"switching on security policy {pol.schema}.{pol.name}")
                    self._ddl(tgt, ddl.policy_state_sql(pol), f"security policy {pol.schema}.{pol.name}", res)
        finally:
            src.close()
            tgt.close()

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
                    if mod.kind == "DDL TRIGGER" and mod.disabled:
                        tgt.execute(f"DISABLE TRIGGER {ddl.q(mod.name)} ON DATABASE")
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
            if ignore_exists and ("already" in msg and ("exists" in msg or "named" in msg)):
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
        if kind == "process":
            # spawn, not fork: forking a process that already runs threads can deadlock the child
            import multiprocessing
            pool = cf.ProcessPoolExecutor(max_workers=m.workers, mp_context=multiprocessing.get_context("spawn"))
        else:
            pool = cf.ThreadPoolExecutor(max_workers=m.workers)
        t0, done_rows, done = time.perf_counter(), 0, 0
        by_id = {c.id: (t, c) for t, c, _ in jobs}
        with pool as ex:
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
                    if r.retried:
                        note = f"{r.chunk_id} succeeded after a retry; first attempt: {r.retried}"
                        if note not in res.warnings:
                            res.warnings.append(note)
                        log.warning(note)
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


def scoped_extras(ex: DatabaseExtras, tables: List[Table], modules: List[ModuleDef]) -> DatabaseExtras:
    """Keep database-level items, and object-level items only for the tables and code being copied."""
    objects = {(t.schema, t.name) for t in tables} | {(m.schema, m.name) for m in modules}

    def in_scope(level) -> bool:
        return len(level) < 2 or (level[0][1], level[1][1]) in objects

    tables_in = {(t.schema, t.name) for t in tables}
    policies = []
    for p in ex.security_policies:
        preds = [x for x in p.predicates if (x[2], x[3]) in tables_in]
        if preds:
            policies.append(SecurityPolicy(p.schema, p.name, p.enabled, p.schemabinding, preds))
    return DatabaseExtras(
        settings=ex.settings, principals=list(ex.principals), role_members=list(ex.role_members),
        schema_owners=dict(ex.schema_owners),
        permissions=[p for p in ex.permissions
                     if p.securable_class != "OBJECT" or (p.schema, p.securable) in objects],
        extended_properties=[e for e in ex.extended_properties if in_scope(e.level)],
        security_policies=policies, certificates=list(ex.certificates),
        signatures=[sg for sg in ex.signatures if (sg.schema, sg.name) in objects],
        has_master_key=ex.has_master_key,
        cdc_tables=[c for c in ex.cdc_tables if (c.schema, c.table) in tables_in],
        warnings=list(ex.warnings))
