"""Key-range chunking: turns sorted boundary keys into [lower, upper) chunks and SQL predicates.

Composite keys are handled with lexicographic comparisons, so any unique, non-null key
(single or multi-column) partitions a table exactly — no row lands in two chunks or none.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

from .model import Chunk

Quote = Callable[[str], str]


def build_chunks(table_fqn: str, boundaries: Sequence[Tuple[Any, ...]]) -> List[Chunk]:
    """``boundaries`` are the first keys of chunks 2..n (sorted ascending)."""
    edges: List[Optional[Tuple[Any, ...]]] = [None, *[tuple(b) for b in boundaries], None]
    return [Chunk(table=table_fqn, index=i, lower=edges[i], upper=edges[i + 1])
            for i in range(len(edges) - 1)]


def _lex(cols: Sequence[str], values: Sequence[Any], strict_op: str, last_op: str,
         quote: Quote, ph: Callable[[str], str]) -> Tuple[str, List[Any]]:
    """(c1 strict v1) OR (c1 = v1 AND c2 strict v2) OR ... OR (c1=v1 ... AND cn last_op vn)."""
    terms, params = [], []
    for i in range(len(cols)):
        parts, p = [], []
        for j in range(i):
            parts.append(f"{quote(cols[j])} = {ph(cols[j])}")
            p.append(values[j])
        op = last_op if i == len(cols) - 1 else strict_op
        parts.append(f"{quote(cols[i])} {op} {ph(cols[i])}")
        p.append(values[i])
        terms.append("(" + " AND ".join(parts) + ")")
        params.extend(p)
    return ("(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0]), params


def predicate(chunk: Chunk, key_cols: Sequence[str], quote: Quote,
              placeholder: Optional[Callable[[str], str]] = None) -> Tuple[str, List[Any]]:
    """WHERE-clause body (without 'WHERE') and parameters for a chunk; '1=1' if unbounded.

    ``placeholder(col)`` lets a dialect wrap the parameter, e.g. CAST(? AS varchar(20)) so a
    varchar key is compared without converting the column (keeps index seeks)."""
    ph = placeholder or (lambda _c: "?")
    clauses, params = [], []
    lead = quote(key_cols[0]) if key_cols else ""
    if chunk.lower is not None:
        if len(key_cols) > 1:   # redundant but sargable: lets the optimizer seek on the leading column
            clauses.append(f"{lead} >= {ph(key_cols[0])}")
            params.append(chunk.lower[0])
        sql, p = _lex(key_cols, chunk.lower, ">", ">=", quote, ph)
        clauses.append(sql)
        params.extend(p)
    if chunk.upper is not None:
        if len(key_cols) > 1:
            clauses.append(f"{lead} <= {ph(key_cols[0])}")
            params.append(chunk.upper[0])
        sql, p = _lex(key_cols, chunk.upper, "<", "<", quote, ph)
        clauses.append(sql)
        params.extend(p)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def chunk_count(rows: int, chunk_rows: int) -> int:
    return max(1, -(-rows // chunk_rows))
