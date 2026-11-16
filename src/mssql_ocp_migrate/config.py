"""Configuration: TOML (Python 3.11+) or JSON files, plus environment variables for secrets."""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(ValueError):
    pass


@dataclass
class ConnectionConfig:
    host: str = "localhost"
    port: int = 1433
    database: str = ""
    user: str = ""
    password: Optional[str] = None          # avoid in files; prefer password_env / password_file
    password_env: Optional[str] = None      # name of an environment variable
    password_file: Optional[str] = None     # e.g. an OpenShift secret mounted as a file
    instance: Optional[str] = None          # named instance (resolved via SQL Browser, UDP 1434)
    driver: str = "auto"                    # auto | pytds | pymssql | pyodbc
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: str = "optional"               # off | optional | required
    trust_server_certificate: bool = False
    ca_file: Optional[str] = None           # PEM bundle; default = OS trust store
    hostname_in_certificate: Optional[str] = None  # e.g. when connecting through oc port-forward
    login_timeout: int = 30
    query_timeout: Optional[int] = None
    app_name: str = "mssql-ocp-migrate"

    def resolve_password(self) -> str:
        if self.password_file:
            return Path(self.password_file).read_text(encoding="utf-8").strip()
        if self.password_env:
            val = os.environ.get(self.password_env)
            if val is None:
                raise ConfigError(f"environment variable {self.password_env} is not set")
            return val
        return self.password or ""

    def describe(self) -> str:
        inst = f"\\{self.instance}" if self.instance else ""
        return f"{self.host}{inst}:{self.port}/{self.database}"


@dataclass
class MigrationConfig:
    include: List[str] = field(default_factory=lambda: ["*.*"])   # schema.table glob patterns
    exclude: List[str] = field(default_factory=list)
    workers: int = 4
    executor: str = "auto"              # auto | thread | process (auto: process for the pure-Python
                                        # driver so parallel chunks use all CPU cores; thread for pyodbc)
    chunk_rows: int = 250_000           # target rows per chunk (unit of parallelism + resume)
    batch_rows: int = 10_000            # rows per bulk-insert batch / fetchmany
    schema_mode: str = "create"         # create | skip  (skip = target schema pre-created, e.g. DACPAC)
    if_target_not_empty: str = "fail"   # fail | truncate | resume
    copy_programmable_objects: bool = True   # views, procedures, functions, triggers, synonyms
    create_indexes_after_load: bool = True
    tablock: bool = True
    state_file: str = "migration_state.json"
    max_retries: int = 3
    # scale-out: run N copies (e.g. an OpenShift Indexed Job), each copies 1/N of the chunks
    shard_index: int = 0
    shard_count: int = 1
    plan_file: str = ""                  # chunk plan shared by all shards (RWX volume)
    plan_wait_seconds: int = 7200
    target_recovery_simple: bool = False  # minimally-logged bulk load; original model restored at the end


@dataclass
class ValidationConfig:
    mode: str = "checksum"              # count | checksum | full
    sample_full_pct: float = 5.0        # in checksum mode, also row-hash-compare this % of chunks
    max_diff_rows: int = 50             # mismatched keys reported per chunk
    compare_schema: bool = True


@dataclass
class LiftConfig:
    """Physical migration: striped compressed backup -> restore -> log catch-up -> cut-over."""
    databases: List[str] = field(default_factory=list)       # source database names
    rename: Dict[str, str] = field(default_factory=dict)     # source name -> target name
    transport: str = "share"            # share | s3 | manual
    source_backup_dir: str = ""         # backup folder as the SOURCE server sees it (\\nas\mig or /mnt/mig)
    target_backup_dir: str = ""         # the same folder as the TARGET pod sees it (NFS PV mount)
    s3_url: str = ""                    # s3://endpoint[:port]/bucket[/prefix]  (SQL Server 2022 on both sides)
    s3_access_key_env: str = "S3_ACCESS_KEY"
    s3_secret_key_env: str = "S3_SECRET_KEY"
    stripes: int = 32                   # backup files written/read in parallel (max 64)
    maxtransfersize: int = 4_194_304    # 4 MB for disk; S3 uses 20 MB automatically
    buffercount: int = 0                # 0 = auto (stripes * 4, capped by memory_mb)
    memory_mb: int = 2048               # cap for buffercount * maxtransfersize
    compression: bool = True
    copy_only: bool = True              # never disturbs the existing full/diff chain
    target_data_dirs: List[str] = field(default_factory=lambda: ["/var/opt/mssql/data"])
    target_log_dir: str = "/var/opt/mssql/data"
    log_interval_seconds: int = 300
    path_map: Dict[str, str] = field(default_factory=dict)   # existing-backup path prefix -> target path
    cutover_source: str = "norecovery"  # norecovery (source -> RESTORING, instant rollback) | read_only | none
    parallel_databases: int = 2
    copy_logins: bool = True            # SQL logins used by the database users (SID + password hash)
    transfer_tde_certificates: bool = True
    target_master_key_password_env: str = "TARGET_MASTER_KEY_PASSWORD"
    checkdb_after: bool = False         # DBCC CHECKDB WITH PHYSICAL_ONLY after recovery (long on 100 TB)
    verify_sample_rows: int = 100_000   # rows per sampled checksum window in `lift verify`
    verify_windows_per_table: int = 3
    state_file: str = "lift_state.json"
    # planner inputs (optional; measured history from msdb is used when available)
    network_gbps: float = 0.0
    target_write_mb_s: float = 0.0
    assumed_compression_ratio: float = 3.0


@dataclass
class Config:
    source: ConnectionConfig
    target: ConnectionConfig
    migration: MigrationConfig = field(default_factory=MigrationConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    report_dir: str = "migration_report"
    lift: LiftConfig = field(default_factory=LiftConfig)

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        def build(dc, raw, section):
            raw = dict(raw or {})
            names = {f.name for f in fields(dc)}
            unknown = set(raw) - names
            if unknown:
                raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
            return dc(**raw)

        if "source" not in data or "target" not in data:
            raise ConfigError("config needs [source] and [target] sections")
        cfg = cls(
            source=build(ConnectionConfig, data["source"], "source"),
            target=build(ConnectionConfig, data["target"], "target"),
            migration=build(MigrationConfig, data.get("migration"), "migration"),
            validation=build(ValidationConfig, data.get("validation"), "validation"),
            report_dir=data.get("report_dir", "migration_report"),
            lift=build(LiftConfig, data.get("lift"), "lift"),
        )
        cfg.check()
        return cfg

    @classmethod
    def load(cls, path: str) -> "Config":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            try:
                import tomllib  # Python 3.11+
            except ModuleNotFoundError as exc:  # pragma: no cover - py<3.11
                raise ConfigError("TOML config needs Python 3.11+; use a .json config instead") from exc
            data = tomllib.loads(text)
        return cls.from_dict(data)

    def check(self) -> None:
        m, v = self.migration, self.validation
        for conn, name in ((self.source, "source"), (self.target, "target")):
            if not conn.database:
                raise ConfigError(f"[{name}] database is required")
            if conn.driver not in ("auto", "pytds", "pymssql", "pyodbc"):
                raise ConfigError(f"[{name}] driver must be auto, pytds, pymssql or pyodbc")
            if conn.encrypt not in ("off", "optional", "required"):
                raise ConfigError(f"[{name}] encrypt must be off, optional or required")
        if m.schema_mode not in ("create", "skip"):
            raise ConfigError("migration.schema_mode must be create or skip")
        if m.if_target_not_empty not in ("fail", "truncate", "resume"):
            raise ConfigError("migration.if_target_not_empty must be fail, truncate or resume")
        if m.executor not in ("auto", "thread", "process"):
            raise ConfigError("migration.executor must be auto, thread or process")
        if m.shard_count < 1 or not 0 <= m.shard_index < m.shard_count:
            raise ConfigError("migration.shard_index must be in 0..shard_count-1")
        if m.workers < 1 or m.chunk_rows < 1 or m.batch_rows < 1:
            raise ConfigError("workers, chunk_rows and batch_rows must be >= 1")
        if v.mode not in ("count", "checksum", "full"):
            raise ConfigError("validation.mode must be count, checksum or full")
        if not 0 <= v.sample_full_pct <= 100:
            raise ConfigError("validation.sample_full_pct must be 0..100")
        lf = self.lift
        if lf.transport not in ("share", "s3", "manual"):
            raise ConfigError("lift.transport must be share, s3 or manual")
        if lf.cutover_source not in ("norecovery", "read_only", "none"):
            raise ConfigError("lift.cutover_source must be norecovery, read_only or none")
        if not 1 <= lf.stripes <= 64:
            raise ConfigError("lift.stripes must be 1..64 (SQL Server limit)")

    def to_dict(self, redact: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        if redact:
            for side in ("source", "target"):
                if d[side].get("password"):
                    d[side]["password"] = "***"
        return d

    def resolved_executor(self) -> str:
        if self.migration.executor != "auto":
            return self.migration.executor
        from .drivers import choose_driver
        both_odbc = choose_driver(self.source) == "pyodbc" and choose_driver(self.target) == "pyodbc"
        return "thread" if both_odbc else "process"

    # ---------------------------------------------------------------- filters
    def selects(self, schema: str, table: str) -> bool:
        name = f"{schema}.{table}".lower()
        inc = any(fnmatch.fnmatchcase(name, p.lower()) for p in self.migration.include)
        exc = any(fnmatch.fnmatchcase(name, p.lower()) for p in self.migration.exclude)
        return inc and not exc
