"""Fuzz key-range chunking and identifier quoting.

Every row must fall in exactly one chunk for any composite key and boundary set, and a quoted
identifier must read back as the same name whatever characters it holds."""
import sqlite3
import sys

import atheris

with atheris.instrument_imports():
    from mssql_ocp_migrate import chunking
    from mssql_ocp_migrate.ddl import q


def _unquote(s: str) -> str:
    assert s.startswith("[") and s.endswith("]")
    body = s[1:-1]
    out, i = [], 0
    while i < len(body):
        if body[i] == "]":
            assert body[i + 1:i + 2] == "]", s      # a lone ] would end the identifier early
            i += 1
        out.append(body[i])
        i += 1
    return "".join(out)


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    name = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 20))
    assert _unquote(q(name)) == name

    ncols = fdp.ConsumeIntInRange(1, 3)
    cols = [f"k{i}" for i in range(ncols)]
    rows = sorted({tuple(fdp.ConsumeIntInRange(-3, 3) for _ in cols) for _ in range(fdp.ConsumeIntInRange(1, 25))})
    bounds = sorted({rows[fdp.ConsumeIntInRange(0, len(rows) - 1)] for _ in range(fdp.ConsumeIntInRange(0, 5))})
    chunks = chunking.build_chunks("t", bounds)
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE t ({', '.join(cols)})")
    db.executemany(f"INSERT INTO t VALUES ({', '.join('?' * ncols)})", rows)
    seen = []
    for c in chunks:
        where, params = chunking.predicate(c, cols, lambda n: '"' + n.replace('"', '""') + '"')
        seen += db.execute(f"SELECT {', '.join(cols)} FROM t WHERE {where}", params).fetchall()
    assert sorted(seen) == rows, (rows, bounds)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
