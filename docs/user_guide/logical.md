# Logical migration (`migrate`)

The logical engine copies rows table by table. Use it for databases up to a few TB, for subsets,
or when the target schema differs from the source.

## Phases

```text
preflight ─► schema ─► plan chunks ─► parallel copy ─► indexes & constraints ─► objects ─► validation ─► report
```

1. **Preflight.** Reads the source catalog: tables, columns, alias types, identity, computed
   columns, defaults, keys, indexes, foreign keys, checks and programmable objects. Applies the
   `if_target_not_empty` policy. Collects warnings for anything that needs attention.
2. **Schema** (`schema_mode = "create"`). Creates schemas, alias types, sequences (starting at the
   source's current value) and tables with their primary keys and defaults.
3. **Plan.** Splits each table into chunks of about `chunk_rows` rows by key range, with one
   ordered scan of the key.
4. **Copy.** `workers` processes or threads copy chunks in parallel, biggest tables first.
5. **Indexes and constraints.** Clustered, then nonclustered and unique indexes (including
   filtered, INCLUDE and columnstore), check constraints, foreign keys `WITH CHECK` so they're
   trusted, and an identity reseed to the source's current value.
6. **Objects.** Views, functions, procedures and synonyms, created in a retry loop until
   dependencies resolve, with triggers last so they never fire during the load. Disabled triggers
   stay disabled.
7. **Validation and report.** See {doc}`validation`.

## Chunks: the unit of parallelism, retry and resume

A chunk is a half-open key range `[lower, upper)` over the primary key, or the first unique key
on non-null columns. Composite keys use lexicographic comparisons, so every row belongs to exactly
one chunk. This is checked by a property test over random 1-, 2- and 3-column keys.

Each chunk is copied in **one target transaction**:

```text
DELETE target rows in [lower, upper)  →  bulk-insert source rows in [lower, upper)  →  COMMIT
```

So a chunk can be retried or resumed any number of times without duplicates. Failed chunks are
retried `max_retries` times on fresh connections with back-off; chunks that still fail are listed
in the report, and indexes are not built until you resume.

Parameters in the range predicates are cast to the key column's exact type (for example
`CAST(? AS varchar(12))`), so SQL Server can seek on the index instead of converting the column
and scanning.

Tables without any usable key are copied as **one chunk** (truncate, then load). That means no
parallelism within the table and validation by aggregate digest. `check` warns about each one.

## Resume

Progress is recorded in `state_file` after every chunk (written atomically):

```console
$ mssql-migrate migrate -c migrate.toml            # interrupted at 60 %
$ mssql-migrate migrate -c migrate.toml --resume   # copies the remaining 40 %
```

On resume the original chunk boundaries are reused, finished chunks are skipped, and the schema
step is not repeated.

## Target policies

| `if_target_not_empty` | Behaviour |
|---|---|
| `fail` (default) | stop if any target table has rows, or already exists when `schema_mode = "create"` |
| `truncate` | reload: every chunk clears its range; single-chunk tables are truncated |
| `resume` | continue from `state_file` |

## Identity columns

Explicit identity values must be kept. With the bundled driver, a tiny `#temp` table with an
IDENTITY column is bulk-loaded once with the value 42 using each candidate method, and the first
method that stores 42 is used:

1. `SET IDENTITY_INSERT ON` + `INSERT BULK`;
2. plain `INSERT BULK`;
3. otherwise, parameterised multi-row `INSERT` with `IDENTITY_INSERT` (always correct, slower).

The chosen method appears in the report. pymssql is probed the same way; pyodbc always uses
`IDENTITY_INSERT` with `fast_executemany`. After the load, each identity is reseeded to the
source's `IDENT_CURRENT`.

## Columns that need special care

| Column kind | Handling |
|---|---|
| computed | created with its definition (and PERSISTED); never inserted; compared in validation |
| `rowversion` / `timestamp` | created; never inserted (the target assigns new values); excluded from validation |
| `datetime2(7)`, `time(7)`, `datetimeoffset(7)`, `datetime` | moved as ISO text, so no precision is lost |
| `geography`, `geometry`, `hierarchyid` | moved as `varbinary(max)` |
| alias types (`CREATE TYPE ... FROM`) | created on the target first, then used by the columns |
| `sysname` | created as `nvarchar(128)` |
| `binary(n)` | sent as `varbinary(n)`; the bundled driver has no `BINARY` bulk serializer, and SQL Server converts |
| NULL in a column with a DEFAULT | stays NULL (`KEEP_NULLS`) |

## Programmable objects

`copy_programmable_objects = true` copies views, procedures, functions (scalar, inline and
multi-statement), triggers, synonyms, sequences and alias types, using their stored definitions.
Encrypted modules can't be read and are reported as DDL errors.

## Scale-out across pods (`--shard`)

One process can only go so fast. Split the chunks across many pods:

```console
# pod k of 16 (an OpenShift Indexed Job sets JOB_COMPLETION_INDEX):
$ mssql-migrate migrate -c migrate.toml --shard auto/16 --skip-validation
# after all 16 finished:
$ mssql-migrate finalize -c migrate.toml --shard-count 16
```

- Shard 1 runs preflight and schema, and writes the chunk plan to `plan_file` (must be on a
  ReadWriteMany volume). The other shards wait for the plan file to appear.
- Each chunk goes to shard `crc32(chunk_id) mod N`. This is the same on every pod, with no
  coordination service.
- Each shard keeps its own `state.shardKofN.json`, so a restarted pod resumes its own work.
- `finalize` refuses to run until every planned chunk is done, then builds indexes, constraints
  and objects, and validates.

See `deploy/openshift/scale-out/indexed-job.yaml`.

## Minimally logged loads

```toml
[migration]
target_recovery_simple = true
```

The target is switched to SIMPLE recovery for the load, so bulk inserts write far less transaction
log, and the original recovery model is restored at the end. Take a full backup afterwards to
start a new log chain; the report reminds you.

## Using it from Python

```python
from mssql_ocp_migrate import Config, Migrator
from mssql_ocp_migrate import report

cfg = Config.load("migrate.toml")
run = Migrator(cfg, progress=print).migrate()
paths = report.write(cfg, run=run)
if not run.ok:
    raise SystemExit(f"migration failed, see {paths['html']}")
```

`Migrator(factory=...)` accepts any object implementing {class}`mssql_ocp_migrate.adapter.Database`.
That's how the test suite runs the whole engine on SQLite.
