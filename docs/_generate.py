"""Generate the configuration and CLI reference pages from the code itself.

Run automatically by ``conf.py`` on every build. The build fails if a configuration field has no
description here, so the reference can never silently drift from the code.
"""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from mssql_ocp_migrate import config as C
from mssql_ocp_migrate.cli import build_parser

OUT = Path(__file__).parent / "reference" / "_generated"

DESCRIPTIONS = {
    "ConnectionConfig": {
        "host": "Server host name or IP. For OpenShift use the Service DNS name inside the cluster, or "
                "``127.0.0.1`` with ``oc port-forward`` from outside.",
        "port": "TCP port. Ignored when ``instance`` is set (resolved through SQL Browser, UDP 1434).",
        "database": "Database to connect to. For ``lift`` this can be ``master``.",
        "user": "SQL login name.",
        "password": "Password in clear text. Avoid: prefer ``password_env`` or ``password_file``.",
        "password_env": "Name of an environment variable holding the password.",
        "password_file": "Path of a file holding the password, e.g. an OpenShift Secret mounted as a file. "
                         "Takes precedence over ``password_env``.",
        "instance": "Named instance (``host\\\\instance``).",
        "driver": "``auto`` (pyodbc if installed with the ODBC driver below, else pymssql if installed, "
                  "else the bundled driver), ``pytds`` (bundled, pure Python), ``pymssql`` or ``pyodbc``.",
        "odbc_driver": "ODBC driver name used when ``driver`` resolves to pyodbc.",
        "encrypt": "``off``, ``optional`` (encrypt when the server supports it) or ``required``.",
        "trust_server_certificate": "Encrypt without verifying the server certificate (self-signed "
                                    "certificates in containers).",
        "ca_file": "PEM bundle used to verify the server certificate. Default: the OS trust store.",
        "hostname_in_certificate": "Host name expected in the certificate when it differs from ``host`` "
                                   "(for example through ``oc port-forward``).",
        "login_timeout": "Seconds to wait for connect and login.",
        "query_timeout": "Seconds per statement; ``null`` = no limit (always used for BACKUP/RESTORE).",
        "app_name": "Application name shown in ``sys.dm_exec_sessions``.",
    },
    "MigrationConfig": {
        "include": "``schema.table`` glob patterns to migrate (case-insensitive).",
        "exclude": "``schema.table`` glob patterns to skip; applied after ``include``.",
        "workers": "Parallel chunk copies.",
        "executor": "``auto`` (processes for the bundled driver, threads when both sides use pyodbc), "
                    "``thread`` or ``process``.",
        "chunk_rows": "Rows per chunk: the unit of parallelism, retry and resume.",
        "batch_rows": "Rows fetched and sent per batch inside a chunk.",
        "schema_mode": "``create`` (create tables, indexes, constraints, objects) or ``skip`` (target "
                       "schema already deployed, e.g. from a DACPAC).",
        "if_target_not_empty": "``fail``, ``truncate`` (reload) or ``resume`` (continue from the state file).",
        "copy_programmable_objects": "Copy views, procedures, functions, triggers, synonyms, sequences "
                                     "and alias types.",
        "create_indexes_after_load": "Build nonclustered indexes and check constraints after the data "
                                     "load (faster) instead of with the tables.",
        "tablock": "Use ``TABLOCK`` when a table is loaded as a single chunk.",
        "state_file": "Checkpoint file recording which chunks are done.",
        "max_retries": "Retries per chunk on a fresh connection before it is marked failed.",
        "shard_index": "This process's shard (0-based). Usually set with ``--shard I/N`` or ``auto/N``.",
        "shard_count": "Number of shards in a scale-out run.",
        "plan_file": "Chunk plan shared by all shards; must be on a volume every shard can read.",
        "plan_wait_seconds": "How long a shard waits for shard 1 to write the plan.",
        "target_recovery_simple": "Switch the target to SIMPLE recovery during the load so bulk inserts "
                                  "are minimally logged; the original model is restored at the end.",
    },
    "ValidationConfig": {
        "mode": "``count``, ``checksum`` or ``full`` (see :doc:`/user_guide/validation`).",
        "sample_full_pct": "In ``checksum`` mode, the percentage of chunks also compared row by row.",
        "max_diff_rows": "Differing keys listed per chunk in the report.",
        "compare_schema": "Compare columns, keys, indexes, constraints and objects as well as data.",
    },
    "LiftConfig": {
        "databases": "Source databases to lift.",
        "rename": "Map of source name to target name.",
        "transport": "``share`` (NFS/SMB reachable by both servers), ``s3`` (SQL Server 2022, below "
                     "~12 TB compressed per backup) or ``manual`` (you copy the files).",
        "source_backup_dir": "Backup folder as the **source** server sees it.",
        "target_backup_dir": "The same folder as the **target** pod sees it.",
        "s3_url": "``s3://endpoint[:port]/bucket[/prefix]`` for ``transport = \"s3\"``.",
        "s3_access_key_env": "Environment variable with the S3 access key.",
        "s3_secret_key_env": "Environment variable with the S3 secret key.",
        "stripes": "Files per full backup, written and read in parallel (1-64).",
        "maxtransfersize": "I/O size for disk backups (64 KB-4 MB). S3 always uses 20 MB.",
        "buffercount": "Backup/restore buffers; ``0`` = stripes x 4, capped by ``memory_mb``.",
        "memory_mb": "Upper bound for ``buffercount`` x ``maxtransfersize``.",
        "compression": "Compress backups (fewer bytes on the network and the share).",
        "copy_only": "Take ``COPY_ONLY`` full backups so existing differential chains are untouched.",
        "target_data_dirs": "Folders for data files on the target; files are spread across them by size.",
        "target_log_dir": "Folder for the transaction log file on the target.",
        "log_interval_seconds": "Seconds between catch-up cycles in ``follow``/``run``.",
        "path_map": "Prefix map translating backup paths recorded by the source (existing log-backup "
                    "jobs) to paths the target can read.",
        "cutover_source": "``norecovery`` (tail-log backup leaves the source RESTORING), ``read_only`` "
                          "or ``none`` (you stop the application yourself).",
        "parallel_databases": "Databases seeded at the same time.",
        "copy_logins": "Create the SQL logins used by the database on the target with the same SID and "
                       "password hash.",
        "transfer_tde_certificates": "Copy the TDE certificate and private key over the connection.",
        "target_master_key_password_env": "Environment variable with the password for a new master key "
                                          "on the target (only needed when it has none).",
        "checkdb_after": "Run ``DBCC CHECKDB WITH PHYSICAL_ONLY`` after cut-over.",
        "verify_sample_rows": "Rows per checksum window in ``lift verify``.",
        "verify_windows_per_table": "Checksum windows per table in ``lift verify``.",
        "state_file": "Lift progress file (phase and restored LSN per database).",
        "network_gbps": "Link speed between source and OpenShift, for ``lift plan`` only.",
        "target_write_mb_s": "Target storage write speed, for ``lift plan`` only.",
        "assumed_compression_ratio": "Compression ratio used by ``lift plan`` when msdb has no history.",
    },
    "Config": {
        "source": "Connection to the source server (``[source]``).",
        "target": "Connection to the target server (``[target]``).",
        "migration": "Logical migration settings (``[migration]``).",
        "validation": "Validation settings (``[validation]``).",
        "report_dir": "Folder for ``report.json`` and ``report.html``.",
        "lift": "Physical migration settings (``[lift]``).",
    },
}

SECTIONS = [("[source] and [target]", C.ConnectionConfig), ("[migration]", C.MigrationConfig),
            ("[validation]", C.ValidationConfig), ("[lift]", C.LiftConfig), ("top level", C.Config)]


def _default(f: dataclasses.Field) -> str:
    if f.default is not dataclasses.MISSING:
        v = f.default
    elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        v = f.default_factory()  # type: ignore[misc]
        if dataclasses.is_dataclass(v):
            return "*(section)*"
    else:
        return "*required*"
    if v is None:
        return "``null``"
    if isinstance(v, bool):
        return f"``{str(v).lower()}``"
    if isinstance(v, str):
        return f'``"{v}"``' if v else '``""``'
    if isinstance(v, (list, dict)):
        import json
        return f"``{json.dumps(v)}``"
    return f"``{v:,}``" if isinstance(v, int) and v >= 10000 else f"``{v}``"


def _type(f: dataclasses.Field) -> str:
    t = str(f.type).replace("typing.", "")
    return {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
            "Optional[str]": "string or null", "Optional[int]": "integer or null",
            "List[str]": "list of strings", "Dict[str, str]": "table (string -> string)"}.get(t, t)


def config_reference() -> str:
    lines = []
    missing = []
    for title, dc in SECTIONS:
        desc = DESCRIPTIONS[dc.__name__]
        lines += [f"## {title}", "", "```{list-table}", ":header-rows: 1", ":widths: 22 14 18 46", "",
                  "* - Key", "  - Type", "  - Default", "  - Description"]
        for f in dataclasses.fields(dc):
            if f.name not in desc:
                missing.append(f"{dc.__name__}.{f.name}")
                continue
            lines += [f"* - ``{f.name}``", f"  - {_type(f)}", f"  - {_default(f)}", f"  - {desc[f.name]}"]
        lines += ["```", ""]
        extra = set(desc) - {f.name for f in dataclasses.fields(dc)}
        missing += [f"(stale description) {dc.__name__}.{x}" for x in extra]
    if missing:
        raise SystemExit("config reference out of date, describe: " + ", ".join(missing))
    return "\n".join(lines)


def cli_reference() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help for a in sub._choices_actions}
    out = ["## Global options", "", "```text", parser.format_help().strip(), "```", ""]
    for name, sp in sub.choices.items():
        out += [f"(cli-{name})=", f"## `mssql-migrate {name}`", "", helps.get(name, "") + ".", "",
                "```text", sp.format_help().replace("usage: mssql-migrate", "mssql-migrate").strip(), "```", ""]
    return "\n".join(out)


def generate() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "config_table.md").write_text(config_reference(), encoding="utf-8")
    (OUT / "cli.md").write_text(cli_reference(), encoding="utf-8")


if __name__ == "__main__":
    generate()
