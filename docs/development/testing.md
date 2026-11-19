# Testing

The suite has three layers. The first two run anywhere, in a few seconds, with no database
server.

| Layer | Files | What it proves |
|---|---|---|
| Unit | `test_units.py`, `test_bulk_types.py`, `test_objects.py`, `test_extras.py`, `test_gaps.py`, `test_physical.py`, `test_pymssql.py`, `test_no_downloads.py` | chunk partitions are exact (property test on 1–3-column keys); DDL text; config validation; checkpoint round-trips; driver-independent hashing; every SQL Server type maps to bulk metadata and serialises; `lift` T-SQL, log-chain rules and timeline maths; the package imports no HTTP or installer modules |
| Engine on SQLite | `test_engine.py` + `sqlite_adapter.py` | the full `migrate` pipeline with parallel workers (threads and processes): exact copies, chunk failure then resume, retry after transient failure, the `fail` and `truncate` policies, filters, 3-shard scale-out with `finalize`, and validation naming corrupted, missing and extra keys |
| Wire level | `test_tls.py`, `test_pymssql.py` | the bundled driver's stdlib TLS completes the handshake inside TDS pre-login packets against a fake TDS server: trusted, CA-verified with host-name override, untrusted (rejected), mismatched (rejected), plain text. pymssql's FreeTDS logs in to the same fake server |
| Simulated servers | `test_physical.py` | `lift` seed → catch-up (including log backups from an existing job, mapped paths) → cut-over in both `norecovery` and `read_only` modes, undoing replayed options, resumable seeding, and blockers for VDI and S3 limits |
| Real SQL Server | `tests/e2e/` | catalog queries, `INSERT BULK`, identity probe, checksums, DDL, objects, `assess`, `sync` and the full `lift` flow, against SQL Server 2019, 2022 and 2025 containers, with each driver |
| Fuzzing | `tests/fuzz/` (Atheris), `test_fuzz_targets.py` | any TOML/JSON config is either valid or a `ConfigError`; rows whose values differ never fingerprint or key the same; every row lands in exactly one chunk for any composite key and boundaries; quoted identifiers read back unchanged |

## Running

CI runs the end-to-end suite on every push (`.github/workflows/e2e.yml`) against two SQL Server
Linux containers with SQL Agent enabled, for each version pair (2022 → 2022, 2019 → 2022,
2022 → 2025 and 2025 → 2025; `SOURCE_IMAGE` / `TARGET_IMAGE` choose the images) and each driver:
the bundled pytds, pyodbc with Microsoft ODBC Driver 18, and pymssql (`E2E_DRIVER`). To run it locally:

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
views, a function, a procedure, a disabled trigger and a synonym. Since 0.3.0 it also has masked,
sparse and typed-XML columns with a column set, row-level security, a table type, a partitioned
table with per-partition compression, XML and spatial indexes, a temporal table with history, a
memory-optimized table, a rule binding, Service Broker objects, a certificate-signed procedure, a
DDL trigger, a plan guide, change tracking, CDC, a SQL login, a SQL Agent job and a linked server.
Since 0.4.0 it adds a procedure created with `ANSI_NULLS` and `QUOTED_IDENTIFIER` off, a trigger
firing order, an object owner, a disabled index, a `sql_variant` table with every base type,
`ROWGUIDCOL`, `NOT FOR REPLICATION`, UTF-8 and `text`/`ntext`/`image` columns, a custom error
message, a login language, a server role with server permissions and an Agent operator; on a
2025 source also `json` and `vector` columns. The last test changes the source and runs `sync`.

Environment variables for e2e: `E2E_SRC_HOST`, `E2E_SRC_PORT`, `E2E_TGT_HOST`, `E2E_TGT_PORT`,
`E2E_USER`, `E2E_PASSWORD`, `E2E_ENCRYPT`, `E2E_DRIVER`, `E2E_WORKERS`.

## Fuzzing

ClusterFuzzLite runs the Atheris fuzz targets in `tests/fuzz` on every pull request (the code that
changed), and daily in batch mode (`.github/workflows/cflite_*.yml`). To run one locally:

```console
$ pip install atheris
$ python tests/fuzz/fuzz_rowhash.py -max_total_time=60
```

`tests/test_fuzz_targets.py` runs every target briefly on random input when Atheris is installed.
