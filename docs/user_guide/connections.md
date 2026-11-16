# Connections, drivers and TLS

## Drivers

| | `pytds` (bundled) | `pymssql` (optional) | `pyodbc` (optional) |
|---|---|---|---|
| Installed by this package | **yes** (inside it) | no, you install it | no, you install it and ODBC Driver 18 |
| Implementation | pure Python | C (FreeTDS bundled in its wheel) | C (Microsoft ODBC driver) |
| Bulk load | TDS `INSERT BULK` with exact column types | FreeTDS bulk copy | `fast_executemany` |
| TLS certificate checks | Python `ssl`: CA file, OS store, host-name override | FreeTDS settings only | full ODBC options |
| Default worker type | processes | processes | threads |

`driver = "auto"` (the default) picks **pyodbc**, then **pymssql**, then **pytds**, using the first
one that is importable (pyodbc also needs a registered ODBC driver named by `odbc_driver`). Each
side is chosen independently: you can read with one driver and write with another.

### Things the tool handles per driver

- **Full temporal precision.** `datetime2(7)`, `time(7)` and `datetimeoffset(7)` hold 100 ns ticks,
  and `datetime` holds 1/300 s; Python's `datetime` stops at microseconds. These columns travel as
  ISO text and SQL Server converts them back, so nothing is rounded. With pymssql, every
  `datetime2`, `time` and `datetimeoffset` column travels this way, because pymssql formats
  datetime parameters with milliseconds only.
- **GUIDs.** pyodbc returns text and the other drivers return `uuid.UUID`; values are converted
  where needed, and validation hashes both forms identically.
- **Bulk copy and NULLs (pymssql).** FreeTDS bulk copy cannot keep NULL in a column that has a
  DEFAULT; SQL Server would write the default instead. Such tables, and tables with xml, CLR or
  `datetimeoffset` columns, are loaded with multi-row INSERTs instead.
- **Spatial and CLR types** (`geography`, `geometry`, `hierarchyid`) travel as `varbinary(max)`
  and SQL Server converts them back.

## Encryption

`encrypt` is `off`, `optional` (encrypt if the server supports it; the default) or `required`.

| Setting | Effect with the bundled driver |
|---|---|
| `trust_server_certificate = true` | encrypted, certificate not verified (typical for SQL Server containers with self-signed certificates) |
| `ca_file = "/etc/pki/sql-ca.pem"` | verify against this CA bundle |
| neither | verify against the operating system's trust store |
| `hostname_in_certificate = "mssql.example.com"` | expect this name in the certificate (needed with `oc port-forward`, where you connect to 127.0.0.1) |

The bundled driver negotiates TLS 1.2 inside the TDS pre-login handshake, the way SQL Server
expects for TDS 7.x. This is implemented on Python's standard `ssl` module, so pyOpenSSL isn't
needed. The test suite checks it at the wire level against a fake TDS server: a trusted
certificate, a CA file with a host-name override, an untrusted certificate (rejected), a host-name
mismatch (rejected), and plain text.

## Reaching SQL Server on OpenShift

| From | `[target]` settings |
|---|---|
| a Pod in the same cluster | `host = "mssql.<namespace>.svc.cluster.local"`, `port = 1433` |
| a workstation, via `oc port-forward svc/mssql 14330:1433` | `host = "127.0.0.1"`, `port = 14330`, `hostname_in_certificate = "<cert name>"` |
| outside, via a LoadBalancer or NodePort Service | the external address and port |

Port-forwarding goes through the OpenShift API server and is fine for tests and small databases.
For large data, run the tool as a Job inside the cluster ({doc}`openshift`).

## Named instances

```toml
[source]
host = "sql01"
instance = "PROD"     # resolved through SQL Server Browser (UDP 1434); port is ignored
```

## Timeouts

`login_timeout` (default 30 s) covers connect and login. `query_timeout` (default none) limits
each statement. Long BACKUP and RESTORE operations in `lift` always run without a timeout.
