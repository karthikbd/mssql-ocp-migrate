# Contributing to mssql-ocp-migrate

Thanks for helping. Bug reports, documentation fixes and support for more SQL Server features are all
welcome.

## Reporting bugs and asking questions

- **Bugs and feature requests:** open a [GitHub issue](https://github.com/karthikbd/mssql-ocp-migrate/issues).
  Include the version (`mssql-migrate --version`), Python version, the source and target SQL Server
  versions and editions, the driver (`pytds`, `pyodbc` or `pymssql`), the command you ran, and the
  relevant part of the report or of `mssql-migrate assess`. Remove host names, credentials and data you
  can't share.
- **Security vulnerabilities:** do **not** open a public issue. Report them privately as described in
  [SECURITY.md](SECURITY.md).

The maintainer aims to respond to new issues within 7 days.

## Making a change

1. Open an issue first for anything larger than a small fix, so the approach can be agreed.
2. Fork the repository and create a branch from `main`. `main` only accepts pull requests whose
   required checks pass; nobody, including the maintainer, pushes to it directly.
3. Set up a development environment:

   ```console
   $ python -m venv .venv && . .venv/bin/activate
   $ pip install --require-hashes -r ci/requirements-test.txt
   $ pip install --no-deps -e .
   ```

4. Make the change, **with tests** (see below), and update the documentation in `docs/` and
   `CHANGELOG.md` when behaviour changes. Sign off every commit (`git commit -s`, see
   *Developer Certificate of Origin* below).
5. Run the checks CI runs:

   ```console
   $ ruff check src tests          # lint: must be clean
   $ mypy src                      # types: must be clean
   $ pytest -q                     # unit, SQLite engine, wire-level and simulated-server tests
   $ python -m sphinx -W -b html docs docs/_build/html   # docs: warnings are errors
   ```

   With Docker you can also run the end-to-end suite against real SQL Server containers
   (see `docs/development/testing.md`).

6. Open a pull request against `main`. CI runs the tests on Python 3.10–3.13, the end-to-end suite
   against SQL Server 2019, 2022 and 2025 with all three drivers, CodeQL, a vulnerability audit and
   ClusterFuzzLite fuzzing. All of them must pass.

## Code review

Every change is reviewed before it is merged:

- the reviewer checks that the change does what the pull request says, has tests that would fail
  without it, keeps the documentation and changelog current, introduces no new runtime dependency or
  network access, and does not weaken the security properties in the documentation's
  *Security assurance case* (new inputs, files written or connections are added to it);
- all required checks must pass: tests, lint, types, coverage, docs, reproducible build, CodeQL,
  dependency review and the DCO check;
- review comments are resolved before merging. While the project has one maintainer, the maintainer's
  own pull requests are checked against the same list; a second reviewer is a goal (see
  [ROADMAP.md](ROADMAP.md)).

Good first contributions are labelled
[`good first issue`](https://github.com/karthikbd/mssql-ocp-migrate/labels/good%20first%20issue).

## Coding standards

- Python code follows [PEP 8](https://peps.python.org/pep-0008/) as enforced by `ruff` with the rule sets
  in `pyproject.toml` (pycodestyle, pyflakes, bugbear, simplify, Bandit security rules, comprehensions,
  ruff-specific rules), and is fully typed: `mypy` runs in **strict** mode.
- Both run in CI on every push and pull request and must be clean.

## Rules for code

- **No runtime dependencies.** The package must install and run with the Python standard library and
  its bundled driver only. `pyodbc` and `pymssql` may be used *only if already installed* (imported
  inside a function). `tests/test_no_downloads.py` enforces this.
- **No network access except to the configured servers.** No HTTP clients, no `subprocess`, no
  telemetry.
- **Tests are required.** New functionality needs tests that would fail without it; bug fixes need a
  regression test. Engine behaviour should be covered on the SQLite adapter (`tests/sqlite_adapter.py`),
  SQL Server behaviour in `tests/e2e` where practical.
- **Data must never be lost or silently changed.** Anything that can affect what reaches the target
  needs validation coverage.
- **Inputs are untrusted.** Code that parses configuration or handles row values should have a fuzz
  target in `tests/fuzz` (Atheris).
- **Style:** `ruff` and `mypy` clean; type hints on public functions.
- **CI tooling is hash-locked.** To change a tool version, edit `ci/requirements-*.in` and regenerate
  the `.txt` file with `uv pip compile --universal --generate-hashes`. GitHub Actions are pinned by
  commit SHA.

## Developer Certificate of Origin (DCO)

By contributing you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/):
that you wrote the change or otherwise have the right to submit it under the project's license. Show this
by adding a `Signed-off-by` line with your real name and e-mail to every commit (`git commit -s`). The
`DCO` check fails a pull request that has an unsigned commit; fix it with
`git rebase --signoff main` and a force-push to your branch.

## Licensing of contributions

mssql-ocp-migrate is MIT-licensed. By submitting a pull request you agree that your contribution is
licensed under the same [MIT License](LICENSE), and you confirm you have the right to submit it. Code
copied from other projects must have a compatible license and be recorded in `THIRD_PARTY_NOTICES.md`.

## Releases

Releases follow [semantic versioning](https://semver.org/) and are described in `CHANGELOG.md` and
`docs/whatsnew/index.md`. See `docs/development/releasing.md` for the release process.
