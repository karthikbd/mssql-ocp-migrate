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
5. **Indexes, statistics and constraints.** Clustered, then nonclustered and unique indexes
   (including filtered, INCLUDE and columnstore) with their storage options, user-created
   statistics, check constraints, foreign keys `WITH CHECK` so they're trusted, and an identity
   reseed to the source's current value.
6. **Objects.** Views, functions, procedures and synonyms, created in a retry loop until
   dependencies resolve, with triggers last so they never fire during the load. Disabled triggers
   stay disabled.
7. **Security, extended properties and settings.** Logins, certificates, users and roles, role
   memberships, schema owners, `GRANT`/`DENY`, signatures, extended properties, row-level
   security policies (switched off), system versioning, then every database option, scoped
   configuration, the owner and the compatibility level (see below).
8. **Late objects.** Change tracking, change data capture, Service Broker objects, database DDL
   triggers and plan guides, created once the data is in so none of them see the load. With the
   switches on, SQL Agent jobs (disabled) and linked servers.
9. **Validation and report.** See {doc}`validation`. Row-level security policies are switched to
   the source's state only after validation, so validation reads every row.

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
| `geography`, `geometry`, CLR user-defined types | moved as `varbinary(max)`; the assembly and type are created first |
| `hierarchyid` | moved as its path text (`/1/3/`), which converts back exactly |
| `xml` (typed or not), column sets | moved as `nvarchar(max)`; the server parses and validates it |
| `binary(n)` | sent with `BINARY(n)` bulk metadata (written by mssql-ocp-migrate; the bundled driver has none) |
| sparse columns and column sets | created `SPARSE` with their `COLUMN_SET FOR ALL_SPARSE_COLUMNS`; the column set itself is never inserted |
| masked columns | created with the same `MASKED WITH (FUNCTION = ...)`; the source login needs `UNMASK` or the run stops |
| typed `xml` | created with `DOCUMENT`/`CONTENT` and its XML schema collection |
| Always Encrypted | created `ENCRYPTED WITH` the same key; ciphertext is copied as-is (bundled driver only) |
| temporal period columns | copied as ordinary columns, then `PERIOD FOR SYSTEM_TIME`, `HIDDEN` and `SYSTEM_VERSIONING` are restored |
| alias types (`CREATE TYPE ... FROM`) | created on the target first, then used by the columns |
| `sysname` | created as `nvarchar(128)` |
| NULL in a column with a DEFAULT | stays NULL (`KEEP_NULLS`) |

## Indexes and statistics

**Clustered and nonclustered.** The clustered index *is* the table, with rows stored in key order;
a table without one is a heap. Nonclustered indexes are separate structures that point back to
the rows. The clustered primary key is created with the table, and rows are bulk-loaded in key
order, so nothing has to be re-sorted. Every other index is built once after the load, which is
much faster than maintaining it row by row.

**Storage options** (`copy_index_options = true`). Each index and primary key keeps the source's
fill factor, `PAD_INDEX`, `IGNORE_DUP_KEY`, `ALLOW_ROW_LOCKS`/`ALLOW_PAGE_LOCKS` and `ROW`/`PAGE`
compression (or `COLUMNSTORE_ARCHIVE`); a compressed heap stays compressed. Without this, a PAGE
compressed source can take two to four times the space on the target, and an `IGNORE_DUP_KEY`
index would start rejecting inserts that used to be silently skipped.

**Partitioning.** Partition functions and schemes are created first, and tables and indexes are
placed on the same scheme with each partition's own compression. `partition_filegroups =
"primary"` (the default) maps every partition to `PRIMARY`, which suits a single OpenShift volume;
`"source"` keeps the source's filegroups, which must already exist.

**Memory-optimized tables** (`memory_optimized = "keep"`). A memory-optimized filegroup is added
to the target if it has none, the table is created with its inline hash and range indexes and its
durability, and the load runs without table locks. `"disk"` creates ordinary tables instead.

**XML, spatial and full-text indexes** are built after the load: primary XML indexes before
secondary ones, spatial indexes with their tessellation, grids and bounding box, and full-text
indexes on their catalog and stoplist (only if the target has the full-text feature).

**Build speed.** `index_sort_in_tempdb = true` (the default) sorts in tempdb; `index_maxdop` caps
or raises parallelism; `index_online = true` builds online on Enterprise or Developer edition.

**Statistics.** Index statistics are built from a full scan of the data as each index is created.
User-created statistics (`CREATE STATISTICS`, with their filter and `NORECOMPUTE`) are recreated
when `copy_statistics = true`, sampled by default or `statistics_fullscan = true`. Statistics the
optimizer created on its own are created again on demand, as on any new database.

## Security, extended properties and database settings

`copy_security = true` copies, in this order:

1. user-defined roles, then database users: users mapped to a login (`CREATE USER ... FOR LOGIN`),
   users without a login, and Windows users and groups;
2. role owners and role memberships, including fixed roles such as `db_datareader`;
3. schema owners, creating empty schemas when needed;
4. `GRANT`, `DENY` and `WITH GRANT OPTION` permissions on the database, schemas, objects (including
   column-level permissions) and types.

**Logins.** With `copy_logins = true` (the default) the logins those users map to are created on
the target with the same SID and password hash, and the same server-role memberships, so users
map without remapping. That needs `sysadmin` on both sides; without it, create the logins first.
A user whose login is still missing isn't created, and the run reports it. Contained users with
passwords and Entra ID users can't be recreated from metadata and are reported the same way. Set
`copy_security = false` to manage security separately.

**Certificates and signatures** (`copy_certificates = true`). Certificates are copied with their
private keys (protected in transit by a random password), followed by certificate-mapped users
and the module signatures that grant permissions through them. A new database master key needs a
password in the environment variable named by `master_key_password_env`. Symmetric and
asymmetric keys can't be exported and are reported.

**Row-level security** (`copy_row_level_security = true`). Before copying, the run stops if a
policy hides rows from the source login. Policies are created switched off and set to the
source's state after validation.

`copy_extended_properties = true` copies `MS_Description` and every other extended property on
the database, schemas, tables, columns, views, procedures, functions, indexes, constraints and
triggers.

`copy_database_settings = true` sets the target's compatibility level (`compatibility_level =
"source"` keeps the source's, so query plans behave as before; use `"target"` to keep the newer
default), read-committed snapshot, snapshot isolation, every other database option (ANSI settings,
`PARAMETERIZATION`, `AUTO_*`, page verify, the broker and so on), database scoped configurations,
Query Store, change tracking retention and the database owner. It runs after the load, so the
`ROLLBACK IMMEDIATE` some options need affects no one.

**Change tracking and CDC** (`copy_change_tracking`, `copy_cdc`). Tracking starts on the target
after the load, so the target's history begins at cut-over; consumers resynchronise from there.
CDC needs SQL Agent on the target.

**Server objects.** `copy_agent_jobs = true` copies jobs whose steps run in this database, with
their steps, schedules and categories, pointed at the target database and created **disabled**:
enable them at cut-over so they don't run against both servers. `copy_linked_servers = true`
copies linked servers and their options; remote passwords can't be read, so mapped logins are
reported for you to re-enter.

The database's **default collation** can't be changed after creation: if it differs, the run warns.
Columns keep their own collations, but temp tables, variables and literals follow the target's
default, so create the target database with the source's collation.

## Programmable objects

`copy_programmable_objects = true` copies views, procedures, functions (scalar, inline and
multi-statement), triggers, synonyms, sequences, alias and table types, XML schema collections,
rules and defaults (with their bindings), plan guides (disabled guides stay disabled) and database
DDL triggers, using their stored definitions. Encrypted modules can't be read and are reported as
DDL errors.

**CLR.** Assemblies are copied from their binaries in dependency order, followed by CLR types,
procedures, functions, triggers and aggregates. Under `clr strict security` set
`trust_clr_assemblies = true` (sysadmin) to register each assembly's SHA-512 hash first.

**Service Broker** (`copy_service_broker = true`). Message types, contracts, queues (with their
activation procedures), services, routes, remote service bindings and event notifications are
created after the data, and the broker is enabled if it is on at the source. Messages in queues
aren't copied.

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

if __name__ == "__main__":          # required: worker processes re-import this module
    cfg = Config.load("migrate.toml")
    run = Migrator(cfg, progress=print).migrate()
    paths = report.write(cfg, run=run)
    if not run.ok:
        raise SystemExit(f"migration failed, see {paths['html']}")
```

Worker processes are started with `spawn` (never `fork`, which can deadlock a process that
already runs threads), so a script must keep its top-level code under
`if __name__ == "__main__":`. In a notebook, set `executor = "thread"`.

`Migrator(factory=...)` accepts any object implementing {class}`mssql_ocp_migrate.adapter.Database`.
That's how the test suite runs the whole engine on SQLite.
