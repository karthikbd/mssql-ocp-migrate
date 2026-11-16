# Third-party code bundled in mssql-ocp-migrate

## python-tds 1.17.1 — `src/mssql_ocp_migrate/_vendor/pytds/`
- Upstream: https://github.com/denisenkom/pytds — MIT License (`_vendor/LICENSE-python-tds.txt`)
- Modifications:
  - absolute `pytds` imports rewritten to `mssql_ocp_migrate._vendor.pytds` (no clash with an installed pytds)
  - `tls.py` re-implemented on the standard-library `ssl` module (MemoryBIO/SSLObject) instead of pyOpenSSL
  - `connect()` gained a `tls_server_hostname` argument (certificate host-name override)
