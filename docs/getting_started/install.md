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
