"""Reading the SQL Server catalog: tables (with every storage and behaviour detail), programmable and
database-level objects, settings, security and the server-level objects a database depends on.

``CatalogMixin`` is mixed into :class:`~mssql_ocp_migrate.sqlserver.SqlServer`; it only needs
``query``, ``conn.scalar``, ``_has_col`` and ``major_version``. Everything is read with catalog
views available since SQL Server 2008, and newer columns are probed with ``COL_LENGTH`` first.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .adapter import ModuleDef
from .ddl import lit, q, type_sql
from .model import (CdcTable, Certificate, CheckConstraint, Column, DatabaseExtras, DatabaseSettings,
                    ExtendedProperty, ForeignKey, FullTextIndex, Index, Permission, Principal,
                    SecurityPolicy, Signature, SpatialIndex, Statistic, Table, Temporal, XmlIndex)

log = logging.getLogger(__name__)

# a sql_variant (boundary values, parameter defaults) as (base type, text) so Python can build a literal
VARIANT_SQL = """CAST(SQL_VARIANT_PROPERTY({v}, 'BaseType') AS nvarchar(128)),
    CASE WHEN {v} IS NULL THEN NULL
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') IN ('date', 'datetime', 'datetime2', 'smalldatetime')
              THEN CONVERT(nvarchar(40), CAST({v} AS datetime2(7)), 126)
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') = 'datetimeoffset'
              THEN CONVERT(nvarchar(40), CAST({v} AS datetimeoffset(7)), 126)
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') = 'time' THEN CAST(CAST({v} AS time(7)) AS nvarchar(40))
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') IN ('binary', 'varbinary')
              THEN CONVERT(nvarchar(max), CAST({v} AS varbinary(max)), 1)
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') = 'float' THEN CONVERT(nvarchar(100), CAST({v} AS float), 3)
         WHEN SQL_VARIANT_PROPERTY({v}, 'BaseType') = 'real' THEN CONVERT(nvarchar(100), CAST({v} AS real), 3)
         ELSE CAST({v} AS nvarchar(4000)) END"""

_NUMERIC = {"bit", "tinyint", "smallint", "int", "bigint", "decimal", "numeric", "money", "smallmoney",
            "float", "real"}


def variant_literal(base: Optional[str], text: Optional[str]) -> str:
    """T-SQL literal for a sql_variant read with VARIANT_SQL."""
    if text is None:
        return "NULL"
    base = (base or "").lower()
    if base in _NUMERIC or base in ("binary", "varbinary"):
        return text
    return lit(text)


def _bool(v) -> bool:
    return bool(v) and v not in (0, "0")


# ------------------------------------------------------------------------------------------------
class CatalogMixin:
    """Catalog reads. See the module docstring."""

    # these come from SqlServer
    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[tuple]: ...  # pragma: no cover
    def _has_col(self, view: str, col: str) -> bool: ...  # pragma: no cover

    def _col(self, view: str, col: str, expr: Optional[str] = None, default: str = "NULL") -> str:
        """``expr`` (default ``alias.col``) if the catalog view has the column, else ``default``."""
        return (expr or col) if self._has_col(view, col) else default

    def _cdc_enabled(self) -> bool:
        if not hasattr(self, "_cdc_cache"):
            self._cdc_cache = bool(self._has_col("sys.databases", "is_cdc_enabled") and self.query(
                "SELECT is_cdc_enabled FROM sys.databases WHERE name = DB_NAME()")[0][0])
        return self._cdc_cache

    def _excluded_schemas(self) -> List[str]:
        return ["cdc"] if self._cdc_enabled() else []

    # ---------------------------------------------------------------------------------- schemas
    def list_schemas(self) -> List[str]:
        rows = self.query(
            "SELECT s.name FROM sys.schemas s WHERE s.schema_id > 4 AND s.schema_id < 16384 "
            "AND s.name NOT IN ('guest','INFORMATION_SCHEMA','sys') ORDER BY s.name")
        skip = set(self._excluded_schemas())
        return [r[0] for r in rows if r[0] not in skip]

    # ----------------------------------------------------------------------------------- tables
    def list_tables(self) -> List[Table]:
        tv = "sys.tables"
        extra = [self._col(tv, "temporal_type", "t.temporal_type", "0"),
                 self._col(tv, "is_memory_optimized", "t.is_memory_optimized", "0"),
                 self._col(tv, "durability_desc", "t.durability_desc", "NULL"),
                 self._col(tv, "is_filetable", "t.is_filetable", "0"),
                 self._col(tv, "history_table_id", "t.history_table_id", "NULL"),
                 self._col(tv, "history_retention_period", "t.history_retention_period", "NULL"),
                 self._col(tv, "history_retention_period_unit_desc", "t.history_retention_period_unit_desc", "NULL"),
                 self._col(tv, "ledger_type", "t.ledger_type", "0"),
                 self._col(tv, "is_node", "t.is_node", "0"), self._col(tv, "is_edge", "t.is_edge", "0"),
                 self._col(tv, "is_external", "t.is_external", "0")]
        skip = self._excluded_schemas()
        not_in = (" AND s.name NOT IN (" + ", ".join(lit(x) for x in skip) + ")") if skip else ""
        rows = self.query(f"""
            SELECT t.object_id, s.name, t.name,
                   ISNULL((SELECT SUM(p.rows) FROM sys.partitions p
                           WHERE p.object_id = t.object_id AND p.index_id IN (0,1)), 0),
                   ISNULL((SELECT SUM(a.total_pages) * 8 / 1024.0 FROM sys.partitions p
                           JOIN sys.allocation_units a ON a.container_id = p.partition_id
                           WHERE p.object_id = t.object_id), 0),
                   {', '.join(extra)}
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.is_ms_shipped = 0{not_in}
              AND NOT EXISTS (SELECT 1 FROM sys.extended_properties ep
                              WHERE ep.major_id = t.object_id AND ep.minor_id = 0
                                AND ep.name = 'microsoft_database_tools_support')
            ORDER BY s.name, t.name""")
        tables: Dict[int, Table] = {}
        temporal: Dict[int, Tuple[int, Any, Any]] = {}
        for (oid, sch, name, nrows, mb, ttype, inmem, durability, filetable, hist, ret, unit, ledger,
             is_node, is_edge, is_ext) in rows:
            t = Table(schema=sch, name=name, row_count=int(nrows or 0), reserved_mb=float(mb or 0))
            if ttype and int(ttype) == 2 and hist:
                temporal[oid] = (int(hist), ret, unit)
            if _bool(inmem):
                t.memory_optimized = True
                t.durability = str(durability or "SCHEMA_AND_DATA")
            if _bool(filetable):
                t.warnings.append("FileTable: not supported by migrate (its files live in FILESTREAM); "
                                  "skipped, use lift")
            if ledger and int(ledger) > 0:
                t.warnings.append("ledger table: created as a normal table (ledger history and digests can't "
                                  "be migrated row by row); use lift to keep it a ledger table")
            if _bool(is_node) or _bool(is_edge):
                t.warnings.append("graph table: not supported by migrate (node ids are generated per database); "
                                  "skipped, use lift")
            if _bool(is_ext):
                t.warnings.append("external table: skipped (recreate the external data source and table)")
            tables[oid] = t
        if not tables:
            return []
        self._load_columns(tables)
        self._load_indexes(tables)
        self._load_fks(tables)
        self._load_checks(tables)
        self._load_storage(tables)
        self._load_statistics(tables)
        self._load_special_indexes(tables)
        self._load_temporal(tables, temporal)
        self._load_change_tracking(tables)
        for t in tables.values():
            for c in t.columns:
                if c.type_name.lower() == "sql_variant":
                    t.warnings.append(f"column {c.name} is sql_variant: copied, verify base types")
            if not t.key_columns and not t.memory_optimized:
                t.warnings.append("no primary key / unique key: copied as one chunk (no parallelism, "
                                  "no resume inside the table, validation by aggregate only)")
        return list(tables.values())

    # ---------------------------------------------------------------------------------- columns
    def _column_select(self) -> str:
        cv = "sys.columns"
        return ", ".join([
            "c.object_id", "c.name", "ut.name", "ut.is_user_defined", "ut.is_assembly_type",
            "SCHEMA_NAME(ut.schema_id)", "st.name", "c.max_length", "c.precision", "c.scale", "c.is_nullable",
            "c.is_identity", "CAST(ic.seed_value AS decimal(38,0))", "CAST(ic.increment_value AS decimal(38,0))",
            "c.is_computed", "cc.definition", "cc.is_persisted", "c.collation_name", "dc.name", "dc.definition",
            self._col(cv, "is_sparse", "c.is_sparse", "0"),
            self._col(cv, "is_column_set", "c.is_column_set", "0"),
            ("mc.masking_function" if self._has_col("sys.columns", "is_masked") else "NULL"),
            self._col(cv, "encryption_type_desc", "c.encryption_type_desc"),
            ("cek.name" if self._has_col(cv, "column_encryption_key_id") else "NULL"),
            self._col(cv, "encryption_algorithm_name", "c.encryption_algorithm_name"),
            self._col(cv, "generated_always_type_desc", "c.generated_always_type_desc"),
            self._col(cv, "is_hidden", "c.is_hidden", "0"),
            "CASE WHEN c.xml_collection_id > 0 THEN SCHEMA_NAME(xsc.schema_id) END",
            "CASE WHEN c.xml_collection_id > 0 THEN xsc.name END",
            "c.is_xml_document"])

    def _column_joins(self) -> str:
        joins = """
            JOIN sys.types ut ON ut.user_type_id = c.user_type_id
            LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id
            LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
            LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
            LEFT JOIN sys.xml_schema_collections xsc ON xsc.xml_collection_id = c.xml_collection_id"""
        if self._has_col("sys.columns", "is_masked"):
            joins += "\n            LEFT JOIN sys.masked_columns mc ON mc.object_id = c.object_id AND mc.column_id = c.column_id"
        if self._has_col("sys.columns", "column_encryption_key_id"):
            joins += ("\n            LEFT JOIN sys.column_encryption_keys cek "
                      "ON cek.column_encryption_key_id = c.column_encryption_key_id")
        return joins

    @staticmethod
    def column_from_row(r) -> Column:
        (_oid, name, utype, is_udt, is_asm, uschema, stype, max_len, prec, scale, nullable, is_ident, seed,
         inc, is_comp, comp_def, persisted, coll, dname, ddef, sparse, colset, mask, enc_type, cek, enc_alg,
         gen, hidden, xsch, xname, xdoc) = r
        clr_udt = False
        if is_asm:                       # CLR: geography / geometry / hierarchyid / user-defined CLR types
            base, alias = utype, None
            if uschema and uschema != "sys":
                clr_udt, alias = True, utype
        elif is_udt:                     # alias type, e.g. CREATE TYPE dbo.Phone FROM varchar(20)
            base, alias = stype, utype
        elif stype and utype != stype:   # built-in alias such as sysname = nvarchar(128)
            base, alias = stype, None
        else:
            base, alias = utype, None
        gen = str(gen or "")
        generated = {"AS_ROW_START": "ROW START", "AS_ROW_END": "ROW END"}.get(gen, "")
        return Column(
            name=name, type_name=base, max_length=int(max_len), precision=int(prec), scale=int(scale),
            nullable=bool(nullable), is_identity=bool(is_ident),
            identity_seed=int(seed) if seed is not None else None,
            identity_increment=int(inc) if inc is not None else None,
            is_computed=bool(is_comp), computed_definition=comp_def, computed_persisted=bool(persisted),
            collation=coll, default_name=dname, default_definition=ddef,
            user_type=alias, user_type_schema=uschema if alias else None, clr_udt=clr_udt,
            is_sparse=_bool(sparse), is_column_set=_bool(colset), masking_function=mask,
            encryption_key=cek, encryption_type=str(enc_type) if cek else None,
            encryption_algorithm=enc_alg if cek else None,
            generated_always=generated, is_hidden=_bool(hidden),
            xml_schema=f"{xsch}.{xname}" if xname else None, xml_document=_bool(xdoc) and bool(xname))

    def _load_columns(self, tables: Dict[int, Table],
                      owner_join: str = "JOIN sys.tables t ON t.object_id = c.object_id AND t.is_ms_shipped = 0"
                      ) -> None:
        rows = self.query(f"""
            SELECT {self._column_select()}
            FROM sys.columns c
            {owner_join}{self._column_joins()}
            ORDER BY c.object_id, c.column_id""")
        for r in rows:
            t = tables.get(r[0])
            if t is None:
                continue
            if r[26] and str(r[26]) not in ("NOT_APPLICABLE", "AS_ROW_START", "AS_ROW_END"):
                t.warnings.append(f"column {r[1]} is generated ({r[26]}): copied as a normal column")
            t.columns.append(self.column_from_row(r))

    # ---------------------------------------------------------------------------------- indexes
    def _load_indexes(self, tables: Dict[int, Table],
                      owner_join: str = "JOIN sys.tables t ON t.object_id = i.object_id AND t.is_ms_shipped = 0",
                      ) -> None:
        filt = "i.filter_definition" if self._has_col("sys.indexes", "filter_definition") else "NULL"
        hashed = self._has_col("sys.indexes", "type") and self._view_exists("sys.hash_indexes")
        rows = self.query(f"""
            SELECT i.object_id, i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key,
                   i.is_unique_constraint, {filt}, ic.key_ordinal, ic.is_descending_key,
                   ic.is_included_column, c.name,
                   i.fill_factor, i.is_padded, i.ignore_dup_key, i.allow_row_locks, i.allow_page_locks,
                   (SELECT TOP (1) p.data_compression_desc FROM sys.partitions p
                    WHERE p.object_id = i.object_id AND p.index_id = i.index_id ORDER BY p.partition_number),
                   ps.name, {'hi.bucket_count' if hashed else 'NULL'}, ic.partition_ordinal
            FROM sys.indexes i
            {owner_join}
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            LEFT JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id
            {'LEFT JOIN sys.hash_indexes hi ON hi.object_id = i.object_id AND hi.index_id = i.index_id'
             if hashed else ''}
            WHERE i.type IN (1, 2, 5, 6, 7) AND i.is_hypothetical = 0
            ORDER BY i.object_id, i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id""")
        built: Dict[Tuple[int, int], Index] = {}
        for (oid, iid, name, type_desc, uniq, is_pk, is_uc, fdef, key_ord, desc, incl, col,
             ff, padded, igdup, rowlk, pagelk, comp, scheme, buckets, part_ord) in rows:
            t = tables.get(oid)
            if t is None:
                continue
            ix = built.get((oid, iid))
            if ix is None:
                tdesc = "NONCLUSTERED" if type_desc == "NONCLUSTERED HASH" else type_desc
                ix = Index(name=name, columns=[], unique=bool(uniq), clustered=tdesc.startswith("CLUSTERED"),
                           is_primary_key=bool(is_pk), is_unique_constraint=bool(is_uc),
                           filter_definition=fdef, type_desc=tdesc,
                           fill_factor=int(ff or 0), is_padded=bool(padded), ignore_dup_key=bool(igdup),
                           allow_row_locks=bool(rowlk), allow_page_locks=bool(pagelk),
                           data_compression=str(comp or "NONE"), partition_scheme=scheme,
                           is_hash=type_desc == "NONCLUSTERED HASH", bucket_count=int(buckets or 0))
                built[(oid, iid)] = ix
                if is_pk:
                    t.primary_key = ix
                else:
                    t.indexes.append(ix)
            if part_ord and int(part_ord) == 1:
                ix.partition_column = col
                if not key_ord and not incl and "COLUMNSTORE" not in ix.type_desc:
                    continue          # added by SQL Server for partitioning, not a key or INCLUDE column
            if incl:
                ix.included.append(col)
            elif "COLUMNSTORE" in ix.type_desc and not key_ord:
                ix.columns.append((col, False))
            else:
                ix.columns.append((col, bool(desc)))
        # per-partition compression where the partitions of one index differ
        for oid, iid, pno, comp in self.query("""
                SELECT p.object_id, p.index_id, p.partition_number, p.data_compression_desc
                FROM sys.partitions p
                WHERE EXISTS (SELECT 1 FROM sys.partitions p2
                              WHERE p2.object_id = p.object_id AND p2.index_id = p.index_id
                              GROUP BY p2.object_id, p2.index_id HAVING COUNT(DISTINCT p2.data_compression) > 1)
                ORDER BY p.object_id, p.index_id, p.partition_number"""):
            ix = built.get((oid, iid))
            if ix is not None:
                ix.partition_compression[int(pno)] = str(comp)
            elif iid == 0 and oid in tables:
                tables[oid].heap_partition_compression[int(pno)] = str(comp)

    def _view_exists(self, view: str) -> bool:
        return self.conn.scalar("SELECT OBJECT_ID(?)", [view]) is not None  # type: ignore[attr-defined]

    def _load_storage(self, tables: Dict[int, Table]) -> None:
        """Heap compression and placement (a partitioned heap is created ON its scheme)."""
        for oid, comp, scheme, col in self.query("""
                SELECT i.object_id,
                       (SELECT TOP (1) p.data_compression_desc FROM sys.partitions p
                        WHERE p.object_id = i.object_id AND p.index_id = 0 ORDER BY p.partition_number),
                       ps.name,
                       (SELECT c.name FROM sys.index_columns ic JOIN sys.columns c
                          ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                        WHERE ic.object_id = i.object_id AND ic.index_id = 0 AND ic.partition_ordinal = 1)
                FROM sys.indexes i LEFT JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id
                WHERE i.index_id = 0"""):
            t = tables.get(oid)
            if t is not None:
                t.heap_compression = str(comp or "NONE")
                t.partition_scheme, t.partition_column = scheme, col

    def _load_statistics(self, tables: Dict[int, Table]) -> None:
        """User-created statistics (CREATE STATISTICS). Index and auto-created statistics are
        rebuilt by the index builds and by the optimizer."""
        filt = ("s.filter_definition" if self._has_col("sys.stats", "filter_definition") else "NULL")
        rows = self.query(f"""
            SELECT s.object_id, s.stats_id, s.name, {filt}, s.no_recompute, c.name
            FROM sys.stats s
            JOIN sys.tables t ON t.object_id = s.object_id AND t.is_ms_shipped = 0
            JOIN sys.stats_columns sc ON sc.object_id = s.object_id AND sc.stats_id = s.stats_id
            JOIN sys.columns c ON c.object_id = sc.object_id AND c.column_id = sc.column_id
            WHERE s.user_created = 1
            ORDER BY s.object_id, s.stats_id, sc.stats_column_id""")
        built: Dict[Tuple[int, int], Statistic] = {}
        for oid, sid, name, fdef, norecompute, col in rows:
            t = tables.get(oid)
            if t is None:
                continue
            st = built.get((oid, sid))
            if st is None:
                st = built[(oid, sid)] = Statistic(name=name, columns=[], filter_definition=fdef,
                                                   no_recompute=bool(norecompute))
                t.statistics.append(st)
            st.columns.append(col)

    def _load_special_indexes(self, tables: Dict[int, Table]) -> None:
        """XML, spatial and full-text indexes."""
        xtype = self._col("sys.xml_indexes", "xml_index_type", "xi.xml_index_type", "0")
        for oid, name, using, sec, kind, col in self.query(f"""
                SELECT xi.object_id, xi.name,
                       (SELECT p.name FROM sys.xml_indexes p
                        WHERE p.object_id = xi.object_id AND p.index_id = xi.using_xml_index_id),
                       xi.secondary_type_desc, {xtype}, c.name
                FROM sys.xml_indexes xi
                JOIN sys.index_columns ic ON ic.object_id = xi.object_id AND ic.index_id = xi.index_id
                JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                ORDER BY xi.object_id, xi.index_id"""):
            t = tables.get(oid)
            if t is None:
                continue
            if int(kind or 0) >= 2:
                t.warnings.append(f"selective XML index {name} not recreated (create it manually)")
                continue
            t.xml_indexes.append(XmlIndex(name=name, column=col, primary=using is None, using=using,
                                          secondary_type=sec if using else None))
        for (oid, name, scheme, x1, y1, x2, y2, g1, g2, g3, g4, cpo, col) in self.query("""
                SELECT si.object_id, si.name, si.tessellation_scheme,
                       st.bounding_box_xmin, st.bounding_box_ymin, st.bounding_box_xmax, st.bounding_box_ymax,
                       st.level_1_grid_desc, st.level_2_grid_desc, st.level_3_grid_desc, st.level_4_grid_desc,
                       st.cells_per_object, c.name
                FROM sys.spatial_indexes si
                JOIN sys.spatial_index_tessellations st ON st.object_id = si.object_id AND st.index_id = si.index_id
                JOIN sys.index_columns ic ON ic.object_id = si.object_id AND ic.index_id = si.index_id
                JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                ORDER BY si.object_id, si.index_id"""):
            t = tables.get(oid)
            if t is None:
                continue
            auto = "AUTO" in str(scheme)
            t.spatial_indexes.append(SpatialIndex(
                name=name, column=col, tessellation=str(scheme),
                bounding_box=(float(x1), float(y1), float(x2), float(y2)) if x1 is not None else None,
                grids=() if auto else tuple(str(g) for g in (g1, g2, g3, g4)), cells_per_object=int(cpo or 16)))
        stop = self._col("sys.fulltext_indexes", "stoplist_id", "fi.stoplist_id", "0")
        sem = self._col("sys.fulltext_index_columns", "statistical_semantics", "fic.statistical_semantics", "0")
        fts = {}
        for oid, cat, key, track, stoplist_id, stoplist, col, lang, tcol, semantics in self.query(f"""
                SELECT fi.object_id, fc.name, i.name, fi.change_tracking_state_desc, {stop},
                       (SELECT sl.name FROM sys.fulltext_stoplists sl WHERE sl.stoplist_id = {stop}),
                       c.name, fic.language_id, tc.name, {sem}
                FROM sys.fulltext_indexes fi
                JOIN sys.fulltext_catalogs fc ON fc.fulltext_catalog_id = fi.fulltext_catalog_id
                JOIN sys.indexes i ON i.object_id = fi.object_id AND i.index_id = fi.unique_index_id
                JOIN sys.fulltext_index_columns fic ON fic.object_id = fi.object_id
                JOIN sys.columns c ON c.object_id = fic.object_id AND c.column_id = fic.column_id
                LEFT JOIN sys.columns tc ON tc.object_id = fic.object_id AND tc.column_id = fic.type_column_id
                ORDER BY fi.object_id, fic.column_id"""):
            t = tables.get(oid)
            if t is None:
                continue
            ft = fts.get(oid)
            if ft is None:
                sl = "OFF" if stoplist_id is None else ("SYSTEM" if int(stoplist_id) == 0 else stoplist)
                track = {"AUTO": "AUTO", "MANUAL": "MANUAL"}.get(str(track), "OFF")
                ft = fts[oid] = t.fulltext = FullTextIndex(catalog=cat, key_index=key, columns=[],
                                                           change_tracking=track, stoplist=sl)
            ft.columns.append((col, int(lang) if lang is not None else None, tcol, _bool(semantics)))

    def _load_temporal(self, tables: Dict[int, Table], temporal: Dict[int, Tuple[int, Any, Any]]) -> None:
        if not temporal:
            return
        periods = {oid: (s, e) for oid, s, e in self.query("""
            SELECT p.object_id, COL_NAME(p.object_id, p.start_column_id), COL_NAME(p.object_id, p.end_column_id)
            FROM sys.periods p""")}
        for oid, (hist, ret, unit) in temporal.items():
            if oid not in periods:
                continue
            hs, hn = self.query("SELECT OBJECT_SCHEMA_NAME(?), OBJECT_NAME(?)", [hist, hist])[0]
            retention = None
            if ret is not None and int(ret) > 0 and unit and str(unit) != "INFINITE":
                retention = f"{int(ret)} {str(unit)}S"
            tables[oid].temporal = Temporal(periods[oid][0], periods[oid][1], hs, hn, retention)

    def _load_change_tracking(self, tables: Dict[int, Table]) -> None:
        try:
            for oid, cols in self.query("SELECT object_id, is_track_columns_updated_on FROM sys.change_tracking_tables"):
                if oid in tables:
                    tables[oid].change_tracking = _bool(cols)
        except Exception as exc:  # pragma: no cover - permissions
            log.warning("change tracking metadata not readable: %s", exc)

    def _load_fks(self, tables: Dict[int, Table]) -> None:
        rows = self.query("""
            SELECT fk.parent_object_id, fk.object_id, fk.name,
                   OBJECT_SCHEMA_NAME(fk.referenced_object_id), OBJECT_NAME(fk.referenced_object_id),
                   fk.delete_referential_action_desc, fk.update_referential_action_desc,
                   fk.is_disabled, fk.is_not_trusted, pc.name, rc.name
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
            ORDER BY fk.object_id, fkc.constraint_column_id""")
        built: Dict[int, ForeignKey] = {}
        for poid, fkid, name, rs, rt, ondel, onupd, dis, untrusted, pcol, rcol in rows:
            t = tables.get(poid)
            if t is None:
                continue
            fk = built.get(fkid)
            if fk is None:
                fk = ForeignKey(name=name, columns=[], ref_schema=rs, ref_table=rt, ref_columns=[],
                                on_delete=ondel, on_update=onupd, disabled=bool(dis),
                                not_trusted=bool(untrusted))
                built[fkid] = fk
                t.foreign_keys.append(fk)
            fk.columns.append(pcol)
            fk.ref_columns.append(rcol)

    def _load_checks(self, tables: Dict[int, Table]) -> None:
        for poid, name, definition, dis in self.query(
                "SELECT parent_object_id, name, definition, is_disabled FROM sys.check_constraints"):
            t = tables.get(poid)
            if t is not None:
                t.checks.append(CheckConstraint(name=name, definition=definition, disabled=bool(dis)))

    # ------------------------------------------------------------------------------- table types
    def list_table_types(self) -> List[Tuple[Table, bool]]:
        if not self._has_col("sys.types", "is_table_type"):
            return []
        mem = self._col("sys.table_types", "is_memory_optimized", "tt.is_memory_optimized", "0")
        types: Dict[int, Table] = {}
        memo: Dict[int, bool] = {}
        for oid, sch, name, m in self.query(f"""
                SELECT tt.type_table_object_id, SCHEMA_NAME(tt.schema_id), tt.name, {mem}
                FROM sys.table_types tt WHERE tt.is_user_defined = 1 ORDER BY tt.user_type_id"""):
            types[oid] = Table(schema=sch, name=name)
            memo[oid] = _bool(m)
        if not types:
            return []
        self._load_columns(types, "JOIN sys.table_types tt ON tt.type_table_object_id = c.object_id")
        self._load_indexes(types, "JOIN sys.table_types tt ON tt.type_table_object_id = i.object_id")
        self._load_checks(types)
        return [(t, memo[oid]) for oid, t in types.items()]

    # ------------------------------------------------------------------------------------ code
    def list_modules(self) -> List[ModuleDef]:
        """Every object created from a definition, in dependency order of its kind.

        Created before the tables: assemblies, CLR types, XML schema collections, alias and table
        types, sequences, partition functions and schemes, Always Encrypted keys, rules and
        defaults. After the load: views, procedures, functions (T-SQL and CLR), aggregates,
        triggers, synonyms. Last: Service Broker objects, database DDL triggers, plan guides."""
        from . import ddl
        out: List[ModuleDef] = []
        out += self._assemblies()
        out += self._clr_types()
        out += self._xml_schema_collections()
        for sch, name, base, max_len, prec, scale, nullable in self.query("""
                SELECT SCHEMA_NAME(t.schema_id), t.name, st.name, t.max_length, t.precision, t.scale, t.is_nullable
                FROM sys.types t JOIN sys.types st ON st.user_type_id = t.system_type_id
                WHERE t.is_user_defined = 1 AND t.is_assembly_type = 0 AND t.is_table_type = 0"""):
            col = Column(name="x", type_name=base, max_length=max_len, precision=prec, scale=scale)
            out.append(ModuleDef("TYPE", sch, name, f"CREATE TYPE {q(sch)}.{q(name)} FROM {type_sql(col)} "
                                                    f"{'NULL' if nullable else 'NOT NULL'}"))
        for t, memory in self.list_table_types():
            out.append(ModuleDef("TYPE", t.schema, t.name, ddl.table_type_sql(t, memory)))
        if self._has_col("sys.sequences", "current_value"):
            for sch, name, tname, start, inc, mn, mx, cyc, cache, cur in self.query("""
                    SELECT SCHEMA_NAME(s.schema_id), s.name, TYPE_NAME(s.user_type_id),
                           CAST(s.start_value AS decimal(38,0)), CAST(s.increment AS decimal(38,0)),
                           CAST(s.minimum_value AS decimal(38,0)), CAST(s.maximum_value AS decimal(38,0)),
                           s.is_cycling, s.cache_size, CAST(s.current_value AS decimal(38,0))
                    FROM sys.sequences s"""):
                restart = int(cur) + int(inc)
                out.append(ModuleDef("SEQUENCE", sch, name,
                                     f"CREATE SEQUENCE {q(sch)}.{q(name)} AS {tname} START WITH {restart} "
                                     f"INCREMENT BY {int(inc)} MINVALUE {int(mn)} MAXVALUE {int(mx)} "
                                     f"{'CYCLE' if cyc else 'NO CYCLE'}"
                                     + (f" CACHE {cache}" if cache else "")))
        out += self._partitioning()
        out += self._always_encrypted_keys()
        out += self._fulltext_objects()
        skip = set(self._excluded_schemas())
        kinds = {"V": "VIEW", "P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION", "TF": "FUNCTION",
                 "TR": "TRIGGER", "R": "RULE", "D": "DEFAULT"}
        for otype, sch, name, definition, parent, disabled in self.query("""
                SELECT o.type, SCHEMA_NAME(o.schema_id), o.name, m.definition,
                       CASE WHEN o.parent_object_id > 0
                            THEN OBJECT_SCHEMA_NAME(o.parent_object_id) + '.' + OBJECT_NAME(o.parent_object_id) END,
                       ISNULL(tr.is_disabled, 0)
                FROM sys.objects o
                JOIN sys.sql_modules m ON m.object_id = o.object_id
                LEFT JOIN sys.triggers tr ON tr.object_id = o.object_id
                WHERE o.is_ms_shipped = 0 AND (o.type IN ('V','P','FN','IF','TF','TR','R')
                                               OR (o.type = 'D' AND o.parent_object_id = 0))
                ORDER BY o.create_date"""):
            if sch in skip:
                continue
            out.append(ModuleDef(kinds[otype.strip()], sch, name, definition, parent, bool(disabled)))
        out += self._clr_modules()
        for sch, name, base in self.query(
                "SELECT SCHEMA_NAME(schema_id), name, base_object_name FROM sys.synonyms"):
            out.append(ModuleDef("SYNONYM", sch, name, f"CREATE SYNONYM {q(sch)}.{q(name)} FOR {base}"))
        out += self._service_broker()
        out += self._ddl_triggers()
        out += self._plan_guides()
        return out

    def _assemblies(self) -> List[ModuleDef]:
        rows = self.query("""SELECT a.assembly_id, a.name, a.permission_set_desc FROM sys.assemblies a
                             WHERE a.is_user_defined = 1 ORDER BY a.assembly_id""")
        if not rows:
            return []
        files: Dict[int, List[Tuple[int, str, str]]] = {}
        for aid, fid, fname, content in self.query("""
                SELECT assembly_id, file_id, name, CONVERT(varchar(max), content, 1) FROM sys.assembly_files
                ORDER BY assembly_id, file_id"""):
            files.setdefault(aid, []).append((int(fid), fname, content))
        refs: Dict[int, List[int]] = {}
        for aid, rid in self.query("SELECT assembly_id, referenced_assembly_id FROM sys.assembly_references"):
            refs.setdefault(aid, []).append(rid)
        by_id = {r[0]: r for r in rows}
        order: List[int] = []

        def visit(aid, seen=()):
            if aid in order or aid not in by_id or aid in seen:
                return
            for rid in refs.get(aid, []):
                visit(rid, seen + (aid,))
            order.append(aid)
        for aid in by_id:
            visit(aid)
        out = []
        perm = {"SAFE_ACCESS": "SAFE", "EXTERNAL_ACCESS": "EXTERNAL_ACCESS", "UNSAFE_ACCESS": "UNSAFE"}
        for aid in order:
            _, name, pset = by_id[aid]
            fl = files.get(aid, [])
            main = next((f for f in fl if f[0] == 1), None)
            if main is None:
                continue
            sql = (f"CREATE ASSEMBLY {q(name)} FROM {main[2]} "
                   f"WITH PERMISSION_SET = {perm.get(str(pset), 'SAFE')}")
            for fid, fname, content in fl:
                if fid != 1:
                    sql += f";\nALTER ASSEMBLY {q(name)} ADD FILE FROM {content} AS {lit(fname)}"
            sha = hashlib.sha512(bytes.fromhex(main[2][2:])).hexdigest()
            out.append(ModuleDef("ASSEMBLY", "", name, sql, meta={"sha512": sha}))
        return out

    def _clr_types(self) -> List[ModuleDef]:
        if not self._view_exists("sys.assembly_types"):
            return []
        return [ModuleDef("TYPE", sch, name, f"CREATE TYPE {q(sch)}.{q(name)} EXTERNAL NAME {q(asm)}.{q(cls)}")
                for sch, name, asm, cls in self.query("""
                    SELECT SCHEMA_NAME(t.schema_id), t.name, a.name, t.assembly_class
                    FROM sys.assembly_types t JOIN sys.assemblies a ON a.assembly_id = t.assembly_id
                    WHERE t.is_user_defined = 1""")]

    def _xml_schema_collections(self) -> List[ModuleDef]:
        out = []
        for sch, name in self.query("""SELECT SCHEMA_NAME(schema_id), name FROM sys.xml_schema_collections
                                       WHERE schema_id <> SCHEMA_ID('sys') ORDER BY xml_collection_id"""):
            body = self.query("SELECT CAST(XML_SCHEMA_NAMESPACE(?, ?) AS nvarchar(max))", [sch, name])[0][0]
            out.append(ModuleDef("XML SCHEMA COLLECTION", sch, name,
                                 f"CREATE XML SCHEMA COLLECTION {q(sch)}.{q(name)} AS {lit(body)}"))
        return out

    def _partitioning(self) -> List[ModuleDef]:
        out = []
        funcs = self.query("""SELECT pf.function_id, pf.name, pf.boundary_value_on_right FROM sys.partition_functions pf
                              ORDER BY pf.function_id""")
        if not funcs:
            return out
        ptype: Dict[int, str] = {}
        for fid, base, is_udt, ut_schema, max_len, prec, scale, coll in self.query("""
                SELECT pp.function_id, st.name, ut.is_user_defined, SCHEMA_NAME(ut.schema_id), pp.max_length,
                       pp.precision, pp.scale, pp.collation_name
                FROM sys.partition_parameters pp
                JOIN sys.types ut ON ut.user_type_id = pp.user_type_id
                JOIN sys.types st ON st.user_type_id = pp.system_type_id
                WHERE pp.parameter_id = 1"""):
            c = Column("p", base, max_length=int(max_len), precision=int(prec), scale=int(scale), collation=coll)
            sql_t = type_sql(c)
            if coll and base.lower() in ("char", "varchar", "nchar", "nvarchar"):
                sql_t += f" COLLATE {coll}"
            ptype[fid] = sql_t
        values: Dict[int, List[str]] = {}
        for fid, base, text in self.query(f"""
                SELECT rv.function_id, {VARIANT_SQL.format(v='rv.value')}
                FROM sys.partition_range_values rv ORDER BY rv.function_id, rv.boundary_id"""):
            values.setdefault(fid, []).append(variant_literal(base, text))
        for fid, name, right in funcs:
            out.append(ModuleDef("PARTITION FUNCTION", "", name,
                                 f"CREATE PARTITION FUNCTION {q(name)} ({ptype.get(fid, 'int')}) AS RANGE "
                                 f"{'RIGHT' if right else 'LEFT'} FOR VALUES ({', '.join(values.get(fid, []))})"))
        fgs: Dict[int, List[str]] = {}
        for sid, fg in self.query("""SELECT dds.partition_scheme_id, ds.name FROM sys.destination_data_spaces dds
                                     JOIN sys.data_spaces ds ON ds.data_space_id = dds.data_space_id
                                     ORDER BY dds.partition_scheme_id, dds.destination_id"""):
            fgs.setdefault(sid, []).append(fg)
        for sid, name, fname in self.query("""SELECT ps.data_space_id, ps.name, pf.name FROM sys.partition_schemes ps
                                              JOIN sys.partition_functions pf ON pf.function_id = ps.function_id"""):
            out.append(ModuleDef("PARTITION SCHEME", "", name, "", meta={
                "function": fname, "filegroups": fgs.get(sid, ["PRIMARY"])}))
        return out

    def _always_encrypted_keys(self) -> List[ModuleDef]:
        if not self._view_exists("sys.column_master_keys"):
            return []
        out = []
        enclave = self._has_col("sys.column_master_keys", "allow_enclave_computations")
        for name, provider, path, allow, sig in self.query(f"""
                SELECT name, key_store_provider_name, key_path,
                       {'allow_enclave_computations' if enclave else '0'},
                       {'CONVERT(varchar(max), signature, 1)' if enclave else 'NULL'}
                FROM sys.column_master_keys"""):
            sql = (f"CREATE COLUMN MASTER KEY {q(name)} WITH (KEY_STORE_PROVIDER_NAME = {lit(provider)}, "
                   f"KEY_PATH = {lit(path)}")
            if _bool(allow) and sig:
                sql += f", ENCLAVE_COMPUTATIONS (SIGNATURE = {sig})"
            out.append(ModuleDef("COLUMN MASTER KEY", "", name, sql + ")"))
        keys: Dict[str, List[str]] = {}
        for name, cmk, alg, val in self.query("""
                SELECT k.name, m.name, v.encryption_algorithm_name, CONVERT(varchar(max), v.encrypted_value, 1)
                FROM sys.column_encryption_keys k
                JOIN sys.column_encryption_key_values v ON v.column_encryption_key_id = k.column_encryption_key_id
                JOIN sys.column_master_keys m ON m.column_master_key_id = v.column_master_key_id
                ORDER BY k.column_encryption_key_id"""):
            keys.setdefault(name, []).append(
                f"(COLUMN_MASTER_KEY = {q(cmk)}, ALGORITHM = {lit(alg).lstrip('N')}, ENCRYPTED_VALUE = {val})")
        for name, vals in keys.items():
            out.append(ModuleDef("COLUMN ENCRYPTION KEY", "", name,
                                 f"CREATE COLUMN ENCRYPTION KEY {q(name)} WITH VALUES {', '.join(vals)}"))
        return out

    def _fulltext_objects(self) -> List[ModuleDef]:
        out = []
        for name, default, accent in self.query(
                "SELECT name, is_default, is_accent_sensitivity_on FROM sys.fulltext_catalogs"):
            out.append(ModuleDef("FULLTEXT CATALOG", "", name,
                                 f"CREATE FULLTEXT CATALOG {q(name)} WITH ACCENT_SENSITIVITY = "
                                 f"{'ON' if _bool(accent) else 'OFF'}" + (" AS DEFAULT" if _bool(default) else "")))
        if not self._view_exists("sys.fulltext_stoplists"):
            return out
        words: Dict[int, List[str]] = {}
        for sid, word, lang in self.query("SELECT stoplist_id, stopword, language_id FROM sys.fulltext_stopwords"):
            words.setdefault(sid, []).append(f"ADD {lit(word)} LANGUAGE {int(lang)}")
        for sid, name in self.query("SELECT stoplist_id, name FROM sys.fulltext_stoplists"):
            sql = f"CREATE FULLTEXT STOPLIST {q(name)}"
            for w in words.get(sid, []):
                sql += f";\nALTER FULLTEXT STOPLIST {q(name)} {w}"
            out.append(ModuleDef("FULLTEXT STOPLIST", "", name, sql))
        return out

    def _type_name(self, row) -> str:
        """Type text from (ut.name, is_user_defined, is_assembly_type, schema, st.name, max_length, precision,
        scale)."""
        utype, is_udt, is_asm, uschema, stype, max_len, prec, scale = row
        r = (0, "x", utype, is_udt, is_asm, uschema, stype, max_len, prec, scale, 1, 0, None, None, 0, None,
             0, None, None, None, 0, 0, None, None, None, None, None, 0, None, None, 0)
        return type_sql(self.column_from_row(r))

    def _clr_modules(self) -> List[ModuleDef]:
        if not self._view_exists("sys.assembly_modules"):
            return []
        mods = self.query("""
            SELECT o.object_id, o.type, SCHEMA_NAME(o.schema_id), o.name, a.name, am.assembly_class,
                   am.assembly_method, am.null_on_null_input, am.execute_as_principal_id,
                   CASE WHEN o.parent_object_id > 0 THEN OBJECT_SCHEMA_NAME(o.parent_object_id) END,
                   CASE WHEN o.parent_object_id > 0 THEN OBJECT_NAME(o.parent_object_id) END
            FROM sys.assembly_modules am JOIN sys.objects o ON o.object_id = am.object_id
            JOIN sys.assemblies a ON a.assembly_id = am.assembly_id
            WHERE o.is_ms_shipped = 0 ORDER BY o.object_id""")
        if not mods:
            return []
        ids = ", ".join(str(int(m[0])) for m in mods)
        params: Dict[int, List[tuple]] = {}
        for row in self.query(f"""
                SELECT p.object_id, p.parameter_id, p.name, ut.name, ut.is_user_defined, ut.is_assembly_type,
                       SCHEMA_NAME(ut.schema_id), st.name, p.max_length, p.precision, p.scale, p.is_output,
                       p.has_default_value, {VARIANT_SQL.format(v='p.default_value')}
                FROM sys.parameters p JOIN sys.types ut ON ut.user_type_id = p.user_type_id
                LEFT JOIN sys.types st ON st.user_type_id = p.system_type_id
                WHERE p.object_id IN ({ids}) ORDER BY p.object_id, p.parameter_id"""):
            params.setdefault(row[0], []).append(row)
        tcols: Dict[int, List[str]] = {}
        for row in self.query(f"""
                SELECT c.object_id, c.name, ut.name, ut.is_user_defined, ut.is_assembly_type,
                       SCHEMA_NAME(ut.schema_id), st.name, c.max_length, c.precision, c.scale
                FROM sys.columns c JOIN sys.types ut ON ut.user_type_id = c.user_type_id
                LEFT JOIN sys.types st ON st.user_type_id = c.system_type_id
                WHERE c.object_id IN ({ids}) ORDER BY c.object_id, c.column_id"""):
            tcols.setdefault(row[0], []).append(f"{q(row[1])} {self._type_name(row[2:10])}")
        events: Dict[int, List[str]] = {}
        insteadof: Dict[int, bool] = {}
        for oid, ev, inst in self.query(f"""
                SELECT te.object_id, te.type_desc, tr.is_instead_of_trigger FROM sys.trigger_events te
                JOIN sys.triggers tr ON tr.object_id = te.object_id WHERE te.object_id IN ({ids})"""):
            events.setdefault(oid, []).append(str(ev))
            insteadof[oid] = _bool(inst)
        out = []
        for oid, otype, sch, name, asm, cls, meth, nullinput, exec_as, psch, pname in mods:
            otype = str(otype).strip()
            ps = params.get(oid, [])
            ret = next((p for p in ps if p[1] == 0), None)
            args = []
            for p in ps:
                if p[1] == 0:
                    continue
                a = f"{p[2]} {self._type_name(p[3:11])}"
                if _bool(p[12]):
                    a += f" = {variant_literal(p[13], p[14])}"
                if _bool(p[11]):
                    a += " OUTPUT"
                args.append(a)
            ext = f"{q(asm)}.{q(cls)}" + (f".{q(meth)}" if meth else "")
            with_ = []
            if exec_as is not None:
                with_.append("EXECUTE AS OWNER" if int(exec_as) == -2 else "EXECUTE AS SELF")
            if otype == "PC":
                sql = (f"CREATE PROCEDURE {q(sch)}.{q(name)} {', '.join(args)}"
                       + (f" WITH {', '.join(with_)}" if with_ else "") + f" AS EXTERNAL NAME {ext}")
                kind = "PROCEDURE"
            elif otype in ("FS", "FT"):
                if otype == "FS" and _bool(nullinput):
                    with_.insert(0, "RETURNS NULL ON NULL INPUT")
                returns = (self._type_name(ret[3:11]) if otype == "FS" and ret
                           else f"TABLE ({', '.join(tcols.get(oid, []))})")
                sql = (f"CREATE FUNCTION {q(sch)}.{q(name)} ({', '.join(args)}) RETURNS {returns}"
                       + (f" WITH {', '.join(with_)}" if with_ else "") + f" AS EXTERNAL NAME {ext}")
                kind = "FUNCTION"
            elif otype == "TA" and pname:
                when = "INSTEAD OF" if insteadof.get(oid) else "AFTER"
                sql = (f"CREATE TRIGGER {q(sch)}.{q(name)} ON {q(psch)}.{q(pname)} {when} "
                       f"{', '.join(events.get(oid, ['INSERT']))} AS EXTERNAL NAME {ext}")
                kind = "TRIGGER"
            elif otype == "AF" and ret:
                sql = (f"CREATE AGGREGATE {q(sch)}.{q(name)} ({', '.join(args)}) "
                       f"RETURNS {self._type_name(ret[3:11])} EXTERNAL NAME {q(asm)}.{q(cls)}")
                kind = "AGGREGATE"
            else:
                continue
            out.append(ModuleDef(kind, sch, name, sql, f"{psch}.{pname}" if pname else None))
        return out

    def _service_broker(self) -> List[ModuleDef]:
        out: List[ModuleDef] = []
        sysname = "N'http://schemas.microsoft.com/SQL/%'"
        for name, valid, xs, xn in self.query(f"""
                SELECT mt.name, mt.validation_desc, SCHEMA_NAME(x.schema_id), x.name
                FROM sys.service_message_types mt
                LEFT JOIN sys.xml_schema_collections x ON x.xml_collection_id = mt.xml_collection_id
                WHERE mt.name NOT LIKE {sysname} AND mt.name <> N'DEFAULT' ORDER BY mt.message_type_id"""):
            v = {"NONE": "NONE", "EMPTY": "EMPTY"}.get(str(valid))
            if v is None:
                v = f"VALID_XML WITH SCHEMA COLLECTION {q(xs)}.{q(xn)}" if xn else "WELL_FORMED_XML"
            out.append(ModuleDef("MESSAGE TYPE", "", name, f"CREATE MESSAGE TYPE {q(name)} VALIDATION = {v}"))
        contracts: Dict[str, List[str]] = {}
        for name, mt, init, target in self.query(f"""
                SELECT c.name, mt.name, u.is_sent_by_initiator, u.is_sent_by_target
                FROM sys.service_contracts c
                JOIN sys.service_contract_message_usages u ON u.service_contract_id = c.service_contract_id
                JOIN sys.service_message_types mt ON mt.message_type_id = u.message_type_id
                WHERE c.name NOT LIKE {sysname} AND c.name <> N'DEFAULT'
                ORDER BY c.service_contract_id"""):
            by = "ANY" if _bool(init) and _bool(target) else ("INITIATOR" if _bool(init) else "TARGET")
            contracts.setdefault(name, []).append(f"{q(mt)} SENT BY {by}")
        for name, usages in contracts.items():
            out.append(ModuleDef("CONTRACT", "", name, f"CREATE CONTRACT {q(name)} ({', '.join(usages)})"))
        poison = self._col("sys.service_queues", "is_poison_message_handling_enabled",
                           "q.is_poison_message_handling_enabled", "1")
        for (sch, name, recv, enq, ret, act, proc, readers, exec_as, pois) in self.query(f"""
                SELECT SCHEMA_NAME(q.schema_id), q.name, q.is_receive_enabled, q.is_enqueue_enabled,
                       q.is_retention_enabled, q.is_activation_enabled, q.activation_procedure, q.max_readers,
                       q.execute_as_principal_id, {poison}
                FROM sys.service_queues q WHERE q.is_ms_shipped = 0"""):
            opts = [f"STATUS = {'ON' if _bool(recv) else 'OFF'}", f"RETENTION = {'ON' if _bool(ret) else 'OFF'}"]
            if proc:            # stored as [db].[schema].[proc]: drop the source database name
                parts = re.findall(r"\[((?:[^\]]|\]\])*)\]", str(proc))
                proc = ".".join(f"[{x}]" for x in parts[-2:]) if len(parts) >= 2 else proc
                who = "OWNER" if exec_as is not None and int(exec_as) == -2 else "SELF"
                opts.append(f"ACTIVATION (STATUS = {'ON' if _bool(act) else 'OFF'}, PROCEDURE_NAME = {proc}, "
                            f"MAX_QUEUE_READERS = {int(readers or 1)}, EXECUTE AS {who})")
            opts.append(f"POISON_MESSAGE_HANDLING (STATUS = {'ON' if _bool(pois) else 'OFF'})")
            out.append(ModuleDef("QUEUE", sch, name, f"CREATE QUEUE {q(sch)}.{q(name)} WITH {', '.join(opts)}"))
        services: Dict[str, Tuple[str, str, List[str]]] = {}
        for name, qs, qn, contract in self.query(f"""
                SELECT s.name, SCHEMA_NAME(q.schema_id), q.name, c.name
                FROM sys.services s JOIN sys.service_queues q ON q.object_id = s.service_queue_id
                LEFT JOIN sys.service_contract_usages u ON u.service_id = s.service_id
                LEFT JOIN sys.service_contracts c ON c.service_contract_id = u.service_contract_id
                WHERE s.name NOT LIKE {sysname}"""):
            svc = services.setdefault(name, (qs, qn, []))
            if contract:
                svc[2].append(q(contract) if contract != "DEFAULT" else "[DEFAULT]")
        for name, (qs, qn, cs) in services.items():
            out.append(ModuleDef("SERVICE", "", name, f"CREATE SERVICE {q(name)} ON QUEUE {q(qs)}.{q(qn)}"
                                 + (f" ({', '.join(cs)})" if cs else "")))
        for name, svc, broker, address, mirror in self.query(
                "SELECT name, remote_service_name, broker_instance, address, mirror_address FROM sys.routes "
                "WHERE name <> 'AutoCreatedLocal'"):
            opts = ([f"SERVICE_NAME = {lit(svc)}"] if svc else []) + (
                [f"BROKER_INSTANCE = {lit(broker)}"] if broker else []) + [f"ADDRESS = {lit(address)}"] + (
                [f"MIRROR_ADDRESS = {lit(mirror)}"] if mirror else [])
            out.append(ModuleDef("ROUTE", "", name, f"CREATE ROUTE {q(name)} WITH {', '.join(opts)}"))
        for name, svc, user, anon in self.query("""
                SELECT b.name, b.remote_service_name, USER_NAME(b.remote_principal_id), b.is_anonymous_on
                FROM sys.remote_service_bindings b"""):
            out.append(ModuleDef("REMOTE SERVICE BINDING", "", name,
                                 f"CREATE REMOTE SERVICE BINDING {q(name)} TO SERVICE {lit(svc)} WITH USER = "
                                 f"{q(user)}, ANONYMOUS = {'ON' if _bool(anon) else 'OFF'}"))
        guid = self.query("SELECT CAST(service_broker_guid AS nvarchar(40)) FROM sys.databases "
                          "WHERE name = DB_NAME()")[0][0]
        notes: Dict[str, Tuple[str, str, List[str]]] = {}
        for oid, name, svc, broker, ev in self.query("""
                SELECT en.object_id, en.name, en.service_name, en.broker_instance, e.type_desc
                FROM sys.event_notifications en JOIN sys.events e ON e.object_id = en.object_id
                WHERE en.parent_class = 0"""):
            n = notes.setdefault(name, (svc, broker, []))
            n[2].append(str(ev))
        for name, (svc, broker, evs) in notes.items():
            where = "N'current database'" if not broker or str(broker).lower() == str(guid).lower() else lit(broker)
            out.append(ModuleDef("EVENT NOTIFICATION", "", name,
                                 f"CREATE EVENT NOTIFICATION {q(name)} ON DATABASE FOR {', '.join(evs)} "
                                 f"TO SERVICE {lit(svc)}, {where}"))
        return out

    def _ddl_triggers(self) -> List[ModuleDef]:
        return [ModuleDef("DDL TRIGGER", "", name, definition, None, _bool(dis))
                for name, definition, dis in self.query("""
                    SELECT t.name, m.definition, t.is_disabled FROM sys.triggers t
                    JOIN sys.sql_modules m ON m.object_id = t.object_id
                    WHERE t.parent_class = 0 AND t.is_ms_shipped = 0""")]

    def _plan_guides(self) -> List[ModuleDef]:
        out = []
        for name, dis, stmt, scope, osch, oname, batch, params, hints in self.query("""
                SELECT pg.name, pg.is_disabled, pg.query_text, pg.scope_type_desc,
                       OBJECT_SCHEMA_NAME(pg.scope_object_id), OBJECT_NAME(pg.scope_object_id),
                       pg.scope_batch, pg.parameters, pg.hints FROM sys.plan_guides pg"""):
            module = f"{q(osch)}.{q(oname)}" if str(scope) == "OBJECT" else batch
            sql = (f"EXEC sys.sp_create_plan_guide @name = {lit(name)}, @stmt = {lit(stmt)}, "
                   f"@type = N'{scope}', @module_or_batch = {lit(module) if module else 'NULL'}, "
                   f"@params = {lit(params) if params else 'NULL'}, @hints = {lit(hints) if hints else 'NULL'}")
            if _bool(dis):
                sql += f";\nEXEC sys.sp_control_plan_guide N'DISABLE', {lit(name)}"
            out.append(ModuleDef("PLAN GUIDE", "", name, sql))
        return out

    def rule_default_bindings(self) -> List[str]:
        """sp_bindrule / sp_binddefault calls for legacy rules and defaults bound to columns."""
        out = []
        for kind, osch, oname, tsch, tname, col in self.query("""
                SELECT 'RULE', SCHEMA_NAME(o.schema_id), o.name, OBJECT_SCHEMA_NAME(c.object_id),
                       OBJECT_NAME(c.object_id), c.name
                FROM sys.columns c JOIN sys.objects o ON o.object_id = c.rule_object_id
                JOIN sys.tables t ON t.object_id = c.object_id
                UNION ALL
                SELECT 'DEFAULT', SCHEMA_NAME(o.schema_id), o.name, OBJECT_SCHEMA_NAME(c.object_id),
                       OBJECT_NAME(c.object_id), c.name
                FROM sys.columns c JOIN sys.objects o ON o.object_id = c.default_object_id AND o.parent_object_id = 0
                JOIN sys.tables t ON t.object_id = c.object_id"""):
            proc = "sp_bindrule" if kind == "RULE" else "sp_binddefault"
            out.append(f"EXEC sys.{proc} {lit(f'{q(osch)}.{q(oname)}')}, {lit(f'{tsch}.{tname}.{col}')}")
        return out

    # ------------------------------------------------------------------------- database level
    def database_extras(self) -> DatabaseExtras:
        """Settings, users/roles, memberships, schema owners, permissions, extended properties,
        row-level security, certificates and signatures, CDC; plus warnings for what can't be copied."""
        ex = DatabaseExtras()
        ex.settings = self._database_settings()
        ex.principals, ex.role_members, ex.schema_owners, ex.permissions = self._security()
        ex.extended_properties = self._extended_properties()
        ex.security_policies = self._security_policies()
        ex.certificates, ex.signatures, ex.has_master_key = self._certificates(ex.warnings)
        ex.cdc_tables = self._cdc_tables(ex.warnings)
        self._unsupported_warnings(ex.warnings)
        return ex

    def _database_settings(self) -> DatabaseSettings:
        dv = "sys.databases"
        cols = {
            "ANSI_NULL_DEFAULT": "is_ansi_null_default_on", "ANSI_NULLS": "is_ansi_nulls_on",
            "ANSI_PADDING": "is_ansi_padding_on", "ANSI_WARNINGS": "is_ansi_warnings_on",
            "ARITHABORT": "is_arithabort_on", "CONCAT_NULL_YIELDS_NULL": "is_concat_null_yields_null_on",
            "NUMERIC_ROUNDABORT": "is_numeric_roundabort_on", "QUOTED_IDENTIFIER": "is_quoted_identifier_on",
            "RECURSIVE_TRIGGERS": "is_recursive_triggers_on", "CURSOR_CLOSE_ON_COMMIT": "is_cursor_close_on_commit_on",
            "AUTO_CLOSE": "is_auto_close_on", "AUTO_SHRINK": "is_auto_shrink_on",
            "AUTO_CREATE_STATISTICS": "is_auto_create_stats_on", "AUTO_UPDATE_STATISTICS": "is_auto_update_stats_on",
            "AUTO_UPDATE_STATISTICS_ASYNC": "is_auto_update_stats_async_on",
            "DATE_CORRELATION_OPTIMIZATION": "is_date_correlation_on", "TRUSTWORTHY": "is_trustworthy_on",
            "DB_CHAINING": "is_db_chaining_on", "HONOR_BROKER_PRIORITY": "is_honor_broker_priority_on",
            "MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT": "is_memory_optimized_elevate_to_snapshot_on",
        }
        present = {k: v for k, v in cols.items() if self._has_col(dv, v)}
        other = {"PARAMETERIZATION": "is_parameterization_forced", "CURSOR_DEFAULT": "is_local_cursor_default",
                 "PAGE_VERIFY": "page_verify_option_desc", "BROKER": "is_broker_enabled",
                 "TARGET_RECOVERY_TIME": "target_recovery_time_in_seconds",
                 "DELAYED_DURABILITY": "delayed_durability_desc"}
        other = {k: v for k, v in other.items() if self._has_col(dv, v)}
        names = list(present.values()) + list(other.values())
        row = self.query(
            "SELECT compatibility_level, is_read_committed_snapshot_on, snapshot_isolation_state, collation_name, "
            "SUSER_SNAME(owner_sid)" + "".join(f", {n}" for n in names)
            + " FROM sys.databases WHERE name = DB_NAME()")[0]
        st = DatabaseSettings(int(row[0]), bool(row[1]), int(row[2] or 0) == 1, str(row[3] or ""), owner=row[4])
        vals = dict(zip(names, row[5:]))
        for key, col in present.items():
            st.options[key] = ("= " if key == "MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT" else "") + (
                "ON" if _bool(vals[col]) else "OFF")
        for key, col in other.items():
            v = vals[col]
            if key == "PARAMETERIZATION":
                st.options[key] = "FORCED" if _bool(v) else "SIMPLE"
            elif key == "CURSOR_DEFAULT":
                st.options[key] = "LOCAL" if _bool(v) else "GLOBAL"
            elif key == "PAGE_VERIFY" and v:
                st.options[key] = str(v)
            elif key == "BROKER":
                st.options[key] = "ENABLE_BROKER" if _bool(v) else "DISABLE_BROKER"
            elif key == "TARGET_RECOVERY_TIME" and v is not None:
                st.options[key] = f"= {int(v)} SECONDS"
            elif key == "DELAYED_DURABILITY" and v:
                st.options[key] = f"= {v}"
        if self._view_exists("sys.database_scoped_configurations"):
            for name, value in self.query("SELECT name, CAST(value AS nvarchar(200)) FROM "
                                          "sys.database_scoped_configurations WHERE value IS NOT NULL"):
                st.scoped_configurations[str(name)] = str(value)
        if self._view_exists("sys.change_tracking_databases"):
            r = self.query("SELECT retention_period, retention_period_units_desc, is_auto_cleanup_on "
                           "FROM sys.change_tracking_databases WHERE database_id = DB_ID()")
            if r:
                st.change_tracking = (f"CHANGE_RETENTION = {int(r[0][0])} {r[0][1]}, "
                                      f"AUTO_CLEANUP = {'ON' if _bool(r[0][2]) else 'OFF'}")
        if self._view_exists("sys.database_query_store_options"):
            r = self.query("SELECT desired_state_desc FROM sys.database_query_store_options")
            if r and str(r[0][0]) in ("READ_WRITE", "READ_ONLY"):
                st.query_store = str(r[0][0])
        return st

    def _security(self):
        auth = ("dp.authentication_type" if self._has_col("sys.database_principals", "authentication_type")
                else "CASE WHEN SUSER_SNAME(dp.sid) IS NULL THEN 0 ELSE 1 END")
        skip = ["dbo", "guest", "INFORMATION_SCHEMA", "sys", "public"] + self._excluded_schemas()
        rows = self.query(f"""
            SELECT dp.principal_id, dp.name, dp.type, dp.default_schema_name, SUSER_SNAME(dp.sid), {auth},
                   dp.owning_principal_id,
                   (SELECT c.name FROM sys.certificates c WHERE c.sid = dp.sid),
                   (SELECT k.name FROM sys.asymmetric_keys k WHERE k.sid = dp.sid)
            FROM sys.database_principals dp
            WHERE dp.principal_id BETWEEN 5 AND 16383 AND dp.is_fixed_role = 0
              AND dp.name NOT IN ({', '.join(lit(s) for s in skip)})
            ORDER BY dp.principal_id""")
        names = {int(r[0]): r[1] for r in self.query("SELECT principal_id, name FROM sys.database_principals")}
        principals = []
        for pid, name, ptype, dschema, login, authtype, owner, cert, akey in rows:
            ptype = str(ptype).strip()
            certificate = None
            if ptype == "R":
                kind = "ROLE"
            elif ptype == "S":
                kind = {1: "SQL_USER", 2: "CONTAINED", 0: "NO_LOGIN"}.get(int(authtype or 0), "OTHER")
                if kind == "SQL_USER" and not login:
                    kind = "ORPHANED"
            elif ptype == "U":
                kind = "WINDOWS_USER"
            elif ptype == "G":
                kind = "WINDOWS_GROUP"
            elif ptype == "C" and cert:
                kind, certificate = "CERTIFICATE_USER", cert
            elif ptype == "K" and akey:
                kind, certificate = "ASYMMETRIC_KEY_USER", akey
            else:
                kind = "OTHER"
            principals.append(Principal(name=name, kind=kind, login=login, certificate=certificate,
                                        default_schema=dschema if dschema and dschema != "dbo" else None,
                                        owner=names.get(int(owner)) if owner is not None and kind == "ROLE"
                                        else None))
        members = [(r, m) for r, m in self.query("""
            SELECT r.name, m.name FROM sys.database_role_members rm
            JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
            JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
            WHERE m.name NOT IN ('dbo') ORDER BY r.name, m.name""") if m not in skip[5:]]
        owners = {sch: own for sch, own in self.query("""
            SELECT s.name, p.name FROM sys.schemas s JOIN sys.database_principals p ON p.principal_id = s.principal_id
            WHERE s.schema_id BETWEEN 5 AND 16383 AND p.name <> 'dbo'
              AND s.name NOT IN ('guest', 'INFORMATION_SCHEMA', 'sys')""") if sch not in skip[5:]}
        classes = {0: "DATABASE", 1: "OBJECT", 3: "SCHEMA", 6: "TYPE", 5: "ASSEMBLY", 25: "CERTIFICATE",
                   10: "XML SCHEMA COLLECTION", 15: "MESSAGE TYPE", 16: "CONTRACT", 17: "SERVICE"}
        perms = []
        for cls, perm, state, grantee, sch, obj, col in self.query("""
                SELECT p.class, p.permission_name, p.state_desc, g.name,
                       CASE p.class WHEN 1 THEN OBJECT_SCHEMA_NAME(p.major_id) WHEN 3 THEN SCHEMA_NAME(p.major_id)
                                    WHEN 6 THEN SCHEMA_NAME(ty.schema_id) WHEN 10 THEN SCHEMA_NAME(xc.schema_id) END,
                       CASE p.class WHEN 1 THEN OBJECT_NAME(p.major_id) WHEN 6 THEN ty.name
                                    WHEN 5 THEN (SELECT a.name FROM sys.assemblies a WHERE a.assembly_id = p.major_id)
                                    WHEN 25 THEN (SELECT c.name FROM sys.certificates c WHERE c.certificate_id = p.major_id)
                                    WHEN 10 THEN xc.name
                                    WHEN 15 THEN (SELECT m.name COLLATE DATABASE_DEFAULT FROM sys.service_message_types m
                                                  WHERE m.message_type_id = p.major_id)
                                    WHEN 16 THEN (SELECT c.name COLLATE DATABASE_DEFAULT FROM sys.service_contracts c
                                                  WHERE c.service_contract_id = p.major_id)
                                    WHEN 17 THEN (SELECT s.name COLLATE DATABASE_DEFAULT FROM sys.services s
                                                  WHERE s.service_id = p.major_id) END,
                       CASE WHEN p.class = 1 AND p.minor_id > 0 THEN COL_NAME(p.major_id, p.minor_id) END
                FROM sys.database_permissions p
                JOIN sys.database_principals g ON g.principal_id = p.grantee_principal_id
                LEFT JOIN sys.types ty ON p.class = 6 AND ty.user_type_id = p.major_id
                LEFT JOIN sys.xml_schema_collections xc ON p.class = 10 AND xc.xml_collection_id = p.major_id
                LEFT JOIN sys.objects o ON p.class = 1 AND o.object_id = p.major_id
                WHERE p.class IN (0, 1, 3, 5, 6, 10, 15, 16, 17, 25)
                  AND g.name NOT IN ('dbo', 'guest', 'sys', 'INFORMATION_SCHEMA')
                  AND (p.class <> 1 OR o.is_ms_shipped = 0)
                ORDER BY p.class, g.name, p.permission_name"""):
            if cls != 0 and obj is None and sch is None:
                continue
            if grantee in skip[5:] or (sch in skip[5:]):
                continue
            perms.append(Permission(state=state, permission=perm, grantee=grantee,
                                    securable_class=classes[int(cls)], schema=sch, securable=obj, column=col))
        return principals, members, owners, perms

    def _extended_properties(self) -> List[ExtendedProperty]:
        out: List[ExtendedProperty] = []
        kinds = {"U": "TABLE", "V": "VIEW", "P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION",
                 "TF": "FUNCTION", "SN": "SYNONYM", "SO": "SEQUENCE", "PC": "PROCEDURE", "FS": "FUNCTION",
                 "FT": "FUNCTION", "AF": "AGGREGATE", "SQ": "QUEUE", "R": "RULE", "D": "DEFAULT"}
        for cls, name, value, sch, obj, otype, parent, ptype, col, ixname in self.query("""
                SELECT ep.class, ep.name, CAST(ep.value AS nvarchar(4000)),
                       CASE WHEN ep.class IN (1, 7) THEN OBJECT_SCHEMA_NAME(ep.major_id)
                            WHEN ep.class = 3 THEN SCHEMA_NAME(ep.major_id) END,
                       CASE WHEN ep.class IN (1, 7) THEN OBJECT_NAME(ep.major_id) END,
                       o.type, po.name, po.type,
                       CASE WHEN ep.class = 1 AND ep.minor_id > 0 THEN COL_NAME(ep.major_id, ep.minor_id) END,
                       CASE WHEN ep.class = 7 THEN (SELECT i.name FROM sys.indexes i
                                                   WHERE i.object_id = ep.major_id AND i.index_id = ep.minor_id) END
                FROM sys.extended_properties ep
                LEFT JOIN sys.objects o ON ep.class IN (1, 7) AND o.object_id = ep.major_id
                LEFT JOIN sys.objects po ON po.object_id = o.parent_object_id
                WHERE ep.class IN (0, 1, 3, 7) AND ep.name <> 'microsoft_database_tools_support'
                  AND (o.object_id IS NULL OR o.is_ms_shipped = 0)
                ORDER BY ep.class, ep.major_id, ep.minor_id, ep.name"""):
            cls = int(cls)
            otype = str(otype or "").strip()
            ptype = str(ptype or "").strip()
            if sch in self._excluded_schemas():
                continue
            if cls == 0:
                level: Tuple[Tuple[str, str], ...] = ()
            elif cls == 3:
                level = (("SCHEMA", sch),)
            elif cls == 7:
                level = (("SCHEMA", sch), ("VIEW" if otype == "V" else "TABLE", obj), ("INDEX", ixname))
            elif otype in kinds:
                level = (("SCHEMA", sch), (kinds[otype], obj))
                if col:
                    if otype not in ("U", "V"):
                        continue          # function/procedure columns: not supported by level types
                    level += (("COLUMN", col),)
            elif otype in ("TR", "TA") and parent:
                level = (("SCHEMA", sch), ("VIEW" if ptype == "V" else "TABLE", parent), ("TRIGGER", obj))
            elif otype in ("PK", "UQ", "F", "C", "D") and parent:
                level = (("SCHEMA", sch), ("TABLE", parent), ("CONSTRAINT", obj))
            else:
                continue
            out.append(ExtendedProperty(name=name, value=value, level=level))
        return out

    def _security_policies(self) -> List[SecurityPolicy]:
        if not self._view_exists("sys.security_policies"):
            return []
        policies: Dict[int, SecurityPolicy] = {}
        for oid, sch, name, enabled, bound in self.query("""
                SELECT object_id, SCHEMA_NAME(schema_id), name, is_enabled, is_schema_bound FROM sys.security_policies"""):
            policies[oid] = SecurityPolicy(sch, name, _bool(enabled), _bool(bound))
        for oid, ptype, definition, tsch, tname, op in self.query("""
                SELECT sp.object_id, sp.predicate_type_desc, sp.predicate_definition,
                       OBJECT_SCHEMA_NAME(sp.target_object_id), OBJECT_NAME(sp.target_object_id), sp.operation_desc
                FROM sys.security_predicates sp ORDER BY sp.object_id, sp.security_predicate_id"""):
            if oid in policies:
                policies[oid].predicates.append((str(ptype), definition, tsch, tname,
                                                 str(op).replace("_", " ") if op else None))
        return list(policies.values())

    def _certificates(self, warnings: List[str]):
        has_mk = bool(self.query("SELECT COUNT(*) FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##'")[0][0])
        certs: List[Certificate] = []
        pwd = "Mom!" + secrets.token_hex(24)
        for name, owner, pub, ptype, pk in self.query(f"""
                SELECT c.name, USER_NAME(c.principal_id), CONVERT(varchar(max), CERTENCODED(c.certificate_id), 1),
                       c.pvt_key_encryption_type,
                       CONVERT(varchar(max), CERTPRIVATEKEY(c.certificate_id, {lit(pwd)}), 1)
                FROM sys.certificates c WHERE c.name NOT LIKE '##%'"""):
            if not pub:
                warnings.append(f"certificate {name}: not readable (needs VIEW DEFINITION / CONTROL)")
                continue
            if str(ptype) != "NA" and not pk:
                warnings.append(f"certificate {name}: private key can't be exported (protected by a password, or "
                                "the database master key can't be opened); only the public key is copied")
            certs.append(Certificate(name, owner if owner and owner != "dbo" else None, pub, pk or None,
                                     pwd if pk else None))
        sigs = [Signature(sch, name, cert, val, str(ct) == "CPVC") for sch, name, cert, ct, val in self.query("""
                SELECT SCHEMA_NAME(o.schema_id), o.name, c.name, cp.crypt_type, CONVERT(varchar(max), cp.crypt_property, 1)
                FROM sys.crypt_properties cp JOIN sys.objects o ON o.object_id = cp.major_id
                JOIN sys.certificates c ON c.thumbprint = cp.thumbprint
                WHERE cp.class = 1 AND cp.crypt_type IN ('SPVC', 'CPVC')""")]
        for (n,) in self.query("SELECT COUNT(*) FROM sys.crypt_properties WHERE class = 1 AND crypt_type IN ('SPVA', 'CPVA')"):
            if n:
                warnings.append(f"{n} module signatures by asymmetric keys can't be copied: re-sign those modules")
        return certs, sigs, has_mk

    def _cdc_tables(self, warnings: List[str]) -> List[CdcTable]:
        if not self._cdc_enabled():
            return []
        try:
            rows = self.query("""
                SELECT OBJECT_SCHEMA_NAME(ct.source_object_id), OBJECT_NAME(ct.source_object_id), ct.capture_instance,
                       ct.role_name, ct.supports_net_changes, ct.index_name, cc.column_name
                FROM cdc.change_tables ct JOIN cdc.captured_columns cc ON cc.object_id = ct.object_id
                ORDER BY ct.object_id, cc.column_ordinal""")
        except Exception as exc:
            warnings.append(f"change data capture is enabled but its configuration isn't readable ({exc}); "
                            "re-enable it on the target manually")
            return []
        out: Dict[str, CdcTable] = {}
        for sch, tbl, inst, role, net, idx, col in rows:
            c = out.setdefault(inst, CdcTable(sch, tbl, inst, role, _bool(net), [], idx))
            c.columns.append(col)
        return list(out.values())

    def _unsupported_warnings(self, warnings: List[str]) -> None:
        for (name,) in self.query("SELECT name FROM sys.symmetric_keys WHERE name NOT LIKE '##%'"):
            warnings.append(f"symmetric key {name} can't be exported: recreate it with the same KEY_SOURCE and "
                            "IDENTITY_VALUE, or data it encrypted can't be decrypted on the target")
        for (name,) in self.query("SELECT name FROM sys.asymmetric_keys"):
            warnings.append(f"asymmetric key {name} can't be exported by T-SQL: recreate it from its original "
                            "file or provider")
        if self._view_exists("sys.database_scoped_credentials"):
            for (name,) in self.query("SELECT name FROM sys.database_scoped_credentials"):
                warnings.append(f"database scoped credential {name}: its secret can't be read; recreate it")
        if self._view_exists("sys.external_data_sources"):
            for (name,) in self.query("SELECT name FROM sys.external_data_sources"):
                warnings.append(f"external data source {name} (PolyBase) not copied; recreate it")
        enc = self.query("SELECT is_encrypted FROM sys.databases WHERE name = DB_NAME()")[0][0]
        if _bool(enc):
            warnings.append("the source database uses TDE: enable TDE on the target if it's required there")
        for sch, name in self.query("""SELECT SCHEMA_NAME(q.schema_id), q.name FROM sys.service_queues q
                                       WHERE q.is_ms_shipped = 0"""):
            try:
                n = self.query(f"SELECT COUNT_BIG(*) FROM {q(sch)}.{q(name)}")[0][0]
            except Exception:  # pragma: no cover - permissions
                n = 0
            if n:
                warnings.append(f"queue {sch}.{name} holds {n} messages, which are not copied")

    # ---------------------------------------------------------------------------- preflight
    def can_unmask(self) -> bool:
        return bool(self.query("SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin') = 1 OR IS_MEMBER('db_owner') = 1 "
                               "OR HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UNMASK') = 1 "
                               "OR HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL') = 1 THEN 1 ELSE 0 END")[0][0])

    def rls_hidden_rows(self, tables: Sequence[Table]) -> List[Tuple[str, int, int]]:
        """(table, visible rows, stored rows) for tables under an enabled FILTER predicate where the
        connected login sees fewer rows than are stored."""
        if not self._view_exists("sys.security_predicates"):
            return []
        filtered = {(s, n) for s, n in self.query("""
            SELECT OBJECT_SCHEMA_NAME(sp.target_object_id), OBJECT_NAME(sp.target_object_id)
            FROM sys.security_predicates sp JOIN sys.security_policies p ON p.object_id = sp.object_id
            WHERE p.is_enabled = 1 AND sp.predicate_type_desc = 'FILTER'""")}
        out = []
        for t in tables:
            if (t.schema, t.name) not in filtered or t.memory_optimized:
                continue
            visible = int(self.query(f"SELECT COUNT_BIG(*) FROM {q(t.schema)}.{q(t.name)}")[0][0])
            stored = int(self.query("SELECT ISNULL(SUM(rows), 0) FROM sys.partitions WHERE object_id = OBJECT_ID(?) "
                                    "AND index_id IN (0, 1)", [f"{q(t.schema)}.{q(t.name)}"])[0][0])
            if visible != stored:
                out.append((t.fqn, visible, stored))
        return out

    def memory_optimized_filegroup(self) -> bool:
        return bool(self.query("SELECT COUNT(*) FROM sys.filegroups WHERE type = 'FX'")[0][0])

    def primary_data_dir(self) -> str:
        path = str(self.query("SELECT physical_name FROM sys.database_files WHERE file_id = 1")[0][0])
        sep = "\\" if "\\" in path else "/"
        return path.rsplit(sep, 1)[0] + sep

    # ------------------------------------------------------------------------- server level
    def server_logins(self, users: Sequence[Principal]) -> Tuple[List[Tuple[str, str]], List[str]]:
        """(login, CREATE LOGIN ...) for the logins these database users map to, with the same SID
        and password hash, plus their server role memberships; and errors for logins that can't be
        scripted (password hash not readable without CONTROL SERVER)."""
        names = sorted({u.login for u in users if u.login and u.kind in ("SQL_USER", "WINDOWS_USER", "WINDOWS_GROUP")})
        out, errors = [], []
        for name in names:
            rows = self.query("""
                SELECT sp.type, CONVERT(varchar(514), sp.sid, 1),
                       CONVERT(varchar(514), CAST(LOGINPROPERTY(sp.name, 'PasswordHash') AS varbinary(256)), 1),
                       sp.default_database_name, sl.is_policy_checked, sl.is_expiration_checked, sp.is_disabled
                FROM sys.server_principals sp LEFT JOIN sys.sql_logins sl ON sl.principal_id = sp.principal_id
                WHERE sp.name = ?""", [name])
            if not rows:
                errors.append(f"login {name} not visible on the source (needs VIEW ANY DEFINITION)")
                continue
            ptype, sid, phash, defdb, pol, exp, disabled = rows[0]
            ptype = str(ptype).strip()
            if ptype == "S":
                if not phash:
                    errors.append(f"login {name}: password hash not readable (needs CONTROL SERVER on the source)")
                    continue
                sql = (f"CREATE LOGIN {q(name)} WITH PASSWORD = {phash} HASHED, SID = {sid}, "
                       f"DEFAULT_DATABASE = [master], CHECK_POLICY = {'ON' if _bool(pol) else 'OFF'}, "
                       f"CHECK_EXPIRATION = {'ON' if _bool(exp) else 'OFF'}")
            else:
                sql = f"CREATE LOGIN {q(name)} FROM WINDOWS"
            if _bool(disabled):
                sql += f";\nALTER LOGIN {q(name)} DISABLE"
            for (role,) in self.query("""
                    SELECT r.name FROM sys.server_role_members m
                    JOIN sys.server_principals r ON r.principal_id = m.role_principal_id
                    JOIN sys.server_principals l ON l.principal_id = m.member_principal_id WHERE l.name = ?""", [name]):
                sql += f";\nALTER SERVER ROLE {q(role)} ADD MEMBER {q(name)}"
            out.append((name, sql))
        return out, errors

    def agent_jobs(self, database: str, target_database: str) -> List[Tuple[str, List[str], List[str]]]:
        """(job, msdb statements, warnings) for SQL Agent jobs with a step in ``database``. Jobs are
        created disabled, so they don't run twice before cut-over."""
        jobs = self.query("""
            SELECT j.job_id, j.name, j.description, c.name, SUSER_SNAME(j.owner_sid), j.start_step_id,
                   j.notify_level_email, j.notify_email_operator_id
            FROM msdb.dbo.sysjobs j JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
            WHERE EXISTS (SELECT 1 FROM msdb.dbo.sysjobsteps s WHERE s.job_id = j.job_id AND s.database_name = ?)""",
            [database])
        out = []
        for job_id, name, desc, cat, owner, start, notify, operator in jobs:
            warn = []
            stmts = [f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.syscategories WHERE name = {lit(cat)} AND category_class = 1) "
                     f"EXEC msdb.dbo.sp_add_category @class = N'JOB', @type = N'LOCAL', @name = {lit(cat)}",
                     f"EXEC msdb.dbo.sp_add_job @job_name = {lit(name)}, @enabled = 0, "
                     f"@description = {lit(desc or '')}, @category_name = {lit(cat)}"
                     + (f", @owner_login_name = {lit(owner)}" if owner else "")]
            if operator:
                warn.append(f"job {name}: e-mail notifications to operators are not copied")
            for (sid, sname, sub, cmd, db, dbuser, ok_act, ok_step, fail_act, fail_step, retries, interval,
                 outfile, flags, proxy) in self.query("""
                    SELECT step_id, step_name, subsystem, command, database_name, database_user_name,
                           on_success_action, on_success_step_id, on_fail_action, on_fail_step_id, retry_attempts,
                           retry_interval, output_file_name, flags, proxy_id
                    FROM msdb.dbo.sysjobsteps WHERE job_id = ? ORDER BY step_id""", [job_id]):
                dbn = target_database if db == database else db
                if proxy:
                    warn.append(f"job {name} step {sid}: runs under a proxy, which is not copied")
                stmts.append(
                    f"EXEC msdb.dbo.sp_add_jobstep @job_name = {lit(name)}, @step_id = {int(sid)}, "
                    f"@step_name = {lit(sname)}, @subsystem = {lit(sub)}, @command = {lit(cmd or '')}, "
                    + (f"@database_name = {lit(dbn)}, " if dbn else "")
                    + (f"@database_user_name = {lit(dbuser)}, " if dbuser else "")
                    + f"@on_success_action = {int(ok_act)}, @on_success_step_id = {int(ok_step)}, "
                    f"@on_fail_action = {int(fail_act)}, @on_fail_step_id = {int(fail_step)}, "
                    f"@retry_attempts = {int(retries)}, @retry_interval = {int(interval)}, @flags = {int(flags)}"
                    + (f", @output_file_name = {lit(outfile)}" if outfile else ""))
            stmts.append(f"EXEC msdb.dbo.sp_update_job @job_name = {lit(name)}, @start_step_id = {int(start)}")
            for row in self.query("""
                    SELECT s.name, s.enabled, s.freq_type, s.freq_interval, s.freq_subday_type, s.freq_subday_interval,
                           s.freq_relative_interval, s.freq_recurrence_factor, s.active_start_date, s.active_end_date,
                           s.active_start_time, s.active_end_time
                    FROM msdb.dbo.sysjobschedules js JOIN msdb.dbo.sysschedules s ON s.schedule_id = js.schedule_id
                    WHERE js.job_id = ?""", [job_id]):
                (sname, en, ft, fi, fst, fsi, fri, frf, asd, aed, ast, aet) = row
                stmts.append(
                    f"EXEC msdb.dbo.sp_add_jobschedule @job_name = {lit(name)}, @name = {lit(sname)}, "
                    f"@enabled = {int(en)}, @freq_type = {int(ft)}, @freq_interval = {int(fi)}, "
                    f"@freq_subday_type = {int(fst)}, @freq_subday_interval = {int(fsi)}, "
                    f"@freq_relative_interval = {int(fri)}, @freq_recurrence_factor = {int(frf)}, "
                    f"@active_start_date = {int(asd)}, @active_end_date = {int(aed)}, "
                    f"@active_start_time = {int(ast)}, @active_end_time = {int(aet)}")
            stmts.append(f"EXEC msdb.dbo.sp_add_jobserver @job_name = {lit(name)}, @server_name = N'(local)'")
            out.append((name, stmts, warn))
        return out

    def linked_servers(self) -> List[Tuple[str, List[str], List[str]]]:
        """(name, statements, warnings) for every linked server. Remote passwords can't be read."""
        out = []
        for (sid, name, product, provider, src, location, provstr, catalog, rpc, rpcout, data, collcompat,
             remotecoll, collname, ctimeout, qtimeout) in self.query("""
                SELECT server_id, name, product, provider, data_source, location, provider_string, catalog,
                       is_remote_login_enabled, is_rpc_out_enabled, is_data_access_enabled, is_collation_compatible,
                       uses_remote_collation, collation_name, connect_timeout, query_timeout
                FROM sys.servers WHERE is_linked = 1"""):
            if str(product) == "SQL Server":
                stmts = [f"EXEC master.dbo.sp_addlinkedserver @server = {lit(name)}, @srvproduct = N'SQL Server'"]
            else:
                args = [f"@server = {lit(name)}", f"@srvproduct = {lit(product or '')}", f"@provider = {lit(provider)}"]
                for arg, val in (("@datasrc", src), ("@location", location), ("@provstr", provstr), ("@catalog", catalog)):
                    if val:
                        args.append(f"{arg} = {lit(val)}")
                stmts = ["EXEC master.dbo.sp_addlinkedserver " + ", ".join(args)]
            for opt, val in (("rpc", rpc), ("rpc out", rpcout), ("data access", data),
                             ("collation compatible", collcompat), ("use remote collation", remotecoll)):
                stmts.append(f"EXEC master.dbo.sp_serveroption {lit(name)}, N'{opt}', N'{'true' if _bool(val) else 'false'}'")
            if collname:
                stmts.append(f"EXEC master.dbo.sp_serveroption {lit(name)}, N'collation name', {lit(collname)}")
            for opt, val in (("connect timeout", ctimeout), ("query timeout", qtimeout)):
                if val:
                    stmts.append(f"EXEC master.dbo.sp_serveroption {lit(name)}, N'{opt}', N'{int(val)}'")
            warn = []
            for local, self_cred, remote in self.query("""
                    SELECT SUSER_SNAME(ll.local_principal_id), ll.uses_self_credential, ll.remote_name
                    FROM sys.linked_logins ll WHERE ll.server_id = ?""", [sid]):
                if _bool(self_cred):
                    stmts.append(f"EXEC master.dbo.sp_addlinkedsrvlogin @rmtsrvname = {lit(name)}, @useself = N'True', "
                                 f"@locallogin = {lit(local) if local else 'NULL'}")
                else:
                    warn.append(f"linked server {name}: mapping {local or '(all logins)'} -> {remote} needs its "
                                f"password set with sp_addlinkedsrvlogin")
            out.append((name, stmts, warn))
        return out
