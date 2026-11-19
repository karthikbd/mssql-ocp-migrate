# Roadmap

What we plan for the next 12 months (December 2026 to November 2027). Plans change; issues and pull
requests that move these forward are welcome, and each item will get its own issue when work starts.

## Project health

- **A second maintainer** (ideally from another organization), so changes get a second reviewer, releases
  don't depend on one person, and the project can reach the OpenSSF Best Practices **gold** level and
  **baseline level 3** (non-author approval on every merge).
- **Coverage**: raise unit-test statement coverage from about 81% to 90% and branch coverage to 80%
  (gold levels), and report the coverage of the end-to-end suite as well.
- **Copyright and SPDX license lines** in every source file.
- Mark issues suitable for new contributors with the `good first issue` label.

## Migrations

- **Sources:** Azure SQL Managed Instance and Amazon RDS for SQL Server as `lift` sources (backup to URL /
  native backup to S3).
- **Encrypted backups:** `BACKUP ... WITH ENCRYPTION` for `lift`, with the certificate moved the same way
  TDE certificates are today.
- **Objects:** symmetric and asymmetric keys and database scoped credentials where the operator supplies
  the secrets; graph and ledger tables in `migrate`.
- **Online cut-over:** `sync` for tables without change tracking (via CDC), and a cut-over runbook for
  availability-group based `lift`.
- **Operations:** OpenShift operator-style Job templates with resource sizing from `estimate`.

## Releases

About one minor release per quarter, patch releases as needed (see [SECURITY.md](SECURITY.md) for which
versions receive fixes). Every release is signed, reproducible and listed in [CHANGELOG.md](CHANGELOG.md).
