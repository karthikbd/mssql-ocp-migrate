# Limitations

## Not migrated by `migrate` (reported as warnings or DDL errors)

| Object or feature | What happens | What to do |
|---|---|---|
| logins, server roles, permissions | not copied | script them, or use `lift` (copies SQL logins used by the database) |
| SQL Agent jobs, linked servers, server settings | not copied | out of scope: database level only |
| memory-optimized tables | created as disk-based tables | recreate as memory-optimized if needed |
| system-versioned temporal tables | current and history tables copied as normal tables | re-enable `SYSTEM_VERSIONING` after cut-over |
| partitioned tables | created unpartitioned | recreate the partition function and scheme, then rebuild |
| XML, spatial and full-text indexes | not created | create them manually after the load |
| FileTables | skipped | |
| `WITH ENCRYPTION` modules | can't be read, so reported as DDL errors | deploy them from source control |
| tables without a key | one chunk each; aggregate validation only | add a key if possible |
| `sql_variant` columns | copied; warned | check the base types |

`lift` copies **everything inside the database** (it's a backup), but nothing at server level
except SQL logins and the TDE certificate.

## Engine constraints

- `lift` needs the target version to be the same as or newer than the source's, and whole
  databases.
- `lift` with `transport = "s3"` needs SQL Server 2022 on both sides and is capped at ~12.5 TB
  per compressed backup set.
- Availability-group seeding (Option B) needs Enterprise or Developer edition on both sides.
- With pymssql, certificate verification follows FreeTDS configuration; `ca_file` and
  `hostname_in_certificate` aren't applied.

## Verification status

The engine, chunking, resume, sharding, validation, TLS and driver logic are covered by the
automated suite (SQLite engine tests, a fake-TDS wire-level test, and a simulated pair of servers
for `lift`). The SQL Server-specific parts (catalog queries, `INSERT BULK`, backup and restore
T-SQL) are exercised by `tests/e2e/`, which needs real SQL Server instances. Run that suite in your
environment before production use; see {doc}`/development/testing`.
