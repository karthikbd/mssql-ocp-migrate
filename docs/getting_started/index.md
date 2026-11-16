# Getting started

```{toctree}
:maxdepth: 1

install
tour
choosing
```

## In one minute

```console
$ pip install mssql-ocp-migrate
$ mssql-migrate init-config > migrate.toml        # edit hosts, databases, users
$ mssql-migrate check -c migrate.toml             # can both servers be reached? what will move?
$ mssql-migrate estimate -c migrate.toml          # how long will a logical copy take?
$ mssql-migrate migrate -c migrate.toml           # copy + validate, writes migration_report/report.html
```

For very large databases, use the physical path instead:

```console
$ mssql-migrate lift plan -c migrate.toml         # sizes, blockers, timeline from your own backup history
$ mssql-migrate lift run  -c migrate.toml --follow-hours 168
$ mssql-migrate lift cutover -c migrate.toml --confirm
```

::::{grid} 1 3 3 3
:gutter: 2

:::{grid-item-card} Installation
:link: install
:link-type: doc
PyPI, offline wheels, air-gapped OpenShift images, optional drivers.
:::

:::{grid-item-card} 10 minutes to mssql-ocp-migrate
:link: tour
:link-type: doc
A complete migration, end to end, with the report.
:::

:::{grid-item-card} Choosing an engine
:link: choosing
:link-type: doc
`migrate` or `lift`? A decision table by size, downtime and constraints.
:::
::::
