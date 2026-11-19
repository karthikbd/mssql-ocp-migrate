"""Configuration: TOML (Python 3.11+) or JSON files, plus environment variables for secrets."""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, get_args, get_origin, get_type_hints, Union


class ConfigError(ValueError):
    pass


_LENIENT = {("migration", "compatibility_level"): (str, int)}   # 150 and "150" both mean level 150


def _type_ok(value: Any, tp: Any) -> bool:
    origin, args = get_origin(tp), get_args(tp)
    if origin is Union:
        return any(_type_ok(value, a) for a in args)
    if tp is type(None):
        return value is None
    if origin in (list, List):
        return isinstance(value, list) and all(_type_ok(v, args[0]) for v in value)
    if origin in (dict, Dict):
        return isinstance(value, dict) and all(_type_ok(k, args[0]) and _type_ok(v, args[1])
                                               for k, v in value.items())
    if tp is bool:
        return isinstance(value, bool)
    if tp is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if tp is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, tp)


def _type_name(tp: Any) -> str:
    origin, args = get_origin(tp), get_args(tp)
    if origin is Union:
        return " or ".join(_type_name(a) for a in args)
    if origin in (list, List):
        return f"a list of {_type_name(args[0])}"
    if origin in (dict, Dict):
        return f"a table of {_type_name(args[1])}"
    return {type(None): "nothing", bool: "true/false", int: "a whole number", float: "a number",
            str: "text"}.get(tp, getattr(tp, "__name__", str(tp)))


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
    # what else to carry over besides tables, data and code (0.3.0)
    copy_index_options: bool = True       # fill factor, PAD_INDEX, IGNORE_DUP_KEY, lock options, compression
    index_sort_in_tempdb: bool = True     # index builds after the load sort in tempdb
    index_maxdop: int = 0                 # 0 = server default
    index_online: bool = False            # ONLINE = ON (Enterprise/Developer only)
    copy_statistics: bool = True          # user-created statistics (CREATE STATISTICS)
    statistics_fullscan: bool = False     # build copied statistics WITH FULLSCAN instead of sampled
    copy_extended_properties: bool = True # MS_Description and other extended properties
    copy_security: bool = True            # database users, roles, memberships, schema owners, GRANT/DENY
    copy_database_settings: bool = True   # compatibility level, read-committed snapshot, snapshot isolation
    compatibility_level: str = "source"   # source | target | a number such as "150"
    copy_data_masking: bool = True        # dynamic data masking (needs UNMASK on the source to read real values)
    copy_row_level_security: bool = True  # security policies (created after the load, with the source's state)
    restore_system_versioning: bool = True  # temporal tables: re-enable SYSTEM_VERSIONING after the load
    memory_optimized: str = "keep"        # keep | disk (create memory-optimized tables as disk-based)
    partition_filegroups: str = "primary" # primary (all partitions on PRIMARY) | source (same filegroups)
    copy_full_text: bool = True           # full-text catalogs, stoplists and indexes
    copy_service_broker: bool = True      # message types, contracts, queues, services, routes, bindings
    copy_certificates: bool = True        # certificates (with private keys) and module signatures
    master_key_password_env: str = "TARGET_DB_MASTER_KEY_PASSWORD"   # for the target database master key
    trust_clr_assemblies: bool = False    # sp_add_trusted_assembly on targets with 'clr strict security'
    copy_change_tracking: bool = True
    copy_cdc: bool = True                 # change data capture configuration (needs sysadmin and SQL Agent)
    copy_logins: bool = True              # server logins the database users map to (same SID and password)
    copy_agent_jobs: bool = False         # SQL Agent jobs with steps in this database (created disabled)
    copy_linked_servers: bool = False     # linked server definitions (remote passwords can't be copied)
    copy_custom_messages: bool = True     # user-defined messages (sp_addmessage) the code raises
    copy_server_configuration: bool = False   # apply the source's sp_configure values (curated list)
    copy_server_triggers: bool = False    # server-level DDL/logon triggers (affect the whole instance)
    create_target_database: bool = False  # CREATE DATABASE on the target (source collation) if it's missing
    assess: bool = True                   # run the readiness assessment before migrating
    sync_enable_change_tracking: bool = False   # turn on change tracking on the SOURCE for `sync` (changes it)
    sync_retention_days: int = 7          # change-tracking retention when sync_enable_change_tracking is on
    ignore_blockers: bool = False         # continue when the assessment finds blockers


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
    update_statistics_after: bool = False  # sp_updatestats after recovery (recommended on a newer version)
    copy_agent_jobs: bool = False       # SQL Agent jobs with steps in the database (created disabled)
    copy_linked_servers: bool = False   # linked server definitions (remote passwords can't be copied)
    copy_custom_messages: bool = True   # user-defined messages (sp_addmessage) the code raises
    assess: bool = True                 # readiness assessment in `lift plan`
    database_master_key_password_env: str = "SOURCE_DB_MASTER_KEY_PASSWORD"   # re-encrypt the DMK by the SMK
    verify_sample_rows: int = 100_000   # rows per sampled checksum window in `lift verify`
    verify_windows_per_table: int = 3
    state_file: str = "lift_state.json"
    # planner inputs (optional; measured history from msdb is used when available)
    network_gbps: float = 0.0
    target_write_mb_s: float = 0.0
    assumed_compression_ratio: float = 3.0


def _report_dir(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("report_dir must be a folder name")
    return value


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
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise ConfigError(f"[{section}] must be a table of settings")
            names = {f.name for f in fields(dc)}
            unknown = set(raw) - names
            if unknown:
                raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(map(str, unknown)))}")
            hints = get_type_hints(dc)
            for key, value in raw.items():
                allowed = _LENIENT.get((section, key))
                ok = isinstance(value, allowed) and not isinstance(value, bool) if allowed else _type_ok(value, hints[key])
                if not ok:
                    raise ConfigError(f"[{section}] {key} must be {_type_name(hints[key])}, "
                                      f"not {type(value).__name__} {value!r:.40}")
            return dc(**raw)

        if "source" not in data or "target" not in data:
            raise ConfigError("config needs [source] and [target] sections")
        cfg = cls(
            source=build(ConnectionConfig, data["source"], "source"),
            target=build(ConnectionConfig, data["target"], "target"),
            migration=build(MigrationConfig, data.get("migration"), "migration"),
            validation=build(ValidationConfig, data.get("validation"), "validation"),
            report_dir=_report_dir(data.get("report_dir", "migration_report")),
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
        m.compatibility_level = str(m.compatibility_level).strip().lower()
        if m.compatibility_level not in ("source", "target") and not m.compatibility_level.isdigit():
            raise ConfigError("migration.compatibility_level must be source, target or a number such as 150")
        if m.memory_optimized not in ("keep", "disk"):
            raise ConfigError("migration.memory_optimized must be keep or disk")
        if m.partition_filegroups not in ("primary", "source"):
            raise ConfigError("migration.partition_filegroups must be primary or source")
        if m.index_maxdop < 0:
            raise ConfigError("migration.index_maxdop must be >= 0")
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
