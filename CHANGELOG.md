# Changelog

All notable changes to `mssql-ocp-migrate` are documented here.

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
