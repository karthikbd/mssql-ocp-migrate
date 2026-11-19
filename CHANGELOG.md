# Changelog

All notable changes to `mssql-ocp-migrate` are documented here.

## [0.5.0] - 2026-11-18

Same quality and supply-chain checks as lodestream.

### Fixed
- **Full validation could miss a difference.** Row fingerprints joined column values with a
  separator byte that the values themselves may contain, so two different rows could fingerprint the
  same (e.g. text `"x\x1esy", "z"` and `"x", "y\x1esz"`), and two different keys could collide. Each value
  is now length-prefixed. Found by fuzzing.
- A configuration value of the wrong type (`port = "1433"`, `include = "dbo.*"`, `source = 5`, ...)
  failed later with a Python error, or not at all; it is now rejected at start-up with a message that
  names the setting and the expected type. `compatibility_level` takes a number or text. Found by fuzzing.

### Added
- ClusterFuzzLite (Atheris) fuzzing of config loading, row fingerprints, chunk predicates and
  identifier quoting, on every pull request, push and daily
- End-to-end CI runs every version pair with all three drivers: bundled pytds, pyodbc + ODBC
  Driver 18, and pymssql
- `ruff` and `mypy` in CI (both clean); `CONTRIBUTING.md`; private vulnerability reporting;
  OpenSSF Best Practices badge

## [0.4.0] - 2026-11-18

Gaps found by comparing with dbatools, Microsoft's migration tooling (DMA, Azure DMS, SqlPackage)
and Microsoft's list of features SQL Server on Linux doesn't support.

### Added
- `mssql-migrate assess`: readiness assessment for SQL Server on Linux / OpenShift (xp_cmdshell, OLE Automation, Windows paths, non-SQL Server linked servers and distributed queries, CLR `EXTERNAL_ACCESS`/`UNSAFE`, CmdExec/PowerShell/SSIS job steps, Agent alerts, Windows users, FILESTREAM, mirroring, merge replication, Stretch, EKM, secure enclaves), version and compatibility level, edition and size, server collation and time zone, `sp_configure` differences, target free space, cross-database references, encrypted modules, removed syntax. Also runs before `migrate` (copy blockers stop it; `ignore_blockers`) and in `lift plan`
- `mssql-migrate sync`: change-tracking catch-up after `migrate`, `--follow`, and `--final` for the cut-over (foreign keys re-trusted, identities and sequences aligned, validation); `migrate` records the baseline version; `sync_enable_change_tracking`
- Modules keep their `ANSI_NULLS` / `QUOTED_IDENTIFIER` settings; trigger firing order; object-level owners; disabled indexes (created disabled before the load)
- `sql_variant` values keep their base type, precision, scale and collation
- SQL Server 2025 `json` and `vector` columns; vector indexes reported
- FILESTREAM columns created as `varbinary(max)` (no FILESTREAM on Linux); `ROWGUIDCOL` and `IDENTITY ... NOT FOR REPLICATION`; `ANSI_PADDING OFF` columns reported
- Logins keep their default language, user-defined server roles and server-level permissions (`migrate` and `lift`; `lift` now copies Windows logins too)
- Copied jobs keep the operator they notify; jobs with steps SQL Agent on Linux can't run are skipped and reported
- `copy_custom_messages` (default on), `copy_server_configuration`, `copy_server_triggers`, `create_target_database`
- `lift`: the database master key is re-encrypted by the target's service master key (`database_master_key_password_env`)
- Validation compares module definitions, their settings, disabled state and trigger order, object owners, and reports references that no longer resolve on the target
- End-to-end CI across SQL Server versions: 2022 → 2022, 2019 → 2022, 2022 → 2025, 2025 → 2025

### Fixed
- Tables with `text`, `ntext` or `image` columns failed to load with the bundled driver ("Bulk load data was expected but not sent"); they now load with INSERT statements
- Server-level permissions are granted from `master` (they failed when the session was in another database)

## [0.3.0] - 2026-11-17

### Added
- `migrate` copies index storage options (fill factor, `PAD_INDEX`, `IGNORE_DUP_KEY`, lock options) and `ROW`/`PAGE`/columnstore-archive compression for indexes, primary keys and heaps (`copy_index_options`)
- Index build options after the load: `index_sort_in_tempdb`, `index_maxdop`, `index_online`
- User-created statistics with filters and `NORECOMPUTE` (`copy_statistics`, `statistics_fullscan`)
- Extended properties on the database, schemas, tables, columns, views, code, indexes, constraints and triggers (`copy_extended_properties`)
- Database users, roles, role owners and memberships, schema owners and `GRANT`/`DENY` permissions down to column level (`copy_security`); users whose login is missing on the target are reported as errors
- Compatibility level, read-committed snapshot and snapshot isolation (`copy_database_settings`, `compatibility_level`)
- Warning when the source and target database collations differ
- `lift.update_statistics_after` (`sp_updatestats` after cut-over); `lift plan` warns when the target is a newer SQL Server version
- Columns: dynamic data masking (`copy_data_masking`; stops if the source login lacks `UNMASK`), sparse columns and column sets, typed XML, CLR user-defined types, Always Encrypted columns copied as ciphertext with the bundled driver
- Tables: system-versioned temporal tables (`restore_system_versioning`), memory-optimized tables (`memory_optimized`), partitioned tables and indexes with per-partition compression (`partition_filegroups`), change tracking (`copy_change_tracking`) and CDC (`copy_cdc`)
- XML (primary and secondary), spatial and full-text indexes, full-text catalogs and stoplists (`copy_full_text`)
- Objects: table types, XML schema collections, partition functions and schemes, CLR assemblies/procedures/functions/triggers/aggregates (`trust_clr_assemblies`), Always Encrypted column master and encryption keys, rules and defaults with bindings, database DDL triggers, plan guides, Service Broker message types, contracts, queues, services, routes, remote bindings and event notifications (`copy_service_broker`)
- Security: certificates with private keys, certificate users and module signatures (`copy_certificates`, `master_key_password_env`), row-level security policies switched on after validation (`copy_row_level_security`; stops if a policy hides source rows), database owner
- Settings: every database option, database scoped configurations, Query Store
- Server level: logins of mapped users with the same SID and password hash (`copy_logins`), SQL Agent jobs created disabled (`copy_agent_jobs`) and linked servers (`copy_linked_servers`), for both `migrate` and `lift`
- Skipped tables (graph, FileTable, external, ledger) are reported as errors
- Validation compares all of the above; `schema` scripts include them in run order
- End-to-end suite runs in CI against two SQL Server 2022 containers (`e2e.yml`)

### Fixed
Found by the new end-to-end suite running against real SQL Server 2022 (it now runs in CI on every push):
- `varbinary(max)` values are written to `INSERT BULK` with an unknown PLP length; the known length the bundled driver wrote could corrupt the row stream (rows rejected with "invalid column length" in later columns)
- `binary(n)` columns get `BINARY` bulk metadata, and `char`/`nchar` are declared as `(n)varchar` to match the metadata sent; both were rejected as "Invalid column type"
- `xml` (typed or not) and column sets are bulk-loaded as `nvarchar(max)`; tables with a column set load the column set instead of its sparse columns
- `hierarchyid` travels as its path text, and tables with `hierarchyid` or size-capped CLR types load with `INSERT`
- Byte-string parameters are sent as binary with the bundled driver (they were sent as text)
- If `INSERT BULK` rejects a column, the error names it and the table falls back to `INSERT` on retry; chunks that needed a retry are listed in the run's warnings
- Worker processes are started with `spawn` instead of `fork` (forking a multi-threaded process can deadlock); scripts using the Python API need an `if __name__ == "__main__":` guard
- `lift seed` takes a regular full backup when no backup has started the log chain yet (a `COPY_ONLY` one would make every log backup fail)
- Unique constraints on memory-optimized tables no longer generate `NONCLUSTERED` twice
- Identity reseed failures on memory-optimized tables are warnings (DBCC CHECKIDENT doesn't support them)

### Security
- New `verify-release.yml`: after every publish, verifies the Sigstore signature of each release asset (wheel, sdist, SBOM) against `python-publish.yml`, the tag and the `release` trigger, and confirms PyPI serves byte-identical files
- Read the Docs installs the same hash-locked docs tooling as CI
- Docs tooling locked for Python 3.12, the docs build version (Sphinx 9.1+ no longer supports 3.10/3.11)

### Docs
- Signature check now pins the certificate identity, ref and trigger; `--offline` note for proxies that block Sigstore's update server
- README air-gapped install line no longer names a specific version

## [0.2.1] - 2026-11-16

### Security
- Every CI `pip install` hash-locked (`ci/requirements-*.txt`, `--require-hashes`); package installed with `--no-deps`
- New `vulnerability-audit.yml`: `pip-audit` of the vendored python-tds and all CI tooling on every change and weekly
- Releases: Sigstore-signed wheel, sdist and a CycloneDX SBOM (declaring the vendored driver) attached to the GitHub release
- Dependabot updates grouped (paired actions such as CodeQL init/analyze now move together)

### Added
- Installation docs for Windows and corporate networks (JFrog Artifactory / Curation), signature verification
- Supply-chain and vulnerability-management section in the security docs; `setup.py` shim; `.codacy/cli.sh`

## [0.2.0] - 2026-11-16

First public release.

### Added
- **Physical migration (`mssql-migrate lift`)** for very large databases: striped, compressed,
  copy-only backups; restore `WITH NORECOVERY` across several volumes; continuous log catch-up that
  also applies log backups taken by existing jobs (from `msdb`, with `path_map`) and refuses broken
  chains (gaps, NUL, third-party VDI backups); minutes-long cut-over with a tail-log backup and
  automatic undo of replayed READ_ONLY/SINGLE_USER; logins copied with the same SID and password
  hash; TDE certificates copied over the connection; `plan` from real `msdb` history; `verify`;
  `ag-script` for availability-group seeding
- **Logical migration (`mssql-migrate migrate`)**: chunked, parallel, resumable copy with identity
  preservation and lossless temporal/CLR transport; schema, indexes, constraints and programmable
  objects; scale-out across pods with `--shard` and `finalize`; minimally logged loads
- **Validation**: schema parity, row counts, server-side checksums, SHA-256 row comparison naming the
  exact keys that differ; HTML and JSON reports
- **Drivers**: bundled pure-Python TDS driver (python-tds 1.17.1, MIT) with TLS on the standard
  library; optional `pymssql` and `pyodbc` when already installed
- OpenShift manifests (Jobs, Indexed Job, lift share/volumes), documentation (Sphinx, Read the Docs)
- CI: tests on Python 3.10–3.13, docs build, CodeQL, OpenSSF Scorecard, Dependabot; PyPI Trusted
  Publishing (`python-publish.yml`, environment `release`); every GitHub Action pinned to a commit SHA
