# Choosing an engine

| Question | `migrate` (logical) | `lift` (physical) |
|---|---|---|
| Data size | up to a few TB per run; more with `--shard` across pods | tens to hundreds of TB |
| What travels | rows, decoded and re-encoded | SQL Server's own compressed pages |
| Indexes on target | rebuilt after the load | arrive with the pages (no rebuild) |
| Source during copy | readable; changes after the copy need a reload | fully online; changes replayed from log backups |
| Cut-over downtime | time to reload changed tables | **minutes** (tail-log backup + last restore) |
| Subset of tables | yes (`include` / `exclude`) | no, whole databases |
| Schema changes or a different collation | yes (`schema_mode = "skip"` + your DDL) | no, an exact copy |
| Target version | any SQL Server | same as or newer than the source |
| Needs a shared folder or S3 | no | yes (or `transport = "manual"` and you copy the files) |
| Needs backup permissions on the source | no | yes (`db_backupoperator` or `sysadmin`) |

## Rules of thumb

- **Whole databases above ~2–3 TB:** use `lift`. Backup and restore move compressed pages
  without decoding rows or rebuilding indexes, and the cut-over is a log backup.
- **Smaller databases, subsets or changes:** use `migrate`. Start with `estimate`, then
  raise `workers` or shard across pods.
- **No backups allowed?** Use `migrate --shard` with `target_recovery_simple = true`.
- **Enterprise or Developer on both sides and no file share?** `lift ag-script` prints an
  availability-group seeding setup that streams the database into the pod with no backup files.

## Why physical wins at scale

A logical copy has to send every byte uncompressed (SQL Server's wire protocol has no
compression), decode and re-encode every value, and rebuild every index on the target. A
physical copy sends compressed pages (typically 2–4 times smaller) and restores them as they
are. For 100 TB:

| Link | Logical, 100 TB raw | Physical, ~30 TB compressed |
|---|---|---|
| 1 Gbps | ~10.5 days | ~3.2 days |
| 10 Gbps | ~25 h + index rebuilds | ~7.6 h |
| 25 Gbps | ~10 h + index rebuilds | ~3 h |

For the physical path, the target's storage write speed usually decides the total time: the
restore writes the full uncompressed size. See {doc}`/user_guide/performance`.
