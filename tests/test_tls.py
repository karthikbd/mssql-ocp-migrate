"""Wire-level tests of the bundled driver's stdlib TLS against a fake TDS server.

The fake server speaks just enough TDS: PRELOGIN, the TLS handshake wrapped in PRELOGIN packets
(as SQL Server does), then receives the LOGIN7 packet. Seeing the user name inside the decrypted
LOGIN7 proves the handshake and the encrypted channel work without pyOpenSSL.
"""
import shutil
import socket
import ssl
import struct
import subprocess
import threading

import pytest

from mssql_ocp_migrate.config import ConnectionConfig
from mssql_ocp_migrate.drivers import DriverError, connect

PRELOGIN, REPLY, LOGIN7 = 0x12, 0x04, 0x10
ENCRYPT_OFF, ENCRYPT_ON, ENCRYPT_NOT_SUP = 0, 1, 2


def packet(ptype, payload):
    return struct.pack(">BBHHBB", ptype, 1, len(payload) + 8, 0, 1, 0) + payload


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def read_message(read):
    ptype, payload = None, b""
    while True:
        hdr = read(8)
        ptype, status, length = hdr[0], hdr[1], struct.unpack(">H", hdr[2:4])[0]
        payload += read(length - 8)
        if status & 1:
            return ptype, payload


def prelogin_reply(enc):
    # VERSION (6 bytes) + ENCRYPTION (1 byte) + TERMINATOR
    hdr_len = 5 + 5 + 1
    opts = struct.pack(">BHH", 0, hdr_len, 6) + struct.pack(">BHH", 1, hdr_len + 6, 1) + b"\xff"
    return packet(REPLY, opts + bytes([16, 0, 0x10, 0x00, 0, 0]) + bytes([enc]))


class FakeTds(threading.Thread):
    def __init__(self, encrypt, cert=None, key=None):
        super().__init__(daemon=True)
        self.encrypt, self.cert, self.key = encrypt, cert, key
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.logins = []
        self.errors = []

    def run(self):
        self.sock.settimeout(10)
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(10)
        try:
            self.serve(conn)
        except Exception as exc:  # recorded for assertions
            self.errors.append(repr(exc))
        finally:
            conn.close()

    def serve(self, conn):
        ptype, _ = read_message(lambda n: recv_exact(conn, n))
        assert ptype == PRELOGIN
        conn.sendall(prelogin_reply(self.encrypt))
        if self.encrypt == ENCRYPT_NOT_SUP:
            ptype, payload = read_message(lambda n: recv_exact(conn, n))
            self.logins.append((ptype, payload))
            return
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = ctx.wrap_bio(inc, out, server_side=True)
        while True:                                   # handshake inside PRELOGIN packets
            ptype, payload = read_message(lambda n: recv_exact(conn, n))
            assert ptype == PRELOGIN, ptype
            inc.write(payload)
            try:
                tls.do_handshake()
                done = True
            except ssl.SSLWantReadError:
                done = False
            data = out.read()
            if data:
                conn.sendall(packet(PRELOGIN, data))
            if done:
                break
        buf = b""                                      # now raw TLS records on the socket

        def read(n):
            nonlocal buf
            while len(buf) < n:
                try:
                    buf += tls.read(65536)
                except ssl.SSLWantReadError:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise EOFError
                    inc.write(chunk)
            r, buf = buf[:n], buf[n:]
            return r
        self.logins.append(read_message(read))


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl CLI not available to create a test certificate")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem")],
                   check=True, capture_output=True)
    return str(d / "cert.pem"), str(d / "key.pem")


def cfg(port, **kw):
    base = dict(host="127.0.0.1", port=port, database="db", user="alice", password="pw",
                driver="pytds", login_timeout=5)
    base.update(kw)
    return ConnectionConfig(**base)


def attempt(srv, c):
    srv.start()
    with pytest.raises(DriverError) as ei:      # fake server hangs up after LOGIN7
        connect(c)
    srv.join(10)
    return ei.value


def test_encrypted_login_with_trust_server_certificate(cert):
    srv = FakeTds(ENCRYPT_ON, *cert)
    attempt(srv, cfg(srv.port, encrypt="required", trust_server_certificate=True))
    assert not srv.errors, srv.errors
    ptype, payload = srv.logins[0]
    assert ptype == LOGIN7
    assert "alice".encode("utf-16-le") in payload        # decrypted on the server side


def test_encrypted_login_verifies_ca_and_hostname_override(cert):
    srv = FakeTds(ENCRYPT_ON, *cert)
    attempt(srv, cfg(srv.port, encrypt="required", ca_file=cert[0], hostname_in_certificate="localhost"))
    assert srv.logins and srv.logins[0][0] == LOGIN7


def test_untrusted_certificate_is_rejected(cert):
    srv = FakeTds(ENCRYPT_ON, *cert)
    err = attempt(srv, cfg(srv.port, encrypt="required"))           # OS trust store, self-signed cert
    assert "CERTIFICATE_VERIFY_FAILED" in str(err) or "certificate verify failed" in str(err)
    assert not srv.logins


def test_hostname_mismatch_is_rejected(cert):
    srv = FakeTds(ENCRYPT_ON, *cert)
    err = attempt(srv, cfg(srv.port, encrypt="required", ca_file=cert[0]))   # cert is for localhost
    assert "IP address mismatch" in str(err) or "hostname" in str(err).lower() or "mismatch" in str(err)
    assert not srv.logins


def test_plaintext_login_when_encryption_off():
    srv = FakeTds(ENCRYPT_NOT_SUP)
    attempt(srv, cfg(srv.port, encrypt="off"))
    ptype, payload = srv.logins[0]
    assert ptype == LOGIN7 and "alice".encode("utf-16-le") in payload
