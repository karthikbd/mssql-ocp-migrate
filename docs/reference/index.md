# API reference

This section documents every public interface. The command-line and configuration pages are
**generated from the code** on each build, and the build fails if a configuration key has no
description, so they always match the installed version.

```{toctree}
:maxdepth: 2

cli
config
api
```

| Page | Contents |
|---|---|
| {doc}`cli` | every `mssql-migrate` command and flag |
| {doc}`config` | every configuration key with its type, default and meaning |
| {doc}`api` | Python classes and functions: `Config`, `Migrator`, `Lift`, `Validator`, `estimate`, `report`, and the `Database` adapter interface |
