# Troubleshooting

Start with `--log-level DEBUG` and the report's DDL-error and warning sections.

## Connecting

`cannot connect to host:port/db with pytds: ... TimeoutError`
: The port isn't reachable. Check the Service, `oc port-forward`, firewall and egress rules, and
  that `port` matches. A named instance needs UDP 1434 (SQL Browser).

`Server requires encryption: set encrypt='required' or 'optional'`
: The server forces encryption but the config has `encrypt = "off"`.

`CERTIFICATE_VERIFY_FAILED` / `certificate verify failed`
: The server certificate isn't trusted. Point `ca_file` at its CA, or set
  `trust_server_certificate = true` for self-signed container certificates.

`IP address mismatch` / host name mismatch
: You connect by IP or through a tunnel. Set `hostname_in_certificate` to the name in the
  certificate.

`environment variable X is not set`
: `password_env` names a variable that isn't exported in this shell or Pod.

## Logical copy

`target tables already contain data` / `already exist`
: That's the `if_target_not_empty = "fail"` policy protecting you. Use `--truncate` to reload, or
  `--resume` to continue.

`chunk dbo.T#12 failed: ...` in the report
: The chunk was retried `max_retries` times. Fix the cause (space, permissions, constraint), then
  run with `--resume`: only failed and missing chunks run again.

`indexes/constraints not created because some chunks failed`
: Intentional: indexes are built only on complete data. Resume first.

`schema_mode=skip but target is missing tables`
: Deploy the schema (DACPAC or scripts) first, or use `schema_mode = "create"`.

Validation shows `different` keys right after a copy
: Usually the source changed during the copy. Re-run the affected tables during a freeze
  (`--truncate --include ...`), or use `lift` for live databases.

Slow chunks on a `varchar` key
: Make sure the target and source collations match. Predicates are cast to the key's type, so
  index seeks work. The worker type (processes for the bundled driver) also matters; see
  {doc}`performance`.

## Lift

`log chain gap: next log backup starts at LSN … but target is at …`
: A log backup is missing: deleted, or written somewhere not mapped in `path_map`. Map the folder
  or restore the file. If it's gone, seed again with `lift seed --restart`.

`... was written to NUL: the log chain is broken`
: Someone ran `BACKUP LOG ... TO DISK = 'NUL'`. Seed again with `--restart`.

`... taken by a third-party (virtual device) tool`
: A backup product (Commvault, NetBackup, Veeam, …) takes log backups through VDI. Pause that
  log-backup job for the migration window, then seed.

`target database X already exists (ONLINE)`
: Drop it, or set `lift.rename` to restore under another name.

`S3 backup/restore needs SQL Server 2022 on both sides` / `exceeds the S3 limit`
: Use `transport = "share"`.

The restore is slow
: Check the SQL Server pod's CPU (decompression) and the storage class throughput. Spread
  `target_data_dirs` over more PVs. See {doc}`performance`.

After cut-over the database is read-only or single-user
: The tool reverts replayed READ_ONLY and SINGLE_USER automatically. If you ran the cut-over by
  hand, use `ALTER DATABASE [db] SET READ_WRITE` / `SET MULTI_USER`.

Users can't log in after cut-over
: Windows logins aren't copied. Check the `post_notes` in `lift status` for orphaned users
  without a matching login.

## Getting help

Collect `report.json`, the state file and a `--log-level DEBUG` log (passwords are never logged)
and open an issue.
