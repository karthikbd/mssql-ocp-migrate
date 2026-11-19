# Releasing to PyPI

## Automated (recommended)

`.github/workflows/python-publish.yml` runs when a GitHub release is published (or by hand from the Actions tab). It runs the
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

When the publish run succeeds, `verify-release.yml` downloads the release assets on a fresh runner,
verifies each Sigstore signature against `python-publish.yml`, the tag and the `release` trigger,
and checks that PyPI serves byte-identical files. A red run there means the release must not be
trusted. Re-run it for any tag from the Actions tab (*Run workflow*, tag `v{{ version }}`).

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

- The *Security assurance case* ({doc}`security-assurance`) has been re-read against the changes since
  the last release and updated (new inputs, files written, connections); record the release in its
  *Review at release* section.
- `vulnerability-audit.yml` is green and no CodeQL alert of medium severity or higher is open
  ({doc}`dependencies`).
- The `reproducible-build` job is green: two builds of the commit give identical wheel and sdist.
- `pytest -q` passes, and `MSSQL_E2E=1 pytest tests/e2e` passes against real servers.
- `CHANGELOG.md` and `docs/whatsnew/index.md` describe the release.
- The wheel's metadata has **no** `Requires-Dist` lines except the `test` and `docs` extras.
- `pip install --no-index --no-deps dist/*.whl` works in a clean virtual environment.
- `python -m sphinx -W -b html docs docs/_build/html` builds with no warnings.

## Hosting this documentation

`.readthedocs.yaml` at the repository root builds these docs on Read the Docs: connect the
repository there and every tag gets its own versioned docs, like pandas.

## Verifying a release yourself

Every release can be checked by anyone:

1. **Signature and origin:** each asset on the GitHub release has a `.sigstore.json` bundle. Verify it
   was produced by this repository's `python-publish.yml` for that tag, as shown in
   {doc}`/getting_started/install`.
2. **Same bytes as PyPI:** compare the SHA-256 of the files on PyPI with the GitHub assets (the
   `verify-release.yml` workflow does this after every release).
3. **Rebuild it:** check out the tag, then
   `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) python -m build && python ci/reproducible_sdist.py dist/*.tar.gz`
   with the tools in `ci/requirements-build.txt`; the results are byte-identical to the release.
