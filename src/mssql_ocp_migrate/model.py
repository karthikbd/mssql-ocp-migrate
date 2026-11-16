"""Plain data structures shared by every module (no database code here)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, List, Optional, Tuple

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

    @property
    def insertable(self) -> bool:
        return not self.is_computed and self.type_name.lower() not in NON_INSERTABLE_TYPES

    @property
    def comparable(self) -> bool:
        """Columns included in data validation (rowversion differs by design)."""
        return self.type_name.lower() not in NON_INSERTABLE_TYPES

    @property
    def is_clr(self) -> bool:
        return self.type_name.lower() in CLR_TYPES


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
class Table:
    schema: str
    name: str
    columns: List[Column] = field(default_factory=list)
    primary_key: Optional[Index] = None
    indexes: List[Index] = field(default_factory=list)          # excluding the PK
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    checks: List[CheckConstraint] = field(default_factory=list)
    row_count: int = 0
    reserved_mb: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def key_columns(self) -> List[str]:
        """Columns used for chunking/ordering: PK, else first unique index, else none."""
        if self.primary_key:
            return [c for c, _ in self.primary_key.columns]
        for ix in self.indexes:
            if ix.unique and not ix.filter_definition:
                cols = [c for c, _ in ix.columns]
                if all(not self.column(c).nullable for c in cols):
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
