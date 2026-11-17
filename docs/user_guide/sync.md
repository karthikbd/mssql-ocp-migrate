# Online catch-up (`sync`)

`migrate` copies a consistent picture of each table, but the application keeps writing while it
runs. `sync` brings the target up to date afterwards using SQL Server change tracking on the
source, so the application only stops for the last few minutes. It works like the online mode of
Azure Database Migration Service, without reading the transaction log, and it's the logical
counterpart of `lift`'s log catch-up.

```console
$ mssql-migrate migrate -c migrate.toml           # records the change-tracking version first
$ mssql-migrate sync -c migrate.toml              # any number of times
$ mssql-migrate sync -c migrate.toml --follow 60  # keep catching up every minute
# cut-over window: stop the writes, then
$ mssql-migrate sync -c migrate.toml --final      # last pass, then validation
```

## Before `migrate`

Change tracking must be on for the source database and its tables **before** the copy starts,
so that nothing changed during the copy is missed. Either turn it on yourself:

```sql
ALTER DATABASE SalesDB SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 7 DAYS, AUTO_CLEANUP = ON);
ALTER TABLE sales.Customer ENABLE CHANGE_TRACKING;   -- every table with a primary key
```

or set `migration.sync_enable_change_tracking = true` and `migrate` does it as its first step (this
changes the source; retention is `sync_retention_days`). `migrate` then records
`CHANGE_TRACKING_CURRENT_VERSION()` in its state file before it copies anything.

Keep the retention longer than the time between `migrate` and the last `sync`: if the changes a
table needs have been cleaned up, `sync` says so and that table must be copied again.

## What a pass does

For each table it reads `CHANGETABLE(CHANGES ...)` since the last version, deletes those keys on
the target and inserts the source's current rows (when they still exist), in one transaction per
table. A pass is idempotent, so it can be repeated safely. During the pass:

- foreign keys between synced tables are switched off, so changes apply in any order and no cascade
  runs twice; they're switched back on afterwards (`--final` re-validates them, so they're trusted);
- the target's DML triggers are disabled, because they already ran on the source.

`--final` also moves identity values and sequences to the source's current values and runs the
validation.

## What `sync` can't follow

| Table | Why | What to do |
|---|---|---|
| no primary key | change tracking needs one | re-copy at cut-over: `migrate --truncate --include schema.table` |
| system-versioned (temporal) | its period columns and history can't be written row by row | re-copy at cut-over |
| memory-optimized | change tracking isn't supported on them | re-copy at cut-over |
| Always Encrypted columns | ciphertext is bulk-copied only | re-copy at cut-over |

`sync` lists these tables on every pass. Schema changes made on the source after `migrate` aren't
synced: freeze the schema until cut-over.

## Permissions

On the source: `VIEW CHANGE TRACKING` on the tables (and `ALTER` on the database and tables if
`sync_enable_change_tracking` turns it on). On the target: `db_owner` (deletes, inserts, `ALTER
TABLE ... NOCHECK`, `DISABLE TRIGGER`, identity reseed).
