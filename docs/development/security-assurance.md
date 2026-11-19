# Security assurance case

Why we believe mssql-ocp-migrate meets its security requirements: what it protects, who it trusts,
what can go wrong, and what stops it. It is reviewed before every release (see {doc}`releasing`) and
whenever a migration path or copied object type is added.

## Security requirements (what users can expect)

1. **Confidentiality of data and credentials.** Rows, backups and secrets move only between the source
   and target servers named in the configuration. Passwords are never written to logs, reports or
   state files; private keys travel encrypted with a one-time random password and are never written to
   disk by the tool.
2. **Integrity of the copy.** After `migrate`, `validate` proves the target matches the source (row
   counts, server-side checksums, row-by-row SHA-256 fingerprints by key) and names every row that
   differs; a difference is never reported as a success.
3. **No surprise connections or code.** No telemetry, no update checks, no runtime dependencies, no HTTP
   client, no `subprocess`; connections go only to the configured servers, over TLS with certificate
   verification when `encrypt = "required"` (recommended; see {doc}`/user_guide/security`).
4. **Safe handling of catalog and data values.** Object names, definitions and row values from the source
   are quoted or bound where they are used, never executed as unintended T-SQL.
5. **No destructive surprise.** Steps that stop writes on the source or replace data require an explicit
   flag (`--confirm` for cut-over, `if_target_not_empty = "truncate"` to overwrite a non-empty target).
6. **A trustworthy release.** What users install is what was built from the tagged commit: signed,
   reproducible, with a software bill of materials.

## Design and actors

See {doc}`architecture` for the components. Actors and what they can do:

| Actor | Trust | Can |
|---|---|---|
| Operator (writes the configuration, runs the CLI or the OpenShift Job) | Trusted | Choose servers, credentials, permissions, which objects are copied, and whether to cut over. |
| Source SQL Server | Identity verified (TLS when required); catalog and data treated as data | Supply object names, definitions, settings and rows. |
| Target SQL Server on OpenShift | Identity verified (TLS when required) | Receive schema, data and objects; run RESTORE for `lift`. |
| Backup share / S3 bucket (`lift`) | Protected by the operator | Hold compressed (not encrypted) backup files. |
| Network | Untrusted | Observe or alter traffic unless TLS is used. |
| Contributors | Reviewed | Propose changes through pull requests with required checks. |
| CI and release pipeline | Trusted, constrained | Build, sign and publish from the repository only. |

## Trust boundaries

1. **Configuration → process.** Unknown keys and wrong value types are rejected at start-up; secrets come
   from `password_file`, environment variables or OpenShift Secrets.
2. **Source catalog and rows → process.** Names, module definitions, extended properties, row values.
3. **Process → target server.** Generated DDL, bulk-insert streams, `INSERT` statements, server-level
   objects (logins, jobs, linked servers) when enabled.
4. **Process → local files.** The state file (chunk boundaries, i.e. key values), reports (keys of rows
   that differ), the assessment JSON.
5. **Repository → release.** CI builds and signs; PyPI receives uploads only from the release workflow.

## Threats and countermeasures

| Threat (boundary) | Countermeasure | Evidence |
|---|---|---|
| T-SQL injection through object names (2→3) | Every identifier is bracket-quoted with `]` doubled (`ddl.q`); literals are N-quoted with `'` doubled (`ddl.lit`); fuzzed for round-trips | `fuzz_chunking` (identifier quoting); `tests/test_units.py` |
| T-SQL injection through row values (2→3) | Values travel as bound parameters, TDS bulk-insert values or typed hex/N-literals, never as SQL text | `sqlserver.py`; CodeQL; `ruff S608` reviewed |
| A copy that silently differs (2→3) | Chunked copy in transactions; validation by count, checksum and SHA-256 row fingerprints with length-prefixed fields (no two different rows share a fingerprint); differences reported by key | `fuzz_rowhash`; `tests/test_engine.py`; e2e suite on 3 drivers × 4 version pairs |
| Credential disclosure (1, 4) | Passwords from files or environment; redacted in `Config.to_dict()` and reports; logins copied by password **hash** only; GitHub secret scanning with push protection | {doc}`/user_guide/security` |
| Private-key disclosure during certificate / TDE transfer (3, 4) | Exported encrypted with a 192-bit random one-time password (`secrets.token_hex`), moved over the connection, never written to disk; the `schema` script leaves private keys out | `catalog.py`, `physical.py`, `tests/test_cli.py::test_schema_script_never_contains_private_keys` |
| Eavesdropping or tampering in transit (network) | TLS with certificate verification when `encrypt = "required"` and `trust_server_certificate = false`; the bundled driver's TLS is tested against good, untrusted and mismatched certificates | `tests/test_tls.py` |
| Sensitive values in local files (4) | State files hold chunk-boundary keys and reports hold keys of differing rows; documented so operators keep them with the same care as the data (the OpenShift Job writes them to a PVC) | {doc}`/user_guide/security` |
| Unintended cut-over or overwrite (1→3) | `lift cutover` refuses without `--confirm`; a non-empty target is refused unless `truncate` or `resume` is chosen | `tests/test_physical.py`, `tests/test_engine.py` |
| Malformed configuration failing mid-run (1) | Strict key and type validation at start-up (`ConfigError`); fuzzed | `fuzz_config` |
| Malicious or vulnerable dependency (5) | No runtime dependencies; the bundled driver is pinned, audited by `pip-audit` and declared in the SBOM; CI tooling hash-locked; actions pinned by SHA; Dependabot | `ci/`, `vulnerability-audit.yml` |
| Tampered release (5) | PyPI Trusted Publishing with provenance; Sigstore-signed assets re-verified by `verify-release.yml`; reproducible builds checked in CI | `python-publish.yml`, `tests.yml` |

## Secure design principles applied

- **Least privilege:** each engine documents the minimum grants; every server-level switch is optional;
  CI tokens are read-only unless a job needs more.
- **Fail-safe defaults:** a non-empty target is refused; masked columns stop the run without `UNMASK`;
  server-level copies (jobs, linked servers, server configuration and triggers) are off by default;
  copied SQL Agent jobs are created disabled.
- **Complete mediation:** every name and value is quoted or bound where the statement is built.
- **Economy of mechanism:** no runtime dependencies, no HTTP, no `subprocess` (enforced by a test).
- **Open design:** everything above is public and tested; nothing relies on obscurity.
- **Separation of privilege:** publishing needs the protected `release` environment and an OIDC identity,
  not a stored token; cut-over needs an explicit flag.
- **Psychological acceptability:** `assess`, `estimate`, `schema` and `validate` let operators see what
  will happen before it does.

## Common weaknesses considered

CWE-89 (SQL injection), CWE-78 and CWE-94 (command and code injection: no `subprocess`, `eval` or
`pickle`), CWE-295 (certificate validation), CWE-312 and CWE-532 (cleartext or logged secrets),
CWE-330 (weak randomness for key-protecting passwords), CWE-20 (input validation), CWE-400 (resource
exhaustion: streamed batches, bounded memory), CWE-494 and CWE-829 (unverified or untrusted code).
CodeQL and `ruff`'s `S` rules scan for these on every change; findings must be fixed or justified
before merging.

## Review at release

Before each release the maintainer re-reads this page against the changes since the last release (new
statements generated, new server-level objects, new files written) and updates it; the release
checklist in {doc}`releasing` includes this step. Last review: 0.5.1 (November 2026). It found that
`mssql-migrate schema -o` wrote certificate private keys, with their one-time password, into the script;
the script now leaves private keys out. 0.5.0 added the fuzz targets and fixed what they found.
