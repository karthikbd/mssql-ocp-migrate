# Release notes

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
