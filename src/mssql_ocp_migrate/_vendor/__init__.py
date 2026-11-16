"""Third-party code bundled inside mssql-ocp-migrate so installs never download anything.

* ``pytds`` — python-tds 1.17.1 (MIT, see LICENSE-python-tds.txt), a pure-Python
  implementation of Microsoft's TDS protocol. Imports were rewritten to be package-relative
  and ``tls.py`` was replaced with a standard-library (``ssl``) implementation so encrypted
  connections work without pyOpenSSL.
"""
