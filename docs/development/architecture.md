# Architecture

```text
src/mssql_ocp_migrate/
├── cli.py          command line (argparse) → calls the classes below
├── config.py       dataclasses: Config, ConnectionConfig, MigrationConfig, ValidationConfig, LiftConfig
├── drivers.py      one Connection wrapper over pytds (bundled) / pymssql / pyodbc; "?" placeholders everywhere
├── adapter.py      Database interface the logical engine depends on (no SQL Server specifics)
├── sqlserver.py    SQL Server implementation: catalog, chunk predicates, INSERT BULK, checksums, identity probe
├── ddl.py          T-SQL DDL generation from catalog metadata
├── model.py        plain dataclasses: Table, Column, Index, ForeignKey, Chunk …
├── chunking.py     key-range chunks and lexicographic predicates (dialect-neutral)
├── engine.py       Migrator: preflight → schema → plan → parallel copy → post-load → validation; sharding
├── state.py        atomic checkpoint file with typed key encoding (Decimal, UUID, bytes, datetime …)
├── validate.py     schema parity, counts, server checksums, SHA-256 row diff, multiset digest
├── rowhash.py      driver-independent value normalisation and row fingerprints
├── estimate.py     sample-based throughput measurement and projection
├── report.py       report.json + self-contained report.html
├── physical.py     Lift: plan, seed, log-chain catch-up, cut-over, post-recovery, verify; pure T-SQL builders
└── _vendor/pytds   python-tds 1.17.1 (MIT), private namespace, stdlib-ssl TLS
```

## Design principles

**One unit of work, idempotent.** A chunk is a key range copied in one transaction that first
deletes its own range, so retries, resumes and shards never need coordination beyond a file.

**Separation of dialect and orchestration.** `engine.py` and `validate.py` know nothing about
T-SQL. They call {class}`~mssql_ocp_migrate.adapter.Database`. That's what lets the full engine,
including parallelism, failure injection, resume and corruption detection, run in CI on SQLite.

**Pure functions for anything risky.** Every `lift` statement is built by a pure function
(`backup_full_sql`, `restore_full_sql`, `move_plan`, `next_logs`, …) and unit-tested. The
orchestration is tested against a simulated pair of servers that keeps backup history in a fake
`msdb`.

**Probe instead of guess.** Where server behaviour varies (how bulk insert keeps identity values),
the tool tests it once on a `#temp` table and uses what works.

**Fail loudly on integrity risks.** Log-chain gaps, NUL backups, virtual-device backups, and
NULL-to-default substitution in FreeTDS bulk copy are all detected and either routed around or
stopped with an explanation.

## The logical data path

```text
source cursor ──fetchmany(batch_rows)──► rows ──► target bulk load (one INSERT BULK per chunk)
     ▲  SELECT <transport exprs> WHERE <key range> ORDER BY <key>        │ DELETE range first, COMMIT after
     └──────────────────────── worker process/thread ──────────────────────┘
```

Transport expressions (`sqlserver.SqlServer.select_exprs`) rewrite columns that Python can't hold
exactly: high-precision temporal types become ISO text, and CLR types become `varbinary(max)`.
Bulk metadata (`sqlserver.pytds_type`) declares the matching target types, so SQL Server converts
them back.

## The physical data path

```text
source SQL Server ──BACKUP (N stripes, compressed)──► share / S3 ──RESTORE NORECOVERY──► target SQL Server
        └─ msdb history ──► next_logs() ──► RESTORE LOG … (every log_interval_seconds) ─────────┘
```

The Python process only sends statements and polls `sys.dm_exec_requests`. It needs about
100 millicores of CPU, whatever the database size.

## Bundled driver changes

python-tds is vendored in `_vendor/pytds` with three changes, listed in `THIRD_PARTY_NOTICES.md`:
imports rewritten to the private namespace, `tls.py` re-implemented on `ssl.MemoryBIO` /
`SSLObject`, and a `tls_server_hostname` argument for certificate host-name overrides.
