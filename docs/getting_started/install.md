# Installation

## Requirements

| | |
|---|---|
| Python | 3.10 or newer (CI runs 3.10, 3.11, 3.12, 3.13) |
| Operating system | Linux, Windows, macOS: pure Python, one universal wheel (`py3-none-any`) |
| Source and target | SQL Server 2012 or newer (logical copy). For `lift`, the target version must be the same as or newer than the source. |
| Python dependencies | **None.** `pip install` installs this package only. |

## From PyPI

```console
$ pip install mssql-ocp-migrate
$ mssql-migrate --version
mssql-migrate {{ version }}
```

This installs the `mssql-migrate` command and the `mssql_ocp_migrate` Python package.

## Windows (PowerShell)

```powershell
# 1. A virtual environment
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install
python -m pip install --upgrade pip
pip install mssql-ocp-migrate

# 3. Check
mssql-migrate --version
```

If `Activate.ps1` is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use
`.\.venv\Scripts\activate.bat` from `cmd`.

## Linux / macOS

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install mssql-ocp-migrate
mssql-migrate --version
```

## Inside a corporate network (JFrog Artifactory / Curation)

If pip goes through Artifactory, point it at your PyPI remote repository:

```powershell
pip install --index-url https://<artifactory-host>/artifactory/api/pypi/<pypi-remote>/simple mssql-ocp-migrate
```

or once, for all installs:

```powershell
pip config set global.index-url https://<artifactory-host>/artifactory/api/pypi/<pypi-remote>/simple
```

JFrog Curation evaluates new packages before allowing them. mssql-ocp-migrate is easy to approve:

- **One package, nothing else.** It has no runtime dependencies, so Curation evaluates exactly
  one package.
- **MIT-licensed**, published from GitHub with **PyPI Trusted Publishing**. Every file on PyPI has a
  **provenance attestation** linking it to the GitHub workflow run that built it.
- **Signed releases.** GitHub release assets are signed with **Sigstore** and come with a
  **CycloneDX SBOM**. The SBOM declares the bundled third-party code (python-tds 1.17.1, MIT), which
  scanners that read package metadata would otherwise not see.
- **Continuous checks:** OpenSSF Scorecard, CodeQL, and a weekly `pip-audit` of the bundled driver
  and all build tooling.

A very new version can be held by a "package age" policy for some days. That's a policy wait, not
a problem with the package.

To check a downloaded file's signature against the GitHub workflow that produced it:

```console
$ pip download --no-deps mssql-ocp-migrate=={{ version }} -d dl
$ python -m pip install sigstore
$ python -m sigstore verify github dl/mssql_ocp_migrate-{{ version }}-py3-none-any.whl \
    --bundle mssql_ocp_migrate-{{ version }}-py3-none-any.whl.sigstore.json \
    --cert-identity https://github.com/karthikbd/mssql-ocp-migrate/.github/workflows/python-publish.yml@refs/tags/v{{ version }} \
    --repository karthikbd/mssql-ocp-migrate --ref refs/tags/v{{ version }} --trigger release
OK: dl/mssql_ocp_migrate-{{ version }}-py3-none-any.whl
```

The `.sigstore.json` bundles for the wheel, the sdist and the SBOM are attached to the GitHub
release. Behind a proxy that blocks Sigstore's update server (`tuf-repo-cdn.sigstore.dev`), add
`--offline`: the check then uses the trust root shipped inside the `sigstore` package and still
verifies the signature, the certificate chain and the transparency-log inclusion proof in the
bundle.

The same check runs on GitHub after every release (`verify-release.yml`): it downloads the release
files, verifies each signature against the publish workflow and the tag, and confirms PyPI serves
byte-identical files.

## Offline and air-gapped installs

The package has no dependencies, so a single wheel file is a complete installation. Copy it into the
restricted network and install it without an index:

```console
$ pip install --no-index --no-deps mssql_ocp_migrate-{{ version }}-py3-none-any.whl
```

`--no-index` makes pip refuse any network access, which proves nothing else is fetched.

### OpenShift image

`deploy/openshift/Containerfile` builds a Red Hat UBI image with the same offline install:

```console
$ python -m pip wheel . -w dist --no-deps
$ podman build -f deploy/openshift/Containerfile -t mssql-ocp-migrate:{{ version }} .
$ podman push mssql-ocp-migrate:{{ version }} image-registry.example.com/sqlserver/mssql-ocp-migrate:{{ version }}
```

## Drivers: what talks to SQL Server

The package always works with its **bundled** driver. If one of two optional C drivers is already
installed, it is used automatically because it is faster. Nothing is ever installed for you.

| Driver | Where it comes from | When `driver = "auto"` picks it | Notes |
|---|---|---|---|
| `pyodbc` | you install `pyodbc` **and** Microsoft *ODBC Driver 18 for SQL Server* | first, if both are present | fastest for wide rows; full TLS options |
| `pymssql` | you install `pymssql` (its wheel already contains FreeTDS and OpenSSL) | second | native bulk copy; no ODBC driver needed |
| `pytds` | **bundled** in this package (python-tds 1.17.1, MIT) | always available | pure Python; TLS on Python's own `ssl` module |

Force one with `driver = "pytds" | "pymssql" | "pyodbc"` in `[source]` or `[target]`. See
{doc}`/user_guide/connections`.

```{note}
The phrase "no downloads" is about this package. If you choose `pymssql` or `pyodbc`, installing them
is your decision and happens outside this tool. With `lift` over S3, the HTTPS traffic comes from
SQL Server itself, not from Python.
```

## Optional extras

```console
$ pip install "mssql-ocp-migrate[test]"     # pytest, to run the test suite
$ pip install "mssql-ocp-migrate[docs]"     # Sphinx + theme, to build this documentation
```

## Verifying the installation

```console
$ mssql-migrate init-config > migrate.toml
$ python -c "import mssql_ocp_migrate as m; print(m.__version__)"
```

Then run `mssql-migrate check -c migrate.toml` once you've filled in the connection details.

## Verifying a download

Every GitHub release carries Sigstore signatures (`.sigstore.json`) for the wheel, the sdist and the
CycloneDX SBOM. To check that a file was built by this repository's release workflow for that tag:

```console
$ pip download --no-deps mssql-ocp-migrate=={{ version }} -d dl
$ gh release download v{{ version }} -R karthikbd/mssql-ocp-migrate -p "mssql_ocp_migrate-{{ version }}-py3-none-any.whl.sigstore.json" -D dl
$ python -m pip install sigstore
$ python -m sigstore verify github dl/mssql_ocp_migrate-{{ version }}-py3-none-any.whl \
    --bundle dl/mssql_ocp_migrate-{{ version }}-py3-none-any.whl.sigstore.json \
    --cert-identity https://github.com/karthikbd/mssql-ocp-migrate/.github/workflows/python-publish.yml@refs/tags/v{{ version }} \
    --repository karthikbd/mssql-ocp-migrate --ref refs/tags/v{{ version }} --trigger release
OK: dl/mssql_ocp_migrate-{{ version }}-py3-none-any.whl
```

The identity check proves who produced the file: the `python-publish.yml` workflow of
`karthikbd/mssql-ocp-migrate`, run for that release tag. Behind a proxy that blocks Sigstore's update
server, add `--offline`. Releases are also reproducible: rebuilding the tag gives byte-identical files
(see the release page of the developer documentation). The same signature check runs on GitHub after
every release (`verify-release.yml`), which also confirms PyPI serves the same files.
