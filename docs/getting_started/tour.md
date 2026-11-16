# 10 minutes to mssql-ocp-migrate

This tour moves a database called `SalesDB` from an on-premises SQL Server to SQL Server on
OpenShift with the logical engine, then reads the validation report. The physical engine follows
the same pattern; see {doc}`/user_guide/lift`.

## 1. Write a configuration file

```console
$ mssql-migrate init-config > migrate.toml
```

Edit the two connections. Keep passwords out of the file: point to an environment variable or a
mounted secret.

```toml
[source]
host = "sql01.corp.example.com"
database = "SalesDB"
user = "migration_reader"
password_env = "SRC_DB_PASSWORD"
trust_server_certificate = true

[target]
host = "127.0.0.1"          # oc port-forward svc/mssql 14330:1433 -n sqlserver
port = 14330
database = "SalesDB"        # create the empty database first
user = "migration_writer"
password_env = "TGT_DB_PASSWORD"
trust_server_certificate = true

[migration]
workers = 8

[validation]
mode = "checksum"
```

Every key is listed in {doc}`/reference/config`.

## 2. Reach the target

From a workstation, forward the OpenShift Service to a local port:

```console
$ oc port-forward svc/mssql 14330:1433 -n sqlserver
```

For large copies, run the tool inside the cluster instead; see {doc}`/user_guide/openshift`.

## 3. Check connectivity and inventory

```console
$ export SRC_DB_PASSWORD=... TGT_DB_PASSWORD=...
$ mssql-migrate check -c migrate.toml
[ok]   source: source sql01.corp.example.com:1433/SalesDB via pytds | SQL Server 15.0.4385.2 Enterprise Edition | collation SQL_Latin1_General_CP1_CI_AS
[ok]   target: target 127.0.0.1:14330/SalesDB via pytds | SQL Server 16.0.4165.4 Developer Edition | collation SQL_Latin1_General_CP1_CI_AS

142 tables selected, 1,284,559,012 rows, 412,880 MB
  warn  dbo.AuditLog: no primary key / unique key: copied as one chunk (no parallelism, ...)
```

Warnings tell you, before anything runs, what will need attention: heaps, temporal tables,
partitioning, spatial indexes and so on.

## 4. Estimate

```console
$ mssql-migrate estimate -c migrate.toml
```

The tool reads 50,000 real rows from your largest table, bulk-loads them into a `#temp` table on
the target (then rolls back), and times a server-side checksum. It then projects copy, index and
validation time for your `workers` setting. If the answer is days, look at {doc}`choosing`.

## 5. Optionally review the DDL

```console
$ mssql-migrate schema -c migrate.toml -o schema.sql
```

This writes exactly what `migrate` will create, in run order, for a DBA to review.

## 6. Migrate

```console
$ mssql-migrate migrate -c migrate.toml
12:00:01 INFO    142 tables, ~1,284,559,012 rows, ~412,880 MB on source
12:00:03 INFO    creating schemas, types, sequences and tables on target
12:00:09 INFO    planning chunks for 142 tables (~250,000 rows each)
12:01:40 INFO    copying 5,212 chunks with 8 process workers
12:01:52 INFO    [1/5212] sales.Order#0: 250,000 rows in 11.8s | total 250,000 rows, 21,180 rows/s
...
15:40:10 INFO    creating indexes and constraints
16:02:44 INFO    validation PASSED: 142/142 tables ok, 5212 chunks checksummed, 263 chunks row-compared, 0 object diffs

SUCCESS: 1,284,559,012 rows in 242.7 min. Report: migration_report/report.html
```

If it's interrupted, re-run with `--resume`. Only unfinished chunks are copied again, and each
chunk clears its own key range first, so nothing is duplicated.

## 7. Read the report

`migration_report/report.html` is a self-contained page you can attach to a change ticket. It shows:

- **PASSED or FAILED** at the top;
- per table: source rows, target rows, chunks, copy time, and the result;
- for any difference, the **exact keys** that are missing, extra or changed;
- DDL errors, object differences and warnings.

`report.json` has the same data in machine-readable form for pipelines.

## 8. Validate again at cut-over

Data keeps changing on a live source. Right before switching the application over:

```console
$ mssql-migrate migrate -c migrate.toml --truncate --include "sales.Order*"   # reload what changed
$ mssql-migrate validate -c migrate.toml --mode full
```

For true minutes-long cut-overs on big databases, use {doc}`/user_guide/lift`.
