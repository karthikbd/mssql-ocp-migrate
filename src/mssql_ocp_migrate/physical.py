"""Physical "lift": the fast path for very large databases (tens to hundreds of TB).

Rows are never decoded. SQL Server ships its own pages:

  plan      sizes, msdb backup history (real compression ratio and speed), blockers, timeline
  seed      striped + compressed COPY_ONLY full backup  ->  RESTORE ... WITH NORECOVERY on OCP
            (files spread across several volumes, page checksums verified during the restore)
  catch-up  log backups restored continuously while the source stays online. Log backups that
            the existing backup jobs take are found in msdb and applied too, so the chain never
            breaks (the idea behind dbatools log shipping and sp_DatabaseRestore @ContinueLogs)
  cutover   tail-log backup (source goes to RESTORING = instant rollback path) -> last restore
            -> RECOVERY -> logins, owner, database options, orphaned users
  verify    metadata row counts for every table + sampled server-side checksums

Downtime is only the cut-over (minutes); the bulk copy runs while the source is live.
Everything runs as T-SQL over the TDS connection — no agent, no SSH, no downloads.
"""
from __future__ import annotations

import dataclasses
import decimal
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from .drivers import Connection

from .config import Config, ConnectionConfig, LiftConfig
from .ddl import q

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


class LiftError(RuntimeError):
    pass


def lit(s: str) -> str:
    """T-SQL N'...' literal."""
    return "N'" + str(s).replace("'", "''") + "'"


# ============================================================== pure SQL builders
def _join(base: str, name: str) -> str:
    if base.startswith("s3://"):
        return base.rstrip("/") + "/" + name
    sep = "\\" if ("\\" in base or (len(base) > 1 and base[1] == ":")) else "/"
    return base.rstrip("\\/") + sep + name


def backup_devices(base: str, stem: str, stripes: int) -> List[str]:
    return [_join(base, f"{stem}_{i:02d}of{stripes:02d}.bak") for i in range(1, stripes + 1)]


def _media(devices: Sequence[str]) -> str:
    kind = "URL" if devices and devices[0].startswith("s3://") else "DISK"
    return ", ".join(f"{kind} = {lit(d)}" for d in devices)


def tuning(lift: LiftConfig, devices: Sequence[str]) -> Tuple[int, int]:
    """(MAXTRANSFERSIZE, BUFFERCOUNT). Many buffers keep every stripe busy; memory is capped."""
    s3 = bool(devices) and devices[0].startswith("s3://")
    mts = 20_971_520 if s3 else min(max(lift.maxtransfersize, 65_536), 4_194_304)
    bc = lift.buffercount or len(devices) * 4
    cap = max(2, (lift.memory_mb * 1024 * 1024) // mts)
    return mts, int(max(2, min(bc, cap)))


def backup_full_sql(db: str, devices: Sequence[str], lift: LiftConfig, copy_only: Optional[bool] = None) -> str:
    mts, bc = tuning(lift, devices)
    opts = ["FORMAT", "INIT", "CHECKSUM", "STATS = 1", f"MAXTRANSFERSIZE = {mts}", f"BUFFERCOUNT = {bc}"]
    if lift.compression:
        opts.insert(0, "COMPRESSION")
    if lift.copy_only if copy_only is None else copy_only:
        opts.insert(0, "COPY_ONLY")
    return f"BACKUP DATABASE {q(db)} TO {_media(devices)} WITH {', '.join(opts)}"


def backup_log_sql(db: str, devices: Sequence[str], lift: LiftConfig, tail: bool = False) -> str:
    mts, bc = tuning(lift, devices)
    opts = ["FORMAT", "INIT", "CHECKSUM", f"MAXTRANSFERSIZE = {mts}", f"BUFFERCOUNT = {bc}"]
    if lift.compression:
        opts.insert(0, "COMPRESSION")
    if tail:
        opts.append("NORECOVERY")      # source -> RESTORING: no more writes, instant rollback possible
    return f"BACKUP LOG {q(db)} TO {_media(devices)} WITH {', '.join(opts)}"


@dataclass
class DbFile:
    logical: str
    physical: str
    type: str            # D data, L log, S filestream, F full-text
    size_bytes: int
    filegroup: Optional[str] = None


def move_plan(files: Sequence[DbFile], target_db: str, data_dirs: Sequence[str], log_dir: str) -> Dict[str, str]:
    """Spread data files over several volumes, biggest first onto the least-filled volume,
    so restore writes run in parallel on every PV."""
    if not data_dirs:
        raise LiftError("lift.target_data_dirs is empty")
    load = dict.fromkeys(data_dirs, 0)
    out: Dict[str, str] = {}
    used = set()
    for f in sorted(files, key=lambda x: -x.size_bytes):
        base = os.path.basename(f.physical.replace("\\", "/")) or f.logical
        _stem, ext = os.path.splitext(base)
        if f.type == "L":
            folder = log_dir
            ext = ext or ".ldf"
        else:
            folder = min(load, key=lambda d: load[d])
            load[folder] += f.size_bytes
            ext = ext if f.type != "S" else ""
            ext = ext or (".ndf" if f.type == "D" else "")
        name = f"{target_db}_{f.logical}{ext}"
        path = folder.rstrip("/") + "/" + name
        n = 1
        while path in used:
            n += 1
            path = folder.rstrip("/") + "/" + f"{target_db}_{f.logical}_{n}{ext}"
        used.add(path)
        out[f.logical] = path
    return out


def restore_full_sql(target_db: str, devices: Sequence[str], moves: Dict[str, str], lift: LiftConfig,
                     replace: bool = False) -> str:
    mts, bc = tuning(lift, devices)
    opts = [f"MOVE {lit(k)} TO {lit(v)}" for k, v in moves.items()]
    opts += ["NORECOVERY", "CHECKSUM", "STATS = 1", f"BUFFERCOUNT = {bc}"]
    if not devices[0].startswith("s3://"):
        opts.append(f"MAXTRANSFERSIZE = {mts}")
    if replace:
        opts.append("REPLACE")
    return f"RESTORE DATABASE {q(target_db)} FROM {_media(devices)} WITH {', '.join(opts)}"


def restore_log_sql(target_db: str, devices: Sequence[str]) -> str:
    return f"RESTORE LOG {q(target_db)} FROM {_media(devices)} WITH NORECOVERY, CHECKSUM"


def recover_sql(target_db: str) -> str:
    return f"RESTORE DATABASE {q(target_db)} WITH RECOVERY"


def s3_credential_sql(s3_url: str, access: str, secret: str) -> str:
    # credential name must be the URL prefix (s3://endpoint/bucket)
    parts = s3_url[len("s3://"):].split("/")
    name = "s3://" + "/".join(parts[:2])
    return (f"IF NOT EXISTS (SELECT 1 FROM sys.credentials WHERE name = {lit(name)}) "
            f"CREATE CREDENTIAL {q(name)} WITH IDENTITY = 'S3 Access Key', SECRET = {lit(access + ':' + secret)}")


def map_path(path: str, path_map: Dict[str, str]) -> str:
    """Translate a backup path recorded by the source (e.g. \\\\nas\\logs\\x.trn) to the target's view."""
    for src, dst in sorted(path_map.items(), key=lambda kv: -len(kv[0])):
        if path.lower().startswith(src.lower()):
            rest = path[len(src):].replace("\\", "/").lstrip("/")
            return dst.rstrip("/") + "/" + rest
    return path


# ============================================================== chain logic (pure)
@dataclass
class BackupSet:
    backup_set_id: int
    type: str                  # D full, I diff, L log
    first_lsn: int
    last_lsn: int
    database_backup_lsn: int
    finish: Any
    devices: List[str]
    device_type: int           # 2 disk, 9 URL, 7 virtual device (3rd-party tools)
    is_copy_only: bool = False
    size_bytes: int = 0


class ChainError(LiftError):
    pass


def next_logs(log_sets: Sequence[BackupSet], restored_lsn: int) -> List[BackupSet]:
    """Log backups to apply, in order, starting after ``restored_lsn`` (the last LSN already on target).

    Raises ChainError on a gap (a log backup missing, sent to NUL, or written by a VDI tool)."""
    out: List[BackupSet] = []
    cur = restored_lsn
    for b in sorted(log_sets, key=lambda x: (x.first_lsn, x.last_lsn)):
        if b.is_copy_only:
            continue                       # copy-only logs overlap the regular chain; not needed
        if b.last_lsn <= cur:
            continue                       # already covered
        if b.first_lsn > cur:
            raise ChainError(f"log chain gap: next log backup starts at LSN {b.first_lsn} but target is at "
                             f"{cur}. A log backup is missing or was taken to NUL.")
        if b.device_type == 7:
            raise ChainError("a log backup in the chain was taken by a third-party (virtual device) tool "
                             f"(backup_set_id {b.backup_set_id}); pause that job during the migration or "
                             "restore it with the vendor tool")
        if any(d.upper() == "NUL" or d.endswith("/dev/null") for d in b.devices):
            raise ChainError(f"backup_set_id {b.backup_set_id} was written to NUL: the log chain is broken; "
                             "take a new full backup (`lift seed --restart`)")
        out.append(b)
        cur = b.last_lsn
    return out


# ============================================================== planner (pure)
@dataclass
class DbPlan:
    database: str
    target_database: str
    data_gb: float
    log_gb: float
    files: int
    recovery_model: str
    compatibility_level: int
    encrypted: bool
    compression_ratio: float
    ratio_source: str
    backup_mb_s: Optional[float]
    log_gb_per_hour: Optional[float]
    other_log_backups_24h: int
    vdi_log_backups_24h: int
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    timeline: Dict[str, float] = field(default_factory=dict)


def estimate_timeline(p: DbPlan, lift: LiftConfig) -> Dict[str, float]:
    """Hours per stage. Uses measured history where present, else stated assumptions."""
    size_mb = p.data_gb * 1024
    comp_mb = size_mb / max(p.compression_ratio, 1.0)
    read_mb_s = p.backup_mb_s or 800.0                                   # source read + compress
    net_mb_s = lift.network_gbps * 1000 / 8 * 0.85 if lift.network_gbps else None
    backup_mb_s = read_mb_s if not net_mb_s else min(read_mb_s, net_mb_s * p.compression_ratio)
    write_mb_s = lift.target_write_mb_s or 1000.0
    restore_mb_s = write_mb_s if not net_mb_s else min(write_mb_s, net_mb_s * p.compression_ratio)
    backup_h = size_mb / backup_mb_s / 3600
    restore_h = size_mb / restore_mb_s / 3600
    copy_h = 0.0
    if lift.transport == "manual" and net_mb_s:
        copy_h = comp_mb / net_mb_s / 3600
    log_gb = (p.log_gb_per_hour or 5.0) * (backup_h + copy_h + restore_h)
    catchup_h = log_gb * 1024 / max(restore_mb_s, 1) / 3600
    cut_min = 5 + (p.log_gb_per_hour or 5.0) * lift.log_interval_seconds / 3600 * 1024 / max(restore_mb_s, 1) / 60
    return {"backup_h": round(backup_h, 2), "copy_h": round(copy_h, 2), "restore_h": round(restore_h, 2),
            "catchup_h": round(catchup_h, 2), "online_total_h": round(backup_h + copy_h + restore_h + catchup_h, 2),
            "downtime_min": round(cut_min, 1), "compressed_gb": round(comp_mb / 1024, 1)}


# ============================================================== state
class LiftState:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: Dict[str, Any] = {"databases": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._lock = threading.Lock()

    def db(self, name: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = self.data["databases"].setdefault(
            name, {"phase": "new", "restored_lsn": None, "logs": 0, "history": []})
        return entry

    def update(self, name: str, **kw: Any) -> None:
        with self._lock:
            d = self.db(name)
            d.update(kw)
            d["history"].append({"t": time.time(), **{k: v for k, v in kw.items() if k != "history"}})
            d["history"] = d["history"][-200:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.path)


# ============================================================== server I/O
def _master(cfg: ConnectionConfig) -> ConnectionConfig:
    c = dataclasses.replace(cfg, database="master", query_timeout=None)
    return c


class LongOp:
    """Run one long BACKUP/RESTORE on its own session; report percent complete from a second one."""

    def __init__(self, cfg: ConnectionConfig, progress: Progress, poll_seconds: float = 30.0, connect_fn: Any = None) -> None:
        self.cfg = _master(cfg)
        self.progress = progress
        self.poll = poll_seconds
        if connect_fn is None:
            from .drivers import connect as connect_fn
        self._connect: Callable[..., Any] = connect_fn

    def run(self, sql: str, label: str) -> float:
        connect = self._connect
        work = connect(self.cfg, autocommit=True)
        spid = work.scalar("SELECT @@SPID")
        err: List[BaseException] = []
        t0 = time.time()

        def target() -> None:
            try:
                work.execute(sql)
            except BaseException as exc:
                err.append(exc)

        th = threading.Thread(target=target, name=f"lift-{label}", daemon=True)
        th.start()
        mon = None
        try:
            mon = connect(self.cfg, autocommit=True)
            while th.is_alive():
                th.join(self.poll)
                if not th.is_alive():
                    break
                row = mon.query("SELECT percent_complete, estimated_completion_time FROM sys.dm_exec_requests "
                                "WHERE session_id = ?", [spid])
                if row:
                    pct, eta_ms = row[0]
                    self.progress(f"{label}: {float(pct or 0):5.1f}%  ~{(eta_ms or 0) / 60000:,.0f} min left")
        finally:
            th.join()
            if mon is not None:
                mon.close()
            work.close()
        if err:
            raise LiftError(f"{label} failed: {err[0]}") from err[0]
        secs = time.time() - t0
        self.progress(f"{label}: done in {secs / 60:,.1f} min")
        return secs


class Lift:
    """Physical migration of whole databases (see the module docstring for the phases).

    :param cfg: configuration; the ``[lift]`` section drives everything.
    :param progress: callback for progress lines.
    :param connect_fn: ``(ConnectionConfig, autocommit) -> Connection``; injectable for tests.
    """

    def __init__(self, cfg: Config, progress: Optional[Progress] = None, connect_fn: Any = None) -> None:
        self.cfg = cfg
        self.lift = cfg.lift
        self.progress = progress or (lambda m: log.info(m))
        from .drivers import connect
        self._connect = connect_fn or connect
        self.state = LiftState(self.lift.state_file)

    # -- connections -------------------------------------------------------------
    def src(self) -> "Connection":
        return self._connect(_master(self.cfg.source), autocommit=True)

    def tgt(self) -> "Connection":
        return self._connect(_master(self.cfg.target), autocommit=True)

    def target_name(self, db: str) -> str:
        return self.lift.rename.get(db, db)

    def _devices(self, side: str, stem: str, stripes: int) -> List[str]:
        lf = self.lift
        if lf.transport == "s3":
            if not lf.s3_url.startswith("s3://"):
                raise LiftError("lift.s3_url must start with s3://")
            return backup_devices(lf.s3_url, stem, stripes)
        base = lf.source_backup_dir if side == "source" else (lf.target_backup_dir or lf.source_backup_dir)
        if not base:
            raise LiftError(f"lift.{side}_backup_dir is not set")
        return backup_devices(base, stem, stripes)

    def _to_target_path(self, devices: Sequence[str]) -> List[str]:
        if self.lift.transport == "s3":
            return list(devices)
        pm = dict(self.lift.path_map)
        if self.lift.source_backup_dir and self.lift.target_backup_dir:
            pm.setdefault(self.lift.source_backup_dir, self.lift.target_backup_dir)
        return [map_path(d, pm) for d in devices]

    # -- plan --------------------------------------------------------------------
    def plan(self) -> List[DbPlan]:
        if not self.lift.databases:
            raise LiftError("set lift.databases = [\"...\"]")
        s, t = self.src(), self.tgt()
        try:
            sv = int(str(s.scalar("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)")))
            tv = int(str(t.scalar("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)")))
            s_ed = str(s.scalar("SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(128))"))
            t_ed = str(t.scalar("SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(128))"))
            plans = [self._plan_db(s, t, db, sv, tv, s_ed, t_ed) for db in self.lift.databases]
        finally:
            s.close()
            t.close()
        return plans

    def _plan_db(self, s: Any, t: Any, db: str, sv: int, tv: int, s_ed: str, t_ed: str) -> DbPlan:
        row = s.query("SELECT recovery_model_desc, compatibility_level, is_encrypted, state_desc "
                      "FROM sys.databases WHERE name = ?", [db])
        if not row:
            raise LiftError(f"source database {db} not found")
        recovery, compat, encrypted, state = row[0]
        sizes = s.query("SELECT type_desc, SUM(CAST(size AS bigint)) * 8 / 1024.0 / 1024.0, COUNT(*) "
                        "FROM sys.master_files WHERE database_id = DB_ID(?) GROUP BY type_desc", [db])
        data_gb = sum(float(r[1]) for r in sizes if r[0] != "LOG")
        log_gb = sum(float(r[1]) for r in sizes if r[0] == "LOG")
        nfiles = sum(int(r[2]) for r in sizes)
        hist = s.query("""
            SELECT TOP 5 CAST(backup_size AS float), CAST(compressed_backup_size AS float),
                   DATEDIFF(SECOND, backup_start_date, backup_finish_date)
            FROM msdb.dbo.backupset WHERE database_name = ? AND type = 'D'
            ORDER BY backup_finish_date DESC""", [db])
        ratio, ratio_src, speed = self.lift.assumed_compression_ratio, "assumed", None
        comp = [h for h in hist if h[1] and h[0] and h[1] < h[0]]
        if comp:
            ratio = sum(h[0] / h[1] for h in comp) / len(comp)
            ratio_src = f"measured from {len(comp)} recent compressed full backups"
        timed = [h for h in hist if h[2] and h[2] > 60]
        if timed:
            speed = max(h[0] / 1024 / 1024 / h[2] for h in timed)
        logs = s.query("""
            SELECT COUNT(*), SUM(CASE WHEN mf.device_type = 7 THEN 1 ELSE 0 END),
                   SUM(CAST(bs.backup_size AS float)) / 1024 / 1024 / 1024
            FROM msdb.dbo.backupset bs JOIN msdb.dbo.backupmediafamily mf ON mf.media_set_id = bs.media_set_id
            WHERE bs.database_name = ? AND bs.type = 'L' AND bs.backup_finish_date > DATEADD(HOUR, -24, GETDATE())
              AND mf.family_sequence_number = 1""", [db])
        n_logs, n_vdi, log_gb_24 = (logs[0] if logs else (0, 0, 0))
        p = DbPlan(database=db, target_database=self.target_name(db), data_gb=round(data_gb, 1),
                   log_gb=round(log_gb, 1), files=nfiles, recovery_model=recovery, compatibility_level=int(compat),
                   encrypted=bool(encrypted), compression_ratio=round(ratio, 2), ratio_source=ratio_src,
                   backup_mb_s=round(speed, 1) if speed else None,
                   log_gb_per_hour=round(float(log_gb_24) / 24, 2) if log_gb_24 else None,
                   other_log_backups_24h=int(n_logs or 0), vdi_log_backups_24h=int(n_vdi or 0))
        if tv < sv:
            p.blockers.append(f"target SQL Server major version {tv} is older than source {sv}: "
                              "a backup can only be restored on the same or a newer version")
        if tv > sv:
            p.warnings.append(f"target is a newer SQL Server version ({sv} -> {tv}): the database keeps "
                              f"compatibility level {int(compat)}. Set update_statistics_after = true, and capture "
                              "a Query Store baseline before raising the compatibility level")
        if state != "ONLINE":
            p.blockers.append(f"source database state is {state}")
        if recovery == "SIMPLE":
            p.warnings.append("SIMPLE recovery: no log catch-up possible; downtime = full backup + restore. "
                              "Switch to FULL (and take one full backup) before seeding to get minutes of downtime")
        if p.vdi_log_backups_24h:
            p.blockers.append(f"{p.vdi_log_backups_24h} log backups in the last 24h came from a third-party tool "
                              "(virtual device). Pause that log-backup job during the migration, or the log "
                              "chain cannot be replayed on OpenShift")
        elif p.other_log_backups_24h:
            p.warnings.append(f"{p.other_log_backups_24h} log backups/day are taken by existing jobs; they are "
                              "picked up from msdb and applied — make sure their folder is mapped in lift.path_map")
        if encrypted and not self.lift.transfer_tde_certificates:
            p.blockers.append("database uses TDE: enable lift.transfer_tde_certificates or install the "
                              "certificate on the target first")
        if self.lift.transport == "s3":
            if sv < 16 or tv < 16:
                p.blockers.append("S3 backup/restore needs SQL Server 2022 on both sides; use transport = \"share\"")
            limit_gb = 20 * 10_000 * 64 / 1024        # 20 MB parts x 10,000 parts x 64 URLs
            if data_gb / ratio > limit_gb * 0.9:
                p.blockers.append(f"compressed backup (~{data_gb / ratio:,.0f} GB) exceeds the S3 limit of "
                                  f"~{limit_gb:,.0f} GB per backup set; use transport = \"share\" (NFS/SMB)")
        if "Express" in s_ed or "Express" in t_ed:
            p.warnings.append("Express edition cannot hold databases over 10 GB")
        if "Standard" in s_ed and self.lift.compression:
            p.warnings.append("backup compression is available on Standard from 2008 R2 onward")
        if getattr(self.lift, "assess", True):
            self._assess_db(s, t, db, p)
        p.timeline = estimate_timeline(p, self.lift)
        return p

    def _assess_db(self, s: Any, t: Any, db: str, p: Any) -> None:
        """Readiness assessment for a physical copy: Linux-unsupported features become blockers
        (FILESTREAM can't even be restored), the rest warnings."""
        from .assess import BLOCKER, COPY_BLOCKING_AREAS, WARNING, assess
        try:
            s.execute(f"USE {q(db)}")
            a = assess(s, t, db, lift=True)
        except Exception as exc:  # pragma: no cover - never let the assessment break planning
            p.warnings.append(f"readiness assessment skipped: {exc}")
            return
        finally:
            try:
                s.execute("USE [master]")
            except Exception:
                log.debug("ignored error", exc_info=True)
        for f in a.findings:
            if f.severity == BLOCKER and f.area in COPY_BLOCKING_AREAS and f.area != "Version":  # version: above
                p.blockers.append(f"{f.item}: {f.message}" + (f" ({f.fix})" if f.fix else ""))
            elif f.severity in (BLOCKER, WARNING):
                p.warnings.append(f"{f.item}: {f.message}" + (f" ({f.fix})" if f.fix else ""))

    # -- seed: full backup + restore ---------------------------------------------
    def _log_chain_started(self, db: str) -> bool:
        """True when log backups are possible (a full backup has started the log chain)."""
        s = self.src()
        try:
            rows = s.query("SELECT last_log_backup_lsn FROM sys.database_recovery_status "
                           "WHERE database_id = DB_ID(?)", [db])
        finally:
            s.close()
        return bool(rows) and rows[0][0] is not None

    def seed(self, db: str, restart: bool = False) -> None:
        st = self.state.db(db)
        if st["phase"] in ("restoring", "recovered") and not restart:
            self.progress(f"{db}: already seeded (phase {st['phase']}); continuing with logs")
            return
        lf = self.lift
        stem = f"{db}_full_{time.strftime('%Y%m%d_%H%M%S')}"
        src_dev = self._devices("source", stem, lf.stripes)
        if lf.transport == "s3":
            self._ensure_s3_credentials()
        if lf.transfer_tde_certificates:
            self.transfer_tde(db)
        self.state.update(db, phase="backing_up", full_devices=src_dev)
        copy_only = lf.copy_only
        if copy_only and not self._log_chain_started(db):
            # a COPY_ONLY full backup doesn't start the log chain: BACKUP LOG would then fail
            copy_only = False
            self.progress(f"{db}: no full backup has started the log chain yet, so this full backup "
                          "is a regular one (not COPY_ONLY); later differentials are based on it")
        LongOp(self.cfg.source, self.progress, connect_fn=self._connect).run(
            backup_full_sql(db, src_dev, lf, copy_only), f"{db} full backup")
        s = self.src()
        try:
            full = s.query("""
                SELECT TOP 1 CAST(bs.last_lsn AS decimal(25,0)), CAST(bs.checkpoint_lsn AS decimal(25,0)),
                       CAST(bs.backup_size AS float), CAST(bs.compressed_backup_size AS float)
                FROM msdb.dbo.backupset bs JOIN msdb.dbo.backupmediafamily mf ON mf.media_set_id = bs.media_set_id
                WHERE bs.database_name = ? AND bs.type = 'D' AND mf.physical_device_name = ?
                ORDER BY bs.backup_finish_date DESC""", [db, src_dev[0]])[0]
        finally:
            s.close()
        self.state.update(db, phase="backed_up", full_last_lsn=int(full[0]), full_size=full[2],
                          full_compressed=full[3])
        if lf.transport == "manual":
            self.progress(f"{db}: copy these {len(src_dev)} files to {lf.target_backup_dir}, then run "
                          f"`mssql-migrate lift resume`:\n  " + "\n  ".join(src_dev))
            self.state.update(db, phase="awaiting_copy")
            return
        self.restore_full(db)

    def restore_full(self, db: str) -> None:
        st = self.state.db(db)
        tdb = self.target_name(db)
        devices = self._to_target_path(st["full_devices"])
        t = self.tgt()
        try:
            exists = t.scalar("SELECT state_desc FROM sys.databases WHERE name = ?", [tdb])
            if exists and exists != "RESTORING":
                raise LiftError(f"target database {tdb} already exists ({exists}); drop it or set lift.rename")
            files = self._filelist(t, devices)
        finally:
            t.close()
        moves = move_plan(files, tdb, self.lift.target_data_dirs, self.lift.target_log_dir)
        self.state.update(db, phase="restoring_full", moves=moves)
        LongOp(self.cfg.target, self.progress, connect_fn=self._connect).run(
            restore_full_sql(tdb, devices, moves, self.lift, replace=bool(st.get("replace"))), f"{tdb} restore")
        self.state.update(db, phase="restoring", restored_lsn=st.get("full_last_lsn"))

    @staticmethod
    def _filelist(t: Any, devices: Sequence[str]) -> List[DbFile]:
        cur = t.raw.cursor()
        try:
            cur.execute(f"RESTORE FILELISTONLY FROM {_media(devices)}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        finally:
            cur.close()
        ix = {c: i for i, c in enumerate(cols)}
        return [DbFile(logical=r[ix["LogicalName"]], physical=r[ix["PhysicalName"]], type=r[ix["Type"]],
                       size_bytes=int(r[ix["Size"]] or 0), filegroup=r[ix["FileGroupName"]]) for r in rows]

    # -- catch-up ----------------------------------------------------------------
    def log_sets(self, db: str, after_lsn: int) -> List[BackupSet]:
        s = self.src()
        try:
            rows = s.query("""
                SELECT bs.backup_set_id, bs.type, CAST(bs.first_lsn AS decimal(25,0)), CAST(bs.last_lsn AS decimal(25,0)),
                       CAST(bs.database_backup_lsn AS decimal(25,0)), bs.backup_finish_date,
                       mf.physical_device_name, mf.device_type, bs.is_copy_only, CAST(bs.backup_size AS float),
                       mf.family_sequence_number
                FROM msdb.dbo.backupset bs JOIN msdb.dbo.backupmediafamily mf ON mf.media_set_id = bs.media_set_id
                WHERE bs.database_name = ? AND bs.type = 'L' AND bs.last_lsn > ?
                ORDER BY bs.first_lsn, mf.family_sequence_number""", [db, decimal.Decimal(after_lsn)])
        finally:
            s.close()
        sets: Dict[int, BackupSet] = {}
        for r in rows:
            b = sets.get(r[0])
            if b is None:
                b = sets[r[0]] = BackupSet(int(r[0]), r[1], int(r[2]), int(r[3]), int(r[4] or 0), r[5], [],
                                           int(r[7]), bool(r[8]), int(r[9] or 0))
            b.devices.append(r[6])
        return list(sets.values())

    def catch_up_once(self, db: str, take_backup: bool = True, tail: bool = False) -> int:
        """Take one log backup (ours), then apply every log backup in the chain not yet on target."""
        st = self.state.db(db)
        if st["phase"] not in ("restoring",):
            raise LiftError(f"{db}: phase is {st['phase']}; run `lift seed` first")
        tdb = self.target_name(db)
        if take_backup:
            stem = f"{db}_log_{time.strftime('%Y%m%d_%H%M%S')}{'_tail' if tail else ''}"
            dev = self._devices("source", stem, 1 if not tail else min(4, self.lift.stripes))
            s = self.src()
            try:
                if tail and self.lift.cutover_source == "read_only":
                    s.execute(f"ALTER DATABASE {q(db)} SET READ_ONLY WITH ROLLBACK IMMEDIATE")
                elif tail and self.lift.cutover_source == "norecovery":
                    # the tail-log backup needs the database to itself: disconnect application sessions
                    s.execute(f"ALTER DATABASE {q(db)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                s.execute(backup_log_sql(db, dev, self.lift,
                                         tail=tail and self.lift.cutover_source == "norecovery"))
            finally:
                s.close()
        restored = int(st["restored_lsn"])
        todo = next_logs(self.log_sets(db, restored), restored)
        t = self.tgt()
        try:
            for b in todo:
                t.execute(restore_log_sql(tdb, self._to_target_path(b.devices)))
                self.state.update(db, restored_lsn=b.last_lsn, logs=st["logs"] + 1,
                                  last_log_finish=str(b.finish))
                st = self.state.db(db)
        finally:
            t.close()
        if todo:
            self.progress(f"{db}: applied {len(todo)} log backup(s); target now at LSN {st['restored_lsn']} "
                          f"(source time {st.get('last_log_finish')})")
        return len(todo)

    def follow(self, db: str, hours: float = 0.0, stop: Optional[threading.Event] = None) -> None:
        """Keep the target within one log interval of the source until stopped."""
        end = time.time() + hours * 3600 if hours else None
        stop = stop or threading.Event()
        while not stop.is_set() and (end is None or time.time() < end):
            try:
                self.catch_up_once(db)
            except ChainError:
                raise
            except Exception as exc:     # transient: network blip, share hiccup -> retry next cycle
                self.progress(f"{db}: catch-up attempt failed ({exc}); retrying in {self.lift.log_interval_seconds}s")
            stop.wait(self.lift.log_interval_seconds)

    # -- cut-over ----------------------------------------------------------------
    def cutover(self, db: str) -> None:
        tdb = self.target_name(db)
        t0 = time.time()
        self.progress(f"{db}: cut-over started — final catch-up while source is still online")
        self.catch_up_once(db)
        self.progress(f"{db}: tail-log backup ({self.lift.cutover_source}); source stops accepting writes")
        self.catch_up_once(db, take_backup=True, tail=True)
        t = self.tgt()
        try:
            t.execute(recover_sql(tdb))
            # the cut-over itself (READ_ONLY / SINGLE_USER on the source) is in the replayed log:
            # undo it on the new primary
            flags = t.query("SELECT is_read_only, user_access_desc FROM sys.databases WHERE name = ?", [tdb])
            if flags and flags[0][0]:
                t.execute(f"ALTER DATABASE {q(tdb)} SET READ_WRITE WITH ROLLBACK IMMEDIATE")
            if flags and flags[0][1] != "MULTI_USER":
                t.execute(f"ALTER DATABASE {q(tdb)} SET MULTI_USER WITH ROLLBACK IMMEDIATE")
        finally:
            t.close()
        self.state.update(db, phase="recovered", cutover_seconds=round(time.time() - t0, 1))
        self.post_recovery(db)
        self.progress(f"{db}: ONLINE on target. Write downtime was {time.time() - t0:,.0f} s. "
                      + ("Rollback: RESTORE DATABASE " + q(db) + " WITH RECOVERY, then SET MULTI_USER, on the source."
                         if self.lift.cutover_source == "norecovery" else ""))

    # -- post-recovery -----------------------------------------------------------
    def post_recovery(self, db: str) -> None:
        tdb = self.target_name(db)
        s, t = self.src(), self.tgt()
        notes: List[str] = []
        try:
            props = s.query("SELECT SUSER_SNAME(owner_sid), is_trustworthy_on, is_db_chaining_on, "
                            "is_broker_enabled, is_read_committed_snapshot_on, snapshot_isolation_state "
                            "FROM sys.databases WHERE name = ?", [db])[0]
            if self.lift.copy_logins:
                notes += self.copy_logins(s, t, db)
            owner = props[0]
            if owner and t.scalar("SELECT SUSER_ID(?)", [owner]) is not None:
                t.execute(f"ALTER AUTHORIZATION ON DATABASE::{q(tdb)} TO {q(owner)}")
            else:
                notes.append(f"owner {owner} does not exist on target; database owned by the restoring login")
            if props[1]:
                t.execute(f"ALTER DATABASE {q(tdb)} SET TRUSTWORTHY ON")
            if props[2]:
                t.execute(f"ALTER DATABASE {q(tdb)} SET DB_CHAINING ON")
            if props[3]:
                t.execute(f"ALTER DATABASE {q(tdb)} SET ENABLE_BROKER WITH ROLLBACK IMMEDIATE")
            orphans = t.query(f"SELECT dp.name FROM {q(tdb)}.sys.database_principals dp "
                              f"LEFT JOIN sys.server_principals sp ON sp.sid = dp.sid "
                              f"WHERE dp.type = 'S' AND dp.authentication_type = 1 AND sp.sid IS NULL")
            for (name,) in orphans:
                if t.scalar("SELECT SUSER_ID(?)", [name]) is not None:
                    t.execute(f"USE {q(tdb)}; ALTER USER {q(name)} WITH LOGIN = {q(name)}")
                    notes.append(f"re-mapped orphaned user {name}")
                else:
                    notes.append(f"orphaned user {name}: no login of that name on target")
            if self.lift.checkdb_after:
                LongOp(self.cfg.target, self.progress, connect_fn=self._connect).run(
                    f"DBCC CHECKDB ({q(tdb)}) WITH PHYSICAL_ONLY, NO_INFOMSGS", f"{tdb} CHECKDB")
            try:
                notes += self.reprovision_master_key(s, t, db, tdb)
            except Exception as exc:
                notes.append(f"database master key not re-encrypted: {exc}")
            if self.lift.copy_agent_jobs or self.lift.copy_linked_servers or self.lift.copy_custom_messages:
                notes += self.copy_server_objects(s, t, db, tdb)
            if self.lift.update_statistics_after:
                LongOp(self.cfg.target, self.progress, connect_fn=self._connect).run(
                    f"EXEC {q(tdb)}.sys.sp_updatestats", f"{tdb} update statistics")
                notes.append("statistics updated (sp_updatestats)")
        finally:
            s.close()
            t.close()
        self.state.update(db, phase="recovered", post_notes=notes)
        for n in notes:
            self.progress(f"{db}: {n}")

    @staticmethod
    def _catalog(conn: Any) -> Any:
        """The logical engine's catalog queries over a lift connection (server objects, logins)."""
        from .catalog import CatalogMixin

        class _Catalog(CatalogMixin):
            def __init__(self, c: Any) -> None:
                self.conn = c

            def query(self, sql: Any, params: Any = None) -> Any:
                return self.conn.query(sql, params)

            def _has_col(self, view: Any, col: Any) -> bool:
                return self.conn.scalar("SELECT COL_LENGTH(?, ?)", [view, col]) is not None

        return _Catalog(conn)

    def copy_server_objects(self, s: Any, t: Any, db: str, tdb: str) -> List[str]:
        """SQL Agent jobs with steps in the database (created disabled), linked servers, and the
        user-defined messages the database's code raises."""
        cat, notes = self._catalog(s), []
        if getattr(self.lift, "copy_custom_messages", False):
            try:
                msgs = self._in_db(s, db, cat.custom_messages)
            except Exception as exc:
                msgs = []
                notes.append(f"user-defined messages not read: {exc}")
            for mid, stmts in msgs:
                if t.scalar("SELECT COUNT(*) FROM sys.messages WHERE message_id = ?", [mid]):
                    continue
                for _lang, sql in stmts:
                    t.execute(sql)
                notes.append(f"added user-defined message {mid}")
        linux = bool(self._catalog(t).is_linux())
        if self.lift.copy_agent_jobs:
            for name, stmts, warns in cat.agent_jobs(db, tdb, linux):
                notes += warns
                if not stmts or t.scalar("SELECT COUNT(*) FROM msdb.dbo.sysjobs WHERE name = ?", [name]):
                    continue
                for sql in stmts:
                    t.execute(sql)
                notes.append(f"created SQL Agent job {name} (disabled: enable it at cut-over)")
        if self.lift.copy_linked_servers:
            for name, stmts, warns in cat.linked_servers():
                notes += warns
                if t.scalar("SELECT COUNT(*) FROM sys.servers WHERE name = ?", [name]):
                    continue
                for sql in stmts:
                    t.execute(sql)
                notes.append(f"created linked server {name}")
        return notes

    @staticmethod
    def _in_db(conn: Any, db: str, fn: Any) -> Any:
        """Run a catalog function with ``db`` as the connection's current database."""
        conn.execute(f"USE {q(db)}")
        try:
            return fn()
        finally:
            conn.execute("USE [master]")

    def copy_logins(self, s: Any, t: Any, db: str) -> List[str]:
        """Logins mapped to users of this database with the same SID and password hash (the
        sp_help_revlogin / Copy-DbaLogin technique, so users aren't orphaned), Windows logins and
        groups, their default language, server roles (user-defined ones created) and server-level
        permissions."""
        from .model import Principal
        kinds = {"S": "SQL_USER", "U": "WINDOWS_USER", "G": "WINDOWS_GROUP"}
        users = [Principal(name, kinds[str(typ).strip()], login=login) for name, typ, login in s.query(f"""
            SELECT dp.name, dp.type, sp.name FROM {q(db)}.sys.database_principals dp
            JOIN sys.server_principals sp ON sp.sid = dp.sid
            WHERE dp.type IN ('S', 'U', 'G') AND sp.name NOT LIKE '##%' AND sp.name <> 'sa'""")]
        logins, errors = self._catalog(s).server_logins(users)
        notes = list(errors)
        for name, sql in logins:
            if t.scalar("SELECT SUSER_ID(?)", [name]) is not None:
                continue
            t.execute(sql)
            notes.append(f"created login {name} (same SID and password, roles and server permissions)")
        return notes

    def reprovision_master_key(self, s: Any, t: Any, db: str, tdb: str) -> List[str]:
        """A restored database's master key isn't encrypted by the target's service master key, so
        certificates and keys in it can't be opened automatically. With the DMK password in
        ``database_master_key_password_env``, open it and add the target's SMK encryption (the step
        Microsoft documents for moving a database with encrypted data)."""
        import os
        has = s.query(f"""SELECT k.name, d.is_master_key_encrypted_by_server FROM {q(db)}.sys.symmetric_keys k
                          CROSS JOIN sys.databases d WHERE d.name = ? AND k.name = '##MS_DatabaseMasterKey##'""", [db])
        if not has or not has[0][1]:
            return []
        env = getattr(self.lift, "database_master_key_password_env", "SOURCE_DB_MASTER_KEY_PASSWORD")
        pwd = os.environ.get(env)
        if not pwd:
            return [f"the database master key is encrypted by the source's service master key: set {env} to its "
                    f"password, or run OPEN MASTER KEY DECRYPTION BY PASSWORD = '...'; ALTER MASTER KEY ADD "
                    f"ENCRYPTION BY SERVICE MASTER KEY in {tdb}"]
        t.execute(f"USE {q(tdb)}; OPEN MASTER KEY DECRYPTION BY PASSWORD = {lit(pwd)}; "
                  "ALTER MASTER KEY ADD ENCRYPTION BY SERVICE MASTER KEY; CLOSE MASTER KEY; USE [master]")
        return ["database master key re-encrypted by the target's service master key"]

    # -- TDE ---------------------------------------------------------------------
    def transfer_tde(self, db: str) -> None:
        """Copy the TDE certificate (with private key) over TDS — no files on either server."""
        s = self.src()
        try:
            enc = s.scalar("SELECT is_encrypted FROM sys.databases WHERE name = ?", [db])
            if not enc:
                return
            pwd = "Mom!" + secrets.token_hex(24)
            rows = s.query(f"""
                SELECT c.name, CONVERT(varchar(max), CERTENCODED(c.certificate_id), 1),
                       CONVERT(varchar(max), CERTPRIVATEKEY(c.certificate_id, {lit(pwd)}), 1)
                FROM master.sys.certificates c
                JOIN sys.dm_database_encryption_keys k ON k.encryptor_thumbprint = c.thumbprint
                WHERE k.database_id = DB_ID(?)""", [db])
        finally:
            s.close()
        if not rows:
            raise LiftError(f"{db} is encrypted but its certificate is not in master (EKM/asymmetric key?)")
        name, cert_hex, pk_hex = rows[0]
        if not pk_hex:
            raise LiftError("cannot export the TDE private key (needs CONTROL on the certificate)")
        t = self.tgt()
        try:
            if t.scalar("SELECT 1 FROM master.sys.certificates WHERE name = ?", [name]):
                self.progress(f"TDE certificate {name} already on target")
                return
            if not t.scalar("SELECT 1 FROM master.sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##'"):
                mk = os.environ.get(self.lift.target_master_key_password_env)
                if not mk:
                    raise LiftError(f"target has no master key; set {self.lift.target_master_key_password_env}")
                t.execute(f"CREATE MASTER KEY ENCRYPTION BY PASSWORD = {lit(mk)}")
            t.execute(f"CREATE CERTIFICATE {q(name)} FROM BINARY = {cert_hex} "
                      f"WITH PRIVATE KEY (BINARY = {pk_hex}, DECRYPTION BY PASSWORD = {lit(pwd)})")
            self.progress(f"TDE certificate {name} copied to target (back it up there!)")
        finally:
            t.close()

    def _ensure_s3_credentials(self) -> None:
        a = os.environ.get(self.lift.s3_access_key_env)
        k = os.environ.get(self.lift.s3_secret_key_env)
        if not a or not k:
            raise LiftError(f"set {self.lift.s3_access_key_env} and {self.lift.s3_secret_key_env}")
        for conn in (self.src(), self.tgt()):
            try:
                conn.execute(s3_credential_sql(self.lift.s3_url, a, k))
            finally:
                conn.close()

    # -- verify ------------------------------------------------------------------
    def verify(self, db: str) -> Dict[str, Any]:
        """Physically identical copy => compare metadata row counts for every table and server-side
        checksums of a few random key windows per table (fast even at 100 TB)."""
        from .sqlserver import SqlServer
        import random
        src_db = SqlServer(dataclasses.replace(self.cfg.source, database=db), "source")
        tgt_db = SqlServer(dataclasses.replace(self.cfg.target, database=self.target_name(db)), "target")
        sql_counts = ("SELECT s.name + '.' + t.name, SUM(p.rows) FROM sys.tables t JOIN sys.schemas s ON "
                      "s.schema_id = t.schema_id JOIN sys.partitions p ON p.object_id = t.object_id AND "
                      "p.index_id IN (0,1) WHERE t.is_ms_shipped = 0 GROUP BY s.name, t.name")
        res: Dict[str, Any] = {"database": db, "tables": 0, "count_mismatches": [], "checksum_mismatches": [],
                               "windows": 0}
        try:
            a = dict(src_db.query(sql_counts))
            b = dict(tgt_db.query(sql_counts))
            res["tables"] = len(a)
            res["count_mismatches"] = sorted(n for n in set(a) | set(b) if a.get(n) != b.get(n))
            rnd = random.Random(42)  # noqa: S311 (sampling, not security)
            for t in src_db.list_tables():
                if not t.key_columns or t.row_count == 0:
                    continue
                for _ in range(self.lift.verify_windows_per_table):
                    start = src_db.random_key(t, rnd)
                    if start is None:
                        continue
                    x = src_db.checksum_window(t, start, self.lift.verify_sample_rows)
                    y = tgt_db.checksum_window(t, start, self.lift.verify_sample_rows)
                    res["windows"] += 1
                    if x != y:
                        res["checksum_mismatches"].append(f"{t.fqn} from key {start}")
        finally:
            src_db.close()
            tgt_db.close()
        res["ok"] = not res["count_mismatches"] and not res["checksum_mismatches"]
        return res

    # -- AG seeding script (Enterprise/Developer) ----------------------------------
    def ag_script(self, ag_name: str, source_host: str, target_host: str, port: int = 5022) -> str:
        return ag_seed_script(ag_name, self.lift.databases, source_host, target_host, port)

    # -- orchestration -----------------------------------------------------------
    def run(self, follow_hours: float = 0.0) -> None:
        """Seed every database (parallel_databases at a time), then keep them caught up."""
        import concurrent.futures as cf
        dbs = list(self.lift.databases)
        with cf.ThreadPoolExecutor(max_workers=max(1, self.lift.parallel_databases)) as ex:
            for f in cf.as_completed([ex.submit(self.seed, d) for d in dbs]):
                f.result()
        for d in dbs:
            if self.state.db(d)["phase"] == "restoring":
                self.catch_up_once(d)
        if follow_hours:
            stop = threading.Event()
            threads = [threading.Thread(target=self.follow, args=(d, follow_hours, stop)) for d in dbs]
            for th in threads:
                th.start()
            for th in threads:
                th.join()


def ag_seed_script(ag: str, databases: Sequence[str], src_host: str, tgt_host: str, port: int = 5022) -> str:
    """Cluster-less availability group with automatic seeding into the OpenShift pod
    (SQL Server 2017+ Enterprise/Developer; needs port 5022 exposed by a Service). Review, then run."""
    dbs = ", ".join(q(d) for d in databases)
    return f"""-- ===== Option B: availability-group seeding (no backup files, no file share) =====
-- Requires Enterprise or Developer edition on BOTH replicas, SQL Server 2017+, and a Service that
-- exposes TCP {port} of the SQL Server pod. Certificates authenticate the endpoints; the library's
-- `lift ag-certs` command can copy them over TDS (CREATE CERTIFICATE ... FROM BINARY).

-- 1) BOTH servers: endpoint + compressed seeding stream
CREATE MASTER KEY ENCRYPTION BY PASSWORD = '<strong password>';
CREATE CERTIFICATE ag_cert_<server> WITH SUBJECT = 'AG endpoint <server>';
CREATE ENDPOINT [Hadr_endpoint] STATE = STARTED AS TCP (LISTENER_PORT = {port})
    FOR DATABASE_MIRRORING (ROLE = ALL, AUTHENTICATION = CERTIFICATE ag_cert_<server>,
                            ENCRYPTION = REQUIRED ALGORITHM AES);
DBCC TRACEON (9567, -1);   -- compress the automatic-seeding stream (more CPU, far less network)

-- 2) BOTH servers: trust the other side's certificate (copy CERTENCODED() output across)
-- CREATE LOGIN ag_login_<other>  ...; CREATE USER ...; CREATE CERTIFICATE ... FROM BINARY = 0x...;
-- GRANT CONNECT ON ENDPOINT::[Hadr_endpoint] TO ag_login_<other>;

-- 3) SOURCE ({src_host})
CREATE AVAILABILITY GROUP {q(ag)} WITH (CLUSTER_TYPE = NONE)
FOR DATABASE {dbs}
REPLICA ON
  N'<source @@SERVERNAME>' WITH (ENDPOINT_URL = N'tcp://{src_host}:{port}',
      AVAILABILITY_MODE = ASYNCHRONOUS_COMMIT, FAILOVER_MODE = MANUAL, SEEDING_MODE = AUTOMATIC),
  N'<target @@SERVERNAME>' WITH (ENDPOINT_URL = N'tcp://{tgt_host}:{port}',
      AVAILABILITY_MODE = ASYNCHRONOUS_COMMIT, FAILOVER_MODE = MANUAL, SEEDING_MODE = AUTOMATIC);

-- 4) TARGET (OpenShift pod)
ALTER AVAILABILITY GROUP {q(ag)} JOIN WITH (CLUSTER_TYPE = NONE);
ALTER AVAILABILITY GROUP {q(ag)} GRANT CREATE ANY DATABASE;

-- 5) watch seeding:  SELECT * FROM sys.dm_hadr_physical_seeding_stats;
-- 6) cut-over: switch to SYNCHRONOUS_COMMIT, wait for SYNCHRONIZED, then on the TARGET:
--    ALTER AVAILABILITY GROUP {q(ag)} FORCE_FAILOVER_ALLOW_DATA_LOSS;  (no loss once synchronized)
"""


def format_plan(plans: Sequence[DbPlan], lift: LiftConfig) -> str:
    out = []
    tot_gb = sum(p.data_gb for p in plans)
    for p in plans:
        tl = p.timeline
        out += [f"{p.database} -> {p.target_database}: {p.data_gb:,.0f} GB data, {p.log_gb:,.0f} GB log, "
                f"{p.files} files, recovery {p.recovery_model}, compat {p.compatibility_level}"
                f"{', TDE' if p.encrypted else ''}",
                f"  compression ~{p.compression_ratio}:1 ({p.ratio_source}) -> ~{tl['compressed_gb']:,.0f} GB moved",
                f"  backup ~{tl['backup_h']} h | copy ~{tl['copy_h']} h | restore ~{tl['restore_h']} h | "
                f"log catch-up ~{tl['catchup_h']} h   (source online the whole time: ~{tl['online_total_h']} h)",
                f"  expected cut-over downtime: ~{tl['downtime_min']} min"]
        out += [f"  BLOCKER: {b}" for b in p.blockers]
        out += [f"  warn: {w}" for w in p.warnings]
    assumptions = []
    if not lift.network_gbps:
        assumptions.append("network not given (set lift.network_gbps)")
    if not lift.target_write_mb_s:
        assumptions.append("target storage write speed assumed 1,000 MB/s (set lift.target_write_mb_s)")
    if any(p.backup_mb_s is None for p in plans):
        assumptions.append("no timed backup history: source read speed assumed 800 MB/s")
    out.append(f"TOTAL {tot_gb / 1024:,.1f} TB across {len(plans)} database(s), "
               f"{lift.parallel_databases} in parallel, {lift.stripes} stripes each")
    if assumptions:
        out.append("assumptions: " + "; ".join(assumptions))
    return "\n".join(out)
