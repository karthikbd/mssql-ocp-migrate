"""Plain data structures shared by every module (no database code here)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# SQL Server types whose values cannot be inserted directly, or which differ by design.
NON_INSERTABLE_TYPES = {"timestamp", "rowversion"}
# CLR types are read as varbinary and written back as varbinary (server converts).
CLR_TYPES = {"geography", "geometry", "hierarchyid"}
LOB_LEGACY_TYPES = {"text", "ntext", "image"}


@dataclass
class Column:
    name: str
    type_name: str               # base type, e.g. "nvarchar", "decimal", "geography"
    max_length: int = 0          # bytes as reported by sys.columns (-1 = max)
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    is_identity: bool = False
    identity_seed: Optional[int] = None
    identity_increment: Optional[int] = None
    is_computed: bool = False
    computed_definition: Optional[str] = None
    computed_persisted: bool = False
    collation: Optional[str] = None
    default_name: Optional[str] = None
    default_definition: Optional[str] = None
    user_type: Optional[str] = None   # alias/user-defined type name, if any
    user_type_schema: Optional[str] = None
    clr_udt: bool = False             # a user-defined CLR type (user_type/user_type_schema name it)
    is_sparse: bool = False
    is_column_set: bool = False       # xml COLUMN_SET FOR ALL_SPARSE_COLUMNS (derived, never inserted)
    masking_function: Optional[str] = None      # dynamic data masking, e.g. "partial(1, \"XX\", 0)"
    encryption_key: Optional[str] = None        # Always Encrypted column encryption key
    encryption_type: Optional[str] = None       # DETERMINISTIC | RANDOMIZED
    encryption_algorithm: Optional[str] = None
    generated_always: str = ""        # ROW START | ROW END (temporal period columns)
    is_hidden: bool = False
    xml_schema: Optional[str] = None  # typed xml: "schema.collection"
    xml_document: bool = False        # typed xml DOCUMENT (else CONTENT)

    @property
    def insertable(self) -> bool:
        return (not self.is_computed and not self.is_column_set
                and self.type_name.lower() not in NON_INSERTABLE_TYPES)

    @property
    def comparable(self) -> bool:
        """Columns included in data validation (rowversion differs by design; a column set is
        derived from the sparse columns, which are compared themselves)."""
        return self.type_name.lower() not in NON_INSERTABLE_TYPES and not self.is_column_set

    @property
    def is_clr(self) -> bool:
        return self.clr_udt or self.type_name.lower() in CLR_TYPES

    @property
    def encrypted(self) -> bool:
        return bool(self.encryption_key)


@dataclass
class Index:
    name: str
    columns: List[Tuple[str, bool]]         # (column, descending)
    included: List[str] = field(default_factory=list)
    unique: bool = False
    clustered: bool = False
    is_primary_key: bool = False
    is_unique_constraint: bool = False
    filter_definition: Optional[str] = None
    type_desc: str = "NONCLUSTERED"        # CLUSTERED / NONCLUSTERED / CLUSTERED COLUMNSTORE ...
    # storage and behaviour options (sys.indexes / sys.partitions)
    fill_factor: int = 0                   # 0 = server default (same as 100)
    is_padded: bool = False                # PAD_INDEX
    ignore_dup_key: bool = False
    allow_row_locks: bool = True
    allow_page_locks: bool = True
    data_compression: str = "NONE"         # NONE | ROW | PAGE | COLUMNSTORE | COLUMNSTORE_ARCHIVE
    partition_compression: Dict[int, str] = field(default_factory=dict)   # only when partitions differ
    partition_scheme: Optional[str] = None  # ON scheme(column) when the index is partitioned
    partition_column: Optional[str] = None
    is_hash: bool = False                   # memory-optimized HASH index
    bucket_count: int = 0

    def options(self) -> dict:
        """The storage/behaviour options that must survive a migration (fill factor 100 == 0)."""
        return {"fill_factor": 0 if self.fill_factor in (0, 100) else self.fill_factor,
                "pad_index": self.is_padded, "ignore_dup_key": self.ignore_dup_key,
                "allow_row_locks": self.allow_row_locks, "allow_page_locks": self.allow_page_locks,
                "data_compression": self.data_compression,
                "partition_compression": dict(sorted(self.partition_compression.items())),
                "partition_scheme": self.partition_scheme, "hash_bucket_count": self.bucket_count}


@dataclass
class ForeignKey:
    name: str
    columns: List[str]
    ref_schema: str
    ref_table: str
    ref_columns: List[str]
    on_delete: str = "NO_ACTION"
    on_update: str = "NO_ACTION"
    disabled: bool = False
    not_trusted: bool = False


@dataclass
class CheckConstraint:
    name: str
    definition: str
    disabled: bool = False


@dataclass
class Statistic:
    """A user-created statistics object (``CREATE STATISTICS``); index statistics come with indexes."""
    name: str
    columns: List[str]
    filter_definition: Optional[str] = None
    no_recompute: bool = False


@dataclass
class XmlIndex:
    name: str
    column: str
    primary: bool = True
    using: Optional[str] = None        # secondary: the primary XML index it builds on
    secondary_type: Optional[str] = None   # PATH | VALUE | PROPERTY


@dataclass
class SpatialIndex:
    name: str
    column: str
    tessellation: str                  # GEOMETRY_GRID | GEOGRAPHY_GRID | GEOMETRY_AUTO_GRID | GEOGRAPHY_AUTO_GRID
    bounding_box: Optional[Tuple[float, float, float, float]] = None
    grids: Tuple[str, ...] = ()        # LEVEL_1..LEVEL_4 densities (manual grids only)
    cells_per_object: int = 16


@dataclass
class FullTextIndex:
    catalog: str
    key_index: str
    columns: List[Tuple[str, Optional[int], Optional[str], bool]]   # (column, language, type column, semantics)
    change_tracking: str = "AUTO"      # AUTO | MANUAL | OFF
    stoplist: Optional[str] = "SYSTEM"  # SYSTEM | OFF (None) | a stoplist name


@dataclass
class Temporal:
    """System versioning, re-enabled after the load (period values are copied as data)."""
    period_start: str
    period_end: str
    history_schema: str
    history_table: str
    retention: Optional[str] = None    # e.g. "6 MONTHS"; None = infinite


@dataclass
class Table:
    schema: str
    name: str
    columns: List[Column] = field(default_factory=list)
    primary_key: Optional[Index] = None
    indexes: List[Index] = field(default_factory=list)          # excluding the PK
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    checks: List[CheckConstraint] = field(default_factory=list)
    statistics: List[Statistic] = field(default_factory=list)   # user-created only
    heap_compression: str = "NONE"         # data compression of a heap (no clustered index)
    heap_partition_compression: Dict[int, str] = field(default_factory=dict)
    partition_scheme: Optional[str] = None  # heap placement ON scheme(column)
    partition_column: Optional[str] = None
    memory_optimized: bool = False
    durability: str = "SCHEMA_AND_DATA"
    temporal: Optional[Temporal] = None
    xml_indexes: List[XmlIndex] = field(default_factory=list)
    spatial_indexes: List[SpatialIndex] = field(default_factory=list)
    fulltext: Optional[FullTextIndex] = None
    change_tracking: Optional[bool] = None   # None = off; else TRACK_COLUMNS_UPDATED
    row_count: int = 0
    reserved_mb: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def key_columns(self) -> List[str]:
        """Columns used for chunking/ordering: PK, else first unique index, else none."""
        def usable(cols):           # an encrypted key can't be compared or ordered on the server
            return all(not self.column(c).encrypted for c in cols)
        if self.primary_key:
            cols = [c for c, _ in self.primary_key.columns]
            return cols if usable(cols) else []
        for ix in self.indexes:
            if ix.unique and not ix.filter_definition and "COLUMNSTORE" not in ix.type_desc:
                cols = [c for c, _ in ix.columns]
                if all(not self.column(c).nullable for c in cols) and usable(cols):
                    return cols
        return []

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def insert_columns(self) -> List[Column]:
        return [c for c in self.columns if c.insertable]

    @property
    def compare_columns(self) -> List[Column]:
        return [c for c in self.columns if c.comparable]

    @property
    def has_identity(self) -> bool:
        return any(c.is_identity for c in self.columns)

    @property
    def has_encrypted(self) -> bool:
        return any(c.encrypted for c in self.columns)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Chunk:
    """A key range [lower, upper) of a table. ``None`` bounds mean open-ended."""
    table: str                  # schema.table
    index: int
    lower: Optional[Tuple[Any, ...]] = None
    upper: Optional[Tuple[Any, ...]] = None

    @property
    def id(self) -> str:
        return f"{self.table}#{self.index}"


@dataclass
class ChunkResult:
    chunk_id: str
    rows: int
    seconds: float
    status: str = "done"        # done | failed
    error: Optional[str] = None


# ------------------------------------------------------------------ database-level objects
@dataclass
class ExtendedProperty:
    """``sp_addextendedproperty`` arguments. ``level`` holds up to three (type, name) pairs,
    e.g. (("SCHEMA", "sales"), ("TABLE", "Order"), ("COLUMN", "Amount")); empty = database."""
    name: str
    value: Optional[str]
    level: Tuple[Tuple[str, str], ...] = ()

    @property
    def key(self) -> Tuple[Any, ...]:
        return (self.level, self.name)


@dataclass
class Principal:
    """A database user or user-defined role."""
    name: str
    kind: str                       # ROLE | SQL_USER | WINDOWS_USER | WINDOWS_GROUP | NO_LOGIN | CONTAINED | OTHER
    login: Optional[str] = None     # server login the user maps to (by name)
    certificate: Optional[str] = None   # certificate or asymmetric key the user is created from
    default_schema: Optional[str] = None
    owner: Optional[str] = None     # roles only


@dataclass
class Permission:
    """One row of sys.database_permissions, already resolved to names."""
    state: str                      # GRANT | DENY | GRANT_WITH_GRANT_OPTION
    permission: str                 # e.g. SELECT, EXECUTE, VIEW DEFINITION
    grantee: str
    securable_class: str            # DATABASE | SCHEMA | OBJECT | TYPE
    schema: Optional[str] = None    # SCHEMA: the schema; OBJECT/TYPE: the object's schema
    securable: Optional[str] = None  # OBJECT/TYPE: the object or type name
    column: Optional[str] = None

    @property
    def key(self) -> Tuple[Any, ...]:
        return (self.state, self.permission, self.grantee, self.securable_class, self.schema,
                self.securable, self.column)


@dataclass
class DatabaseSettings:
    compatibility_level: int = 0
    read_committed_snapshot: bool = False
    snapshot_isolation: bool = False
    collation: str = ""
    # other ALTER DATABASE ... SET options, keyword -> value, e.g. {"ANSI_NULLS": "ON", "PAGE_VERIFY": "CHECKSUM"}
    options: Dict[str, str] = field(default_factory=dict)
    scoped_configurations: Dict[str, str] = field(default_factory=dict)   # ALTER DATABASE SCOPED CONFIGURATION
    owner: Optional[str] = None                    # login that owns the database
    change_tracking: Optional[str] = None          # e.g. "CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON"
    query_store: Optional[str] = None              # READ_WRITE | READ_ONLY | None (off)


@dataclass
class SecurityPolicy:
    """Row-level security: ``predicates`` are (FILTER|BLOCK, predicate SQL, schema, table, operation)."""
    schema: str
    name: str
    enabled: bool = True
    schemabinding: bool = True
    predicates: List[Tuple[str, str, str, str, Optional[str]]] = field(default_factory=list)


@dataclass
class Certificate:
    name: str
    owner: Optional[str]
    public: str                         # 0x... (CERTENCODED)
    private_key: Optional[str] = None   # 0x... encrypted with ``password`` (CERTPRIVATEKEY)
    password: Optional[str] = None      # one-time password protecting ``private_key`` in transit


@dataclass
class Signature:
    """ADD [COUNTER] SIGNATURE TO an object BY CERTIFICATE, with the existing signature value."""
    schema: str
    name: str
    certificate: str
    value: str                          # 0x...
    counter: bool = False


@dataclass
class CdcTable:
    schema: str
    table: str
    capture_instance: str
    role: Optional[str]
    net_changes: bool
    columns: List[str]
    index: Optional[str] = None


@dataclass
class ServerObjects:
    """Server-level items the database depends on (copied only when switched on)."""
    logins: List[str] = field(default_factory=list)          # CREATE LOGIN statements
    login_names: List[str] = field(default_factory=list)
    jobs: List[Tuple[str, List[str]]] = field(default_factory=list)          # (job, msdb statements)
    linked_servers: List[Tuple[str, List[str], List[str]]] = field(default_factory=list)  # (name, sql, warnings)


@dataclass
class DatabaseExtras:
    """Everything outside tables and code that a database-level migration should carry over."""
    settings: DatabaseSettings = field(default_factory=DatabaseSettings)
    principals: List[Principal] = field(default_factory=list)
    role_members: List[Tuple[str, str]] = field(default_factory=list)   # (role, member)
    schema_owners: Dict[str, str] = field(default_factory=dict)         # schema -> owner
    permissions: List[Permission] = field(default_factory=list)
    extended_properties: List[ExtendedProperty] = field(default_factory=list)
    security_policies: List[SecurityPolicy] = field(default_factory=list)
    certificates: List[Certificate] = field(default_factory=list)
    signatures: List[Signature] = field(default_factory=list)
    has_master_key: bool = False
    cdc_tables: List[CdcTable] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)      # things that can't be copied (keys, credentials)
