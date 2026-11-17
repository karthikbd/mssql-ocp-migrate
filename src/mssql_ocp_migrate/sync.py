"""Online catch-up for the logical path: apply the source's changes to the target after `migrate`,
so the application keeps running during the copy and cut-over takes minutes.

Uses SQL Server change tracking on the source (the same idea as Azure DMS's online mode, but
without reading the transaction log):

1. Change tracking is on for the source database and tables (``sync_enable_change_tracking`` turns
   it on before the copy; that changes the source).
2. ``migrate`` records ``CHANGE_TRACKING_CURRENT_VERSION()`` before it copies anything.
3. ``mssql-migrate sync`` reads ``CHANGETABLE(CHANGES ...)`` since that version and applies each
   changed key to the target: the target's row is deleted and the source's current row (if it still
   exists) inserted, in one transaction per table. It's idempotent, so passes can repeat.
4. ``mssql-migrate sync --final`` during the cut-over window (writes stopped): one last pass, then
   foreign keys re-trusted, identities and sequences moved to the source's values. Validate after.

During a pass, foreign keys between synced tables are switched off (applied in any order, no
cascades) and the target's DML triggers are disabled (they already ran on the source); both are
restored afterwards. Tables without a primary key, system-versioned tables and Always Encrypted
tables can't be synced row by row: re-copy them at cut-over (``migrate --truncate --include ...``).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .ddl import q, type_sql
from .model import Table

log = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


@dataclass
class SyncResult:
    from_version: int = 0
    to_version: int = 0
    tables: Dict[str, Dict[str, int]] = field(default_factory=dict)     # fqn -> {"deleted": n, "upserted": n}
    skipped: Dict[str, str] = field(default_factory=dict)               # fqn -> why
    final: bool = False
    seconds: float = 0.0

    @property
    def rows(self) -> int:
        return sum(v["upserted"] + v["deleted"] for v in self.tables.values())


def sync_state_path(cfg: Config) -> str:
    return cfg.migration.state_file + ".sync.json"


def load_version(cfg: Config) -> Optional[int]:
    """The version the next pass starts from: the last sync's, else the one migrate recorded."""
    p = sync_state_path(cfg)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return int(json.load(f)["version"])
    if os.path.exists(cfg.migration.state_file):
        with open(cfg.migration.state_file, encoding="utf-8") as f:
            v = json.load(f).get("change_tracking_version")
        return int(v) if v is not None else None
    return None


def save_version(cfg: Config, version: int, res: SyncResult) -> None:
    p = sync_state_path(cfg)
    data = {"version": version, "updated": time.time(), "passes": []}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data["passes"] = json.load(f).get("passes", [])[-49:]
    data["passes"].append({"from": res.from_version, "to": version, "rows": res.rows, "final": res.final,
                           "at": time.time()})
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def sync_skip_reason(t: Table) -> Optional[str]:
    if t.primary_key is None or not t.primary_key.columns:
        return "no primary key (change tracking needs one): re-copy it at cut-over"
    if t.temporal is not None:
        return "system-versioned: re-copy it at cut-over"
    if t.has_encrypted:
        return "Always Encrypted columns: re-copy it at cut-over"
    if t.memory_optimized:
        return "memory-optimized tables can't use change tracking: re-copy it at cut-over"
    return None


def changes_sql(src, t: Table) -> str:
    """Every key changed since a version, with the source's current row if it still exists."""
    keys = [c for c, _ in t.primary_key.columns]
    cols = t.insert_columns
    exprs = src.select_exprs(t, cols)
    inner = ", ".join([f"{q(k)} AS {q('__k' + str(i))}" for i, k in enumerate(keys)]
                      + [f"{e} AS {q('__c' + str(i))}" for i, e in enumerate(exprs)])
    on = " AND ".join(f"t.{q('__k' + str(i))} = ct.{q(k)}" for i, k in enumerate(keys))
    return (f"SELECT {', '.join('ct.' + q(k) for k in keys)}, CASE WHEN t.{q('__k0')} IS NULL THEN 0 ELSE 1 END, "
            + ", ".join(f"t.{q('__c' + str(i))}" for i in range(len(cols)))
            + f" FROM CHANGETABLE(CHANGES {src.qualified(t)}, ?) AS ct"
            + f" LEFT JOIN (SELECT {inner} FROM {src.qualified(t)}) AS t ON {on}")


class Syncer:
    def __init__(self, cfg: Config, factory=None, progress: Optional[Callable[[str], None]] = None):
        from .engine import SqlServerFactory
        self.cfg = cfg
        self.factory = factory or SqlServerFactory(cfg)
        self.progress = progress or (lambda m: log.info(m))

    def run(self, final: bool = False) -> SyncResult:
        from .engine import Migrator
        t0 = time.time()
        start = load_version(self.cfg)
        if start is None:
            raise SyncError("no change-tracking version recorded: enable change tracking on the source database and "
                            "tables (or set migration.sync_enable_change_tracking = true) before running migrate")
        tables = Migrator(self.cfg, self.factory, self.progress).source_tables()
        src, tgt = self.factory("source", True), self.factory("target", False)
        res = SyncResult(from_version=start, final=final)
        try:
            to = int(src.query("SELECT CHANGE_TRACKING_CURRENT_VERSION()")[0][0] or 0)
            tracked = {(s, n): mv for s, n, mv in src.query("""
                SELECT OBJECT_SCHEMA_NAME(object_id), OBJECT_NAME(object_id), CHANGE_TRACKING_MIN_VALID_VERSION(object_id)
                FROM sys.change_tracking_tables""")}
            todo: List[Table] = []
            for t in tables:
                why = sync_skip_reason(t)
                if why is None and (t.schema, t.name) not in tracked:
                    why = "change tracking isn't enabled on this table on the source"
                elif why is None and tracked[(t.schema, t.name)] is not None and int(tracked[(t.schema, t.name)]) > start:
                    why = ("its changes since the last sync were cleaned up (retention too short): "
                           "re-copy it with migrate --truncate --include " + t.fqn)
                if why:
                    res.skipped[t.fqn] = why
                else:
                    todo.append(t)
            self.progress(f"sync: version {start} -> {to}, {len(todo)} tables"
                          + (f", {len(res.skipped)} skipped" if res.skipped else ""))
            fks = self._disable_fks(tgt, todo)
            triggers = self._disable_triggers(tgt, todo)
            try:
                for t in todo:
                    res.tables[t.fqn] = self._apply(src, tgt, t, start)
            finally:
                self._enable_triggers(tgt, triggers)
                self._enable_fks(tgt, fks, trust=final)
            if final:
                self._finalize(src, tgt, todo)
            res.to_version = to
            save_version(self.cfg, to, res)
        finally:
            src.close()
            tgt.close()
        res.seconds = time.time() - t0
        self.progress(f"sync: {res.rows:,} rows applied in {res.seconds:,.1f}s"
                      + (" (final pass: foreign keys trusted, identities and sequences aligned)" if final else ""))
        return res

    # -------------------------------------------------------------- one table
    def _apply(self, src, tgt, t: Table, since: int) -> Dict[str, int]:
        nkeys = len(t.primary_key.columns)
        rows = src.query(changes_sql(src, t), [since])
        if not rows:
            return {"deleted": 0, "upserted": 0}
        keys = [tuple(r[:nkeys]) for r in rows]
        present = [tuple(r[nkeys + 1:]) for r in rows if r[nkeys]]
        try:
            self._delete_keys(tgt, t, keys)
            if present:
                tgt.bulk_load(t, [present], False)
            tgt.commit()
        except Exception:
            tgt.rollback()
            raise
        self.progress(f"sync: {t.fqn}: {len(keys):,} changed keys, {len(present):,} rows written")
        return {"deleted": len(keys) - len(present), "upserted": len(present)}

    @staticmethod
    def _delete_keys(tgt, t: Table, keys: List[Tuple[Any, ...]], batch: int = 300) -> None:
        cols = [t.column(c) for c, _ in t.primary_key.columns]
        cond = "(" + " AND ".join(f"{q(c.name)} = CAST(? AS {type_sql(c)})" for c in cols) + ")"
        per = max(1, min(batch, 2000 // max(1, len(cols))))
        for i in range(0, len(keys), per):
            part = keys[i:i + per]
            params = [v for k in part for v in k]
            tgt.execute(f"DELETE FROM {tgt.qualified(t)} WHERE " + " OR ".join(cond for _ in part), params)

    # -------------------------------------------------------------- constraints and triggers
    @staticmethod
    def _disable_fks(tgt, tables: List[Table]) -> List[Tuple[str, str, str]]:
        names = {(t.schema, t.name) for t in tables}
        fks = [(s, n, fk) for s, n, fk, rs, rn in tgt.query("""
            SELECT OBJECT_SCHEMA_NAME(parent_object_id), OBJECT_NAME(parent_object_id), name,
                   OBJECT_SCHEMA_NAME(referenced_object_id), OBJECT_NAME(referenced_object_id)
            FROM sys.foreign_keys WHERE is_disabled = 0""") if (s, n) in names or (rs, rn) in names]
        for s, n, fk in fks:
            tgt.execute(f"ALTER TABLE {q(s)}.{q(n)} NOCHECK CONSTRAINT {q(fk)}")
        tgt.commit()
        return fks

    @staticmethod
    def _enable_fks(tgt, fks: List[Tuple[str, str, str]], trust: bool) -> None:
        for s, n, fk in fks:
            # between passes: enabled but not re-validated (cheap); final pass: re-validated (trusted)
            tgt.execute(f"ALTER TABLE {q(s)}.{q(n)} {'WITH CHECK ' if trust else ''}CHECK CONSTRAINT {q(fk)}")
        tgt.commit()

    @staticmethod
    def _disable_triggers(tgt, tables: List[Table]) -> List[Tuple[str, str, str]]:
        names = {(t.schema, t.name) for t in tables}
        trs = [(s, n, tr) for s, n, tr in tgt.query("""
            SELECT OBJECT_SCHEMA_NAME(parent_id), OBJECT_NAME(parent_id), name FROM sys.triggers
            WHERE parent_class = 1 AND is_disabled = 0""") if (s, n) in names]
        for s, n, tr in trs:
            tgt.execute(f"DISABLE TRIGGER {q(s)}.{q(tr)} ON {q(s)}.{q(n)}")
        tgt.commit()
        return trs

    @staticmethod
    def _enable_triggers(tgt, trs: List[Tuple[str, str, str]]) -> None:
        for s, n, tr in trs:
            tgt.execute(f"ENABLE TRIGGER {q(s)}.{q(tr)} ON {q(s)}.{q(n)}")
        tgt.commit()

    def _finalize(self, src, tgt, tables: List[Table]) -> None:
        for t in tables:
            cur = src.identity_current(t)
            if cur is not None:
                tgt.reseed_identity(t, cur)
        for s, n, cur, inc in src.query("""SELECT SCHEMA_NAME(schema_id), name, CAST(current_value AS decimal(38,0)),
                                                  CAST(increment AS decimal(38,0)) FROM sys.sequences"""):
            if cur is not None:
                tgt.execute(f"ALTER SEQUENCE {q(s)}.{q(n)} RESTART WITH {int(cur) + int(inc)}")
        tgt.commit()


def enable_change_tracking(src, tables: List[Table], retention_days: int = 7) -> List[str]:
    """Turn change tracking on for the source database and every table with a primary key
    (sync_enable_change_tracking). This changes the source; it's opt-in."""
    done = []
    if not src.query("SELECT COUNT(*) FROM sys.change_tracking_databases WHERE database_id = DB_ID()")[0][0]:
        src.execute(f"ALTER DATABASE CURRENT SET CHANGE_TRACKING = ON (CHANGE_RETENTION = {int(retention_days)} DAYS, "
                    "AUTO_CLEANUP = ON)")
        done.append("database")
    have = {(s, n) for s, n in src.query("""SELECT OBJECT_SCHEMA_NAME(object_id), OBJECT_NAME(object_id)
                                           FROM sys.change_tracking_tables""")}
    for t in tables:
        if sync_skip_reason(t) is None and (t.schema, t.name) not in have:
            src.execute(f"ALTER TABLE {src.qualified(t)} ENABLE CHANGE_TRACKING")
            done.append(t.fqn)
    return done
