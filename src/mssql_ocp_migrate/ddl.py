"""T-SQL DDL generation from catalog metadata.

Order used by the engine (fast loads, then constraints that check the loaded data):
  1. schemas, alias types, sequences
  2. tables: columns, defaults, identity, computed columns, PRIMARY KEY (clustered or not)
  3. -- data load --
  4. unique constraints, indexes (with their storage options), user statistics, check
     constraints, foreign keys (WITH CHECK), identity reseed
  5. views, functions, procedures, synonyms, then triggers (last so they never fire during load)
  6. extended properties, users and roles, role memberships, schema owners, GRANT/DENY,
     database settings (compatibility level, snapshot isolation)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .model import (CdcTable, Certificate, CheckConstraint, Column, DatabaseSettings, ExtendedProperty,
                    ForeignKey, Index, Permission, Principal, SecurityPolicy, Signature, Statistic, Table)


@dataclass
class BuildOptions:
    """How indexes are created on the target.

    ``copy_options`` reproduces the source's storage options (fill factor, padding,
    IGNORE_DUP_KEY, lock options, compression). The others only change how fast the build runs."""
    copy_options: bool = True
    sort_in_tempdb: bool = False
    maxdop: int = 0
    online: bool = False


def lit(value: str) -> str:
    """A T-SQL Unicode string literal."""
    return "N'" + value.replace("'", "''") + "'"


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qt(table: Table) -> str:
    return f"{q(table.schema)}.{q(table.name)}"


def type_sql(c: Column) -> str:
    if c.user_type and c.user_type_schema:
        return f"{q(c.user_type_schema)}.{q(c.user_type)}"
    t = c.type_name.lower()
    if t == "xml" and c.xml_schema:
        sch, _, name = c.xml_schema.partition(".")
        return f"xml({'DOCUMENT' if c.xml_document else 'CONTENT'} {q(sch)}.{q(name)})"
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
    if t == "vector":
        base = (c.vector_base_type or "float32").lower()
        return f"vector({c.vector_dimensions}{', float16' if base == 'float16' else ''})"
    return t


def column_sql(c: Column, masking: bool = True, named_default: bool = True) -> str:
    if c.is_computed:
        return f"{q(c.name)} AS {c.computed_definition}{' PERSISTED' if c.computed_persisted else ''}"
    if c.is_column_set:
        return f"{q(c.name)} xml COLUMN_SET FOR ALL_SPARSE_COLUMNS"
    parts = [q(c.name), type_sql(c)]
    if c.collation and c.type_name.lower() in ("char", "varchar", "nchar", "nvarchar", "text", "ntext") \
            and not c.user_type:
        parts.append(f"COLLATE {c.collation}")
    if c.is_sparse:
        parts.append("SPARSE")
    if masking and c.masking_function:
        parts.append(f"MASKED WITH (FUNCTION = '{c.masking_function.replace(chr(39), chr(39) * 2)}')")
    if c.is_identity:
        parts.append(f"IDENTITY({c.identity_seed or 1},{c.identity_increment or 1})"
                     + (" NOT FOR REPLICATION" if c.identity_not_for_replication else ""))
    if c.is_rowguidcol:
        parts.append("ROWGUIDCOL")
    if c.encrypted:
        parts.append(f"ENCRYPTED WITH (COLUMN_ENCRYPTION_KEY = {q(c.encryption_key or '')}, "
                     f"ENCRYPTION_TYPE = {c.encryption_type}, "
                     f"ALGORITHM = '{c.encryption_algorithm or 'AEAD_AES_256_CBC_HMAC_SHA_256'}')")
    parts.append("NULL" if c.nullable else "NOT NULL")
    if c.default_definition:
        name = f"CONSTRAINT {q(c.default_name)} " if c.default_name and named_default else ""
        parts.append(f"{name}DEFAULT {c.default_definition}")
    return " ".join(parts)


def _cols(cols: Sequence[Tuple[str, bool]]) -> str:
    return ", ".join(f"{q(c)}{' DESC' if desc else ''}" for c, desc in cols)


def storage_options(ix: Index) -> List[str]:
    """The source index's storage/behaviour options that differ from SQL Server's defaults."""
    if "COLUMNSTORE" in ix.type_desc:
        return ["DATA_COMPRESSION = COLUMNSTORE_ARCHIVE"] if ix.data_compression == "COLUMNSTORE_ARCHIVE" else []
    out = []
    if ix.is_padded:
        out.append("PAD_INDEX = ON")
    if ix.fill_factor not in (0, 100):
        out.append(f"FILLFACTOR = {int(ix.fill_factor)}")
    if ix.ignore_dup_key:
        out.append("IGNORE_DUP_KEY = ON")
    if not ix.allow_row_locks:
        out.append("ALLOW_ROW_LOCKS = OFF")
    if not ix.allow_page_locks:
        out.append("ALLOW_PAGE_LOCKS = OFF")
    out += compression_options(ix.data_compression, ix.partition_compression)
    return out


def compression_options(compression: str, per_partition: Optional[Dict[int, str]]) -> List[str]:
    """DATA_COMPRESSION, per partition when the partitions differ."""
    if per_partition:
        groups: Dict[str, List[str]] = {}
        for pno, comp in sorted(per_partition.items()):
            groups.setdefault(comp, []).append(str(pno))
        return [f"DATA_COMPRESSION = {comp} ON PARTITIONS ({', '.join(nums)})"
                for comp, nums in groups.items() if comp in ("NONE", "ROW", "PAGE")]
    return [f"DATA_COMPRESSION = {compression}"] if compression in ("ROW", "PAGE") else []


def _on(scheme: Optional[str], column: Optional[str], partitions: bool) -> str:
    return f" ON {q(scheme)}({q(column)})" if partitions and scheme and column else ""


def _with(opts: List[str]) -> str:
    return f" WITH ({', '.join(opts)})" if opts else ""


def _has_clustered(t: Table) -> bool:
    return bool((t.primary_key and t.primary_key.clustered) or any(i.clustered for i in t.indexes))


def _memory_index(ix: Index) -> str:
    """An inline index of a memory-optimized table: HASH with a bucket count, a range index, or
    a clustered columnstore."""
    if ix.is_hash:
        body = f"NONCLUSTERED HASH ({_cols(ix.columns)}) WITH (BUCKET_COUNT = {ix.bucket_count or 1024})"
    elif "COLUMNSTORE" in ix.type_desc:
        body = "CLUSTERED COLUMNSTORE"
    else:
        body = f"NONCLUSTERED ({_cols(ix.columns)})"
    if ix.is_primary_key:
        return f"CONSTRAINT {q(ix.name)} PRIMARY KEY {body}"
    if ix.is_unique_constraint:
        return f"CONSTRAINT {q(ix.name)} UNIQUE {body}"
    return f"INDEX {q(ix.name)} {'UNIQUE ' if ix.unique and not ix.is_hash else ''}{body}"


def create_table_sql(t: Table, copy_options: bool = True, masking: bool = True, memory: bool = True,
                     partitions: bool = True) -> str:
    """CREATE TABLE with the primary key. A memory-optimized table (``memory``) gets every index
    inline, because they live with the rows; any other table gets its other indexes after the load."""
    lines = [column_sql(c, masking) for c in t.columns]
    in_memory = memory and t.memory_optimized
    if in_memory:
        lines += [_memory_index(ix) for ix in ([t.primary_key] if t.primary_key else []) + t.indexes]
    elif t.primary_key:
        pk = t.primary_key
        kind = "CLUSTERED" if pk.clustered else "NONCLUSTERED"
        lines.append(f"CONSTRAINT {q(pk.name)} PRIMARY KEY {kind} ({_cols(pk.columns)})"
                     + (_with(storage_options(pk)) if copy_options else "")
                     + _on(pk.partition_scheme, pk.partition_column, partitions))
    body = ",\n    ".join(lines)
    sql = f"CREATE TABLE {qt(t)} (\n    {body}\n)"
    if in_memory:
        return sql + f" WITH (MEMORY_OPTIMIZED = ON, DURABILITY = {t.durability})"
    if not _has_clustered(t):
        sql += _on(t.partition_scheme, t.partition_column, partitions)
        if copy_options:
            opts = compression_options(t.heap_compression, t.heap_partition_compression)
            sql += f" WITH ({', '.join(opts)})" if opts else ""
    return sql


def index_sql(t: Table, ix: Index, build: Optional[BuildOptions] = None, partitions: bool = True) -> str:
    build = build or BuildOptions()
    on = _on(ix.partition_scheme, ix.partition_column, partitions)
    opts = storage_options(ix) if build.copy_options else []
    if "COLUMNSTORE" in ix.type_desc:
        if build.maxdop:
            opts.append(f"MAXDOP = {build.maxdop}")
        if ix.type_desc == "CLUSTERED COLUMNSTORE":
            return f"CREATE CLUSTERED COLUMNSTORE INDEX {q(ix.name)} ON {qt(t)}" + _with(opts) + on
        cols = ", ".join(q(c) for c, _ in ix.columns + [(i, False) for i in ix.included])
        return f"CREATE NONCLUSTERED COLUMNSTORE INDEX {q(ix.name)} ON {qt(t)} ({cols})" + _with(opts) + on
    if build.sort_in_tempdb:
        opts.append("SORT_IN_TEMPDB = ON")
    if build.maxdop:
        opts.append(f"MAXDOP = {build.maxdop}")
    if build.online:
        opts.append("ONLINE = ON")
    if ix.is_unique_constraint:
        kind = "CLUSTERED" if ix.clustered else "NONCLUSTERED"
        return (f"ALTER TABLE {qt(t)} ADD CONSTRAINT {q(ix.name)} UNIQUE {kind} ({_cols(ix.columns)})"
                + _with(opts) + on)
    sql = (f"CREATE {'UNIQUE ' if ix.unique else ''}{'CLUSTERED' if ix.clustered else 'NONCLUSTERED'} "
           f"INDEX {q(ix.name)} ON {qt(t)} ({_cols(ix.columns)})")
    if ix.included:
        sql += " INCLUDE (" + ", ".join(q(c) for c in ix.included) + ")"
    if ix.filter_definition:
        sql += f" WHERE {ix.filter_definition}"
    return sql + _with(opts) + on


def statistics_sql(t: Table, st: Statistic, fullscan: bool = False) -> str:
    sql = f"CREATE STATISTICS {q(st.name)} ON {qt(t)} ({', '.join(q(c) for c in st.columns)})"
    if st.filter_definition:
        sql += f" WHERE {st.filter_definition}"
    opts = (["FULLSCAN"] if fullscan else []) + (["NORECOMPUTE"] if st.no_recompute else [])
    return sql + (f" WITH {', '.join(opts)}" if opts else "")


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


def post_load_sql(t: Table, build: Optional[BuildOptions] = None, memory: bool = True,
                  partitions: bool = True) -> List[str]:
    """Indexes and constraints applied after the data is in (a memory-optimized table already has
    its indexes); XML and spatial indexes after the regular ones, primary XML before secondary."""
    out: List[str] = []
    if not (memory and t.memory_optimized):
        # clustered first so nonclustered indexes are not rebuilt
        for ix in sorted(t.indexes, key=lambda i: not i.clustered):
            if not ix.is_disabled:
                out.append(index_sql(t, ix, build, partitions))
    for xi in sorted(t.xml_indexes, key=lambda x: not x.primary):
        out.append(xml_index_sql(t, xi))
    for si in t.spatial_indexes:
        out.append(spatial_index_sql(t, si))
    for ck in t.checks:
        out.extend(check_sql(t, ck))
    return out


def disabled_indexes_sql(t: Table, partitions: bool = True) -> List[str]:
    """Indexes disabled on the source, created and disabled while the table is still empty, so the
    load doesn't maintain them (as on the source) and no build is wasted."""
    out = []
    for ix in t.indexes:
        if ix.is_disabled and not ix.clustered and not t.memory_optimized:
            out += [index_sql(t, ix, None, partitions), f"ALTER INDEX {q(ix.name)} ON {qt(t)} DISABLE"]
    return out


def xml_index_sql(t: Table, xi: Any) -> str:
    if xi.primary:
        return f"CREATE PRIMARY XML INDEX {q(xi.name)} ON {qt(t)} ({q(xi.column)})"
    return (f"CREATE XML INDEX {q(xi.name)} ON {qt(t)} ({q(xi.column)}) USING XML INDEX {q(xi.using)} "
            f"FOR {xi.secondary_type}")


def spatial_index_sql(t: Table, si: Any) -> str:
    opts = []
    if si.bounding_box and si.tessellation.startswith("GEOMETRY"):
        opts.append("BOUNDING_BOX = (" + ", ".join(repr(float(v)) for v in si.bounding_box) + ")")
    if si.grids:
        opts.append("GRIDS = (" + ", ".join(f"LEVEL_{i} = {g}" for i, g in enumerate(si.grids, 1)) + ")")
    opts.append(f"CELLS_PER_OBJECT = {int(si.cells_per_object)}")
    return (f"CREATE SPATIAL INDEX {q(si.name)} ON {qt(t)} ({q(si.column)}) USING {si.tessellation} "
            f"WITH ({', '.join(opts)})")


def fulltext_index_sql(t: Table) -> Optional[str]:
    ft = t.fulltext
    if ft is None:
        return None
    cols = []
    for col, lang, tcol, sem in ft.columns:
        c = q(col) + (f" TYPE COLUMN {q(tcol)}" if tcol else "") + (f" LANGUAGE {lang}" if lang is not None else "")
        cols.append(c + (" STATISTICAL_SEMANTICS" if sem else ""))
    stop = "OFF" if ft.stoplist in (None, "OFF") else ("SYSTEM" if ft.stoplist == "SYSTEM" else q(ft.stoplist))
    return (f"CREATE FULLTEXT INDEX ON {qt(t)} ({', '.join(cols)}) KEY INDEX {q(ft.key_index)} "
            f"ON {q(ft.catalog)} WITH (CHANGE_TRACKING = {ft.change_tracking}, STOPLIST = {stop})")


def temporal_sql(t: Table) -> List[str]:
    """Turn a loaded current/history pair back into a system-versioned table. The period values
    were copied as data, and DATA_CONSISTENCY_CHECK proves the history is consistent."""
    tp = t.temporal
    if tp is None:
        return []
    out = [f"ALTER TABLE {qt(t)} ADD PERIOD FOR SYSTEM_TIME ({q(tp.period_start)}, {q(tp.period_end)})"]
    out += [f"ALTER TABLE {qt(t)} ALTER COLUMN {q(c.name)} ADD HIDDEN"
            for c in t.columns if c.is_hidden and c.name in (tp.period_start, tp.period_end)]
    ret = f", HISTORY_RETENTION_PERIOD = {tp.retention}" if tp.retention else ""
    out.append(f"ALTER TABLE {qt(t)} SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = {q(tp.history_schema)}."
               f"{q(tp.history_table)}, DATA_CONSISTENCY_CHECK = ON{ret}))")
    return out


def change_tracking_sql(t: Table) -> Optional[str]:
    if t.change_tracking is None:
        return None
    return (f"ALTER TABLE {qt(t)} ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = "
            f"{'ON' if t.change_tracking else 'OFF'})")


def table_type_sql(t: Table, memory_optimized: bool = False) -> str:
    """CREATE TYPE ... AS TABLE. Constraint names in table types are generated, so none are given."""
    lines = [column_sql(c, masking=False, named_default=False) for c in t.columns]
    for ix in ([t.primary_key] if t.primary_key else []) + t.indexes:
        if ix.is_hash:
            body = f"HASH ({_cols(ix.columns)}) WITH (BUCKET_COUNT = {ix.bucket_count or 1024})"
        else:
            body = f"{'CLUSTERED' if ix.clustered else 'NONCLUSTERED'} ({_cols(ix.columns)})"
        if ix.is_primary_key:
            lines.append(f"PRIMARY KEY {body}")
        elif ix.is_unique_constraint:
            lines.append(f"UNIQUE {body}")
        else:
            lines.append(f"INDEX {q(ix.name)} {'UNIQUE ' if ix.unique and not ix.is_hash else ''}{body}")
    lines += [f"CHECK {ck.definition}" for ck in t.checks]
    sql = f"CREATE TYPE {q(t.schema)}.{q(t.name)} AS TABLE (\n    " + ",\n    ".join(lines) + "\n)"
    return sql + (" WITH (MEMORY_OPTIMIZED = ON)" if memory_optimized else "")


def partition_scheme_sql(name: str, function: str, filegroups: List[str], mode: str = "primary") -> str:
    if mode == "primary" or not filegroups:
        return f"CREATE PARTITION SCHEME {q(name)} AS PARTITION {q(function)} ALL TO ([PRIMARY])"
    return f"CREATE PARTITION SCHEME {q(name)} AS PARTITION {q(function)} TO ({', '.join(q(f) for f in filegroups)})"


def fk_sql_all(t: Table) -> List[str]:
    out: List[str] = []
    for fk in t.foreign_keys:
        out.extend(fk_sql(t, fk))
    return out


# ------------------------------------------------------------------ database-level objects
def extended_property_sql(ep: ExtendedProperty) -> str:
    args = [f"@name = {lit(ep.name)}", f"@value = {lit(ep.value) if ep.value is not None else 'NULL'}"]
    for i, (typ, name) in enumerate(ep.level):
        args += [f"@level{i}type = N'{typ}'", f"@level{i}name = {lit(name)}"]
    return "EXEC sys.sp_addextendedproperty " + ", ".join(args)


def principal_sql(p: Principal) -> Optional[str]:
    """CREATE ROLE / CREATE USER, or None for principals that can't be recreated from metadata
    (contained users with passwords, certificate users, Entra ID users, orphaned users)."""
    schema = f" WITH DEFAULT_SCHEMA = {q(p.default_schema)}" if p.default_schema else ""
    if p.kind == "ROLE":
        return f"CREATE ROLE {q(p.name)}"
    if p.kind in ("SQL_USER", "WINDOWS_USER") and p.login:
        return f"CREATE USER {q(p.name)} FOR LOGIN {q(p.login)}{schema}"
    if p.kind == "WINDOWS_GROUP" and p.login:
        return f"CREATE USER {q(p.name)} FOR LOGIN {q(p.login)}"
    if p.kind == "NO_LOGIN":
        return f"CREATE USER {q(p.name)} WITHOUT LOGIN{schema}"
    if p.kind == "CERTIFICATE_USER" and p.certificate:
        return f"CREATE USER {q(p.name)} FROM CERTIFICATE {q(p.certificate)}"
    if p.kind == "ASYMMETRIC_KEY_USER" and p.certificate:
        return f"CREATE USER {q(p.name)} FROM ASYMMETRIC KEY {q(p.certificate)}"
    return None


def role_owner_sql(p: Principal) -> Optional[str]:
    if p.kind == "ROLE" and p.owner and p.owner != "dbo":
        return f"ALTER AUTHORIZATION ON ROLE::{q(p.name)} TO {q(p.owner)}"
    return None


def role_member_sql(role: str, member: str) -> str:
    return f"ALTER ROLE {q(role)} ADD MEMBER {q(member)}"


def schema_owner_sql(schema: str, owner: str) -> str:
    return f"ALTER AUTHORIZATION ON SCHEMA::{q(schema)} TO {q(owner)}"


def permission_sql(p: Permission) -> str:
    verb = "DENY" if p.state == "DENY" else "GRANT"
    if p.securable_class == "DATABASE":
        on = ""
    elif p.securable_class == "SCHEMA":
        on = f" ON SCHEMA::{q(p.schema or '')}"
    elif p.securable_class in ("TYPE", "XML SCHEMA COLLECTION"):
        on = f" ON {p.securable_class}::{q(p.schema or '')}.{q(p.securable or '')}"
    elif p.securable_class in ("ASSEMBLY", "CERTIFICATE", "MESSAGE TYPE", "CONTRACT", "SERVICE"):
        on = f" ON {p.securable_class}::{q(p.securable or '')}"
    else:
        on = f" ON {q(p.schema or '')}.{q(p.securable or '')}" + (f" ({q(p.column)})" if p.column else "")
    tail = " WITH GRANT OPTION" if p.state == "GRANT_WITH_GRANT_OPTION" else ""
    return f"{verb} {p.permission}{on} TO {q(p.grantee)}{tail}"


def database_settings_sql(src: DatabaseSettings, tgt: DatabaseSettings, compatibility: str) -> List[str]:
    """ALTER DATABASE statements that make the target's settings match the source.

    ``compatibility`` is ``source`` (keep the source's level), ``target`` (leave the target's)
    or a number. Database options, scoped configurations, change tracking and Query Store follow
    the compatibility level and snapshot settings."""
    out = []
    for key, value in src.options.items():
        if key in tgt.options and tgt.options[key] == value:
            continue
        if key == "BROKER":
            out.append(f"ALTER DATABASE CURRENT SET {value} WITH ROLLBACK IMMEDIATE")
        else:
            out.append(f"ALTER DATABASE CURRENT SET {key} {value}")
    level = src.compatibility_level if compatibility == "source" else (
        int(compatibility) if compatibility.isdigit() else 0)
    if level and level != tgt.compatibility_level:
        out.append(f"ALTER DATABASE CURRENT SET COMPATIBILITY_LEVEL = {level}")
    if src.snapshot_isolation != tgt.snapshot_isolation:
        out.append(f"ALTER DATABASE CURRENT SET ALLOW_SNAPSHOT_ISOLATION {'ON' if src.snapshot_isolation else 'OFF'}")
    if src.read_committed_snapshot != tgt.read_committed_snapshot:
        out.append("ALTER DATABASE CURRENT SET READ_COMMITTED_SNAPSHOT "
                   f"{'ON' if src.read_committed_snapshot else 'OFF'} WITH ROLLBACK IMMEDIATE")
    for name, value in src.scoped_configurations.items():
        if name in tgt.scoped_configurations and tgt.scoped_configurations[name] != value:
            if name == "MAXDOP" or not value.replace("-", "").isdigit():
                v = value if value.replace("-", "").isdigit() or value.isupper() else lit(value)
            else:                      # on/off settings are stored as 0/1
                v = "ON" if value != "0" else "OFF"
            out.append(f"ALTER DATABASE SCOPED CONFIGURATION SET {name} = {v}")
    if src.change_tracking and not tgt.change_tracking:
        out.append(f"ALTER DATABASE CURRENT SET CHANGE_TRACKING = ON ({src.change_tracking})")
    if src.query_store != tgt.query_store:
        out.append("ALTER DATABASE CURRENT SET QUERY_STORE = " + (
            f"ON (OPERATION_MODE = {src.query_store})" if src.query_store else "OFF"))
    return out


def owner_sql(login: str) -> str:
    return ("DECLARE @s nvarchar(max) = N'ALTER AUTHORIZATION ON DATABASE::' + QUOTENAME(DB_NAME()) + "
            f"N' TO {q(login).replace(chr(39), chr(39) * 2)}'; EXEC (@s)")


def strip_outer_parens(expr: str) -> str:
    """'([rls].[fn]([Rep]))' -> '[rls].[fn]([Rep])': the catalog wraps predicates in parentheses,
    which CREATE SECURITY POLICY doesn't accept around the function call."""
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        depth, quote = 0, None
        for i, ch in enumerate(s):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'[":
                quote = "'" if ch == "'" else "]"
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    return s           # the first '(' closes before the end: not one wrapping pair
        s = s[1:-1].strip()
    return s


def security_policy_sql(p: SecurityPolicy, tables: Optional[Set[Any]] = None, enabled: bool = False) -> Optional[str]:
    """CREATE SECURITY POLICY, created disabled so it can't hide rows from validation; it's
    switched to the source's state at the very end (``policy_state_sql``)."""
    preds = []
    for ptype, definition, tsch, tname, op in p.predicates:
        if tables is not None and (tsch, tname) not in tables:
            continue
        preds.append(f"ADD {ptype} PREDICATE {strip_outer_parens(definition)} ON {q(tsch)}.{q(tname)}"
                     + (f" {op}" if op else ""))
    if not preds:
        return None
    return (f"CREATE SECURITY POLICY {q(p.schema)}.{q(p.name)} {', '.join(preds)} "
            f"WITH (STATE = {'ON' if enabled else 'OFF'}, SCHEMABINDING = {'ON' if p.schemabinding else 'OFF'})")


def policy_state_sql(p: SecurityPolicy) -> str:
    return f"ALTER SECURITY POLICY {q(p.schema)}.{q(p.name)} WITH (STATE = {'ON' if p.enabled else 'OFF'})"


def master_key_sql(password: str) -> str:
    return f"CREATE MASTER KEY ENCRYPTION BY PASSWORD = {lit(password)}"


def certificate_sql(c: Certificate) -> str:
    sql = f"CREATE CERTIFICATE {q(c.name)} FROM BINARY = {c.public}"
    if c.private_key and c.password:
        sql += f" WITH PRIVATE KEY (BINARY = {c.private_key}, DECRYPTION BY PASSWORD = {lit(c.password)})"
    return sql


def owner_change_sql(securable: str, name: str, owner: str) -> str:
    return f"ALTER AUTHORIZATION ON {securable}::{q(name)} TO {q(owner)}"


def signature_sql(s: Signature) -> str:
    return (f"ADD {'COUNTER ' if s.counter else ''}SIGNATURE TO {q(s.schema)}.{q(s.name)} "
            f"BY CERTIFICATE {q(s.certificate)} WITH SIGNATURE = {s.value}")


def cdc_sql(tables: List[CdcTable]) -> List[str]:
    out = ["IF (SELECT is_cdc_enabled FROM sys.databases WHERE name = DB_NAME()) = 0 EXEC sys.sp_cdc_enable_db"]
    for c in tables:
        out.append(f"EXEC sys.sp_cdc_enable_table @source_schema = {lit(c.schema)}, @source_name = {lit(c.table)}, "
                   f"@capture_instance = {lit(c.capture_instance)}, "
                   f"@role_name = {lit(c.role) if c.role else 'NULL'}, "
                   f"@supports_net_changes = {1 if c.net_changes else 0}, "
                   + (f"@index_name = {lit(c.index)}, " if c.index else "")
                   + f"@captured_column_list = {lit(', '.join(q(x) for x in c.columns))}")
    return out


# ------------------------------------------------------------------ modules
def module_statements(mod: Any) -> List[str]:
    """Statements that recreate one module exactly as the source has it: under the source's
    ANSI_NULLS / QUOTED_IDENTIFIER settings (they are compiled into the module: ANSI_NULLS OFF
    makes ``= NULL`` true for NULLs, QUOTED_IDENTIFIER OFF makes "x" a string), then its disabled
    state and trigger firing order. The session settings are restored at the end."""
    if mod.definition is None:
        return []
    meta = mod.meta or {}
    before, after = [], []
    if meta.get("ansi_nulls") is False:
        before.append("SET ANSI_NULLS OFF")
        after.append("SET ANSI_NULLS ON")
    if meta.get("quoted_identifier") is False:
        before.append("SET QUOTED_IDENTIFIER OFF")
        after.append("SET QUOTED_IDENTIFIER ON")
    # the text goes over byte for byte: a module signature (ADD SIGNATURE) covers it, whitespace included
    out = [*before, mod.definition, *after]
    if mod.kind == "TRIGGER" and mod.disabled and mod.parent:
        out.append(f"DISABLE TRIGGER {q(mod.schema)}.{q(mod.name)} ON "
                   + ".".join(q(p) for p in mod.parent.split(".", 1)))
    if mod.kind == "DDL TRIGGER" and mod.disabled:
        out.append(f"DISABLE TRIGGER {q(mod.name)} ON DATABASE")
    for order, event in meta.get("trigger_order", []):
        name = q(mod.name) if mod.kind == "DDL TRIGGER" else f"{q(mod.schema)}.{q(mod.name)}"
        out.append(f"EXEC sys.sp_settriggerorder @triggername = {lit(name)}, @order = {lit(order.title())}, "
                   f"@stmttype = {lit(event)}" + (", @namespace = N'DATABASE'" if mod.kind == "DDL TRIGGER" else ""))
    return out


def object_owner_sql(schema: str, name: str, owner: str) -> str:
    """An object owned by a principal other than its schema's owner (ownership chains, EXECUTE AS OWNER)."""
    return f"ALTER AUTHORIZATION ON OBJECT::{q(schema)}.{q(name)} TO {q(owner)}"
