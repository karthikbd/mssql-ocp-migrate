"""T-SQL DDL generation from catalog metadata.

Order used by the engine (fast loads, then constraints that check the loaded data):
  1. schemas, alias types, sequences
  2. tables: columns, defaults, identity, computed columns, PRIMARY KEY (clustered or not)
  3. -- data load --
  4. unique constraints, indexes, check constraints, foreign keys (WITH CHECK), identity reseed
  5. views, functions, procedures, synonyms, then triggers (last so they never fire during load)
"""
from __future__ import annotations

from typing import List

from .model import CheckConstraint, Column, ForeignKey, Index, Table


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qt(table: Table) -> str:
    return f"{q(table.schema)}.{q(table.name)}"


def type_sql(c: Column) -> str:
    if c.user_type and c.user_type_schema:
        return f"{q(c.user_type_schema)}.{q(c.user_type)}"
    t = c.type_name.lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if c.max_length == -1 else c.max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if c.max_length == -1 else c.max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({c.precision},{c.scale})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({c.scale})"
    if t == "float":
        return "float" if c.precision in (0, 53) else f"float({c.precision})"
    return t


def column_sql(c: Column) -> str:
    if c.is_computed:
        return f"{q(c.name)} AS {c.computed_definition}{' PERSISTED' if c.computed_persisted else ''}"
    parts = [q(c.name), type_sql(c)]
    if c.collation and c.type_name.lower() in ("char", "varchar", "nchar", "nvarchar", "text", "ntext") \
            and not c.user_type:
        parts.append(f"COLLATE {c.collation}")
    if c.is_identity:
        parts.append(f"IDENTITY({c.identity_seed or 1},{c.identity_increment or 1})")
    parts.append("NULL" if c.nullable else "NOT NULL")
    if c.default_definition:
        name = f"CONSTRAINT {q(c.default_name)} " if c.default_name else ""
        parts.append(f"{name}DEFAULT {c.default_definition}")
    return " ".join(parts)


def _cols(cols) -> str:
    return ", ".join(f"{q(c)}{' DESC' if desc else ''}" for c, desc in cols)


def create_table_sql(t: Table) -> str:
    lines = [column_sql(c) for c in t.columns]
    if t.primary_key:
        pk = t.primary_key
        kind = "CLUSTERED" if pk.clustered else "NONCLUSTERED"
        lines.append(f"CONSTRAINT {q(pk.name)} PRIMARY KEY {kind} ({_cols(pk.columns)})")
    body = ",\n    ".join(lines)
    return f"CREATE TABLE {qt(t)} (\n    {body}\n)"


def index_sql(t: Table, ix: Index) -> str:
    if ix.type_desc == "CLUSTERED COLUMNSTORE":
        return f"CREATE CLUSTERED COLUMNSTORE INDEX {q(ix.name)} ON {qt(t)}"
    if ix.type_desc == "NONCLUSTERED COLUMNSTORE":
        cols = ", ".join(q(c) for c, _ in ix.columns + [(i, False) for i in ix.included])
        return f"CREATE NONCLUSTERED COLUMNSTORE INDEX {q(ix.name)} ON {qt(t)} ({cols})"
    if ix.is_unique_constraint:
        kind = "CLUSTERED" if ix.clustered else "NONCLUSTERED"
        return f"ALTER TABLE {qt(t)} ADD CONSTRAINT {q(ix.name)} UNIQUE {kind} ({_cols(ix.columns)})"
    sql = (f"CREATE {'UNIQUE ' if ix.unique else ''}{'CLUSTERED' if ix.clustered else 'NONCLUSTERED'} "
           f"INDEX {q(ix.name)} ON {qt(t)} ({_cols(ix.columns)})")
    if ix.included:
        sql += " INCLUDE (" + ", ".join(q(c) for c in ix.included) + ")"
    if ix.filter_definition:
        sql += f" WHERE {ix.filter_definition}"
    return sql


def check_sql(t: Table, ck: CheckConstraint) -> List[str]:
    out = [f"ALTER TABLE {qt(t)} WITH CHECK ADD CONSTRAINT {q(ck.name)} CHECK {ck.definition}"]
    if ck.disabled:
        out = [f"ALTER TABLE {qt(t)} WITH NOCHECK ADD CONSTRAINT {q(ck.name)} CHECK {ck.definition}",
               f"ALTER TABLE {qt(t)} NOCHECK CONSTRAINT {q(ck.name)}"]
    return out


def fk_sql(t: Table, fk: ForeignKey) -> List[str]:
    check = "NOCHECK" if (fk.not_trusted or fk.disabled) else "CHECK"
    sql = (f"ALTER TABLE {qt(t)} WITH {check} ADD CONSTRAINT {q(fk.name)} FOREIGN KEY "
           f"({', '.join(q(c) for c in fk.columns)}) REFERENCES {q(fk.ref_schema)}.{q(fk.ref_table)} "
           f"({', '.join(q(c) for c in fk.ref_columns)})")
    if fk.on_delete != "NO_ACTION":
        sql += " ON DELETE " + fk.on_delete.replace("_", " ")
    if fk.on_update != "NO_ACTION":
        sql += " ON UPDATE " + fk.on_update.replace("_", " ")
    out = [sql]
    if fk.disabled:
        out.append(f"ALTER TABLE {qt(t)} NOCHECK CONSTRAINT {q(fk.name)}")
    return out


def post_load_sql(t: Table) -> List[str]:
    """Indexes and constraints applied after the data is in."""
    out: List[str] = []
    # clustered first so nonclustered indexes are not rebuilt
    for ix in sorted(t.indexes, key=lambda i: not i.clustered):
        out.append(index_sql(t, ix))
    for ck in t.checks:
        out.extend(check_sql(t, ck))
    return out


def fk_sql_all(t: Table) -> List[str]:
    out: List[str] = []
    for fk in t.foreign_keys:
        out.extend(fk_sql(t, fk))
    return out
