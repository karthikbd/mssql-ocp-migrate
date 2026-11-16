"""TLS for TDS using only the Python standard library.

Replaces python-tds' original pyOpenSSL-based module (modified for mssql-ocp-migrate).
TDS 7.x wraps the TLS handshake inside PRELOGIN packets, then switches the raw socket to
TLS. ``ssl.MemoryBIO`` + ``ssl.SSLObject`` give us exactly that control without any
third-party package.

``create_context(cafile)`` accepts:
  * a path to a PEM CA bundle        -> verify chain + hostname
  * ``":system:"``                   -> verify against the OS trust store
  * ``":insecure:"``                 -> encrypt but do not verify (TrustServerCertificate=yes)
"""
from __future__ import annotations

import logging
import ssl
import typing
from typing import Any

from . import tds_base

# Kept for API compatibility with the original module: encryption is always available now.
OPENSSL_AVAILABLE = True
BUFSIZE = 65536
SYSTEM_CA = ":system:"
INSECURE = ":insecure:"

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    from mssql_ocp_migrate._vendor.pytds.tds_session import _TdsSession


class _ClientTLS:
    """SSLObject + memory BIOs; the caller moves bytes between the BIOs and the wire."""

    def __init__(self, ctx: ssl.SSLContext, server_hostname: str | None) -> None:
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(
            self.incoming, self.outgoing, server_side=False, server_hostname=server_hostname
        )


class EncryptedSocket(tds_base.TransportProtocol):
    def __init__(self, transport: tds_base.TransportProtocol, tls: _ClientTLS) -> None:
        super().__init__()
        self._transport = transport
        self._tls = tls

    def gettimeout(self) -> float | None:
        return self._transport.gettimeout()

    def settimeout(self, timeout: float | None) -> None:
        self._transport.settimeout(timeout)

    def _flush(self) -> None:
        pending = self._tls.outgoing.read()
        if pending:
            self._transport.sendall(pending)

    def sendall(self, data: Any, flags: int = 0) -> None:
        view = memoryview(bytes(data))
        while view:
            written = self._tls.obj.write(view)
            view = view[written:]
            self._flush()

    def recv_into(self, buffer: bytearray | memoryview, size: int = 0, flags: int = 0) -> int:
        if size == 0:
            size = len(buffer)
        res = self.recv(size)
        buffer[0 : len(res)] = res
        return len(res)

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        while True:
            try:
                data = self._tls.obj.read(bufsize)
                self._flush()
                return data
            except ssl.SSLWantReadError:
                self._flush()
                chunk = self._transport.recv(BUFSIZE)
                if not chunk:
                    return b""
                self._tls.incoming.write(chunk)
            except ssl.SSLZeroReturnError:
                return b""

    def close(self) -> None:
        try:
            self.shutdown()
        finally:
            self._transport.close()

    def shutdown(self, how: int = 0) -> None:
        try:
            self._tls.obj.unwrap()
            self._flush()
        except (ssl.SSLError, OSError):
            pass


def create_context(cafile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # TDS 7.x wraps the handshake in PRELOGIN packets; SQL Server negotiates TLS 1.2 there
    # (TLS 1.3 needs TDS 8 / strict mode). Mirrors python-tds' TLSv1_2_METHOD choice.
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    if cafile == INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cafile == SYSTEM_CA:
        ctx.load_default_certs()
    else:
        ctx.load_verify_locations(cafile=cafile)
    return ctx


# https://msdn.microsoft.com/en-us/library/dd357559.aspx
def establish_channel(tds_sock: _TdsSession) -> None:
    w = tds_sock._writer
    r = tds_sock._reader
    login = tds_sock.conn._login
    tls_ctx = login.tls_ctx
    if not tls_ctx:
        raise tds_base.Error(
            "Server requires encryption: set encrypt='required' or 'optional' in the connection config"
        )
    host = getattr(login, "tls_server_hostname", None) or login.server_name
    sni = None if tls_ctx.verify_mode == ssl.CERT_NONE else host
    tls = _ClientTLS(tls_ctx, sni)
    logger.info("doing TLS handshake")
    while True:
        try:
            tls.obj.do_handshake()
        except ssl.SSLWantReadError:
            req = tls.outgoing.read()
            if req:
                w.begin_packet(tds_base.PacketType.PRELOGIN)
                w.write(req)
                w.flush()
            resp_meta = r.begin_response()
            if resp_meta.type != tds_base.PacketType.PRELOGIN:
                raise tds_base.Error(
                    f"Invalid packet type was received from server, expected PRELOGIN(18) got {resp_meta.type}"
                )
            while not r.stream_finished():
                tls.incoming.write(r.recv(4096))
        else:
            # A final flight (e.g. client Finished) may still be pending.
            req = tls.outgoing.read()
            if req:
                w.begin_packet(tds_base.PacketType.PRELOGIN)
                w.write(req)
                w.flush()
            logger.info("TLS handshake is complete (%s)", tls.obj.version())
            enc_sock = EncryptedSocket(transport=tds_sock.conn.sock, tls=tls)
            tds_sock.conn.sock = enc_sock
            tds_sock._writer._transport = enc_sock
            tds_sock._reader._transport = enc_sock
            return


def revert_to_clear(tds_sock: _TdsSession) -> None:
    """Revert to clear text after login when only the login packet was encrypted."""
    enc_conn = tds_sock.conn.sock
    if isinstance(enc_conn, EncryptedSocket):
        clear_conn = enc_conn._transport
        tds_sock.conn.sock = clear_conn
        tds_sock._writer._transport = clear_conn
        tds_sock._reader._transport = clear_conn
