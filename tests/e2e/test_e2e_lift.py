"""Physical lift against two real SQL Server 2022 containers sharing a backup volume.

    docker compose -f tests/e2e/docker-compose.yml up -d
    MSSQL_E2E=1 pytest tests/e2e/test_e2e_lift.py -v      (run after test_e2e_sqlserver.py seeded e2e_src)
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MSSQL_E2E") != "1", reason="set MSSQL_E2E=1 to run")

from mssql_ocp_migrate import Config  # noqa: E402
from mssql_ocp_migrate.drivers import connect  # noqa: E402
from mssql_ocp_migrate.physical import Lift  # noqa: E402

from test_e2e_sqlserver import HERE, conn_cfg, run_script  # noqa: E402


def test_lift_seed_catchup_cutover_verify(tmp_path):
    src_master = connect(conn_cfg("source", "master"))
    if src_master.scalar("SELECT DB_ID('e2e_src')") is None:
        run_script("source", HERE / "seed_source.sql")
    src_master.execute("ALTER DATABASE e2e_src SET RECOVERY FULL")
    tgt_master = connect(conn_cfg("target", "master"))
    tgt_master.execute("IF DB_ID('e2e_lift') IS NOT NULL DROP DATABASE e2e_lift")
    cfg = Config(source=conn_cfg("source", "master"), target=conn_cfg("target", "master"))
    lf = cfg.lift
    lf.databases, lf.rename = ["e2e_src"], {"e2e_src": "e2e_lift"}
    lf.source_backup_dir = lf.target_backup_dir = "/var/opt/mssql/mig"
    lf.stripes, lf.target_data_dirs, lf.target_log_dir = 8, ["/var/opt/mssql/data"], "/var/opt/mssql/data"
    lf.cutover_source, lf.state_file, lf.copy_logins = "read_only", str(tmp_path / "lift.json"), False
    msgs = []
    lift = Lift(cfg, progress=msgs.append)
    plan = lift.plan()[0]
    assert not plan.blockers, plan.blockers
    lift.seed("e2e_src")
    assert tgt_master.scalar("SELECT state_desc FROM sys.databases WHERE name='e2e_lift'") == "RESTORING"

    src = connect(conn_cfg("source", "e2e_src"))
    src.execute("INSERT dbo.Empty (Id, V) VALUES (1, 'after-seed')")       # change while "online"
    assert lift.catch_up_once("e2e_src") >= 1
    src.execute("INSERT dbo.Empty (Id, V) VALUES (2, 'before-cutover')")
    lift.cutover("e2e_src")
    tgt = connect(conn_cfg("target", "e2e_lift"))
    assert tgt.query("SELECT Id, V FROM dbo.Empty ORDER BY Id") == [(1, "after-seed"), (2, "before-cutover")]
    r = lift.verify("e2e_src")
    assert r["ok"], r
    src.close()
    src_master.execute("ALTER DATABASE e2e_src SET READ_WRITE")
    for c in (tgt, src_master, tgt_master):
        c.close()
