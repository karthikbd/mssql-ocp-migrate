"""mssql-ocp-migrate: move SQL Server databases to SQL Server on OpenShift, with end-to-end validation.

Zero install-time dependencies: the SQL Server wire-protocol driver (python-tds, MIT) is bundled
under ``mssql_ocp_migrate._vendor``. pyodbc is used only if it is already installed.

    from mssql_ocp_migrate import Config, Migrator
    run = Migrator(Config.load("migrate.toml")).migrate()
    print(run.ok, run.rows)
"""
__version__ = "0.5.1"

from .config import Config, ConnectionConfig, MigrationConfig, ValidationConfig
from .engine import Migrator, RunResult

__all__ = [
           "Config",
           "ConnectionConfig",
           "MigrationConfig",
           "Migrator",
           "RunResult",
           "ValidationConfig",
           "__version__",
]
