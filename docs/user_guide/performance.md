# Performance and sizing

## Where the time goes

**Physical (`lift`):** the total is roughly backup + restore + catch-up, and downtime is roughly
the tail-log restore.

- The backup is limited by source read speed, or by the network (compressed bytes) when the share
  sits on the target side.
- The restore writes the **full uncompressed size** to the target's storage, so it is usually the
  slowest stage.

**Logical (`migrate`):** the total is roughly copy + index build + validation.

- The copy is CPU-bound in the client for the bundled driver, and network- or log-bound for the C
  drivers.

## Physical: what to tune

| Lever | Setting | Effect |
|---|---|---|
| stripes | `lift.stripes` (1–64) | one backup and restore thread per file; 16–64 for large databases |
| buffers | `lift.buffercount`, `lift.memory_mb` | more buffers keep every stripe busy; memory = buffers × transfer size |
| transfer size | `lift.maxtransfersize` | 4 MB maximum for disk (S3 uses 20 MB) |
| compression | `lift.compression` | 2–4 times fewer bytes on the network and share; costs source CPU |
| target volumes | `lift.target_data_dirs` | spread data files over 4–8 PVs so restore writes run in parallel |
| storage class | OpenShift | the biggest lever: restore speed ≈ aggregate write throughput |
| SQL Server pod CPU | OpenShift | restore decompression is CPU-bound; give the pod real cores |
| NFS mount | `nconnect=16`, 1 MB rsize/wsize | many TCP streams per mount (see `01-backup-share.yaml`) |
| databases in parallel | `lift.parallel_databases` | overlaps backup and restore of different databases |

### Reference timeline: 100 TB, ~3:1 compression

| Stage | 10 Gbps, 2 GB/s target | 25 Gbps, 4 GB/s target |
|---|---|---|
| full backup (source online; assumes ≥3 GB/s source reads) | ~10 h | ~4–7 h |
| restore (source online) | ~15 h | ~7 h |
| log catch-up | minutes to ~1 h | minutes |
| **cut-over downtime** | **~5–15 min** | **~5–15 min** |

`lift plan` recomputes this with **your** compression ratio and backup speed from `msdb`, plus
`network_gbps` and `target_write_mb_s`.

## Logical: what to tune

| Lever | Setting | Notes |
|---|---|---|
| workers | `migration.workers`, `-w` | parallel chunks |
| executor | `migration.executor` | `auto` uses processes for the bundled driver and pymssql (one core each), and threads when both sides use pyodbc |
| driver | `driver` | pyodbc or pymssql, if installed, are typically several times faster per stream than the pure-Python driver; run `estimate` to measure |
| scale out | `--shard auto/N` | more pods, each with its own workers |
| chunk size | `migration.chunk_rows` | 100k–1M; smaller chunks resume and balance better, larger ones have less overhead |
| batch size | `migration.batch_rows` | rows per fetch and send batch |
| logging | `migration.target_recovery_simple` | minimally logged bulk inserts |
| indexes | `create_indexes_after_load = true` | build once after the load instead of maintaining them during it |

### Measured baseline (bundled driver)

The bundled driver serialises about **37,000 rows/s per CPU core** for a 10-column mixed-type row
(~122 bytes on the wire), measured in the test environment. Reading costs about the same, so plan
for **~15–25k rows/s (~2–4 MB/s) per worker process**, roughly:

| Workers (processes) | Typical throughput |
|---|---|
| 8 | ~55–100 GB/hour |
| 16 | ~100–180 GB/hour, if the network and target log disk keep up |
| 16 pods × 8 workers (`--shard`) | limited by the target's write capacity, not the client |

Add 20–30 % for index and foreign-key builds. Checksum validation is a server-side scan of both
databases.

```{important}
These figures are estimates from the client-side measurement above, not from a benchmark
against a real server. `mssql-migrate estimate` measures your actual read, write and checksum
rates and projects the total for your `workers` setting. Rely on its numbers.
```

## Estimate before you commit

```console
$ mssql-migrate estimate -c migrate.toml -w 16
Tables: 142   Rows: 1,284,559,012   Source size: 412,880 MB
Sample: 50,000 rows from sales.OrderLine
Per stream: read 41,200 rows/s, write 23,900 rows/s (~2.9 MB/s); server checksum ~610 MB/s
With 16 workers:
  copy        ~212.4 min
  indexes/FKs ~53.1 min
  validation  ~48.0 min
  TOTAL       ~313.5 min  (likely range 219.5–501.6 min)
```

The write probe bulk-loads the sample into a `#temp` table on the target and rolls it back, so the
target is not changed. Use `--no-write-probe` to measure reads only.
