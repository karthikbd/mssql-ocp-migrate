# Python API

The public API is small: load a {class}`~mssql_ocp_migrate.config.Config`, then run a
{class}`~mssql_ocp_migrate.engine.Migrator` (logical) or a {class}`~mssql_ocp_migrate.physical.Lift`
(physical). Everything the command line does goes through these classes.

```python
from mssql_ocp_migrate import Config, Migrator
from mssql_ocp_migrate.physical import Lift
from mssql_ocp_migrate.validate import Validator
from mssql_ocp_migrate.estimate import estimate
from mssql_ocp_migrate import report
```

## Configuration

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.config.Config
   :members: load, from_dict, check, to_dict, selects, resolved_executor

.. autoclass:: mssql_ocp_migrate.config.ConnectionConfig
   :members: resolve_password, describe

.. autoclass:: mssql_ocp_migrate.config.MigrationConfig
   :no-members:

.. autoclass:: mssql_ocp_migrate.config.ValidationConfig
   :no-members:

.. autoclass:: mssql_ocp_migrate.config.LiftConfig
   :no-members:

.. autoexception:: mssql_ocp_migrate.config.ConfigError
```

The dataclass fields are documented in {doc}`config`.

## Logical migration

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.engine.Migrator
   :members: migrate, finalize, source_tables, schema_script

.. autoclass:: mssql_ocp_migrate.engine.RunResult
   :members: rows, ok

.. autoclass:: mssql_ocp_migrate.engine.TableRun

.. autoexception:: mssql_ocp_migrate.engine.MigrationError

.. autofunction:: mssql_ocp_migrate.engine.in_shard

.. autofunction:: mssql_ocp_migrate.engine.shard_path
```

## Physical migration

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.physical.Lift
   :members: plan, run, seed, restore_full, catch_up_once, follow, cutover, post_recovery, copy_logins, transfer_tde, verify, ag_script

.. autoclass:: mssql_ocp_migrate.physical.DbPlan

.. autofunction:: mssql_ocp_migrate.physical.format_plan

.. autofunction:: mssql_ocp_migrate.physical.estimate_timeline

.. autoexception:: mssql_ocp_migrate.physical.LiftError

.. autoexception:: mssql_ocp_migrate.physical.ChainError
```

### T-SQL builders

Pure functions, useful for reviewing exactly what will run:

```{eval-rst}
.. autofunction:: mssql_ocp_migrate.physical.backup_devices
.. autofunction:: mssql_ocp_migrate.physical.backup_full_sql
.. autofunction:: mssql_ocp_migrate.physical.backup_log_sql
.. autofunction:: mssql_ocp_migrate.physical.restore_full_sql
.. autofunction:: mssql_ocp_migrate.physical.restore_log_sql
.. autofunction:: mssql_ocp_migrate.physical.move_plan
.. autofunction:: mssql_ocp_migrate.physical.tuning
.. autofunction:: mssql_ocp_migrate.physical.map_path
.. autofunction:: mssql_ocp_migrate.physical.next_logs
.. autofunction:: mssql_ocp_migrate.physical.ag_seed_script
```

```python
>>> from mssql_ocp_migrate.config import LiftConfig
>>> from mssql_ocp_migrate.physical import backup_devices, backup_full_sql
>>> lf = LiftConfig(stripes=4)
>>> print(backup_full_sql("SalesDB", backup_devices("/mnt/mig", "SalesDB_full", 4), lf))
BACKUP DATABASE [SalesDB] TO DISK = N'/mnt/mig/SalesDB_full_01of04.bak', DISK = N'/mnt/mig/SalesDB_full_02of04.bak', DISK = N'/mnt/mig/SalesDB_full_03of04.bak', DISK = N'/mnt/mig/SalesDB_full_04of04.bak' WITH COPY_ONLY, COMPRESSION, FORMAT, INIT, CHECKSUM, STATS = 1, MAXTRANSFERSIZE = 4194304, BUFFERCOUNT = 16
```

## Validation

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.validate.Validator
   :members: run

.. autoclass:: mssql_ocp_migrate.validate.ValidationResult
   :members: ok, summary

.. autoclass:: mssql_ocp_migrate.validate.TableCheck

.. autoclass:: mssql_ocp_migrate.validate.ChunkCheck

.. autoclass:: mssql_ocp_migrate.rowhash.RowHasher
   :members: digest, key
```

## Estimation and reports

```{eval-rst}
.. autofunction:: mssql_ocp_migrate.estimate.estimate

.. autoclass:: mssql_ocp_migrate.estimate.Estimate

.. autofunction:: mssql_ocp_migrate.estimate.format_estimate

.. autofunction:: mssql_ocp_migrate.report.write
```

## Connections

```{eval-rst}
.. autofunction:: mssql_ocp_migrate.drivers.connect

.. autofunction:: mssql_ocp_migrate.drivers.choose_driver

.. autoclass:: mssql_ocp_migrate.drivers.Connection
   :members: execute, query, scalar, stream, commit, rollback, close
```

## Extending: the `Database` adapter

{class}`~mssql_ocp_migrate.engine.Migrator` talks to databases only through this interface.
SQL Server implements it in {mod}`mssql_ocp_migrate.sqlserver`, and the test suite implements it on
SQLite (`tests/sqlite_adapter.py`) to run the whole engine without a server. Pass your own
implementation with `Migrator(cfg, factory=lambda side, autocommit: MyDatabase(...))`.

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.adapter.Database
   :members:

.. autoclass:: mssql_ocp_migrate.adapter.ModuleDef
```

## Data model

```{eval-rst}
.. autoclass:: mssql_ocp_migrate.model.Table
   :members: fqn, key_columns, insert_columns, compare_columns, has_identity

.. autoclass:: mssql_ocp_migrate.model.Column
   :members: insertable, comparable, is_clr

.. autoclass:: mssql_ocp_migrate.model.Index

.. autoclass:: mssql_ocp_migrate.model.ForeignKey

.. autoclass:: mssql_ocp_migrate.model.Chunk
```
