"""Checkpoint file: which chunks are done, so an interrupted run resumes where it stopped."""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import Chunk


def _enc(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return {"$t": "dec", "v": str(v)}
    if isinstance(v, uuid.UUID):
        return {"$t": "uuid", "v": str(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$t": "bin", "v": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, _dt.datetime):
        return {"$t": "dt", "v": v.isoformat()}
    if isinstance(v, _dt.date):
        return {"$t": "date", "v": v.isoformat()}
    if isinstance(v, _dt.time):
        return {"$t": "time", "v": v.isoformat()}
    raise TypeError(f"cannot checkpoint key value of type {type(v).__name__}")


def _dec(v: Any) -> Any:
    if not isinstance(v, dict) or "$t" not in v:
        return v
    t, s = v["$t"], v["v"]
    conv: Dict[str, Callable[[Any], Any]] = {"dec": decimal.Decimal, "uuid": uuid.UUID,
            "bin": lambda x: base64.b64decode(x),
            "dt": _dt.datetime.fromisoformat, "date": _dt.date.fromisoformat,
            "time": _dt.time.fromisoformat}
    return conv[t](s)


def encode_key(key: Optional[Tuple[Any, ...]]) -> Optional[List[Any]]:
    return None if key is None else [_enc(v) for v in key]


def decode_key(key: Optional[List[Any]]) -> Optional[Tuple[Any, ...]]:
    return None if key is None else tuple(_dec(v) for v in key)


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"version": 1, "created": time.time(), "tables": {}, "phases": {}}

    @classmethod
    def load_or_new(cls, path: str) -> "State":
        st = cls(path)
        if st.path.exists():
            st.data = json.loads(st.path.read_text(encoding="utf-8"))
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, default=str)
        os.replace(tmp, self.path)

    # phases --------------------------------------------------------------------
    def phase_done(self, name: str) -> bool:
        return bool(self.data["phases"].get(name))

    def mark_phase(self, name: str) -> None:
        self.data["phases"][name] = time.time()
        self.save()

    # chunks --------------------------------------------------------------------
    def set_chunks(self, table: str, chunks: List[Chunk]) -> None:
        self.data["tables"][table] = {
            "chunks": {str(c.index): {"lower": encode_key(c.lower), "upper": encode_key(c.upper),
                                      "status": "pending", "rows": 0, "seconds": 0.0}
                       for c in chunks}}

    def chunks(self, table: str) -> Optional[List[Chunk]]:
        t = self.data["tables"].get(table)
        if not t:
            return None
        return [Chunk(table=table, index=int(i), lower=decode_key(c["lower"]), upper=decode_key(c["upper"]))
                for i, c in sorted(t["chunks"].items(), key=lambda kv: int(kv[0]))]

    def chunk_done(self, chunk: Chunk) -> bool:
        t = self.data["tables"].get(chunk.table, {}).get("chunks", {})
        return t.get(str(chunk.index), {}).get("status") == "done"

    def record(self, chunk: Chunk, rows: int, seconds: float, status: str, error: Optional[str] = None) -> None:
        entry = self.data["tables"][chunk.table]["chunks"][str(chunk.index)]
        entry.update(rows=rows, seconds=round(seconds, 3), status=status, error=error)

    def table_stats(self, table: str) -> Dict[str, Any]:
        cs = self.data["tables"].get(table, {}).get("chunks", {}).values()
        return {"chunks": len(cs), "done": sum(1 for c in cs if c["status"] == "done"),
                "rows": sum(c["rows"] for c in cs), "seconds": sum(c["seconds"] for c in cs)}
