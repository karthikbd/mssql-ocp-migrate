# Development

```{toctree}
:maxdepth: 2

architecture
testing
releasing
```

```console
$ git clone <repo> && cd mssql-ocp-migrate
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e ".[test,docs]"
$ pytest -q                                   # unit + engine + wire-level tests, no server needed
$ python -m sphinx -W -b html docs docs/_build/html   # this documentation, warnings are errors
```
