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
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapter import Database
from .config import Config
from .model import Chunk, DatabaseExtras, Table
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
TEXT_KINDS = {"VIEW", "PROCEDURE", "FUNCTION", "TRIGGER", "RULE", "DEFAULT", "DDL TRIGGER"}


def _norm_definition(text: Optional[str]) -> str:
    """Definitions compared without line-ending and trailing-whitespace differences."""
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").strip().split("\n"))


def module_diffs(src_modules: Any, tgt_modules: Any) -> List[str]:
    """Same text, same ANSI_NULLS / QUOTED_IDENTIFIER, same disabled state and trigger order
    for every view, procedure, function and trigger (not just "it exists")."""
    tm = {(m.kind, m.fqn): m for m in tgt_modules}
    out = []
    for s in src_modules:
        t = tm.get((s.kind, s.fqn))
        if t is None or s.kind not in TEXT_KINDS or s.definition is None:
            continue
        name = f"{s.kind.lower()} {s.fqn.strip('.')}"
        if _norm_definition(s.definition) != _norm_definition(t.definition):
            out.append(f"{name}: definition differs")
        sm, tmeta = s.meta or {}, t.meta or {}
        for key, label in (("ansi_nulls", "ANSI_NULLS"), ("quoted_identifier", "QUOTED_IDENTIFIER")):
            if key in sm and key in tmeta and sm[key] != tmeta[key]:
                out.append(f"{name}: {label} {'ON' if sm[key] else 'OFF'} -> {'ON' if tmeta[key] else 'OFF'}")
        if s.disabled != t.disabled:
            out.append(f"{name}: disabled {s.disabled} -> {t.disabled}")
        if sorted(sm.get("trigger_order", [])) != sorted(tmeta.get("trigger_order", [])):
            out.append(f"{name}: trigger order {sm.get('trigger_order', [])} -> {tmeta.get('trigger_order', [])}")
    return out

def _schema_diff(s: Table, t: Optional[Table], options: bool = False, statistics: bool = False,
                 m: Any = None) -> List[str]:
    """Differences between the source table ``s`` and the target table ``t``. ``m`` (the
    MigrationConfig) switches on the checks for each feature the migration copies."""
    if t is None:
        return ["table missing on target"]
    out = []
    tcols = {c.name: c for c in t.columns}
    attrs = ["type_name", "max_length", "precision", "scale", "nullable", "is_identity", "is_computed", "collation"]
    if m is not None:
        attrs += ["is_sparse", "is_column_set", "xml_schema", "xml_document", "encryption_type", "encryption_key",
                  "is_rowguidcol", "identity_not_for_replication"]
        if m.copy_data_masking:
            attrs.append("masking_function")
        if m.restore_system_versioning and s.temporal:
            attrs += ["generated_always", "is_hidden"]
    for c in s.columns:
        tc = tcols.pop(c.name, None)
        if tc is None:
            out.append(f"column {c.name} missing on target")
            continue
        for attr in attrs:
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
    if options:
        pairs = [(s.primary_key, t.primary_key)] if s.primary_key and t.primary_key else []
        tix = {i.name: i for i in t.indexes}
        pairs += [(i, tix[i.name]) for i in s.indexes if i.name in tix]
        for si, ti in pairs:
            a, b = si.options(), ti.options()
            out.extend(f"index {si.name}: {k} {a[k]!r} -> {b[k]!r}" for k in a if a[k] != b[k])
        has_ci = bool((s.primary_key and s.primary_key.clustered) or any(i.clustered for i in s.indexes))
        if not has_ci and s.heap_compression != t.heap_compression:
            out.append(f"heap compression {s.heap_compression} -> {t.heap_compression}")
    if m is not None:
        if m.memory_optimized == "keep" and (s.memory_optimized, s.durability) != (t.memory_optimized, t.durability):
            out.append(f"memory optimized {s.memory_optimized}/{s.durability} -> {t.memory_optimized}/{t.durability}")
        if m.restore_system_versioning and s.temporal and (
                t.temporal is None or (t.temporal.history_schema, t.temporal.history_table, t.temporal.retention)
                != (s.temporal.history_schema, s.temporal.history_table, s.temporal.retention)):
            out.append("system versioning not enabled like the source" if t.temporal is None
                       else "system versioning: history table or retention differs")
        if (s.partition_scheme, s.partition_column) != (t.partition_scheme, t.partition_column):
            out.append(f"heap partitioning {s.partition_scheme}({s.partition_column}) -> "
                       f"{t.partition_scheme}({t.partition_column})")
        for kind, sa, ta in (("XML index", {x.name for x in s.xml_indexes}, {x.name for x in t.xml_indexes}),
                             ("spatial index", {x.name for x in s.spatial_indexes}, {x.name for x in t.spatial_indexes})):
            out.extend(f"{kind} {n} missing on target" for n in sorted(sa - ta))
        if m.copy_full_text and s.fulltext and (t.fulltext is None or
                                                [c[0] for c in s.fulltext.columns] != [c[0] for c in t.fulltext.columns]):
            out.append("full-text index missing or different on target")
        if m.copy_change_tracking and s.change_tracking != t.change_tracking:
            out.append(f"change tracking {s.change_tracking} -> {t.change_tracking}")
    if statistics:
        tst = {st.name: st for st in t.statistics}
        for st in s.statistics:
            other = tst.get(st.name)
            if other is None:
                out.append(f"statistics {st.name} missing on target")
            elif other.columns != st.columns:
                out.append(f"statistics {st.name}: columns {st.columns} -> {other.columns}")
    return out


def extras_diff(cfg: Config, src: DatabaseExtras, tgt: DatabaseExtras) -> List[str]:
    """Database-level differences: settings, users/roles, memberships, schema owners, permissions,
    extended properties. ``src`` is already scoped to the tables and code being migrated."""
    m, out = cfg.migration, []
    if m.copy_database_settings:
        want = (src.settings.compatibility_level if m.compatibility_level == "source"
                else int(m.compatibility_level) if m.compatibility_level.isdigit() else None)
        if want and want != tgt.settings.compatibility_level:
            out.append(f"compatibility level {want} -> {tgt.settings.compatibility_level}")
        if src.settings.read_committed_snapshot != tgt.settings.read_committed_snapshot:
            out.append(f"READ_COMMITTED_SNAPSHOT {src.settings.read_committed_snapshot} -> "
                       f"{tgt.settings.read_committed_snapshot}")
        if src.settings.snapshot_isolation != tgt.settings.snapshot_isolation:
            out.append(f"ALLOW_SNAPSHOT_ISOLATION {src.settings.snapshot_isolation} -> "
                       f"{tgt.settings.snapshot_isolation}")
        for key, val in src.settings.options.items():
            if key in tgt.settings.options and tgt.settings.options[key] != val:
                out.append(f"database option {key} {val} -> {tgt.settings.options[key]}")
        for key, val in src.settings.scoped_configurations.items():
            if key in tgt.settings.scoped_configurations and tgt.settings.scoped_configurations[key] != val:
                out.append(f"scoped configuration {key} {val} -> {tgt.settings.scoped_configurations[key]}")
        if bool(src.settings.change_tracking) != bool(tgt.settings.change_tracking) and m.copy_change_tracking:
            out.append(f"change tracking {src.settings.change_tracking} -> {tgt.settings.change_tracking}")
        if src.settings.query_store != tgt.settings.query_store:
            out.append(f"Query Store {src.settings.query_store} -> {tgt.settings.query_store}")
    if m.copy_security:
        tp = {p.name: p for p in tgt.principals}
        for p in src.principals:
            if p.name not in tp:
                out.append(f"{'role' if p.kind == 'ROLE' else 'user'} {p.name} missing on target")
            elif (p.kind == "ROLE") != (tp[p.name].kind == "ROLE"):
                out.append(f"principal {p.name}: {p.kind} -> {tp[p.name].kind}")
        present = set(tp)
        tm = set(tgt.role_members)
        out.extend(f"{mem} is not a member of role {r} on target" for r, mem in src.role_members
                   if (r, mem) not in tm and mem in present)
        for sch, own in src.schema_owners.items():
            if tgt.schema_owners.get(sch) != own and own in present:
                out.append(f"schema {sch}: owner {own} -> {tgt.schema_owners.get(sch, 'dbo')}")
        towners = {(s, n): o for s, n, o in tgt.object_owners}
        for s, n, own in src.object_owners:
            if towners.get((s, n)) != own and own in present:
                out.append(f"object {s}.{n}: owner {own} -> {towners.get((s, n), 'schema owner')}")
        tk = {perm.key for perm in tgt.permissions}
        for perm in src.permissions:
            if perm.key not in tk and perm.grantee in present | {"public"}:
                what = perm.securable_class.lower() + (f" {perm.schema}" if perm.schema else "") + (
                    f".{perm.securable}" if perm.securable else "") + (f"({perm.column})" if perm.column else "")
                out.append(f"{perm.state} {perm.permission} on {what} to {perm.grantee} missing on target")
    if m.copy_row_level_security:
        tpol = {(pol.schema, pol.name): pol for pol in tgt.security_policies}
        for pol in src.security_policies:
            other = tpol.get((pol.schema, pol.name))
            if other is None:
                out.append(f"security policy {pol.schema}.{pol.name} missing on target")
                continue
            missing = set(pol.predicates) - set(other.predicates)
            out.extend(f"security policy {pol.schema}.{pol.name}: {x[0].lower()} predicate on {x[2]}.{x[3]} missing"
                       for x in sorted(missing))
    if m.copy_certificates:
        tc = {c.name for c in tgt.certificates}
        out.extend(f"certificate {c.name} missing on target" for c in src.certificates if c.name not in tc)
        ts = {(s.schema, s.name, s.certificate, s.counter) for s in tgt.signatures}
        out.extend(f"signature on {s.schema}.{s.name} by {s.certificate} missing on target"
                   for s in src.signatures if (s.schema, s.name, s.certificate, s.counter) not in ts)
    if m.copy_cdc:
        tcdc = {c.capture_instance for c in tgt.cdc_tables}
        out.extend(f"change data capture instance {c.capture_instance} missing on target"
                   for c in src.cdc_tables if c.capture_instance not in tcdc)
    if m.copy_extended_properties:
        te = {e.key: e.value for e in tgt.extended_properties}
        for e in src.extended_properties:
            where = ".".join(n for _, n in e.level) or "database"
            if e.key not in te:
                out.append(f"extended property {e.name} on {where} missing on target")
            elif te[e.key] != e.value:
                out.append(f"extended property {e.name} on {where}: value differs")
    return out


def _full_compare(src: Database, tgt: Database, table: Table, chunk: Chunk, batch: int,
                  max_diff: int) -> ChunkCheck:
    cols = table.compare_columns
    keys = table.key_columns
    h = RowHasher(cols, keys)
    cc = ChunkCheck(chunk.id, "full", True)
    if keys:
        side: Dict[bytes, Tuple[Any, ...]] = {}
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


def _check_chunk(cfg: Config, factory: Any, table: Table, chunk: Chunk, full: bool) -> ChunkCheck:
    from .engine import _db, _drop_connections
    try:
        src, tgt = _db(factory, "source"), _db(factory, "target")
        if table.has_encrypted:
            full = True       # encrypted columns can't be checksummed on the server: compare the ciphertext
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
    def __init__(self, cfg: Config, factory: Any, progress: Optional[Callable[[str], None]] = None) -> None:
        self.cfg = cfg
        self.factory: Callable[[str, bool], Any] = factory
        self.progress = progress or (lambda m: log.info(m))

    def _server_diffs(self, src: Database, tgt: Database) -> List[str]:
        """SQL Agent jobs and linked servers, when the migration copies them."""
        m = self.cfg.migration
        out: List[str] = []
        query: Any = getattr(tgt, "query", None)
        if query is None:
            return out
        try:
            if m.copy_agent_jobs and hasattr(src, "agent_jobs"):
                have = {r[0] for r in query("SELECT name FROM msdb.dbo.sysjobs")}
                out += [f"SQL Agent job {j[0]} missing on target"
                        for j in src.agent_jobs(self.cfg.source.database, self.cfg.target.database) if j[0] not in have]
            if m.copy_linked_servers and hasattr(src, "linked_servers"):
                have = {r[0] for r in query("SELECT name FROM sys.servers WHERE is_linked = 1")}
                out += [f"linked server {x[0]} missing on target" for x in src.linked_servers() if x[0] not in have]
        except Exception as exc:
            out.append(f"server objects not comparable: {exc}")
        return out

    def run(self, tables: List[Table], chunks: Dict[str, List[Chunk]],
            mode: Optional[str] = None) -> ValidationResult:
        """Validate ``tables`` using the given chunk plan; ``mode`` overrides ``validation.mode``."""
        mode = mode or self.cfg.validation.mode
        v = self.cfg.validation
        t0 = time.perf_counter()
        res = ValidationResult(mode=mode)
        src, tgt = self.factory("source", True), self.factory("target", True)
        try:
            mc = self.cfg.migration
            tgt_tables = {t.fqn: t for t in tgt.list_tables()} if v.compare_schema else {}
            for t in tables:
                tc = TableCheck(t.fqn)
                tc.source_rows = src.count_rows(t)
                tc.target_rows = tgt.count_rows(t) if tgt.table_exists(t) else -1
                if v.compare_schema:
                    tc.schema_diffs = _schema_diff(t, tgt_tables.get(t.fqn), mc.copy_index_options,
                                                   mc.copy_statistics, mc)
                res.tables[t.fqn] = tc
            from .engine import Migrator
            src_modules = Migrator(self.cfg, self.factory, self.progress)._modules(src)
            if v.compare_schema and mc.copy_programmable_objects:
                tgt_modules = tgt.list_modules()
                smods = {(m.kind, m.fqn) for m in src_modules}
                tmods = {(m.kind, m.fqn) for m in tgt_modules}
                res.object_diffs = [f"{k} {n} missing on target" for k, n in sorted(smods - tmods)]
                res.object_diffs += module_diffs(src_modules, tgt_modules)
                if hasattr(src, "unresolved_references") and hasattr(tgt, "unresolved_references"):
                    try:
                        new = tgt.unresolved_references() - src.unresolved_references()
                        res.object_diffs += [f"{a} references {b}, which doesn't exist on the target"
                                             for a, b in sorted(new)]
                    except Exception as exc:  # pragma: no cover - permissions
                        log.warning("reference check skipped: %s", exc)
                for t in tables:
                    a, b = src.identity_current(t), tgt.identity_current(t)
                    if a is not None and b is not None and a != b:
                        res.object_diffs.append(f"identity {t.fqn}: current value {a} -> {b}")
            if v.compare_schema and (mc.copy_security or mc.copy_extended_properties or mc.copy_database_settings):
                se, te = src.database_extras(), tgt.database_extras()
                if se is not None and te is not None:
                    from .engine import scoped_extras
                    res.object_diffs += extras_diff(self.cfg, scoped_extras(se, tables, src_modules), te)
            if v.compare_schema:
                res.object_diffs += self._server_diffs(src, tgt)
        finally:
            src.close()
            tgt.close()
        self.progress("row counts: " + ("all match" if all(
            c.source_rows == c.target_rows for c in res.tables.values()) else "MISMATCHES found"))

        if mode != "count":
            rnd = random.Random(20240601)  # noqa: S311 (sampling, not security)
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
