# mssql-ocp-migrate

[![PyPI version](https://badge.fury.io/py/mssql-ocp-migrate.svg)](https://pypi.org/project/mssql-ocp-migrate/)
[![Python versions](https://img.shields.io/pypi/pyversions/mssql-ocp-migrate.svg)](https://pypi.org/project/mssql-ocp-migrate/)
[![Tests](https://github.com/karthikbd/mssql-ocp-migrate/actions/workflows/tests.yml/badge.svg)](https://github.com/karthikbd/mssql-ocp-migrate/actions/workflows/tests.yml)
[![Docs](https://readthedocs.org/projects/mssql-ocp-migrate/badge/?version=latest)](https://mssql-ocp-migrate.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/karthikbd/mssql-ocp-migrate/badge)](https://securityscorecards.dev/viewer/?uri=github.com/karthikbd/mssql-ocp-migrate)

Move a SQL Server database to SQL Server running on **OpenShift (OCP)**, or to any other SQL Server,
with a parallel chunked copy, resume after failure, and end-to-end validation that proves the
target matches the source.

**No downloads, ever.** The package declares no dependencies: the SQL Server network driver
(python-tds, MIT) is bundled inside it, with TLS rebuilt on Python's own `ssl` module, so
`pip install` pulls in nothing else and the tool makes no HTTP calls at runtime. If `pyodbc` and a
Microsoft ODBC driver are already installed, it can use them; they are never required.

```
pip install mssql-ocp-migrate                       # from PyPI
pip install --no-index --find-links ./wheels mssql-ocp-migrate   # air-gapped: the one wheel copied into ./wheels
```

## Pick the path by size

| Data | Use | Why |
|---|---|---|
| up to a few TB, or subsets / schema changes / different collation | `migrate` (logical copy) | row-level control, per-table filters, scale out with `--shard` across pods |
| **tens to hundreds of TB, whole databases** | **`lift`** (physical) | SQL Server ships its own compressed pages: no row decoding, no index rebuilds, source stays online, cut-over takes minutes |

### `lift`: the 100 TB path

```bash
mssql-migrate lift plan    -c migrate.toml          # sizes, real compression ratio + speed from msdb, blockers, timeline
mssql-migrate lift run     -c migrate.toml --follow-hours 168   # seed + keep caught up (source online)
mssql-migrate lift status  -c migrate.toml
mssql-migrate lift cutover -c migrate.toml --confirm  # maintenance window: minutes
mssql-migrate lift verify  -c migrate.toml          # row counts for every table + sampled checksums
```

1. **Seed:** a `COPY_ONLY`, compressed, `CHECKSUM` full backup striped over up to 64 files (one backup
   thread per file) to a share both servers can reach. The tool sizes `MAXTRANSFERSIZE` and
   `BUFFERCOUNT` to fit a memory cap. The restore runs `WITH NORECOVERY`, spreading the data files
   over several OpenShift volumes so writes run in parallel, and verifies page checksums as it goes.
   (If no full backup has ever started the database's log chain, the seed is a regular full backup
   instead of `COPY_ONLY`, because log backups would otherwise fail.)
2. **Catch-up:** log backups every `log_interval_seconds`, restored continuously. Log backups taken
   by your *existing* backup jobs are read from `msdb` and applied too (paths translated with
   `path_map`), so the chain never breaks. A missing log, a backup sent to `NUL`, or a third-party
   (VDI) log backup stops the loop with a clear message instead of silently corrupting the chain.
3. **Cut-over:** a final catch-up, then a tail-log backup `WITH NORECOVERY`. The source goes to
   RESTORING, so nothing can write to it, and rollback is a single `RESTORE ... WITH RECOVERY` on the
   source. Then the last restore, `RECOVERY`, and the post-steps: SQL logins with the same SID and
   password hash (no orphaned users), database owner, TRUSTWORTHY / DB_CHAINING / Service Broker,
   orphaned-user re-mapping, and optionally `DBCC CHECKDB WITH PHYSICAL_ONLY`.
4. **TDE:** the certificate and private key are copied over the connection
   (`CERTENCODED` / `CERTPRIVATEKEY` → `CREATE CERTIFICATE ... FROM BINARY`), with no files on either
   server.

Timeline for **100 TB, ~3:1 compression** (`lift plan` prints this from your own msdb history):

| Stage | 10 Gbps link, 2 GB/s target storage | 25 Gbps, 4 GB/s |
|---|---|---|
| full backup (source online; assumes the source can read ≥3 GB/s across its files) | ~10 h | ~4–7 h (source read speed bound) |
| restore on OCP (source online) | ~15 h | ~7 h |
| log catch-up | minutes to ~1 h | minutes |
| **cut-over downtime** | **~5–15 min** | **~5–15 min** |

The restore usually takes longest. It writes the full uncompressed size, so give the SQL Server pod
4–8 data volumes on the fastest storage class (`deploy/openshift/lift/02-sqlserver-volumes-patch.yaml`).
With S3, the HTTPS calls come from SQL Server itself, not from this package. S3 is supported for SQL Server 2022, but one backup set is capped at about 12 TB (64 URLs × 10,000 parts
× 20 MB), so a 100 TB database needs `transport = "share"` (NFS/SMB). `lift plan` reports this as a blocker.

**Option B, no backup files** (Enterprise/Developer on both sides): `mssql-migrate lift ag-script`
prints a cluster-less availability group (`CLUSTER_TYPE = NONE`, `SEEDING_MODE = AUTOMATIC`, trace
flag 9567 to compress the seeding stream) that streams the database straight into the OpenShift pod
and keeps it synchronized. Cut-over is a manual failover.

## Security and supply chain

No runtime dependencies. Releases are published with PyPI Trusted Publishing and carry provenance
attestations, plus Sigstore-signed assets and a CycloneDX SBOM on GitHub. CI is hash-locked, runs a
weekly `pip-audit`, CodeQL and OpenSSF Scorecard. See [SECURITY.md](SECURITY.md).
Corporate networks: see the JFrog Artifactory / Curation notes in the
[installation guide](https://mssql-ocp-migrate.readthedocs.io/en/latest/getting_started/install.html).

## Quick start

```bash
mssql-migrate init-config > migrate.toml   # edit hosts, database, users
export SRC_DB_PASSWORD=...                  # or password_file = an OpenShift secret mount
mssql-migrate check    -c migrate.toml      # connectivity, versions, table inventory, warnings
mssql-migrate estimate -c migrate.toml      # measures real throughput, projects total time
mssql-migrate migrate  -c migrate.toml      # schema -> data -> indexes/FKs -> objects -> validation
mssql-migrate validate -c migrate.toml      # re-run any time (for example right before cut-over)
```

Python API:

```python
from mssql_ocp_migrate import Config, Migrator

if __name__ == "__main__":      # worker processes re-import this script
    run = Migrator(Config.load("migrate.toml")).migrate()
    print(run.ok, run.rows, run.validation.summary())
```

Each run writes `migration_report/report.html` (evidence for sign-off) and `report.json`.

## What `migrate` does

| Phase | Details |
|---|---|
| Preflight | Reads the catalog: tables, columns, alias types, identity, computed, defaults, PK/unique/indexes (incl. filtered, INCLUDE, columnstore), FKs, checks, sequences, views, procs, functions, triggers, synonyms. Refuses a non-empty target unless told to `truncate` or `resume`. |
| Schema | Creates schemas, alias types, sequences (at the source's current value), and tables with their primary keys. |
| Copy | Splits each table into key ranges (`chunk_rows`, single or composite keys) and copies them in parallel with TDS bulk insert (`INSERT BULK`). Each chunk is one transaction that first clears its own range, so a retry or a resume never duplicates rows. Identity values are preserved, and the strategy is probed on the target at runtime. `datetime2(7)`, `time(7)`, `datetimeoffset(7)` and `datetime` are moved as ISO text, so no 100 ns ticks are lost. Geography, geometry and CLR types are moved as varbinary, hierarchyid as its path text, xml as nvarchar. |
| Post-load | Nonclustered/unique indexes with the source's fill factor, lock options and ROW/PAGE compression; user-created statistics; check constraints and foreign keys `WITH CHECK` (trusted); identity reseed; then views/functions/procs/synonyms with a dependency-retry loop, triggers last (disabled ones stay disabled). |
| Security and settings | Logins (same SID and password hash), certificates and signatures, database users and roles, role memberships, schema owners, `GRANT`/`DENY` down to column level, extended properties, row-level security, then every database option, scoped configuration and the compatibility level. |
| Everything else | Temporal tables (versioning restored), memory-optimized and partitioned tables, masked/sparse/typed-XML/Always Encrypted columns, XML/spatial/full-text indexes, table types, CLR, Service Broker, DDL triggers, plan guides, change tracking and CDC; optionally SQL Agent jobs (disabled) and linked servers. |
| Validate | See below. |

`mssql-migrate schema -c migrate.toml -o schema.sql` writes the DDL without running it, for DBA review.

## Validation (end-to-end testing)

| `validation.mode` | What is compared |
|---|---|
| `count` | Exact `COUNT_BIG(*)` per table, schema parity (columns, types, nullability, collation, identity, PK, indexes and their storage options, statistics, FKs, checks), objects present, identity current values, users/roles/permissions, extended properties and database settings. |
| `checksum` (default) | The `count` checks, plus a per-chunk `COUNT` / `SUM(BINARY_CHECKSUM)` / `CHECKSUM_AGG` computed inside both servers (no data crosses the network; LOB, xml and CLR columns are hashed with SHA-256), plus an exact row-by-row SHA-256 comparison of a random `sample_full_pct` % of chunks. Any chunk whose checksum differs is compared row by row automatically, and the report lists the exact keys that are missing, extra or changed. |
| `full` | Every row of every table is hashed with SHA-256 on both sides and compared by key. Keyless tables use an order-independent multiset digest, which also catches duplicate-row differences. |

`rowversion` columns are excluded by design, because the target assigns new values.

## Scaling the logical copy out

The bundled pure-Python driver moves about 2–4 MB/s per worker process. To go wider, run the copy as
an OpenShift **Indexed Job**: `migrate --shard auto/16` on 16 pods. Each pod copies the chunks that
hash to its index. Shard 1 creates the schema and writes the shared chunk plan. Then
`mssql-migrate finalize --shard-count 16` builds indexes, constraints and objects and validates.
`target_recovery_simple = true` keeps bulk loads minimally logged, and the original recovery model is
restored at the end. See `deploy/openshift/scale-out/indexed-job.yaml`.

## How long will it take?

Run `mssql-migrate estimate`. It reads a sample from your largest table, bulk-loads it into a
`#temp` table on the target, times a server-side checksum, and projects copy, index and validation
time for your `workers` setting. Rough guide for the bundled pure-Python driver with
`executor = "auto"` (one process per worker):

| Per worker | 8 workers | 16 workers |
|---|---|---|
| ~15–25k rows/s (~2–4 MB/s) for a 10–15-column row | ~55–100 GB/hour | ~100–180 GB/hour, if the network and the target's log disk keep up |

Add about 20–30 % for index and FK builds. Checksum validation scans both databases on the server,
which usually takes minutes per 100 GB. With `pyodbc` + ODBC Driver 18 already installed,
per-stream throughput is typically 2–4× higher.

For multi-terabyte databases, a native `BACKUP` → copy into the pod → `RESTORE` is the fastest
lift. You can still run `mssql-migrate validate` afterwards to prove parity.

## Running against OpenShift

**From a workstation or VM:** `oc port-forward svc/mssql 14330:1433 -n <ns>`, then point `[target]` at
`127.0.0.1:14330`. Set `hostname_in_certificate` if you verify the server certificate.

**Inside the cluster (recommended for large data):** see `deploy/openshift/`:
`Containerfile` builds an offline image (`pip install --no-index` of the wheel), and `configmap.yaml`,
`secret.yaml` and `job.yaml` run it as a Job. A PVC keeps the checkpoint and the report, so a
failed or evicted Job resumes where it stopped (`if_target_not_empty = "resume"`). The source SQL
Server must be reachable from the cluster (egress / firewall on 1433).

## Cut-over pattern for a live source

1. `migrate` while the application is still running (bulk of the data, hours).
2. Stop writes (application maintenance window, or `ALTER DATABASE ... SET READ_ONLY`).
3. `migrate --truncate --include <tables that changed>`, or a full re-run for small databases.
4. `validate --mode checksum` (or `full`), keep the HTML report as evidence, then switch connection strings.

## Permissions

- **Source:** `db_datareader` + `VIEW DEFINITION` (+ `VIEW DATABASE STATE` for size estimates).
- **Target:** `db_owner` on the pre-created, empty database. The tool creates the schema, and bulk insert needs INSERT plus ALTER on each table. `ADMINISTER BULK OPERATIONS` is not needed, because no files are read on the server.
- **Copying users (`copy_security`):** add `VIEW ANY DEFINITION` on both servers so login names resolve.
- **Server level (`copy_logins`, `copy_cdc`, `copy_agent_jobs`, `copy_linked_servers`):** `sysadmin` on both servers; turn them off otherwise and create logins first.
- **Masked columns:** `UNMASK` on the source.

## Not migrated by `migrate` (reported as warnings)

Since 0.3.0 almost everything inside the database is copied. What's left can't be read over a
connection: symmetric and asymmetric keys, database scoped credentials, linked-server remote
passwords, contained-user passwords and `WITH ENCRYPTION` modules; and graph, FileTable,
external and ledger tables, which are reported and left for `lift`. `lift` copies everything
inside the database.

## Development

```bash
pip install -e . pytest && pytest            # unit + engine (SQLite) + TLS wire tests
docker compose -f tests/e2e/docker-compose.yml up -d
MSSQL_E2E=1 pytest tests/e2e/test_e2e_sqlserver.py tests/e2e/test_e2e_lift.py -v   # real SQL Server 2022
```

CI runs the same end-to-end suite on every push against two SQL Server 2022 containers
(`.github/workflows/e2e.yml`): a full logical migration of every object kind with validation, and
a physical lift through seed, log catch-up, cut-over and verify.

Releasing: bump the version in `pyproject.toml` and `src/mssql_ocp_migrate/__init__.py`, update
`CHANGELOG.md`, push `main`, then publish a GitHub release `vX.Y.Z`. `.github/workflows/python-publish.yml`
runs the tests and publishes to PyPI with Trusted Publishing (environment `release`, no stored token).

Third-party notices: see `THIRD_PARTY_NOTICES.md`.
