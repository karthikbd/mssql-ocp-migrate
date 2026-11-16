# Releasing to PyPI

## Automated (recommended)

`.github/workflows/python-publish.yml` runs on a published GitHub release or a `v*` tag. It runs the
tests, builds the sdist and wheel, checks the wheel declares no runtime dependencies, and publishes
with **PyPI Trusted Publishing** (OIDC) from the GitHub environment `release`, so no API token is
stored anywhere.

One-time setup on pypi.org → *Your account* → *Publishing* → *Add a new pending publisher*:

| Field | Value |
|---|---|
| PyPI project name | `mssql-ocp-migrate` |
| Owner | `karthikbd` |
| Repository name | `mssql-ocp-migrate` |
| Workflow name | `python-publish.yml` |
| Environment name | `release` |

Each release:

```console
$ # bump version in pyproject.toml and src/mssql_ocp_migrate/__init__.py, update CHANGELOG.md and docs/whatsnew
$ git push origin main
$ gh release create v{{ version }} --title v{{ version }} --notes-file CHANGELOG.md
```

`tests.yml` (Python 3.10–3.13 plus a warnings-as-errors docs build), `codeql.yml` and
`scorecard.yml` run on every push to `main`. Every action is pinned to a commit SHA, and Dependabot
keeps the pins current.

## Manual

```console
$ pip install build twine
$ python -m build                    # dist/*.whl and dist/*.tar.gz
$ twine check dist/*
$ twine upload dist/*                # asks for a PyPI API token
```

## Checks before tagging

- `pytest -q` passes, and `MSSQL_E2E=1 pytest tests/e2e` passes against real servers.
- `CHANGELOG.md` and `docs/whatsnew/index.md` describe the release.
- The wheel's metadata has **no** `Requires-Dist` lines except the `test` and `docs` extras.
- `pip install --no-index --no-deps dist/*.whl` works in a clean virtual environment.
- `python -m sphinx -W -b html docs docs/_build/html` builds with no warnings.

## Hosting this documentation

`.readthedocs.yaml` at the repository root builds these docs on Read the Docs: connect the
repository there and every tag gets its own versioned docs, like pandas.
