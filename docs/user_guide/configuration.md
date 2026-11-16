# Configuration

Everything the tool does is driven by one configuration file. TOML is the default; JSON works too,
and is required on Python 3.10, which has no built-in TOML parser.

```console
$ mssql-migrate init-config > migrate.toml     # a commented starting point
```

## Structure

```toml
report_dir = "migration_report"     # top-level keys first

[source]        # the existing SQL Server
[target]        # the SQL Server on OpenShift
[migration]     # logical copy: what, how many workers, chunk size, resume policy, sharding
[validation]    # how thoroughly to compare
[lift]          # physical copy of whole databases
```

`[source]` and `[target]` are required. Every other section has defaults. Unknown keys are
rejected with an error, so a typo such as `wrkers = 8` never silently falls back to a default.
The full list of keys, types and defaults is in {doc}`/reference/config`.

## Secrets

Never put passwords in the file. Three options, checked in this order:

1. `password_file`: read from a file. In OpenShift, mount a Secret as a file:
   ```toml
   [target]
   password_file = "/secrets/target-password"
   ```
2. `password_env`: read from an environment variable:
   ```toml
   [source]
   password_env = "SRC_DB_PASSWORD"
   ```
3. `password`: clear text, for throw-away labs only.

Reports redact passwords (`"***"`), and the JSON report never contains secrets.

## Choosing tables

`include` and `exclude` are case-insensitive `schema.table` glob patterns. A table is migrated if
it matches any `include` pattern and no `exclude` pattern.

```toml
[migration]
include = ["dbo.*", "sales.Order*"]
exclude = ["dbo.tmp_*", "*.zz_*"]
```

The command line can override them for one run:

```console
$ mssql-migrate migrate -c migrate.toml --include "sales.*" --exclude "sales.Archive*"
```

FileTables are always skipped.

## Command-line overrides

| Flag | Overrides |
|---|---|
| `-w/--workers N` | `migration.workers` |
| `--include ...` | `migration.include` (replaces it) |
| `--exclude ...` | appended to `migration.exclude` |
| `--resume` | `migration.if_target_not_empty = "resume"` |
| `--truncate` | `migration.if_target_not_empty = "truncate"` |
| `--mode ...` | `validation.mode` |
| `--report-dir` | `report_dir` |
| `--shard I/N` | `migration.shard_index`, `migration.shard_count` |
| `-d/--databases` (lift) | `lift.databases` |

## Using the configuration from Python

```python
from mssql_ocp_migrate import Config, Migrator

cfg = Config.load("migrate.toml")
cfg.migration.workers = 16          # plain dataclasses: change anything
cfg.check()                         # re-validate after changing values

run = Migrator(cfg).migrate()
```

You can also build a configuration from a dictionary, for example one read from Vault or a
pipeline variable:

```python
cfg = Config.from_dict({
    "source": {"host": "sql01", "database": "SalesDB", "user": "r", "password_env": "SRC"},
    "target": {"host": "mssql.sqlserver.svc", "database": "SalesDB", "user": "w", "password_env": "TGT"},
    "migration": {"workers": 8},
})
```

## Validation rules

`Config.check()` runs on load and rejects:

- a missing `database` on either side;
- unknown values for `driver`, `encrypt`, `schema_mode`, `if_target_not_empty`, `executor`,
  `validation.mode`, `lift.transport` or `lift.cutover_source`;
- `workers`, `chunk_rows` or `batch_rows` below 1, or `sample_full_pct` outside 0–100;
- `lift.stripes` outside 1–64 (SQL Server's limit);
- a `shard_index` outside `0 .. shard_count-1`.
