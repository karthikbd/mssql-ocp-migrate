"""Database connections.

Three drivers are supported behind one small interface:

* ``pytds``   – bundled (``mssql_ocp_migrate._vendor.pytds``), pure Python, always available.
* ``pymssql`` – used only if *already installed*. Its wheels carry FreeTDS (C) inside, so no
  system driver is needed; bulk loads use FreeTDS bulk copy.
* ``pyodbc``  – used only if *already installed* together with a Microsoft ODBC driver.

None of them is ever installed by this package. ``driver = "auto"`` picks pyodbc (when an ODBC
driver is registered), then pymssql, then the bundled pytds.
"""
from __future__ import annotations

import datetime as _dt
import logging
import struct
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import ConnectionConfig

log = logging.getLogger(__name__)


class DriverError(RuntimeError):
    pass


def _pyodbc_usable(cfg: ConnectionConfig) -> bool:
    try:
        import pyodbc
    except Exception:
        return False
    try:
        return cfg.odbc_driver in pyodbc.drivers()
    except Exception:
        return False


def _pymssql_usable() -> bool:
    try:
        import pymssql  # noqa: F401  (optional, user-provided)
        return True
    except Exception:
        return False


def choose_driver(cfg: ConnectionConfig) -> str:
    if cfg.driver != "auto":
        return cfg.driver
    if _pyodbc_usable(cfg):
        return "pyodbc"
    return "pymssql" if _pymssql_usable() else "pytds"


class Connection:
    """Thin DB-API wrapper. SQL is always written with ``?`` placeholders."""

    def __init__(self, raw: Any, driver: str, cfg: ConnectionConfig, autocommit: bool) -> None:
        self.raw = raw
        self.driver = driver
        self.cfg = cfg
        self.autocommit = autocommit

    # -- helpers ------------------------------------------------------------
    def _sql(self, sql: str, params: Optional[Sequence[Any]]) -> str:
        if self.driver == "pytds" and params:
            return sql.replace("%", "%%").replace("?", "%s")
        if self.driver == "pymssql" and params:
            return sql.replace("?", "%s")        # pymssql leaves bare % alone and does not unescape %%
        return sql

    def _params(self, params: Sequence[Any]) -> Tuple[Any, ...]:
        if self.driver == "pymssql":
            # pymssql quotes bytes as a *text* literal ('...'); only bytearray becomes 0x... binary
            return tuple(bytearray(v) if isinstance(v, (bytes, memoryview)) else pymssql_value(v) for v in params)
        if self.driver == "pytds":
            # the bundled driver sends plain bytes as *text* (bytes_to_unicode); Binary keeps them binary
            from ._vendor.pytds.tds_types import Binary
            return tuple(Binary(bytes(v)) if isinstance(v, (bytes, bytearray, memoryview)) else v
                         for v in params)
        return tuple(params)

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> int:
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(self._sql(sql, params), self._params(params))
            else:
                cur.execute(sql)
            count = cur.rowcount
            # Drain every remaining result/info stream before closing. BACKUP/RESTORE ... WITH STATS
            # emit many DONE_IN_PROC tokens; closing early could cancel a multi-hour operation.
            # Server errors raised while draining (e.g. a backup failing at 80%) must propagate.
            try:
                while cur.nextset():
                    pass
            except Exception as exc:
                msg = str(exc).lower()
                if not any(s in msg for s in ("no results", "not a query", "previous sql")):
                    raise
            return int(count)
        finally:
            cur.close()

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(self._sql(sql, params), self._params(params))
            else:
                cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    def scalar(self, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
        rows = self.query(sql, params)
        return rows[0][0] if rows else None

    def stream(self, sql: str, params: Optional[Sequence[Any]] = None,
               batch: int = 10_000) -> Iterator[List[Tuple[Any, ...]]]:
        cur = self.raw.cursor()
        try:
            if params:
                cur.execute(self._sql(sql, params), self._params(params))
            else:
                cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                yield [tuple(r) for r in rows]
        finally:
            cur.close()

    def commit(self) -> None:
        if not self.autocommit:
            self.raw.commit()

    def rollback(self) -> None:
        if not self.autocommit:
            try:
                self.raw.rollback()
            except Exception:
                log.debug("ignored error", exc_info=True)

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            log.debug("ignored error", exc_info=True)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ----------------------------------------------------------------------- pytds
def _connect_pytds(cfg: ConnectionConfig, autocommit: bool) -> Any:
    from ._vendor import pytds
    from ._vendor.pytds import tls

    password = cfg.resolve_password()
    kwargs: Dict[str, Any] = dict(
        dsn=cfg.host, port=None if cfg.instance else cfg.port, database=cfg.database,
        user=cfg.user, password=password, login_timeout=cfg.login_timeout,
        timeout=cfg.query_timeout, appname=cfg.app_name, autocommit=autocommit,
        bytes_to_unicode=True, disable_connect_retry=True,
        tls_server_hostname=cfg.hostname_in_certificate,
    )
    if cfg.instance:
        kwargs["dsn"] = f"{cfg.host}\\{cfg.instance}"

    if cfg.encrypt == "off":
        return pytds.connect(**kwargs)

    cafile = tls.INSECURE if cfg.trust_server_certificate else cfg.ca_file or tls.SYSTEM_CA
    try:
        return pytds.connect(cafile=cafile, validate_host=False, **kwargs)
    except Exception as exc:
        if cfg.encrypt == "optional" and "not supported by server" in str(exc):
            log.warning("%s does not support encryption; connecting unencrypted", cfg.describe())
            return pytds.connect(**kwargs)
        raise


# ---------------------------------------------------------------------- pyodbc
def _dto_converter(value: bytes) -> _dt.datetime:
    # SQL_SS_TIMESTAMPOFFSET (-155): year, month, day, hour, minute, second, fraction(ns), tzh, tzm
    tup = struct.unpack("<6hI2h", value)
    tz = _dt.timezone(_dt.timedelta(hours=tup[7], minutes=tup[8]))
    return _dt.datetime(tup[0], tup[1], tup[2], tup[3], tup[4], tup[5], tup[6] // 1000, tz)


def _connect_pyodbc(cfg: ConnectionConfig, autocommit: bool) -> Any:
    import pyodbc  # optional, user-provided

    # pyodbc turns on ODBC connection pooling by default: a "new" connection can then be a pooled one a
    # worker used with an explicit transaction, and arrive with that transaction state (a validation
    # connection with @@TRANCOUNT 1 fails on memory-optimized tables). Every connection here is long-lived,
    # so pooling gains nothing. It must be set before the first connect.
    pyodbc.pooling = False

    server = f"{cfg.host}\\{cfg.instance}" if cfg.instance else f"{cfg.host},{cfg.port}"
    parts = {
        "DRIVER": "{%s}" % cfg.odbc_driver,
        "SERVER": server,
        "DATABASE": cfg.database,
        "UID": cfg.user,
        "PWD": "{%s}" % cfg.resolve_password().replace("}", "}}"),
        "Encrypt": {"off": "no", "optional": "optional", "required": "yes"}[cfg.encrypt],
        "TrustServerCertificate": "yes" if cfg.trust_server_certificate else "no",
        "APP": cfg.app_name,
    }
    if cfg.hostname_in_certificate:
        parts["HostNameInCertificate"] = cfg.hostname_in_certificate
    conn = pyodbc.connect(";".join(f"{k}={v}" for k, v in parts.items()),
                          autocommit=autocommit, timeout=cfg.login_timeout)
    conn.autocommit = autocommit         # explicit, whatever the driver manager did
    conn.add_output_converter(-155, _dto_converter)
    if cfg.query_timeout:
        conn.timeout = cfg.query_timeout
    return conn


# --------------------------------------------------------------------- pymssql
def pymssql_value(v: Any) -> Any:
    """pymssql renders datetimes with milliseconds only; send full-precision ISO text instead."""
    if isinstance(v, _dt.datetime):
        return v.isoformat(sep=" ", timespec="microseconds")
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    return v


_warned_pymssql_tls = False


def _connect_pymssql(cfg: ConnectionConfig, autocommit: bool) -> Any:
    import pymssql  # optional, user-provided
    global _warned_pymssql_tls
    if cfg.encrypt != "off" and (cfg.ca_file or not cfg.trust_server_certificate) and not _warned_pymssql_tls:
        log.warning("pymssql encrypts but verifies the server certificate only through FreeTDS settings "
                    "(freetds.conf 'ca file'); ca_file/hostname_in_certificate are not applied")
        _warned_pymssql_tls = True
    server = f"{cfg.host}\\{cfg.instance}" if cfg.instance else cfg.host
    kwargs: Dict[str, Any] = dict(server=server, user=cfg.user, password=cfg.resolve_password(), database=cfg.database,
                  login_timeout=cfg.login_timeout, timeout=cfg.query_timeout or 0, appname=cfg.app_name,
                  autocommit=autocommit, tds_version="7.4", use_datetime2=True,
                  encryption={"off": "off", "optional": "request", "required": "require"}[cfg.encrypt])
    if not cfg.instance:
        kwargs["port"] = str(cfg.port)
    return pymssql.connect(**kwargs)


def connect(cfg: ConnectionConfig, autocommit: bool = True) -> Connection:
    driver = choose_driver(cfg)
    try:
        if driver == "pyodbc":
            raw = _connect_pyodbc(cfg, autocommit)
        elif driver == "pymssql":
            raw = _connect_pymssql(cfg, autocommit)
        else:
            raw = _connect_pytds(cfg, autocommit)
    except Exception as exc:
        raise DriverError(f"cannot connect to {cfg.describe()} with {driver}: {exc}") from exc
    return Connection(raw, driver, cfg, autocommit)
