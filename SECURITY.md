# Security Policy

## Supported versions and end of support

| Version | Security fixes |
|---------|----------------|
| 0.5.x   | ✅ yes: the latest minor release |
| older   | ❌ no: upgrade to the latest release |

Only the latest minor release line receives fixes. When a new minor version is released, the previous
one stops receiving security updates **on that day**; this is announced in [CHANGELOG.md](CHANGELOG.md).
Upgrading within 0.x keeps the configuration format unless the changelog says otherwise, so the upgrade
path is always "install the latest release". Before 1.0, minor versions may change behaviour; each
change is listed in the changelog.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately via GitHub's [Security Advisories](https://github.com/karthikbd/mssql-ocp-migrate/security/advisories/new).

You can expect:
- **Acknowledgement** within 48 hours
- **Status update** within 7 days
- **Fix or mitigation** as soon as reasonably possible, coordinated with a disclosure date

## How reports are handled

1. **Acknowledge** within 48 hours and agree on a way to keep in touch.
2. **Triage** within 7 days: reproduce, assess severity (CVSS v4), and decide whether the report is a
   vulnerability in mssql-ocp-migrate or in something it uses.
3. **Fix** on a private fork (GitHub Security Advisory), with a regression test. Target: 30 days for
   high and critical issues, 60 days for medium; low-severity issues may be fixed in the next release.
4. **Release** a patched version, then **publish** the advisory with a CVE (requested through GitHub)
   and list the fix under *Security* in the changelog.
5. **Credit** the reporter by name in the advisory and the release notes, unless they prefer otherwise.

Known vulnerabilities, fixed or not, are published as GitHub Security Advisories for this repository.
Vulnerabilities in dependencies that do **not** affect mssql-ocp-migrate are recorded in the VEX document
[`vex.openvex.json`](vex.openvex.json) (OpenVEX), so scanners can tell users they need not act.

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

## Secrets and credentials used by the project

- **None are stored.** PyPI uploads use Trusted Publishing (short-lived OIDC tokens); release signing is
  keyless (Sigstore). The repository has no Actions secrets except GitHub's own `GITHUB_TOKEN`.
- `GITHUB_TOKEN` is read-only by default in every workflow (`permissions: read-all`); jobs ask for write
  access only for what they do (for example `id-token: write` in the publish job).
- Secret scanning with push protection is enabled; a leaked credential would be revoked at the issuer
  first, then removed from history.
- If a secret is ever needed, it is stored as a GitHub environment secret limited to the job that uses
  it, reviewed when a maintainer changes and rotated at least yearly.
- For users: mssql-ocp-migrate reads credentials from environment variables or mounted files and never writes them
  to logs, reports or state files (see the documentation's Security page).
