# Physical migration (`lift`): the 100 TB path

`lift` moves **whole databases** by having SQL Server back up and restore its own pages. No row
is ever decoded, no index is rebuilt, and the source keeps serving the application until a
cut-over measured in minutes. All of it runs as T-SQL over the same connections. No agents, SSH or
SQL Agent jobs are needed, and nothing is downloaded.

```text
plan ─► seed ─────────────────────► catch-up ──────────────► cut-over ─► verify
        full backup → restore       log backups every few      tail log,
        WITH NORECOVERY             minutes, applied in order  recovery
        └──── source online, application running ────┘         └ minutes ┘
```

The design follows established community practice: copy-only backups that leave existing backup
chains alone (dbatools `Copy-DbaDatabase`), log shipping for very large databases (dbatools
`Invoke-DbaDbLogShipping`), and continuing log restores without recovery (`sp_DatabaseRestore
@ContinueLogs` from the First Responder Kit).

## Setting up

```toml
[source]
host = "sql01.corp.example.com"
database = "master"
user = "migrator"                     # sysadmin, or db_backupoperator + VIEW SERVER STATE + msdb read
password_env = "SRC_DB_PASSWORD"

[target]
host = "mssql.sqlserver.svc.cluster.local"
database = "master"
user = "sa"                           # dbcreator + sysadmin for logins/TDE
password_file = "/secrets/target-password"

[lift]
databases = ["SalesDB", "Billing"]
transport = "share"
source_backup_dir = '\\nas01\sqlmig'            # as the source server sees the share
target_backup_dir = "/var/opt/mssql/mig"        # the same share mounted into the SQL Server pod
stripes = 32
target_data_dirs = ["/var/opt/mssql/data1", "/var/opt/mssql/data2",
                    "/var/opt/mssql/data3", "/var/opt/mssql/data4"]
target_log_dir = "/var/opt/mssql/log"
network_gbps = 10                     # only used by `lift plan`
target_write_mb_s = 2000              # only used by `lift plan`
```

```{tip}
In TOML, write Windows paths in single quotes (`'\\nas01\sqlmig'`). That's a literal string, so the
backslashes don't need escaping.
```

## 1. Plan

```console
$ mssql-migrate lift plan -c migrate.toml
SalesDB -> SalesDB: 102,400 GB data, 500 GB log, 64 files, recovery FULL, compat 150
  compression ~3.4:1 (measured from 5 recent compressed full backups) -> ~30,118 GB moved
  backup ~9.7 h | copy ~0.0 h | restore ~14.6 h | log catch-up ~0.4 h   (source online the whole time: ~24.7 h)
  expected cut-over downtime: ~6.2 min
  warn: 96 log backups/day are taken by existing jobs; they are picked up from msdb and applied — make sure their folder is mapped in lift.path_map
TOTAL 100.0 TB across 1 database(s), 2 in parallel, 32 stripes each
```

`plan` reads real numbers from the source:

- sizes from `sys.master_files`;
- the **compression ratio and backup speed** from the last five full backups in
  `msdb.dbo.backupset`;
- **log volume per hour** and **who takes log backups** from the last 24 hours of `msdb` history.

It then reports **blockers**, which make `lift run` refuse to start unless you pass `--force`:

| Blocker | Why |
|---|---|
| target version older than source | a backup restores only on the same or a newer version |
| source database not ONLINE | |
| log backups taken by a third-party (virtual device) tool | those files can't be restored by SQL Server alone, so the log chain can't be replayed |
| TDE without `transfer_tde_certificates` | the target couldn't read the backup |
| S3 transport below SQL Server 2022 on either side | S3 URLs need 2022 |
| S3 backup larger than ~12.5 TB compressed | 64 URLs × 10,000 parts × 20 MB per backup set |

It also gives warnings, for example SIMPLE recovery (no log catch-up is possible, so downtime
equals the full backup and restore), Express edition, or existing log-backup jobs.

## 2. Seed

```console
$ mssql-migrate lift seed -c migrate.toml          # or: lift run (seed + catch-up + follow)
```

For each database (`parallel_databases` at a time):

1. **TDE.** If the database is encrypted, its certificate and private key are copied to the
   target's `master` over the connection (`CERTENCODED`/`CERTPRIVATEKEY` →
   `CREATE CERTIFICATE ... FROM BINARY`). A master key is created on the target if needed (password
   from `target_master_key_password_env`).
2. **Full backup**, striped and compressed:
   ```sql
   BACKUP DATABASE [SalesDB] TO DISK = N'\\nas01\sqlmig\SalesDB_full_..._01of32.bak', ... (32 files)
   WITH COPY_ONLY, COMPRESSION, FORMAT, INIT, CHECKSUM, STATS = 1,
        MAXTRANSFERSIZE = 4194304, BUFFERCOUNT = 128
   ```
   SQL Server uses one writer thread per file. `BUFFERCOUNT` defaults to stripes × 4, capped
   so that buffers × transfer size stays within `memory_mb`.
3. **Restore** on the target, reading the same files through the pod's mount:
   ```sql
   RESTORE DATABASE [SalesDB] FROM DISK = N'/var/opt/mssql/mig/SalesDB_full_..._01of32.bak', ...
   WITH MOVE N'SalesDB' TO N'/var/opt/mssql/data1/SalesDB_SalesDB.mdf', MOVE ..., 
        NORECOVERY, CHECKSUM, STATS = 1, BUFFERCOUNT = 128, MAXTRANSFERSIZE = 4194304
   ```
   Data files are spread over `target_data_dirs`, largest first onto the least-filled volume, so
   the restore writes to every volume in parallel. `CHECKSUM` verifies every page's checksum as it
   is restored.

Both long operations run on their own session while a second session reports
`percent_complete` and the estimated time left from `sys.dm_exec_requests`.

**Resumable.** Progress (phase and restored LSN per database) is kept in `lift.state_file`. Running
`seed` again on a seeded database doesn't take another backup. Use `--restart` to seed from a
fresh full backup.

### Transports

| `transport` | How the files move | Use when |
|---|---|---|
| `share` | the source writes to an NFS/SMB share that the target pod mounts | default; any version; any size |
| `s3` | `BACKUP TO URL` / `RESTORE FROM URL` (`s3://...`), with credentials created on both servers | SQL Server 2022 on both sides and below ~12 TB compressed per database |
| `manual` | `seed` stops after the backup and prints the file list; you copy the files, then run `lift resume` | no shared storage is possible |

## 3. Catch up while the source stays online

```console
$ mssql-migrate lift catchup -c migrate.toml                   # one cycle
$ mssql-migrate lift follow  -c migrate.toml --follow-hours 72 # a cycle every log_interval_seconds
```

Each cycle takes one log backup and then applies, **in LSN order**, every log backup in the chain
that the target doesn't have yet, including **log backups taken by your existing jobs** (Ola
Hallengren, maintenance plans, …). Those are found in `msdb.dbo.backupset` /
`backupmediafamily`, and their paths are translated with `path_map`:

```toml
[lift]
path_map = { '\\nas01\sqlbackups' = "/var/opt/mssql/existing-backups" }
```

The chain is checked before anything is applied. The cycle stops with a clear error instead of
restoring a broken sequence when:

- there is a **gap** (a log backup is missing or was deleted);
- a backup went to **`NUL`** (the chain is broken, so seed again with `--restart`);
- a backup came from a **third-party virtual-device tool** (pause that job during the migration).

Copy-only log backups are ignored, because they overlap the regular chain. Transient errors (a
network blip, a share hiccup) are retried on the next cycle.

## 4. Cut over

Stop the application's writes (or let `cutover_source` do it), then:

```console
$ mssql-migrate lift cutover -c migrate.toml --confirm
```

1. A final catch-up while the source is still online.
2. The tail-log backup. What happens to the source depends on `cutover_source`:

   | `cutover_source` | Source after cut-over | Rollback |
   |---|---|---|
   | `norecovery` (default) | `SINGLE_USER WITH ROLLBACK IMMEDIATE`, then `BACKUP LOG ... WITH NORECOVERY`: the database is RESTORING and can't be written | `RESTORE DATABASE [db] WITH RECOVERY`, then `SET MULTI_USER` |
   | `read_only` | `SET READ_ONLY WITH ROLLBACK IMMEDIATE`, then a log backup | `SET READ_WRITE` |
   | `none` | untouched: you must have stopped the application yourself | nothing to undo |

3. The last log restores, then `RESTORE DATABASE ... WITH RECOVERY`. The target is online.
4. Undo replayed options. A READ_ONLY or SINGLE_USER setting made on the source for the cut-over
   is in the replayed log, so it's reverted on the target.
5. Post-recovery steps:
   - **SQL logins** used by the database's users are created with the **same SID and password
     hash**, so no users are orphaned and no passwords need resetting (`copy_logins`);
   - the **database owner** is set to the source's owner if that login exists;
   - **TRUSTWORTHY**, **DB_CHAINING** and **Service Broker** are set to match the source;
   - **orphaned users** are re-mapped to logins of the same name, and the rest are listed;
   - optionally `DBCC CHECKDB WITH PHYSICAL_ONLY` (`checkdb_after`; slow on 100 TB, so usually run
     later).

The write downtime is printed at the end and stored in the state file.

## 5. Verify

```console
$ mssql-migrate lift verify -c migrate.toml
```

The copy is physically identical and every page checksum was verified during the restore.
`verify` adds evidence that's cheap even at 100 TB:

- **row counts for every table** from `sys.partitions`, on both sides;
- **server-side checksums of random key windows**: for each table with a key,
  `verify_windows_per_table` random start keys found with `TABLESAMPLE` (which reads a few pages,
  not the table), and a checksum of the next `verify_sample_rows` rows in key order, on both
  servers.

For a full logical comparison afterwards, `mssql-migrate validate` works on any two databases.

## Option B: availability-group seeding (no backup files)

With **Enterprise or Developer edition on both sides**, a cluster-less availability group
(`CLUSTER_TYPE = NONE`) can stream the database straight into the pod with automatic seeding and
keep it synchronized. Cut-over is then a manual failover.

```console
$ mssql-migrate lift ag-script -c migrate.toml --ag-name ag_mig --target-host mssql-hadr.example.com
```

This prints a script to review: endpoints with certificate authentication, trace flag 9567 to
compress the seeding stream, the AG on the source, the join on the target, how to watch
`sys.dm_hadr_physical_seeding_stats`, and the failover. Expose port 5022 of the SQL Server pod with
`deploy/openshift/lift/04-ag-endpoint-service.yaml`. For production availability groups on
OpenShift, a certified SQL Server operator (for example DH2i DxOperator) handles the AG lifecycle.

## From Python

```python
from mssql_ocp_migrate import Config
from mssql_ocp_migrate.physical import Lift, format_plan

cfg = Config.load("migrate.toml")
lift = Lift(cfg, progress=print)
plans = lift.plan()
print(format_plan(plans, cfg.lift))
assert not any(p.blockers for p in plans)

lift.run()                          # seed all databases + one catch-up
lift.follow("SalesDB", hours=24)    # keep current
lift.cutover("SalesDB")             # in the window
print(lift.verify("SalesDB"))
```
