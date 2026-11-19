"""Driver-independent row fingerprints for exact data comparison."""
from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import uuid
from typing import Any, Callable, List, Sequence

from .model import Column

_NULL = b"\x00N"


def _join(parts: Any) -> bytes:
    """Length-prefix every field so no two different rows encode the same (a value may contain any byte)."""
    return b"".join(len(p).to_bytes(4, "big") + p for p in parts)


def _norm_default(v: Any) -> bytes:
    if v is None:
        return _NULL
    if isinstance(v, bool):
        return b"b1" if v else b"b0"
    if isinstance(v, int):
        return b"i" + str(v).encode()
    if isinstance(v, float):
        return b"f" + repr(v).encode()
    if isinstance(v, decimal.Decimal):
        return b"d" + _dec(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return b"x" + bytes(v)
    if isinstance(v, uuid.UUID):
        return b"u" + str(v).encode()
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return b"t" + v.isoformat().encode()
    if isinstance(v, str):
        return b"s" + v.encode("utf-8", "surrogatepass")
    return b"r" + repr(v).encode()


def _dec(v: decimal.Decimal) -> bytes:
    if v == 0:
        return b"0"
    return format(v.normalize(), "f").encode()


def _for(c: Column) -> Callable[[Any], bytes]:
    t = c.type_name.lower()
    if t == "uniqueidentifier":
        return lambda v: _NULL if v is None else b"u" + str(uuid.UUID(str(v))).encode()
    if t in ("decimal", "numeric", "money", "smallmoney"):
        return lambda v: _NULL if v is None else b"d" + _dec(decimal.Decimal(str(v)))
    if t in ("float", "real"):
        return lambda v: _NULL if v is None else b"f" + repr(float(v)).encode()
    if t == "bit":
        return lambda v: _NULL if v is None else (b"b1" if v else b"b0")
    if t in ("tinyint", "smallint", "int", "bigint"):
        return lambda v: _NULL if v is None else b"i" + str(int(v)).encode()
    return _norm_default


class RowHasher:
    def __init__(self, columns: Sequence[Column], key_columns: Sequence[str]) -> None:
        self.norms = [_for(c) for c in columns]
        names = [c.name for c in columns]
        self.key_pos: List[int] = [names.index(k) for k in key_columns]

    def encode(self, row: Sequence[Any]) -> bytes:
        return _join(f(v) for f, v in zip(self.norms, row))

    def digest(self, row: Sequence[Any]) -> bytes:
        return hashlib.sha256(self.encode(row)).digest()

    def key(self, row: Sequence[Any]) -> bytes:
        return _join(self.norms[i](row[i]) for i in self.key_pos)

    def display_key(self, row: Sequence[Any]) -> str:
        return ", ".join(repr(row[i]) for i in self.key_pos)
