# Readiness assessment (`assess`)

Copying the data is half of a migration. The other half is finding what won't work, or will
behave differently, once the database runs on SQL Server on Linux in OpenShift. `assess` checks
for that before you migrate. `migrate` and `lift plan` run the same checks on their own.

```console
$ mssql-migrate assess -c migrate.toml            # source and target
$ mssql-migrate assess -c migrate.toml --lift     # for a physical copy
$ mssql-migrate assess -c migrate.toml --source-only
```

It prints the findings and writes `assessment.json` to the report folder. The exit code is `2` when
there are blockers. The checks draw on Microsoft's list of features SQL Server on Linux doesn't
support, its "metadata to manage when moving a database to another server" checklist, the Data
Migration Assistant's feature-parity rules, and the instance-level scope of dbatools'
`Start-DbaMigration`.

## Severities

| Severity | Meaning |
|---|---|
| `blocker` | The migration, or the application on the target, will fail. |
| `warning` | It works differently, or needs a manual step. |
| `info` | Worth knowing; nothing to do unless it matters to you. |

Only blockers that stop the copy itself (version, edition, storage, and for `lift` anything that
can't be restored) stop `migrate` or `lift run`. Set `migration.ignore_blockers = true` to go on
anyway. Blockers for the application (`xp_cmdshell` in a procedure, say) become warnings in the
run and appear in the report's "Readiness assessment" table, so the copy still goes ahead and you
can fix the code in parallel.

## What it checks

**SQL Server on Linux (OpenShift)**

- code calling `xp_cmdshell`, OLE Automation (`sp_OA*`) or Windows-specific extended procedures
  (registry, drives);
- `BULK INSERT` / `OPENROWSET(BULK ...)` reading Windows paths or UNC shares;
- distributed queries and linked servers to anything other than SQL Server;
- CLR assemblies with `EXTERNAL_ACCESS` or `UNSAFE` (Linux runs `SAFE` only);
- SQL Agent job steps of type CmdExec, PowerShell, SSIS, SSAS or Queue Reader, and Agent alerts;
- Windows users and groups (they need Active Directory integration on Linux);
- linked servers whose logins connect with their own Windows credentials;
- FILESTREAM and FileTable (a `lift` blocker; `migrate` converts the columns to `varbinary(max)`);
- database mirroring, merge replication, Stretch Database, EKM providers, Always Encrypted with
  secure enclaves.

**Version and edition**

- a target older than the source (blocks `lift`);
- a compatibility level the target doesn't support (blocks `migrate` unless `compatibility_level`
  is set);
- SQL Server 2025 `json` / `vector` columns going to an older target;
- an Express target smaller than the data;
- edition features the database uses (`sys.dm_db_persisted_sku_features`).

**Server differences**

- server collation (tempdb follows it, so temp-table joins can hit collation conflicts);
- time zone (`GETDATE()` returns the pod's local time; containers default to UTC);
- `sp_configure` values the code depends on (`clr enabled`, `cross db ownership chaining`,
  `nested triggers`, `contained database authentication`, ...); `copy_server_configuration` applies
  the safe ones;
- free space on the target's data volume versus the source's size.

**The database and its code**

- references to other databases and linked servers, and synonyms that point elsewhere;
- modules created `WITH ENCRYPTION` (`migrate` can't copy them);
- user-defined messages the code raises (`copy_custom_messages` copies them);
- orphaned users, database audit specifications, Service Broker routes with network addresses;
- server triggers, startup procedures, Extended Events sessions, Resource Governor and
  credentials that live outside the database;
- removed syntax (`*=` joins, `RAISERROR n 'text'`, `COMPUTE`, `FASTFIRSTROW`, `sp_dboption`) and
  deprecated `text` / `ntext` / `image` columns.

## Permissions

`VIEW SERVER STATE` and `VIEW ANY DEFINITION` on both servers cover every check; `msdb` read access
adds the Agent checks. A check that can't run is listed as skipped instead of failing the rest.
