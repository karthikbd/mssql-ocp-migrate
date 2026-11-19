"""The interface the engine talks to. SQL Server implements it in ``sqlserver.py``;
tests implement it on SQLite so the orchestration logic can be exercised anywhere."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .model import Chunk, Column, DatabaseExtras, Table


@dataclass
class ModuleDef:
    """A programmable object copied verbatim: view, procedure, function, trigger, synonym, sequence."""
    kind: str            # VIEW | PROCEDURE | FUNCTION | TRIGGER | SYNONYM | SEQUENCE | TYPE | ASSEMBLY | ...
    schema: str
    name: str
    definition: Optional[str]
    parent: Optional[str] = None     # schema.table for triggers
    disabled: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)   # kind-specific extras (assembly hash, filegroups)

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}"


class Database(ABC):
    label: str = "db"

    # -- metadata ------------------------------------------------------------
    @abstractmethod
    def list_tables(self) -> List[Table]: ...

    def list_modules(self) -> List[ModuleDef]:
        return []

    def list_schemas(self) -> List[str]:
        return []

    def database_extras(self) -> Optional["DatabaseExtras"]:
        """Database settings, principals, permissions and extended properties (None if unsupported)."""
        return None

    @abstractmethod
    def quote(self, name: str) -> str: ...

    def qualified(self, table: Table) -> str:
        return f"{self.quote(table.schema)}.{self.quote(table.name)}"

    @abstractmethod
    def table_exists(self, table: Table) -> bool: ...

    # -- data ------------------------------------------------------------------
    @abstractmethod
    def boundaries(self, table: Table, chunk_rows: int) -> List[Tuple[Any, ...]]: ...

    @abstractmethod
    def read_rows(self, table: Table, chunk: Chunk, batch: int, ordered: bool = True,
                  columns: Optional[Sequence[Column]] = None) -> Iterator[List[Tuple[Any, ...]]]:
        """Rows of ``columns`` (default: table.insert_columns) inside the chunk's key range."""

    @abstractmethod
    def count_rows(self, table: Table, chunk: Optional[Chunk] = None) -> int: ...

    @abstractmethod
    def checksum(self, table: Table, chunk: Chunk) -> Tuple[int, Any]: ...

    @abstractmethod
    def delete_range(self, table: Table, chunk: Chunk) -> None: ...

    @abstractmethod
    def truncate(self, table: Table) -> None: ...

    @abstractmethod
    def bulk_load(self, table: Table, batches: Iterable[Sequence[Tuple[Any, ...]]], tablock: bool) -> int: ...

    def prepare(self, table: Table) -> None:
        """Hook called before a chunk transaction starts (e.g. one-off capability probes)."""

    # -- identity --------------------------------------------------------------
    def identity_current(self, table: Table) -> Optional[int]:
        return None

    def reseed_identity(self, table: Table, value: int) -> None:
        pass

    # -- plumbing --------------------------------------------------------------
    @abstractmethod
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def describe(self) -> str:
        return self.label
