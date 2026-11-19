# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.5.x   | ✅ Active  |
| < 0.5   | ❌         |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately via GitHub's [Security Advisories](https://github.com/karthikbd/mssql-ocp-migrate/security/advisories/new).

You can expect:
- **Acknowledgement** within 48 hours
- **Status update** within 7 days
- **Fix or mitigation** as soon as reasonably possible, coordinated with a disclosure date

## Network access

`mssql-ocp-migrate` has no telemetry, no update check and no HTTP calls. It declares **no runtime
dependencies** (the SQL Server driver is bundled), so `pip install` downloads nothing else. A test
fails the build if any module imports an HTTP client, `pip` or `subprocess`.

It opens connections only to the servers named in your configuration:

| Connection | Why |
|------------|-----|
| `[source]` host, TCP (TDS) | read the catalog and data; for `lift`, run BACKUP and read `msdb` history |
| `[target]` host, TCP (TDS) | create the schema, load data, validate; for `lift`, run RESTORE |
| SQL Browser, UDP 1434 | only when `instance` is set, to resolve a named instance |

With `lift.transport = "s3"`, **SQL Server itself** makes the HTTPS calls to your S3 endpoint; the
package only sends the T-SQL.

## Credentials and data

- Passwords come from `password_file` (e.g. an OpenShift Secret) or `password_env`; reports redact them.
- `lift` copies SQL logins with their existing SID and **password hash**; no clear-text password is
  read or written. The TDE private key is exported encrypted with a one-time random password and
  moved over the connection, never to disk.
- Backup files written by `lift` are compressed, not encrypted: protect the share.

## Supply chain

- Every CI `pip install` is hash-locked (`ci/requirements-*.txt`); every GitHub Action is pinned to a commit SHA.
- `vulnerability-audit.yml`: `pip-audit` of the vendored python-tds and all CI tooling, weekly and on every change.
- CodeQL and OpenSSF Scorecard on every push to `main` and weekly; Dependabot for actions and CI tooling.
- Releases: PyPI Trusted Publishing with provenance attestations; Sigstore-signed wheel, sdist and a
  CycloneDX SBOM (declaring the vendored driver) attached to each GitHub release.

See the [Security page](https://mssql-ocp-migrate.readthedocs.io/en/latest/user_guide/security.html)
of the documentation for permissions and TLS settings.
