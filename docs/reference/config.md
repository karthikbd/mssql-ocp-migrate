# Configuration reference

Every key accepted in the configuration file, generated from the
{class}`~mssql_ocp_migrate.config.Config` dataclasses. TOML example:

```toml
report_dir = "migration_report"
[source]
host = "sql01"
database = "SalesDB"
[target]
host = "mssql.sqlserver.svc"
database = "SalesDB"
[migration]
workers = 8
```

The same structure works in JSON (`{"source": {...}, "target": {...}, "migration": {...}}`).
Unknown keys are rejected.

```{include} _generated/config_table.md
```
