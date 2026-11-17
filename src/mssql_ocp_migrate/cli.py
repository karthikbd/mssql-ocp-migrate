"""Command line: mssql-migrate {init-config,check,estimate,schema,migrate,validate}."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError

EXAMPLE_CONFIG = """\
# mssql-ocp-migrate configuration (TOML; use a .json file on Python < 3.11)

[source]                       # existing SQL Server
host = "sql01.corp.example.com"
port = 1433
database = "SalesDB"
user = "migration_reader"      # needs db_datareader + VIEW DEFINITION
password_env = "SRC_DB_PASSWORD"
encrypt = "optional"           # off | optional | required
trust_server_certificate = true

[target]                       # SQL Server running on OpenShift (OCP)
host = "127.0.0.1"             # `oc port-forward svc/mssql 14330:1433`, or mssql.<ns>.svc inside the cluster
port = 14330
database = "SalesDB"           # must already exist
user = "migration_writer"      # needs db_owner (or db_ddladmin + db_datawriter + ALTER)
password_file = "/var/run/secrets/mssql/target-password"   # OpenShift secret mount
encrypt = "optional"
trust_server_certificate = true

[migration]
include = ["*.*"]              # schema.table globs, e.g. ["dbo.*", "sales.Order*"]
exclude = []
workers = 8                    # parallel chunk copies
executor = "auto"              # auto | thread | process (auto = process for the bundled driver)
chunk_rows = 250000
batch_rows = 10000
schema_mode = "create"         # create | skip (target schema already deployed, e.g. DACPAC)
if_target_not_empty = "fail"   # fail | truncate | resume
copy_programmable_objects = true
state_file = "migration_state.json"
# besides tables, data and code, also copy (all default to true):
copy_index_options = true      # fill factor, PAD_INDEX, IGNORE_DUP_KEY, lock options, ROW/PAGE compression
copy_statistics = true         # user-created statistics (CREATE STATISTICS)
copy_extended_properties = true
copy_security = true           # users, roles, memberships, schema owners, GRANT/DENY (create the logins first)
copy_database_settings = true  # compatibility level, read-committed snapshot, snapshot isolation
compatibility_level = "source" # source | target | a number such as "150"
index_sort_in_tempdb = true    # faster index builds after the load
index_maxdop = 0               # 0 = server default
copy_data_masking = true       # MASKED WITH (...) columns; the source login needs UNMASK to read real data
copy_row_level_security = true # security policies, switched on after validation
restore_system_versioning = true   # temporal tables: history copied, SYSTEM_VERSIONING turned back on
memory_optimized = "keep"      # keep | disk (create memory-optimized tables as ordinary tables)
partition_filegroups = "primary"   # primary (all partitions on PRIMARY) | source (filegroups must exist)
copy_full_text = true          # full-text catalogs, stoplists and indexes (needs the full-text feature)
copy_service_broker = true     # message types, contracts, queues, services, routes, event notifications
copy_certificates = true       # certificates with private keys, certificate users, module signatures
master_key_password_env = "TARGET_DB_MASTER_KEY_PASSWORD"   # for a new database master key
trust_clr_assemblies = false   # sp_add_trusted_assembly for CLR assemblies (needs sysadmin)
copy_change_tracking = true
copy_cdc = true                # change data capture (needs sysadmin and SQL Agent on the target)
copy_logins = true             # SQL logins of mapped users, same SID and password hash (needs sysadmin)
copy_agent_jobs = false        # SQL Agent jobs that run in this database, created DISABLED
copy_linked_servers = false    # linked servers (remote passwords are never readable: re-enter them)

[validation]
mode = "checksum"              # count | checksum | full
sample_full_pct = 5            # % of chunks also compared row-by-row with SHA-256
max_diff_rows = 50

# ---- PHYSICAL lift: use this for very large databases (TB-scale) -------------------------
# mssql-migrate lift plan|run|catchup|follow|cutover --confirm|verify -c migrate.toml
[lift]
databases = ["SalesDB"]
transport = "share"            # share (NFS/SMB both servers can reach) | s3 (2022, < ~12 TB compressed) | manual
source_backup_dir = '\\\\nas01\\sqlmig'   # as the SOURCE server sees the share ('...' = TOML literal string)
target_backup_dir = "/var/opt/mssql/mig"       # the same share mounted into the OCP pod (NFS PV)
stripes = 32                   # parallel backup/restore streams (max 64)
target_data_dirs = ["/var/opt/mssql/data1", "/var/opt/mssql/data2", "/var/opt/mssql/data3", "/var/opt/mssql/data4"]
target_log_dir = "/var/opt/mssql/log"
log_interval_seconds = 300
cutover_source = "norecovery"  # source goes to RESTORING at cut-over = instant rollback path
parallel_databases = 2
network_gbps = 10              # for the timeline estimate
target_write_mb_s = 1500       # OCP storage write speed for the timeline estimate
update_statistics_after = false # sp_updatestats after cut-over (recommended when the target is a newer version)
copy_agent_jobs = false        # SQL Agent jobs of the lifted databases (created disabled)
copy_linked_servers = false
# path_map = { '\\\\nas01\\sqlbackups\\LOG' = "/var/opt/mssql/existing-logs" }  # existing log-backup jobs
"""


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("mssql_ocp_migrate._vendor").setLevel(logging.WARNING)


def _load(args) -> Config:
    cfg = Config.load(args.config)
    m = cfg.migration
    if getattr(args, "workers", None):
        m.workers = args.workers
    if getattr(args, "include", None):
        m.include = args.include
    if getattr(args, "exclude", None):
        m.exclude = m.exclude + args.exclude
    if getattr(args, "resume", False):
        m.if_target_not_empty = "resume"
    if getattr(args, "truncate", False):
        m.if_target_not_empty = "truncate"
    if getattr(args, "mode", None):
        cfg.validation.mode = args.mode
    if getattr(args, "shard_count", None):
        m.shard_count = args.shard_count
    if getattr(args, "shard", None):
        idx, _, cnt = args.shard.partition("/")
        if idx == "auto":                       # OpenShift/Kubernetes Indexed Job
            idx = os.environ.get("JOB_COMPLETION_INDEX", "0")
            m.shard_index = int(idx)
        else:
            m.shard_index = int(idx) - 1        # 1-based on the command line
        m.shard_count = int(cnt)
    if getattr(args, "report_dir", None):
        cfg.report_dir = args.report_dir
    cfg.check()
    return cfg


def cmd_init(args) -> int:
    sys.stdout.write(EXAMPLE_CONFIG)
    return 0


def cmd_check(args) -> int:
    from .engine import Migrator, SqlServerFactory
    cfg = _load(args)
    fac = SqlServerFactory(cfg)
    ok = True
    for side in ("source", "target"):
        try:
            db = fac(side, True)
            info = db.server_info()
            print(f"[ok]   {side}: {db.describe()} | SQL Server {info['version']} {info['edition']} | "
                  f"collation {info['collation']}")
            db.close()
        except Exception as exc:
            ok = False
            print(f"[FAIL] {side}: {exc}")
    if ok:
        mig = Migrator(cfg, fac)
        tables = mig.source_tables()
        print(f"\n{len(tables)} tables selected, {sum(t.row_count for t in tables):,} rows, "
              f"{sum(t.reserved_mb for t in tables):,.0f} MB")
        for t in tables:
            for w in t.warnings:
                print(f"  warn  {t.fqn}: {w}")
    return 0 if ok else 2


def cmd_assess(args) -> int:
    """Readiness assessment: what won't work, or will behave differently, on the target."""
    import dataclasses
    import json
    from pathlib import Path
    from .assess import assess, render
    from .sqlserver import SqlServer
    cfg = _load(args)
    src = SqlServer(cfg.source, "source", autocommit=True)
    tgt = None
    if not args.source_only:
        tcfg = cfg.target
        try:
            tgt = SqlServer(tcfg, "target", autocommit=True)
        except Exception:                      # the target database may not exist yet
            tgt = SqlServer(dataclasses.replace(tcfg, database="master"), "target", autocommit=True)
    try:
        a = assess(src, tgt, cfg.source.database, lift=args.lift)
    finally:
        src.close()
        if tgt is not None:
            tgt.close()
    print(render(a))
    out = Path(args.json) if args.json else Path(cfg.report_dir) / "assessment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(a.to_dict(), indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 2 if a.blockers else 0


def cmd_sync(args) -> int:
    """Apply the source's changes (change tracking) to the target after migrate."""
    from .sync import Syncer
    cfg = _load(args)
    log = logging.getLogger("mssql_ocp_migrate")
    syncer = Syncer(cfg, progress=log.info)
    while True:
        res = syncer.run(final=args.final)
        for fqn, why in res.skipped.items():
            print(f"  skipped {fqn}: {why}")
        print(f"sync {res.from_version} -> {res.to_version}: {res.rows:,} rows in {res.seconds:,.1f}s")
        if args.final or not args.follow:
            break
        time.sleep(args.follow)
    if args.final and not args.skip_validation:
        return cmd_validate(args)
    return 0


def cmd_estimate(args) -> int:
    from .estimate import estimate, format_estimate
    from . import report
    cfg = _load(args)
    est = estimate(cfg, sample_rows=args.sample_rows, write_probe=not args.no_write_probe)
    print(format_estimate(est))
    paths = report.write(cfg, estimate=est)
    print(f"\nreport: {paths['html']}")
    return 0


def cmd_schema(args) -> int:
    from .engine import Migrator
    cfg = _load(args)
    script = Migrator(cfg).schema_script()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(script)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(script)
    return 0


def cmd_migrate(args) -> int:
    from .engine import Migrator
    from . import report
    cfg = _load(args)
    t0 = time.time()
    run = Migrator(cfg, progress=lambda m: logging.getLogger("mssql_ocp_migrate").info(m)).migrate(
        validate=not args.skip_validation)
    paths = report.write(cfg, run=run)
    status = "SUCCESS" if run.ok else "FAILED"
    print(f"\n{status}: {run.rows:,} rows in {(time.time() - t0) / 60:,.1f} min. Report: {paths['html']}")
    return 0 if run.ok else 1


def cmd_finalize(args) -> int:
    from .engine import Migrator
    from . import report
    cfg = _load(args)
    run = Migrator(cfg, progress=lambda m: logging.getLogger("mssql_ocp_migrate").info(m)).finalize(
        validate=not args.skip_validation)
    paths = report.write(cfg, run=run)
    print(f"\n{'SUCCESS' if run.ok else 'FAILED'}: {run.rows:,} rows. Report: {paths['html']}")
    return 0 if run.ok else 1


def cmd_validate(args) -> int:
    from .chunking import build_chunks
    from .engine import Migrator, SqlServerFactory, _boundaries, _drop_connections
    from .state import State
    from .validate import Validator
    from . import report
    cfg = _load(args)
    fac = SqlServerFactory(cfg)
    tables = Migrator(cfg, fac).source_tables()
    st = State.load_or_new(cfg.migration.state_file)
    chunks = {}
    for t in tables:
        chunks[t.fqn] = st.chunks(t.fqn) or build_chunks(t.fqn, _boundaries(fac, t, cfg.migration.chunk_rows))
    _drop_connections()
    res = Validator(cfg, fac, lambda m: logging.getLogger("mssql_ocp_migrate").info(m)).run(tables, chunks)
    paths = report.write(cfg, validation=res)
    print(json.dumps(res.summary(), indent=2))
    print(f"report: {paths['html']}")
    return 0 if res.ok else 1


def cmd_lift(args) -> int:
    from .physical import Lift, format_plan
    cfg = _load(args)
    if args.databases:
        cfg.lift.databases = args.databases
    log = logging.getLogger("mssql_ocp_migrate")
    lift = Lift(cfg, progress=log.info)
    dbs = cfg.lift.databases
    a = args.action
    if a == "plan":
        plans = lift.plan()
        print(format_plan(plans, cfg.lift))
        return 1 if any(p.blockers for p in plans) else 0
    if a == "ag-script":
        print(lift.ag_script(args.ag_name, args.source_host or cfg.source.host,
                             args.target_host or cfg.target.host))
        return 0
    if a == "status":
        print(json.dumps({d: {k: v for k, v in lift.state.db(d).items() if k != "history"} for d in dbs},
                         indent=2, default=str))
        return 0
    if a == "run":
        plans = lift.plan()
        print(format_plan(plans, cfg.lift))
        if any(p.blockers for p in plans) and not args.force:
            print("blockers found; fix them or pass --force", file=sys.stderr)
            return 1
        lift.run(follow_hours=args.follow_hours)
        return 0
    for d in dbs:
        if a == "seed":
            lift.seed(d, restart=args.restart)
        elif a == "resume":            # after a manual copy of the backup files
            lift.restore_full(d)
        elif a == "catchup":
            lift.catch_up_once(d)
        elif a == "follow":
            lift.follow(d, hours=args.follow_hours)
        elif a == "cutover":
            if not args.confirm:
                print("cut-over stops writes on the source; re-run with --confirm", file=sys.stderr)
                return 2
            lift.cutover(d)
        elif a == "verify":
            r = lift.verify(d)
            print(json.dumps(r, indent=2, default=str))
            if not r["ok"]:
                return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mssql-migrate",
                                description="Migrate SQL Server databases to SQL Server on OpenShift, "
                                            "with end-to-end validation. No runtime downloads.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, workers=True):
        sp.add_argument("-c", "--config", required=True, help="TOML or JSON config file")
        sp.add_argument("--include", nargs="+", help="override include globs (schema.table)")
        sp.add_argument("--exclude", nargs="+", help="additional exclude globs")
        sp.add_argument("--report-dir")
        if workers:
            sp.add_argument("-w", "--workers", type=int)

    sp = sub.add_parser("init-config", help="print an example config")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("check", help="test connectivity and list what will be migrated")
    common(sp, workers=False)
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("assess", help="readiness assessment: what won't work or behaves differently on the target "
                                       "(Linux/OpenShift, version, edition, server settings, code)")
    common(sp, workers=False)
    sp.add_argument("--lift", action="store_true", help="assess for a physical lift (backup/restore)")
    sp.add_argument("--source-only", action="store_true", help="don't connect to the target")
    sp.add_argument("--json", help="write the findings as JSON here (default: report_dir/assessment.json)")
    sp.set_defaults(fn=cmd_assess)
    sp = sub.add_parser("sync", help="after migrate: apply the source's changes (change tracking) so cut-over takes "
                                     "minutes; --final during the cut-over window")
    common(sp, workers=False)
    sp.add_argument("--follow", type=int, metavar="SECONDS", help="repeat every SECONDS until interrupted")
    sp.add_argument("--final", action="store_true", help="last pass (writes stopped): trust foreign keys, align "
                                                         "identities and sequences, then validate")
    sp.add_argument("--skip-validation", action="store_true")
    sp.set_defaults(fn=cmd_sync)
    sp = sub.add_parser("estimate", help="measure throughput on a sample and project total time")
    common(sp)
    sp.add_argument("--sample-rows", type=int, default=50_000)
    sp.add_argument("--no-write-probe", action="store_true", help="only measure reads")
    sp.set_defaults(fn=cmd_estimate)
    sp = sub.add_parser("schema", help="write the DDL script without running it")
    common(sp, workers=False)
    sp.add_argument("-o", "--out")
    sp.set_defaults(fn=cmd_schema)
    sp = sub.add_parser("migrate", help="create schema, copy data, build constraints, validate")
    common(sp)
    sp.add_argument("--resume", action="store_true", help="continue an interrupted run (uses state file)")
    sp.add_argument("--truncate", action="store_true", help="reload tables that already have data")
    sp.add_argument("--skip-validation", action="store_true")
    sp.add_argument("--mode", choices=["count", "checksum", "full"], help="validation mode")
    sp.add_argument("--shard", help="I/N: copy only shard I of N (1-based), or auto/N inside an Indexed Job")
    sp.set_defaults(fn=cmd_migrate)
    sp = sub.add_parser("finalize", help="after all shards: indexes, constraints, objects, validation")
    common(sp)
    sp.add_argument("--shard-count", type=int, required=True)
    sp.add_argument("--skip-validation", action="store_true")
    sp.add_argument("--mode", choices=["count", "checksum", "full"])
    sp.set_defaults(fn=cmd_finalize)
    sp = sub.add_parser("lift", help="PHYSICAL migration for very large databases: striped compressed "
                                     "backup -> restore -> continuous log catch-up -> minutes-long cut-over")
    common(sp, workers=False)
    sp.add_argument("action", choices=["plan", "run", "seed", "resume", "catchup", "follow", "cutover",
                                       "verify", "status", "ag-script"])
    sp.add_argument("-d", "--databases", nargs="+", help="override lift.databases")
    sp.add_argument("--follow-hours", type=float, default=0.0, help="keep catching up for N hours (0 = once)")
    sp.add_argument("--restart", action="store_true", help="seed again from a new full backup")
    sp.add_argument("--confirm", action="store_true", help="required for cutover")
    sp.add_argument("--force", action="store_true", help="run despite plan blockers")
    sp.add_argument("--ag-name", default="ag_migrate")
    sp.add_argument("--source-host")
    sp.add_argument("--target-host")
    sp.set_defaults(fn=cmd_lift)
    sp = sub.add_parser("validate", help="compare source and target (run again after cut-over)")
    common(sp)
    sp.add_argument("--mode", choices=["count", "checksum", "full"])
    sp.set_defaults(fn=cmd_validate)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    try:
        return args.fn(args)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted — re-run with --resume to continue", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.getLogger("mssql_ocp_migrate").debug("fatal", exc_info=True)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
