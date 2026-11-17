# Security

## Permissions

Grant the least that each engine needs.

| Engine | Source | Target |
|---|---|---|
| `migrate` | `db_datareader` + `VIEW DEFINITION` (+ `VIEW DATABASE STATE` for size estimates) | `db_owner` on the pre-created, empty database (creates the schema; bulk insert needs INSERT and ALTER) |
| `lift` | `db_backupoperator` on each database + read on `msdb` backup history + `VIEW SERVER STATE`; `sysadmin` (or CONTROL on the certificate) for TDE and logins | `dbcreator`; `sysadmin` to create logins, a master key and certificates |
| `validate` | `db_datareader` | `db_datareader` |
| `migrate` with `copy_security` | add `VIEW ANY DEFINITION` (server level), so users' login names can be resolved | add `VIEW ANY DEFINITION`, so existing logins can be found; `db_owner` covers creating users and roles, granting, and `ALTER DATABASE` for the settings |
| `migrate` with `copy_logins`, `copy_cdc`, `copy_agent_jobs`, `copy_linked_servers` or `trust_clr_assemblies` | `sysadmin` (password hashes, msdb, linked-server catalog) | `sysadmin` (`CREATE LOGIN`, `sp_cdc_enable_db`, msdb jobs, `sp_addlinkedserver`, `sp_add_trusted_assembly`) |
| `migrate` with masked columns | `UNMASK` (the run stops without it, so masked values are never copied as real data) | |
| `migrate` with `copy_certificates` | `CONTROL` on each certificate (to export the private key) | a database master key password in `master_key_password_env` when the target has no master key |

Every server-level switch is optional: with less than `sysadmin`, turn those switches off and the
run reports what it didn't copy.

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

## Supply chain and vulnerability management

| Control | How |
|---|---|
| No runtime dependencies | `pip install` brings one package. A test fails the build if any module imports an HTTP client, `pip`, `subprocess` or a non-standard-library module other than the optional drivers, and the release job refuses a wheel with `Requires-Dist` lines |
| Bundled code is declared | python-tds 1.17.1 is listed in `THIRD_PARTY_NOTICES.md` and in the CycloneDX SBOM attached to each GitHub release, so JFrog Xray, Dependency-Track and similar tools can see it |
| Known-vulnerability audit | `vulnerability-audit.yml` runs `pip-audit` (OSV and PyPA advisory databases) on the bundled driver and every CI tool set, on every push and pull request and weekly |
| Static analysis | CodeQL on every push, pull request and weekly |
| OpenSSF Scorecard | on every push to `main` and weekly; results in the GitHub Security tab |
| Pinned build | every GitHub Action pinned to a commit SHA; every CI `pip install` hash-locked (`ci/requirements-*.txt`, `--require-hashes`); the package installed with `--no-deps` |
| Dependency updates | Dependabot, grouped weekly PRs for actions and CI tooling |
| Publishing | PyPI Trusted Publishing (OIDC, no stored token) from the protected `release` environment; PyPI provenance attestations |
| Signed releases | Sigstore signatures for the wheel, sdist and SBOM attached to each GitHub release |
| Least privilege | workflows default to `read-all`; only the publish and signing jobs get `id-token: write` / `contents: write` |

At release 0.2.1, `pip-audit` reports **no known vulnerabilities** for python-tds 1.17.1 or for any
CI tool, and OSV has no advisories for python-tds, pymssql 2.4.6 or pyodbc 5.2.0.

Other notes:

- The bundled driver is python-tds 1.17.1 (MIT). Its imports were rewritten to a private
  namespace, so it never clashes with an installed `pytds`, and its TLS layer was rewritten on the
  standard library. See `THIRD_PARTY_NOTICES.md`.
- Air-gapped installs: `pip install --no-index` of one wheel.
