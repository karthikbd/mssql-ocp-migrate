"""Readiness assessment: what in the source database won't work, or will behave differently, once it
runs on SQL Server on Linux in OpenShift (or on another target).

Draws on Microsoft's list of features SQL Server on Linux doesn't support, its "metadata to manage
when moving a database to another server" checklist, the Data Migration Assistant's feature-parity
rules, and dbatools' instance-migration scope. Every check is independent: one that fails for lack
of permission is reported as skipped and the rest still run.

Severities: ``blocker`` (the migration or the application will fail), ``warning`` (works
differently or needs a manual step), ``info`` (worth knowing).
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

BLOCKER, WARNING, INFO = "blocker", "warning", "info"
# areas whose blockers stop the copy itself (the rest stop the application, not the migration)
COPY_BLOCKING_AREAS = {"Version", "Edition", "Storage", "Restore"}
SEVERITY_ORDER = {BLOCKER: 0, WARNING: 1, INFO: 2}

# SQL Agent subsystems SQL Server on Linux can't run (CmdExec, PowerShell, SSIS, SSAS, SSRS, Queue Reader)
LINUX_UNSUPPORTED_SUBSYSTEMS = {"CMDEXEC", "POWERSHELL", "SSIS", "ANALYSISQUERY", "ANALYSISCOMMAND",
                                "QUEUEREADER", "ACTIVESCRIPTING"}
SQL_SERVER_PROVIDERS = {"SQLNCLI", "SQLNCLI10", "SQLNCLI11", "MSOLEDBSQL", "MSOLEDBSQL19", "SQLOLEDB"}

# sp_configure options a database's code or behaviour can depend on; compared between servers
RELEVANT_CONFIGURATION = [
    "clr enabled", "clr strict security", "xp_cmdshell", "Ole Automation Procedures",
    "Ad Hoc Distributed Queries", "cross db ownership chaining", "nested triggers", "remote access",
    "contained database authentication", "max text repl size (B)", "default full-text language",
    "default language", "disallow results from triggers", "max degree of parallelism",
    "cost threshold for parallelism", "optimize for ad hoc workloads", "user options",
    "two digit year cutoff", "Database Mail XPs", "external scripts enabled", "transform noise words",
]
# the subset that's safe to set on the target to match the source (copy_server_configuration)
APPLICABLE_CONFIGURATION = {
    "clr enabled", "clr strict security", "cross db ownership chaining", "nested triggers", "remote access",
    "contained database authentication", "max text repl size (B)", "default full-text language",
    "default language", "disallow results from triggers", "max degree of parallelism",
    "cost threshold for parallelism", "optimize for ad hoc workloads", "user options", "two digit year cutoff",
    "transform noise words",
}

# (pattern, severity, message, fix) applied to procedure/function/view/trigger text and job steps
CODE_RULES: List[Tuple[str, str, str, str]] = [
    (r"\bxp_cmdshell\b", BLOCKER, "calls xp_cmdshell, which SQL Server on Linux doesn't support",
     "move the shell work out of the database (an OpenShift Job or CronJob)"),
    (r"\bsp_OA(Create|Method|GetProperty|SetProperty|Destroy|GetErrorInfo)\b", BLOCKER,
     "uses OLE Automation (sp_OA*), which SQL Server on Linux doesn't support",
     "replace it with SQL CLR (SAFE), external code, or built-in functions"),
    (r"\bxp_(regread|regwrite|instance_regread|instance_regwrite|regdeletevalue|regenumvalues|"
     r"fixeddrives|dirtree|fileexist|availablemedia|subdirs|getnetname|loginconfig|ntsec_enumdomains)\b",
     WARNING, "uses a Windows-specific extended procedure (registry, drives or files)",
     "replace it: the Windows registry and drive letters don't exist on Linux"),
    (r"\b(BULK\s+INSERT\b[^;]*?\bFROM|OPENROWSET\s*\(\s*BULK)\s*N?'(\\\\|[A-Za-z]:\\)", WARNING,
     "reads a file by a Windows path (drive letter or UNC share)",
     "use a Linux path the SQL Server pod can see (a mounted volume); UNC shares aren't supported"),
    (r"\b(OPENROWSET|OPENDATASOURCE)\s*\(\s*N?'(?!SQLNCLI|MSOLEDBSQL|SQLOLEDB)[^']*'", BLOCKER,
     "runs a distributed query through a non-SQL Server provider, which SQL Server on Linux doesn't support",
     "load the data another way (an ETL job, PolyBase to supported sources, or a SQL Server linked server)"),
    (r"\bsp_send_dbmail\b", WARNING, "sends e-mail with Database Mail",
     "configure Database Mail profiles and accounts on the target (passwords can't be copied)"),
    (r"\bsp_start_job\b|\bsp_add_job\b", INFO, "starts or creates SQL Agent jobs", "copy the jobs too"),
    (r"\bxp_logininfo\b|\bSUSER_SNAME\s*\(|\bIS_MEMBER\s*\(\s*N?'[^']*\\", INFO,
     "checks Windows logins or groups", "configure Active Directory authentication on the target"),
    (r"@@SERVERNAME\b|\bSERVERPROPERTY\s*\(\s*N?'(MachineName|ServerName|ComputerNamePhysicalNetBIOS)'", INFO,
     "depends on the server's name, which becomes the pod's host name on OpenShift",
     "check the logic after cut-over"),
    (r"\bEXEC(UTE)?\s+AS\s+LOGIN\b", INFO, "impersonates a login (EXECUTE AS LOGIN)",
     "make sure that login exists on the target"),
    (r"\*=|=\*", WARNING, "uses the old *= / =* outer-join syntax (if this isn't inside a comment or string)",
     "rewrite with LEFT/RIGHT OUTER JOIN; it fails at compatibility level 90 and above"),
    (r"\bRAISERROR\s+\d+\s+N?'", BLOCKER, "uses the old RAISERROR integer 'text' syntax, removed in SQL Server 2012",
     "use RAISERROR (n, severity, state) or THROW"),
    (r"\bFASTFIRSTROW\b", BLOCKER, "uses the FASTFIRSTROW hint, removed in SQL Server 2012", "use OPTION (FAST 1)"),
    (r"\bsp_dboption\b", BLOCKER, "calls sp_dboption, removed in SQL Server 2012", "use ALTER DATABASE"),
    (r"\bCOMPUTE\s+(SUM|AVG|COUNT|MIN|MAX)\b", BLOCKER, "uses COMPUTE / COMPUTE BY, removed in SQL Server 2012",
     "use ROLLUP or GROUPING SETS"),
]


@dataclass
class Finding:
    severity: str
    area: str            # Linux | Code | Agent | Security | Server | Edition | Version | Database | Storage
    item: str
    message: str
    fix: str = ""

    def text(self) -> str:
        return f"[{self.severity}] {self.area}: {self.item}: {self.message}" + (f" -> {self.fix}" if self.fix else "")


@dataclass
class Assessment:
    findings: List[Finding]
    source: Dict[str, Any]
    target: Dict[str, Any]

    @property
    def blockers(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == BLOCKER]

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "findings": [asdict(f) for f in self.findings]}


def _strip_comments(sql: str) -> str:
    """Code without comments and string literals' contents, so rules don't match commented-out code.
    String bodies are kept for the rules that look at paths and providers (they need them)."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def scan_code(objects: Iterable[Tuple[str, str]]) -> List[Finding]:
    """Apply CODE_RULES to (object name, definition) pairs."""
    out: List[Finding] = []
    compiled = [(re.compile(p, re.I | re.S), sev, msg, fix) for p, sev, msg, fix in CODE_RULES]
    for name, definition in objects:
        if not definition:
            continue
        text = _strip_comments(definition)
        for rx, sev, msg, fix in compiled:
            if rx.search(text):
                out.append(Finding(sev, "Code", name, msg, fix))
    return out


class Assessor:
    """Runs the checks against a source database and, when given, the target server."""

    def __init__(self, src: Any, tgt: Any = None, source_db: str = "", target_is_linux: Optional[bool] = None,
                 lift: bool = False) -> None:
        self.src, self.tgt, self.db, self.lift = src, tgt, source_db, lift
        self.findings: List[Finding] = []
        self.source: Dict[str, Any] = {}
        self.target: Dict[str, Any] = {}
        self._target_is_linux = target_is_linux

    # ------------------------------------------------------------------ helpers
    def add(self, severity: str, area: str, item: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(severity, area, item, message, fix))

    def _q(self, db: Any, sql: str, params: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
        return [tuple(r) for r in db.query(sql, params)] if params else [tuple(r) for r in db.query(sql)]

    def _one(self, db: Any, sql: str, params: Any = None) -> Any:
        rows = self._q(db, sql, params)
        return rows[0][0] if rows else None

    def _check(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # pragma: no cover - depends on permissions and versions
            log.debug("assessment check %s skipped: %s", name, exc)
            self.add(INFO, "Assessment", name, f"check skipped ({str(exc)[:160]})",
                     "grant VIEW SERVER STATE / VIEW ANY DEFINITION (or sysadmin) to run it")

    @property
    def target_is_linux(self) -> bool:
        if self._target_is_linux is not None:
            return self._target_is_linux
        return str(self.target.get("platform", "Linux")).lower() == "linux"   # OpenShift: Linux by definition

    # ------------------------------------------------------------------ run
    def run(self) -> Assessment:
        self._check("server facts", self._facts)
        checks = [("code", self._code), ("CLR assemblies", self._clr), ("Windows principals", self._windows_principals),
                  ("linked servers", self._linked_servers), ("SQL Agent", self._agent),
                  ("FILESTREAM", self._filestream), ("high availability and replication", self._ha_replication),
                  ("encryption", self._encryption), ("database features", self._database_features),
                  ("cross-database references", self._cross_database), ("server objects", self._server_objects),
                  ("encrypted modules", self._encrypted_modules)]
        for name, fn in checks:
            self._check(name, fn)
        if self.tgt is not None:
            for name, fn in [("version", self._version), ("edition", self._edition),
                             ("server collation and time zone", self._collation_timezone),
                             ("server configuration", self._configuration), ("storage", self._storage)]:
                self._check(name, fn)
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.area, f.item))
        return Assessment(self.findings, self.source, self.target)

    # ------------------------------------------------------------------ facts
    def _server_facts(self, db: Any) -> Dict[str, Any]:
        row = self._q(db, """
            SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int), CAST(SERVERPROPERTY('Edition') AS nvarchar(128)),
                   CAST(SERVERPROPERTY('EngineEdition') AS int), CAST(SERVERPROPERTY('Collation') AS nvarchar(128)),
                   DATEDIFF(MINUTE, GETUTCDATE(), GETDATE())""")[0]
        facts = {"major": int(row[0] or 0), "edition": row[1], "engine_edition": int(row[2] or 0),
                 "collation": row[3], "utc_offset_minutes": int(row[4] or 0)}
        try:
            facts["platform"] = self._one(db, "SELECT host_platform FROM sys.dm_os_host_info")
        except Exception:
            facts["platform"] = "Windows"     # sys.dm_os_host_info exists from 2017; older servers are Windows
        return facts

    def _facts(self) -> None:
        self.source = self._server_facts(self.src)
        self.source["database"] = self.db
        size = self._q(self.src, """SELECT CAST(SUM(CASE WHEN type = 0 THEN size END) AS bigint) * 8 / 1024,
                                          CAST(SUM(CASE WHEN type = 1 THEN size END) AS bigint) * 8 / 1024
                                   FROM sys.database_files""")[0]
        self.source["data_mb"], self.source["log_mb"] = int(size[0] or 0), int(size[1] or 0)
        self.source["compatibility_level"] = self._one(
            self.src, "SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()")
        if self.tgt is not None:
            self.target = self._server_facts(self.tgt)

    # ------------------------------------------------------------------ source checks
    def _code(self) -> None:
        objects = [(f"{s}.{n}", d) for s, n, d in self._q(self.src, """
            SELECT OBJECT_SCHEMA_NAME(m.object_id), OBJECT_NAME(m.object_id), m.definition
            FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id WHERE o.is_ms_shipped = 0""")]
        objects += [(f"DDL trigger {n}", d) for n, d in self._q(self.src, """
            SELECT t.name, m.definition FROM sys.triggers t JOIN sys.sql_modules m ON m.object_id = t.object_id
            WHERE t.parent_class = 0""")]
        self.findings += scan_code(objects)

    def _clr(self) -> None:
        for name, perm in self._q(self.src, """SELECT name, permission_set_desc FROM sys.assemblies
                                               WHERE is_user_defined = 1"""):
            if perm in ("EXTERNAL_ACCESS", "UNSAFE_ACCESS") and self.target_is_linux:
                self.add(BLOCKER, "Linux", f"assembly {name}",
                         f"CLR assembly with {perm.replace('_ACCESS', '')} permission: SQL Server on Linux only "
                         "runs SAFE assemblies", "rework the assembly to SAFE or move the logic outside the database")
            else:
                self.add(INFO, "Code", f"assembly {name}", f"CLR assembly ({perm})",
                         "the target needs 'clr enabled', and under 'clr strict security' a trusted or signed "
                         "assembly (trust_clr_assemblies)")

    def _windows_principals(self) -> None:
        rows = self._q(self.src, """SELECT name, type_desc FROM sys.database_principals
                                    WHERE type IN ('U', 'G') AND name NOT IN ('dbo', 'guest')""")
        for name, kind in rows:
            self.add(WARNING, "Security", f"user {name}",
                     f"{kind.lower().replace('_', ' ')}: Windows authentication on SQL Server on Linux needs Active "
                     "Directory integration (keytab, adutil) or the user must switch to a SQL login")

    def _linked_servers(self) -> None:
        for name, provider, product, self_cred in self._q(self.src, """
                SELECT s.name, s.provider, s.product,
                       (SELECT COUNT(*) FROM sys.linked_logins l WHERE l.server_id = s.server_id AND l.uses_self_credential = 1)
                FROM sys.servers s WHERE s.is_linked = 1"""):
            prov = str(provider or "").upper()
            if product != "SQL Server" and prov not in SQL_SERVER_PROVIDERS and self.target_is_linux:
                self.add(BLOCKER, "Linux", f"linked server {name}",
                         f"uses the {provider} provider: SQL Server on Linux only links to SQL Server",
                         "replace it with an ETL job or a SQL Server-to-SQL Server link")
            if self_cred and self.target_is_linux:
                self.add(WARNING, "Linux", f"linked server {name}",
                         "logins connect with their own Windows credentials (uses_self_credential), which "
                         "isn't supported on Linux", "map logins to SQL logins on the remote server")

    def _agent(self) -> None:
        db = self.db or self._one(self.src, "SELECT DB_NAME()")
        for job, step, subsystem in self._q(self.src, """
                SELECT j.name, s.step_name, s.subsystem FROM msdb.dbo.sysjobs j
                JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
                WHERE j.job_id IN (SELECT job_id FROM msdb.dbo.sysjobsteps WHERE database_name = ?)""", [db]):
            if str(subsystem).upper() in LINUX_UNSUPPORTED_SUBSYSTEMS and self.target_is_linux:
                self.add(WARNING, "Agent", f"job {job}",
                         f"step {step} uses the {subsystem} subsystem, which SQL Server Agent on Linux can't run "
                         "(copy_agent_jobs skips such jobs)",
                         "run it as an OpenShift CronJob, or rewrite the step in T-SQL")
        for name, _msg, _sev in self._q(self.src, """SELECT name, message_id, severity FROM msdb.dbo.sysalerts
                                                   WHERE database_name = ? OR database_name IS NULL""", [db]):
            if self.target_is_linux:
                self.add(WARNING, "Agent", f"alert {name}", "SQL Agent alerts aren't supported on Linux",
                         "monitor errors with Extended Events or your platform's monitoring")
        jobs = self._q(self.src, """SELECT j.name, s.command FROM msdb.dbo.sysjobs j
                                    JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
                                    WHERE s.database_name = ? AND s.subsystem = 'TSQL'""", [db])
        self.findings += [Finding(f.severity, "Agent", f"job {f.item}", f.message, f.fix)
                          for f in scan_code(jobs)]

    def _filestream(self) -> None:
        # FILESTREAM filegroups are type FD (memory-optimized containers are FILESTREAM-type files too, FX)
        if self._one(self.src, "SELECT COUNT(*) FROM sys.filegroups WHERE type = 'FD'"):
            fs_cols = self._q(self.src, """SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + OBJECT_NAME(object_id) + '.' + name
                                          FROM sys.columns WHERE is_filestream = 1""")
            if self.lift and self.target_is_linux:
                self.add(BLOCKER, "Restore", "FILESTREAM filegroup",
                         "the database has a FILESTREAM filegroup, which SQL Server on Linux can't restore",
                         "use migrate (FILESTREAM columns become varbinary(max)) or move the files out first")
            else:
                self.add(WARNING, "Linux", "FILESTREAM",
                         f"{len(fs_cols)} FILESTREAM columns ({', '.join(c[0] for c in fs_cols[:3])}): SQL Server on "
                         "Linux has no FILESTREAM, so migrate creates them as varbinary(max) and copies the data",
                         "check the size: the files' contents now live in the data files")

    def _ha_replication(self) -> None:
        row = self._q(self.src, """SELECT is_published, is_merge_published, is_subscribed, is_distributor
                                   FROM sys.databases WHERE name = DB_NAME()""")[0]
        pub, merge, sub, dist = (bool(x) for x in row)
        if merge and self.target_is_linux:
            self.add(BLOCKER, "Linux", "merge replication",
                     "the database is a merge publisher; SQL Server on Linux doesn't support merge replication")
        elif pub or merge:
            self.add(WARNING, "Database", "replication", "the database is published: publications aren't migrated",
                     "recreate publications and subscriptions after cut-over")
        if sub:
            self.add(INFO, "Database", "replication", "the database is a subscriber: subscriptions aren't migrated")
        if dist:
            self.add(WARNING, "Database", "replication", "the database is a distribution database")
        if self._one(self.src, """SELECT COUNT(*) FROM sys.database_mirroring
                                 WHERE database_id = DB_ID() AND mirroring_guid IS NOT NULL"""):
            self.add(WARNING, "Linux", "database mirroring",
                     "the database is mirrored; SQL Server on Linux doesn't support mirroring",
                     "use an availability group (lift ag-script) for HA on OpenShift")
        try:
            if self._one(self.src, """SELECT COUNT(*) FROM msdb.dbo.log_shipping_primary_databases
                                     WHERE primary_database = DB_NAME()"""):
                self.add(INFO, "Database", "log shipping", "the database is a log-shipping primary",
                         "lift reads its log backups (path_map); reconfigure log shipping after cut-over")
        except Exception:
            log.debug("ignored error", exc_info=True)
        try:
            if self._one(self.src, """SELECT is_remote_data_archive_enabled FROM sys.databases
                                     WHERE name = DB_NAME()"""):
                self.add(BLOCKER if self.lift else WARNING, "Restore" if self.lift else "Linux", "Stretch Database",
                         "Stretch DB is enabled; it's retired and not on Linux",
                         "bring the remote data back (unstretch) before migrating")
        except Exception:
            log.debug("ignored error", exc_info=True)

    def _encryption(self) -> None:
        try:
            providers = self._q(self.src, "SELECT name FROM sys.cryptographic_providers")
        except Exception:
            providers = []
        for (name,) in providers:
            self.add(WARNING, "Security", f"EKM provider {name}",
                     "Extensible Key Management provider on the source; on Linux only Azure Key Vault is supported "
                     "(SQL Server 2022 CU12+)", "keys protected by it must be re-protected on the target")
        try:
            for (name,) in self._q(self.src, "SELECT name FROM sys.column_master_keys WHERE allow_enclave_computations = 1"):
                self.add(WARNING, "Linux", f"column master key {name}",
                         "allows secure-enclave computations; Always Encrypted with secure enclaves isn't supported "
                         "on Linux", "queries that rely on enclaves will fail on the target")
        except Exception:
            log.debug("ignored error", exc_info=True)
        if self._one(self.src, "SELECT is_encrypted FROM sys.databases WHERE name = DB_NAME()"):
            self.add(INFO, "Security", "TDE",
                     "the database uses TDE" + ("; lift copies the certificate (transfer_tde_certificates)"
                                                 if self.lift else "; migrate copies data, so enable TDE on the "
                                                 "target yourself if required"))
        if self._one(self.src, """SELECT COUNT(*) FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##'"""):
            self.add(INFO, "Security", "database master key",
                     "the database has a master key: on a restored copy it must be re-encrypted with the target's "
                     "service master key (lift does this when master_key_password_env is set)")

    def _database_features(self) -> None:
        row = self._q(self.src, """SELECT is_auto_close_on, is_auto_shrink_on, page_verify_option_desc,
                                          recovery_model_desc, containment_desc
                                   FROM sys.databases WHERE name = DB_NAME()""")[0]
        if row[0]:
            self.add(INFO, "Database", "AUTO_CLOSE", "AUTO_CLOSE is ON (copied); it slows down busy workloads")
        if row[1]:
            self.add(INFO, "Database", "AUTO_SHRINK", "AUTO_SHRINK is ON (copied); it fragments indexes")
        if row[2] != "CHECKSUM":
            self.add(INFO, "Database", "PAGE_VERIFY", f"PAGE_VERIFY is {row[2]}; CHECKSUM is recommended")
        if row[4] and row[4] != "NONE":
            self.add(INFO, "Database", "containment", f"containment is {row[4]}: the target needs "
                     "'contained database authentication' = 1, and contained users' passwords can't be copied")
        for sch, name, typ in self._q(self.src, """
                SELECT SCHEMA_NAME(t.schema_id), t.name, ty.name FROM sys.columns c
                JOIN sys.tables t ON t.object_id = c.object_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
                WHERE ty.name IN ('text', 'ntext', 'image') AND t.is_ms_shipped = 0"""):
            self.add(INFO, "Code", f"{sch}.{name}", f"uses the deprecated {typ} type",
                     "plan a move to varchar(max) / nvarchar(max) / varbinary(max)")
        for (name,) in self._q(self.src, """SELECT name FROM sys.database_audit_specifications"""):
            self.add(WARNING, "Security", f"database audit specification {name}",
                     "not migrated: it depends on a server audit", "recreate the server audit and the specification")
        orphans = self._q(self.src, """SELECT dp.name FROM sys.database_principals dp
                                       LEFT JOIN sys.server_principals sp ON sp.sid = dp.sid
                                       WHERE dp.type IN ('S', 'U', 'G') AND dp.authentication_type_desc = 'INSTANCE'
                                         AND sp.sid IS NULL AND dp.name NOT IN ('dbo', 'guest')""")
        for (name,) in orphans:
            self.add(WARNING, "Security", f"user {name}", "orphaned on the source (its login doesn't exist)",
                     "fix or drop it before migrating; migrate reports it and doesn't create it")
        for (name, addr) in self._q(self.src, """SELECT name, address FROM sys.routes
                                               WHERE address NOT IN ('LOCAL', 'TRANSPORT')"""):
            self.add(WARNING, "Database", f"Service Broker route {name}",
                     f"points to {addr}, which is copied as is", "update it for the OpenShift service name/port")

    def _cross_database(self) -> None:
        seen = set()
        for obj, db, server in self._q(self.src, """
                SELECT DISTINCT OBJECT_SCHEMA_NAME(d.referencing_id) + '.' + OBJECT_NAME(d.referencing_id),
                       d.referenced_database_name, d.referenced_server_name
                FROM sys.sql_expression_dependencies d
                WHERE (d.referenced_database_name IS NOT NULL AND d.referenced_database_name <> DB_NAME())
                   OR d.referenced_server_name IS NOT NULL"""):
            key = (obj, db, server)
            if key in seen:
                continue
            seen.add(key)
            if server:
                self.add(WARNING, "Code", obj, f"queries linked server {server}",
                         "copy the linked server (copy_linked_servers) and make it reachable from OpenShift")
            elif db and db.lower() not in ("master", "msdb", "tempdb", "model"):
                self.add(WARNING, "Code", obj, f"references database {db}",
                         "migrate that database to the same target too, or change the reference")
        for sch, name, base in self._q(self.src, "SELECT SCHEMA_NAME(schema_id), name, base_object_name FROM sys.synonyms"):
            parts = [p.strip("[]") for p in re.findall(r"\[[^\]]+\]|[^.]+", base)]
            if len(parts) >= 3 and parts[-3].lower() != str(self._one(self.src, "SELECT DB_NAME()")).lower():
                self.add(WARNING, "Code", f"synonym {sch}.{name}", f"points to another database or server ({base})",
                         "migrate that database too, or repoint the synonym")

    def _server_objects(self) -> None:
        refs = self._q(self.src, "SELECT definition FROM sys.sql_modules")
        numbers = sorted({int(n) for (d,) in refs for n in re.findall(r"RAISERROR\s*\(\s*(\d{5,})", d or "", re.I)
                          if int(n) >= 50001})
        if numbers:
            have = {int(r[0]) for r in self._q(self.src, "SELECT DISTINCT message_id FROM sys.messages WHERE message_id >= 50001")}
            used = [n for n in numbers if n in have]
            if used:
                self.add(INFO, "Server", "user-defined messages",
                         f"the code raises {len(used)} user-defined messages ({', '.join(map(str, used[:5]))}): "
                         "copied with copy_custom_messages")
        try:
            for (name,) in self._q(self.src, "SELECT name FROM sys.server_triggers WHERE is_ms_shipped = 0"):
                self.add(WARNING, "Server", f"server trigger {name}",
                         "server-level DDL/logon trigger: not migrated by default", "set copy_server_triggers = true")
        except Exception:
            log.debug("ignored error", exc_info=True)
        try:
            for (name,) in self._q(self.src, """SELECT name FROM master.sys.procedures
                                               WHERE OBJECTPROPERTY(object_id, 'ExecIsStartup') = 1"""):
                self.add(INFO, "Server", f"startup procedure {name}", "runs at server start; lives in master",
                         "recreate it on the target if the database depends on it")
        except Exception:
            log.debug("ignored error", exc_info=True)
        try:
            for (name,) in self._q(self.src, "SELECT name FROM sys.server_event_sessions WHERE name NOT IN "
                                             "('system_health', 'AlwaysOn_health', 'telemetry_xevents', 'sp_server_diagnostics session')"):
                self.add(INFO, "Server", f"Extended Events session {name}", "server-level: not migrated",
                         "recreate it on the target if you need it")
        except Exception:
            log.debug("ignored error", exc_info=True)
        try:
            if self._one(self.src, "SELECT classifier_function_id FROM sys.resource_governor_configuration"):
                self.add(INFO, "Server", "Resource Governor", "a classifier function is configured: not migrated")
        except Exception:
            log.debug("ignored error", exc_info=True)
        try:
            for (name,) in self._q(self.src, "SELECT name FROM sys.credentials"):
                self.add(INFO, "Server", f"credential {name}", "secrets can't be read: recreate it if jobs, proxies "
                         "or linked servers use it")
        except Exception:
            log.debug("ignored error", exc_info=True)

    def _encrypted_modules(self) -> None:
        for (name,) in self._q(self.src, """SELECT OBJECT_SCHEMA_NAME(object_id) + '.' + OBJECT_NAME(object_id)
                                           FROM sys.sql_modules WHERE definition IS NULL"""):
            self.add(BLOCKER if not self.lift else INFO, "Code", name,
                     "created WITH ENCRYPTION: its definition can't be read" + ("" if self.lift else ", so migrate can't copy it"),
                     "deploy it from source control, or use lift")

    # ------------------------------------------------------------------ target checks
    def _version(self) -> None:
        sv, tv = self.source.get("major", 0), self.target.get("major", 0)
        if tv and sv and tv < sv:
            self.add(BLOCKER if self.lift else WARNING, "Version", "target version",
                     f"target is SQL Server {tv}, older than the source ({sv})",
                     "lift can't restore to an older version; migrate works but newer features may not exist")
        level = int(self.source.get("compatibility_level") or 0)
        if tv and level and level > tv * 10:
            self.add(BLOCKER, "Version", "compatibility level",
                     f"the source runs at compatibility level {level}; the target supports up to {tv * 10}",
                     f"set compatibility_level = \"{tv * 10}\" or \"target\"")
        if tv and level and level < 100 and tv >= 14:
            self.add(WARNING, "Version", "compatibility level",
                     f"level {level} isn't supported by the target; it will be raised to 100",
                     "test the application at the higher level")
        if tv and tv < 17:
            new_types = self._q(self.src, """SELECT OBJECT_SCHEMA_NAME(c.object_id) + '.' + OBJECT_NAME(c.object_id) + '.' + c.name
                                            FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.system_type_id
                                            WHERE t.name IN ('json', 'vector')""")
            for (col,) in new_types:
                self.add(BLOCKER, "Version", col, "uses a SQL Server 2025 type (json or vector) the target doesn't have",
                         "migrate to SQL Server 2025, or change the column to nvarchar(max)")

    def _edition(self) -> None:
        if self.target.get("engine_edition") == 4:          # Express
            mb = self.source.get("data_mb", 0)
            if mb > 10 * 1024:
                self.add(BLOCKER, "Edition", "database size",
                         f"the target is Express edition (10 GB limit) and the data is {mb / 1024:,.1f} GB")
        try:
            feats = [r[0] for r in self._q(self.src, "SELECT feature_name FROM sys.dm_db_persisted_sku_features")]
        except Exception:
            feats = []
        if feats and self.target.get("engine_edition") not in (3,):       # 3 = Enterprise / Developer / Evaluation
            self.add(INFO, "Edition", "edition features",
                     f"the database uses {', '.join(feats)}; check they're in the target's "
                     f"{self.target.get('edition')} (most are in every edition from SQL Server 2016 SP1)")

    def _collation_timezone(self) -> None:
        if self.source.get("collation") != self.target.get("collation"):
            self.add(WARNING, "Server", "server collation",
                     f"source server collation {self.source.get('collation')}, target {self.target.get('collation')}: "
                     "temp tables and variables follow the server (tempdb) collation, so joins between them and "
                     "your tables can fail with collation conflicts",
                     "set MSSQL_COLLATION on the target pod, or use COLLATE DATABASE_DEFAULT in temp tables")
        so, to = self.source.get("utc_offset_minutes"), self.target.get("utc_offset_minutes")
        if so is not None and to is not None and so != to:
            self.add(WARNING, "Server", "time zone",
                     f"the source server's clock is UTC{so / 60:+.1f}h, the target's UTC{to / 60:+.1f}h: GETDATE(), "
                     "SYSDATETIME() and defaults that use them will return different times",
                     "set TZ (and MSSQL_TIMEZONE) on the SQL Server pod to the source's time zone")

    def _configuration(self) -> None:
        def cfg(db: Any) -> Dict[str, int]:
            return {str(n): int(v) for n, v in self._q(db, """SELECT name, CAST(value_in_use AS int)
                                                             FROM sys.configurations""")}
        s, t = cfg(self.src), cfg(self.tgt)
        for name in RELEVANT_CONFIGURATION:
            if name in s and name in t and s[name] != t[name]:
                sev = WARNING if name in ("clr enabled", "cross db ownership chaining", "nested triggers",
                                          "contained database authentication", "xp_cmdshell",
                                          "Ole Automation Procedures", "Ad Hoc Distributed Queries",
                                          "disallow results from triggers", "user options") else INFO
                self.add(sev, "Server", f"sp_configure '{name}'", f"source {s[name]}, target {t[name]}",
                         "set copy_server_configuration = true to apply it" if name in APPLICABLE_CONFIGURATION
                         else "review: not applied automatically")

    def _storage(self) -> None:
        need = self.source.get("data_mb", 0) + self.source.get("log_mb", 0) // 4
        try:
            free = self._q(self.tgt, """SELECT TOP (1) CAST(vs.available_bytes / 1048576 AS bigint)
                                        FROM sys.master_files mf
                                        CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
                                        WHERE mf.database_id = DB_ID() AND mf.type = 0""")
        except Exception:
            free = []
        if free and free[0][0] is not None:
            self.target["free_mb"] = int(free[0][0])
            if int(free[0][0]) < need:
                self.add(BLOCKER, "Storage", "target volume",
                         f"the target data volume has {int(free[0][0]):,} MB free; the source uses about {need:,} MB",
                         "grow the PersistentVolumeClaim before migrating")


def assess(src: Any, tgt: Any = None, source_db: str = "", lift: bool = False,
           target_is_linux: Optional[bool] = None) -> Assessment:
    """Run every check. ``src``/``tgt`` are objects with ``query(sql, params=None)``; ``tgt`` may be None."""
    return Assessor(src, tgt, source_db, target_is_linux, lift).run()


def render(a: Assessment) -> str:
    """Plain-text report for the console."""
    lines = []
    s, t = a.source, a.target
    if s:
        lines.append(f"source: SQL Server {s.get('major')} {s.get('edition')} on {s.get('platform')}, "
                     f"{s.get('database')} ({s.get('data_mb', 0):,} MB data), compatibility {s.get('compatibility_level')}")
    if t:
        lines.append(f"target: SQL Server {t.get('major')} {t.get('edition')} on {t.get('platform')}"
                     + (f", {t['free_mb']:,} MB free" if "free_mb" in t else ""))
    counts = {k: sum(1 for f in a.findings if f.severity == k) for k in (BLOCKER, WARNING, INFO)}
    lines.append(f"{counts[BLOCKER]} blockers, {counts[WARNING]} warnings, {counts[INFO]} notes")
    for f in a.findings:
        lines.append("  " + f.text())
    return "\n".join(lines)
