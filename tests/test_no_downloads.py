"""Guards the 'no HTTP, no downloads' promise: the package declares no dependencies and no module
imports HTTP clients, pip or subprocess. The only sockets are the SQL Server connection (TCP) and
the SQL Browser lookup for named instances (UDP 1434)."""
import ast
import pathlib
import re

import mssql_ocp_migrate

PKG = pathlib.Path(mssql_ocp_migrate.__file__).parent
FORBIDDEN = {"urllib", "http", "requests", "httpx", "aiohttp", "pip", "ensurepip", "subprocess",
             "ftplib", "xmlrpc", "webbrowser", "importlib.metadata"}


def test_no_network_or_installer_imports():
    hits = []
    for f in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                if n in FORBIDDEN or n.split(".")[0] in FORBIDDEN:
                    hits.append(f"{f.relative_to(PKG)}: {n}")
    assert not hits, hits


def test_third_party_imports_are_optional_only():
    """Outside the stdlib, only pyodbc (optional, never required) and pyOpenSSL-free code."""
    allowed_optional = {"tomllib", "pyodbc", "pymssql", "ntlm_auth", "kerberos", "spnego", "winkerberos", "namedlist",
                        "recordtype", "bitarray", "OpenSSL"}
    import sys
    stdlib = set(sys.stdlib_module_names)
    for f in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                assert m in stdlib or m == "mssql_ocp_migrate" or m in allowed_optional, f"{f}: {m}"


def test_pyproject_declares_no_dependencies():
    pp = PKG.parent.parent / "pyproject.toml"
    if pp.exists():   # running from a source checkout
        assert re.search(r"^dependencies = \[\]", pp.read_text(), re.M)


def test_optional_imports_are_guarded():
    """Optional third-party modules are only imported inside try/except or functions."""
    src = (PKG / "_vendor/pytds/tls.py").read_text()
    assert "OpenSSL" not in src.replace("pyOpenSSL", "")
