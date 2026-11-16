# Security

## Permissions

Grant the least that each engine needs.

| Engine | Source | Target |
|---|---|---|
| `migrate` | `db_datareader` + `VIEW DEFINITION` (+ `VIEW DATABASE STATE` for size estimates) | `db_owner` on the pre-created, empty database (creates the schema; bulk insert needs INSERT and ALTER) |
| `lift` | `db_backupoperator` on each database + read on `msdb` backup history + `VIEW SERVER STATE`; `sysadmin` (or CONTROL on the certificate) for TDE and logins | `dbcreator`; `sysadmin` to create logins, a master key and certificates |
| `validate` | `db_datareader` | `db_datareader` |

`ADMINISTER BULK OPERATIONS` is **not** needed: TDS bulk insert streams rows over the connection
and never reads files on the server.

## Secrets

- Use `password_file` (an OpenShift Secret mounted as a file) or `password_env`, not `password`.
- Reports and `Config.to_dict()` redact passwords.
- S3 keys come from environment variables (`s3_access_key_env`, `s3_secret_key_env`) and are
  stored only as a SQL Server CREDENTIAL on the servers.
- The TDE private key is exported encrypted with a **random one-time password**, moved over the
  connection and imported. It is never written to disk by the tool. Back up the certificate on the
  target afterwards.

## Encryption in transit

Use `encrypt = "required"` with certificate verification (`ca_file` or the OS trust store, plus
`hostname_in_certificate` when connecting through a tunnel). `trust_server_certificate = true`
still encrypts but doesn't verify; use it only inside trusted networks or with self-signed
container certificates. See {doc}`connections`.

Backup files are compressed but **not encrypted** by this tool. Protect the share, or use
`BACKUP ... WITH ENCRYPTION` by pre-creating a certificate (not automated).

## Logins and passwords (`lift`)

`copy_logins = true` recreates SQL logins used by the database **with the same SID and the same
password hash** (`CREATE LOGIN ... WITH PASSWORD = 0x... HASHED, SID = 0x...`), the technique used
by `sp_help_revlogin` and dbatools `Copy-DbaLogin`. No clear-text password is ever read or
written. Windows logins aren't copied.

## Supply chain

- The package has **no runtime dependencies**. A test fails the build if any module imports an
  HTTP client, `pip`, `subprocess` or any non-standard-library module other than the optional
  drivers.
- The bundled driver is python-tds 1.17.1 (MIT). Its imports were rewritten to a private
  namespace, so it never clashes with an installed `pytds`, and its TLS layer was rewritten on the
  standard library. See `THIRD_PARTY_NOTICES.md`.
- Air-gapped installs: `pip install --no-index` of one wheel.
