# Command-line reference

The `mssql-migrate` command is installed with the package. You can also run it as
`python -m mssql_ocp_migrate`.

## Commands at a glance

| Command | Purpose |
|---|---|
| [`init-config`](#cli-init-config) | print a commented example configuration |
| [`check`](#cli-check) | test both connections and list what would be migrated, with warnings |
| [`estimate`](#cli-estimate) | measure throughput on a sample and project the logical copy's duration |
| [`schema`](#cli-schema) | write the DDL script without running it |
| [`migrate`](#cli-migrate) | logical copy: schema → data → constraints → objects → validation |
| [`finalize`](#cli-finalize) | after a sharded copy: constraints, objects, validation |
| [`lift`](#cli-lift) | physical copy: `plan`, `run`, `seed`, `resume`, `catchup`, `follow`, `cutover`, `verify`, `status`, `ag-script` |
| [`validate`](#cli-validate) | compare source and target, any time |

```{include} _generated/cli.md
```
