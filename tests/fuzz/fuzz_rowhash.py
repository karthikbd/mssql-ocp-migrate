"""Fuzz row fingerprints: rows whose normalized values differ must never encode (or key) the same."""
import sys

import atheris

with atheris.instrument_imports():
    from mssql_ocp_migrate.model import Column
    from mssql_ocp_migrate.rowhash import RowHasher

TYPES = ["nvarchar", "varbinary", "int", "bit", "float", "decimal", "uniqueidentifier", "datetime2", "sql_variant"]


def _value(fdp, t):
    if fdp.ConsumeBool():
        return None
    if t in ("int", "bit"):
        return fdp.ConsumeIntInRange(-2**40, 2**40)
    if t == "float":
        return fdp.ConsumeRegularFloat()
    if t == "decimal":
        return str(fdp.ConsumeIntInRange(-10**6, 10**6)) + "." + str(fdp.ConsumeIntInRange(0, 999))
    if t == "varbinary":
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 12))
    if t == "uniqueidentifier":
        return "%032x" % fdp.ConsumeIntInRange(0, 2**128 - 1)
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 12))


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    types = [TYPES[fdp.ConsumeIntInRange(0, len(TYPES) - 1)] for _ in range(fdp.ConsumeIntInRange(1, 4))]
    cols = [Column(f"c{i}", t) for i, t in enumerate(types)]
    nkeys = fdp.ConsumeIntInRange(1, len(cols))
    h = RowHasher(cols, [c.name for c in cols[:nkeys]])
    r1 = tuple(_value(fdp, t) for t in types)
    r2 = tuple(_value(fdp, t) for t in types)
    n1 = [f(v) for f, v in zip(h.norms, r1)]
    n2 = [f(v) for f, v in zip(h.norms, r2)]
    assert (h.encode(r1) == h.encode(r2)) == (n1 == n2), (r1, r2)
    assert (h.key(r1) == h.key(r2)) == (n1[:nkeys] == n2[:nkeys]), (r1, r2)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
