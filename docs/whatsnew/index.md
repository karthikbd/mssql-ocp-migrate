# Release notes

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
