---
html_theme.sidebar_secondary.remove: true
---

# mssql-ocp-migrate documentation

**Date**: Nov 16, 2026 **Version**: {{ version }}

**Download**: [PyPI package](https://pypi.org/project/mssql-ocp-migrate/)

**Useful links**:
[Installation](getting_started/install.md) |
[10-minute tour](getting_started/tour.md) |
[The 100 TB lift](user_guide/lift.md) |
[Troubleshooting](user_guide/troubleshooting.md) |
[Release notes](whatsnew/index.md)

**mssql-ocp-migrate** is an open source, MIT-licensed Python package for moving SQL Server databases
to SQL Server on Red Hat OpenShift, or to any other SQL Server, and proving the copy is correct.
It has no install-time dependencies, because the SQL Server driver is bundled inside it. It offers a
parallel, resumable row-level copy (`migrate`) for up to a few TB, and a backup-based physical copy
(`lift`) for tens to hundreds of TB with a cut-over measured in minutes.

::::::{grid} 1 2 2 2
:gutter: 4
:padding: 2 2 0 0
:class-container: sd-text-center

::::{grid-item-card} Getting started
:img-top: _static/index_getting_started.svg
:class-card: intro-card
:shadow: md

New to *mssql-ocp-migrate*? The getting started guides cover installation, a complete
ten-minute migration, and how to choose between the logical and physical engines.

:::{card-footer}
```{button-ref} getting_started/index
:ref-type: doc
:click-parent:
:color: secondary
:expand:

To the getting started guides
```
:::
::::

::::{grid-item-card} User guide
:img-top: _static/index_user_guide.svg
:class-card: intro-card
:shadow: md

The user guide explains every concept in depth: configuration, drivers and TLS, chunked copy,
the 100 TB lift, validation, performance, OpenShift deployment and troubleshooting.

:::{card-footer}
```{button-ref} user_guide/index
:ref-type: doc
:click-parent:
:color: secondary
:expand:

To the user guide
```
:::
::::

::::{grid-item-card} API reference
:img-top: _static/index_api.svg
:class-card: intro-card
:shadow: md

The reference describes every command, flag, configuration key and Python class. It's
generated from the code, so it always matches the installed version.

:::{card-footer}
```{button-ref} reference/index
:ref-type: doc
:click-parent:
:color: secondary
:expand:

To the reference guide
```
:::
::::

::::{grid-item-card} Developer guide
:img-top: _static/index_contribute.svg
:class-card: intro-card
:shadow: md

Want to extend the engine or add a database adapter? The developer guide covers the
architecture, the test layers and releasing to PyPI.

:::{card-footer}
```{button-ref} development/index
:ref-type: doc
:click-parent:
:color: secondary
:expand:

To the development guide
```
:::
::::

::::::

```{toctree}
:maxdepth: 3
:hidden:
:titlesonly:

getting_started/index
user_guide/index
reference/index
development/index
whatsnew/index
```
