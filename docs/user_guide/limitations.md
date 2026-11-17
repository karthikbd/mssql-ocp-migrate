# Limitations

## Not migrated by `migrate` (reported as warnings or DDL errors)

Since 0.3.0 `migrate` copies almost everything inside a database (see {doc}`logical`). What's left
can't be copied over a connection, or isn't supported by SQL Server on Linux:

| Object or feature | What happens | What to do |
|---|---|---|
| symmetric and asymmetric keys | not copied; reported | their key material can't be exported: recreate them from the same `KEY_SOURCE`/`IDENTITY_VALUE`, or use `lift` |
| database scoped credentials, external data sources | not copied; reported | recreate them with the secret |
| linked-server remote passwords | the linked server is copied (`copy_linked_servers`), its mapped logins are reported | re-enter the remote passwords |
| contained users with passwords, Entra ID users | not recreated; reported | recreate them (a contained user's password hash can't be copied) |
| graph tables, FileTables, external tables, ledger tables | skipped and reported as errors | use `lift` |
| selective XML indexes | not created; reported | create them after the load |
| server settings, server roles other than memberships of copied logins, other databases' jobs | not copied | out of scope: one database at a time |
| `WITH ENCRYPTION` modules | can't be read, so reported as DDL errors | deploy them from source control |
| tables without a key (and keys on encrypted columns) | one chunk each; row-by-row validation | add a key if possible |
| `sql_variant` columns | copied; warned | check the base types |

Some features need something on the target first: CDC needs SQL Agent (`MSSQL_AGENT_ENABLED`),
full-text indexes need the full-text package (`mssql-server-fts`), CLR needs `clr enabled` (and
`trust_clr_assemblies` or `TRUSTWORTHY` under `clr strict security`), and certificates need a
database master key password in `master_key_password_env`. The run reports what was skipped and why.

**Always Encrypted** columns are copied as ciphertext without decrypting them (the column master
key never leaves your key store). This needs the bundled `pytds` driver, and those columns are
compared row by row rather than by server-side checksum.

`lift` copies **everything inside the database** (it's a backup), but at server level only SQL
logins, the TDE certificate and, when switched on, SQL Agent jobs and linked servers.

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
T-SQL) are exercised by `tests/e2e/`, which runs in CI against two SQL Server 2022 Linux
containers. Always Encrypted, CLR assemblies, full-text and Windows logins aren't available in
those containers, so they're covered by unit tests only; try them in your environment before
production use. See {doc}`/development/testing`.
