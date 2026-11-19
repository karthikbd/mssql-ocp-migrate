# Release notes

## 0.5.1 (November 2026)

A security fix found while writing the threat model, and the groundwork for the OpenSSF silver and
baseline badges.

- **Schema scripts no longer contain certificate private keys.** `mssql-migrate schema -o FILE` wrote
  each certificate's private key together with the one-time password protecting it, so anyone who could
  read the file could recover the key. The script now leaves private keys out and says how to move them
  (`migrate` still copies them over the connection). If you wrote a schema script with
  `copy_certificates` on, delete it and consider those private keys exposed to whoever could read it.
- Governance, code of conduct, roadmap, a security assurance case, dependency and scanning policy,
  support and end-of-life policy, and an OpenVEX document.
- Reproducible builds (checked in CI), `mypy --strict`, `ruff` security rules (errors that were silently
  ignored are now logged at debug level), an 80% coverage floor, DCO and dependency review on pull
  requests; `main` accepts changes only through pull requests.

## 0.5.0 (November 2026)

The same quality and supply-chain checks as lodestream, and two fixes found by fuzzing:

- **Full validation could miss a difference.** Row fingerprints joined column values with a
  separator byte that the values themselves may contain, so two different rows could fingerprint
  the same (for example text `"x\x1esy", "z"` and `"x", "y\x1esz"`), and two different keys could
  collide. Every value is now length-prefixed.
- **Wrong value types in the configuration** (`port = "1433"`, `include = "dbo.*"`, a section
  that isn't a table) are rejected at start-up with a message naming the setting and the expected
  type, instead of failing later. `compatibility_level` accepts `150` or `"150"`.
- **pymssql and pyodbc fixes**, found by running the end-to-end suite with each driver: with
  pymssql, binary values were sent as text, UTF-8 `varchar` was mangled on read and long `(max)`
  values broke bulk copy; with pyodbc, `text`/`ntext`/`image` failed with `MemoryError`, CLR and
  `hierarchyid` columns with a cast error, a failed chunk left `IDENTITY_INSERT` on, and pooled
  connections carried a transaction into validation (pooling is now off).
- **Fuzzing** with ClusterFuzzLite (Atheris) on every pull request, push and daily: config
  loading, row fingerprints, chunk predicates and identifier quoting.
- **All three drivers end to end:** every SQL Server version pair now runs with the bundled
  pytds, pyodbc + Microsoft ODBC Driver 18, and pymssql.
- `ruff` and `mypy` in CI, `CONTRIBUTING.md`, private vulnerability reporting and an OpenSSF Best
  Practices badge.

## 0.4.0 (November 2026)

**Gaps found against other migration tools**: dbatools, Microsoft's migration tooling (DMA, Azure
DMS, SqlPackage) and the list of features SQL Server on Linux doesn't support.

- **Readiness assessment** ({doc}`/user_guide/assess`): `mssql-migrate assess` finds what won't
  work, or will behave differently, on SQL Server on Linux in OpenShift (`xp_cmdshell`, OLE
  Automation, Windows paths, non-SQL Server linked servers, CLR `UNSAFE`, CmdExec/PowerShell job
  steps, Windows users, FILESTREAM, mirroring, merge replication, ...), plus version, edition,
  storage, server collation and time zone, `sp_configure` differences and cross-database
  references. `migrate` and `lift plan` run it too.
- **Online catch-up** ({doc}`/user_guide/sync`): `mssql-migrate sync` applies the changes made
  after the copy using change tracking, so the application keeps running and cut-over takes
  minutes; `--final` re-trusts foreign keys, aligns identities and sequences, and validates.
- **Exact behaviour of code:** modules keep their `ANSI_NULLS` / `QUOTED_IDENTIFIER` settings (a
  procedure created with `ANSI_NULLS OFF` used to come back with `= NULL` behaving differently),
  trigger firing order, object owners, and disabled indexes.
- **Types:** `sql_variant` keeps each value's base type; SQL Server 2025 `json` and `vector`;
  FILESTREAM columns become `varbinary(max)`; `ROWGUIDCOL` and `NOT FOR REPLICATION` are kept.
- **Server objects:** user-defined messages the code raises; logins' default language, server roles
  and server-level permissions; job operators; optional `sp_configure` alignment and server
  triggers; jobs SQL Agent on Linux can't run are reported instead of failing.
- **lift:** re-encrypts the database master key by the target's service master key, copies Windows
  logins, server roles and permissions, and blocks FILESTREAM restores to Linux in `plan`.
- **Validation** compares module definitions and their settings, and reports references that no
  longer resolve on the target.
- `create_target_database` creates the target with the source's collation.
- The end-to-end suite runs in CI for 2022 → 2022, 2019 → 2022, 2022 → 2025 and 2025 → 2025.

## 0.3.0 (November 2026)

**`migrate` now carries over everything inside a database that it used to leave behind**, and
validation checks each item:

- **index storage options:** fill factor, `PAD_INDEX`, `IGNORE_DUP_KEY`, row and page lock options,
  and `ROW`/`PAGE`/columnstore-archive compression on every index, primary key and heap;
- faster index builds after the load: `SORT_IN_TEMPDB`, `MAXDOP` and optional `ONLINE`;
- **user-created statistics**, with filters and `NORECOMPUTE` (optionally `FULLSCAN`);
- **extended properties** (`MS_Description` and others) on every kind of object;
- **security:** database users, roles, role owners and memberships, schema owners (empty schemas
  included) and `GRANT`/`DENY` down to column level. Users whose login is missing on the target
  are reported, not silently skipped;
- **database settings:** compatibility level (keep the source's, the target's, or a chosen
  level), read-committed snapshot and snapshot isolation;
- a warning when the database default collation differs;
- **columns:** dynamic data masks (the run stops if the source login can't `UNMASK`), sparse
  columns and column sets, typed XML (`DOCUMENT`/`CONTENT` with its schema collection), CLR
  user-defined types, and Always Encrypted columns copied as ciphertext;
- **tables:** system-versioned temporal tables (history copied, versioning and retention
  restored), memory-optimized tables (filegroup created on the target; or `memory_optimized =
  "disk"`), partitioned tables and indexes with per-partition compression, change tracking and
  change data capture;
- **indexes:** XML (primary and secondary), spatial and full-text indexes with their catalogs and
  stoplists;
- **database objects:** user-defined table types, XML schema collections, partition functions and
  schemes, CLR assemblies, procedures, functions, triggers and aggregates, Always Encrypted keys,
  rules and defaults with their bindings, database DDL triggers, plan guides, and Service Broker
  message types, contracts, queues, services, routes and event notifications;
- **security:** certificates with private keys, certificate users and module signatures,
  row-level security policies (switched on after validation; the run stops if a policy hides
  source rows), database owner, every database option and scoped configuration, Query Store;
- **server level:** the logins used by the database's users (same SID and password hash), and
  optionally SQL Agent jobs (created disabled) and linked servers;
- `schema_script` writes all of this in run order, and skipped tables (graph, FileTable,
  external, ledger) are reported as errors instead of being dropped silently.

`lift` gains `update_statistics_after` (`sp_updatestats` after cut-over), `copy_agent_jobs` and
`copy_linked_servers`, and `lift plan` warns when the target is a newer SQL Server version. The
end-to-end suite now runs in CI against two SQL Server 2022 containers.

**Fixes found by running against real servers.** The bulk-insert path wrote `varbinary(max)` in
a way that could corrupt the row stream, and rejected `binary(n)`, `char`/`nchar`, `xml`,
column sets and `hierarchyid` columns; all of these now load, and if SQL Server ever rejects a
column the error names it and the table falls back to `INSERT`. Byte-string parameters are sent
as binary. Worker processes are spawned rather than forked, so scripts that use the Python API
need an `if __name__ == "__main__":` guard. `lift seed` no longer uses `COPY_ONLY` on a database
whose log chain was never started. See the changelog for the full list. Each new copy step has a `copy_*` switch that also
controls its validation; all default to on.

## 0.2.1 (November 2026)

Supply-chain hardening, no functional changes:

- every CI `pip install` is hash-locked, and the package is installed with `--no-deps`;
- a weekly and per-change `pip-audit` of the bundled python-tds and all CI tooling;
- Sigstore-signed wheel, sdist and CycloneDX SBOM on each GitHub release (the SBOM declares the
  bundled driver for JFrog Xray / Curation);
- Dependabot updates grouped;
- installation docs for Windows and JFrog Artifactory / Curation.

## 0.2.0 (November 2026)

**Physical migration for very large databases.** New `mssql-migrate lift` commands (`plan`, `run`,
`seed`, `resume`, `catchup`, `follow`, `cutover`, `verify`, `status`, `ag-script`):

- striped (up to 64 files), compressed, `COPY_ONLY` full backups with tuned `MAXTRANSFERSIZE` and
  `BUFFERCOUNT`, restored `WITH NORECOVERY` with data files spread across several volumes;
- continuous log catch-up that also applies log backups taken by existing backup jobs (found in
  `msdb`, paths translated with `path_map`), with checks for gaps, NUL backups and
  third-party (VDI) backups;
- a minutes-long cut-over with a tail-log backup (`norecovery` or `read_only`), automatic undo of
  the replayed read-only and single-user settings, and post-recovery steps: logins copied with the
  same SID and password hash, owner, database options, orphaned users;
- TDE certificates copied over the connection;
- `plan` uses real compression ratio, speed and log volume from `msdb`, and reports blockers (S3
  size limit, version, VDI log backups) and a timeline;
- `verify`: metadata row counts plus sampled server-side checksum windows;
- `ag-script`: a cluster-less availability group with compressed automatic seeding.

**Logical engine**

- Scale-out across pods: `migrate --shard I/N` (or `auto/N` in an Indexed Job) plus `finalize`.
- `target_recovery_simple` for minimally logged loads.
- `create_indexes_after_load = false` builds indexes with the tables.
- Range predicates cast parameters to the key column's type, so index seeks work.

**Drivers**

- `pymssql` supported as an optional driver (`auto` order: pyodbc, pymssql, bundled), with
  FreeTDS bulk copy, full temporal precision, and INSERT used where bulk copy would replace NULL
  with a column default.

**Other**

- The default executor is `auto` (processes for the pure-Python driver).
- Long BACKUP/RESTORE statements are drained to completion and errors are always raised.
- This documentation.

## 0.1.0 (November 2026)

First release: logical migration with a bundled pure-Python driver (no runtime dependencies),
stdlib TLS, chunked parallel copy with resume, identity preservation, lossless temporal and CLR
transport, schema and object migration, three validation modes with key-level differences, HTML
and JSON reports, throughput estimation, and OpenShift Job manifests.
