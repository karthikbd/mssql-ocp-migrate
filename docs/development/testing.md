# Testing

The suite has three layers. The first two run anywhere, in a few seconds, with no database
server.

| Layer | Files | What it proves |
|---|---|---|
| Unit | `test_units.py`, `test_bulk_types.py`, `test_physical.py`, `test_pymssql.py`, `test_no_downloads.py` | chunk partitions are exact (property test on 1–3-column keys); DDL text; config validation; checkpoint round-trips; driver-independent hashing; every SQL Server type maps to bulk metadata and serialises; `lift` T-SQL, log-chain rules and timeline maths; the package imports no HTTP or installer modules |
| Engine on SQLite | `test_engine.py` + `sqlite_adapter.py` | the full `migrate` pipeline with parallel workers (threads and processes): exact copies, chunk failure then resume, retry after transient failure, the `fail` and `truncate` policies, filters, 3-shard scale-out with `finalize`, and validation naming corrupted, missing and extra keys |
| Wire level | `test_tls.py`, `test_pymssql.py` | the bundled driver's stdlib TLS completes the handshake inside TDS pre-login packets against a fake TDS server: trusted, CA-verified with host-name override, untrusted (rejected), mismatched (rejected), plain text. pymssql's FreeTDS logs in to the same fake server |
| Simulated servers | `test_physical.py` | `lift` seed → catch-up (including log backups from an existing job, mapped paths) → cut-over in both `norecovery` and `read_only` modes, undoing replayed options, resumable seeding, and blockers for VDI and S3 limits |
| Real SQL Server | `tests/e2e/` | catalog queries, `INSERT BULK`, identity probe, checksums, DDL, objects, and the full `lift` flow, against SQL Server 2022 containers |

## Running

```console
$ pytest -q                               # everything except e2e (those are skipped)
$ docker compose -f tests/e2e/docker-compose.yml up -d
$ MSSQL_E2E=1 pytest tests/e2e -v         # real servers: source 14331, target 14332, shared /var/opt/mssql/mig
$ MSSQL_E2E=1 E2E_DRIVER=pymssql pytest tests/e2e -v
```

The end-to-end seed script (`tests/e2e/seed_source.sql`) builds about 1.1 million rows covering
every common type: identity with gaps and a custom seed, composite keys, computed and rowversion
columns, `datetime2(7)` with 100 ns ticks, `datetimeoffset(7)`, geography, hierarchyid, xml, LOBs,
a heap with duplicate rows, alias types, sequences, filtered and INCLUDE indexes, cascading FKs,
views, a function, a procedure, a disabled trigger and a synonym.

Environment variables for e2e: `E2E_SRC_HOST`, `E2E_SRC_PORT`, `E2E_TGT_HOST`, `E2E_TGT_PORT`,
`E2E_USER`, `E2E_PASSWORD`, `E2E_ENCRYPT`, `E2E_DRIVER`, `E2E_WORKERS`.
