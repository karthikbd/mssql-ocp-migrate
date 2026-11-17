# Changelog

All notable changes to `mssql-ocp-migrate` are documented here.

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
