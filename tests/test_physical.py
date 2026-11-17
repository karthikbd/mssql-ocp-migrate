"""Physical lift: T-SQL generation, log-chain logic, planner, and the full seed -> catch-up ->
cut-over flow against a simulated pair of SQL Servers (backup history kept in a fake msdb)."""
import re

import pytest

from mssql_ocp_migrate.config import Config
from mssql_ocp_migrate.physical import (BackupSet, ChainError, DbFile, DbPlan, Lift, ag_seed_script,
                                        backup_devices, backup_full_sql, backup_log_sql, estimate_timeline,
                                        format_plan, map_path, move_plan, next_logs, restore_full_sql,
                                        s3_credential_sql, tuning)


def make_cfg(tmp_path, **lift):
    base = {"databases": ["Sales"], "source_backup_dir": "\\\\nas01\\mig", "target_backup_dir": "/mnt/mig",
            "stripes": 8, "target_data_dirs": ["/d1", "/d2"], "target_log_dir": "/log",
            "state_file": str(tmp_path / "lift.json"), "path_map": {"\\\\nas01\\joblogs": "/mnt/joblogs"},
            "copy_logins": True}
    base.update(lift)
    return Config.from_dict({"source": {"database": "master", "host": "src"},
                             "target": {"database": "master", "host": "tgt"}, "lift": base})


# ------------------------------------------------------------------ SQL builders
def test_striped_compressed_copy_only_backup(tmp_path):
    lf = make_cfg(tmp_path, stripes=32).lift
    dev = backup_devices(lf.source_backup_dir, "Sales_full", 32)
    assert dev[0] == "\\\\nas01\\mig\\Sales_full_01of32.bak" and len(dev) == 32
    sql = backup_full_sql("Sales", dev, lf)
    assert sql.startswith("BACKUP DATABASE [Sales] TO DISK = N'\\\\nas01\\mig\\Sales_full_01of32.bak', DISK =")
    for opt in ("COPY_ONLY", "COMPRESSION", "CHECKSUM", "MAXTRANSFERSIZE = 4194304", "BUFFERCOUNT = 128", "STATS = 1"):
        assert opt in sql
    assert sql.count("DISK =") == 32


def test_s3_uses_url_and_20mb_parts(tmp_path):
    lf = make_cfg(tmp_path, transport="s3", s3_url="s3://minio.ocp:9000/mig/prod").lift
    dev = backup_devices(lf.s3_url, "Sales_full", 4)
    assert dev[1] == "s3://minio.ocp:9000/mig/prod/Sales_full_02of04.bak"
    sql = backup_full_sql("Sales", dev, lf)
    assert "URL = N's3://" in sql and "MAXTRANSFERSIZE = 20971520" in sql
    cred = s3_credential_sql(lf.s3_url, "AK", "SK")
    assert "CREATE CREDENTIAL [s3://minio.ocp:9000/mig]" in cred and "N'AK:SK'" in cred


def test_buffercount_is_capped_by_memory(tmp_path):
    lf = make_cfg(tmp_path, stripes=64, memory_mb=256).lift
    assert tuning(lf, ["/x"] * 64) == (4194304, 64)          # 256 MB / 4 MB


def test_tail_log_backup_puts_source_in_restoring(tmp_path):
    lf = make_cfg(tmp_path).lift
    assert backup_log_sql("Sales", ["/m/a.trn"], lf, tail=True).endswith("NORECOVERY")
    assert "NORECOVERY" not in backup_log_sql("Sales", ["/m/a.trn"], lf)


def test_move_plan_spreads_data_files_over_volumes():
    files = [DbFile("data1", "D:\\x\\Sales.mdf", "D", 40), DbFile("data2", "E:\\Sales_2.ndf", "D", 30),
             DbFile("data3", "F:\\Sales_3.ndf", "D", 20), DbFile("data4", "G:\\Sales_4.ndf", "D", 20),
             DbFile("log", "L:\\Sales_log.ldf", "L", 10), DbFile("fs", "F:\\fs", "S", 0)]
    m = move_plan(files, "SalesNew", ["/d1", "/d2"], "/log")
    assert m["log"] == "/log/SalesNew_log.ldf"
    assert m["data1"] == "/d1/SalesNew_data1.mdf" and m["data2"] == "/d2/SalesNew_data2.ndf"
    on_d1 = [k for k, v in m.items() if v.startswith("/d1/")]
    assert set(on_d1) >= {"data1"} and len(set(m.values())) == len(m)
    sql = restore_full_sql("SalesNew", ["/mnt/mig/a.bak"], m, make_cfg_lift())
    assert "MOVE N'data1' TO N'/d1/SalesNew_data1.mdf'" in sql and "NORECOVERY" in sql and "CHECKSUM" in sql


def make_cfg_lift():
    return Config.from_dict({"source": {"database": "m"}, "target": {"database": "m"}}).lift


def test_map_path_translates_windows_share_to_pod_mount():
    pm = {"\\\\nas01\\joblogs": "/mnt/joblogs", "\\\\nas01": "/mnt/nas"}
    assert map_path("\\\\NAS01\\JobLogs\\Sales\\LOG\\Sales_1.trn", pm) == "/mnt/joblogs/Sales/LOG/Sales_1.trn"
    assert map_path("\\\\nas01\\other\\x.trn", pm) == "/mnt/nas/other/x.trn"
    assert map_path("/already/linux.trn", pm) == "/already/linux.trn"


# ------------------------------------------------------------------ log chain
def bs(i, first, last, dev="d", dtype=2, copy_only=False):
    return BackupSet(i, "L", first, last, 0, None, [dev], dtype, copy_only)


def test_next_logs_orders_and_skips_covered_and_copy_only():
    sets = [bs(3, 300, 400), bs(1, 100, 200), bs(2, 200, 300), bs(9, 100, 250, copy_only=True), bs(0, 50, 100)]
    assert [b.backup_set_id for b in next_logs(sets, 150)] == [1, 2, 3]


@pytest.mark.parametrize("bad,match", [
    (bs(2, 250, 300), "gap"),
    (bs(2, 200, 300, dtype=7), "third-party"),
    (bs(2, 200, 300, dev="NUL"), "NUL"),
])
def test_next_logs_detects_broken_chains(bad, match):
    with pytest.raises(ChainError, match=match):
        next_logs([bs(1, 100, 200), bad], 150)


# ------------------------------------------------------------------ planner
def test_timeline_for_100tb():
    p = DbPlan("Big", "Big", data_gb=100 * 1024, log_gb=500, files=64, recovery_model="FULL",
               compatibility_level=150, encrypted=False, compression_ratio=3.0, ratio_source="measured",
               backup_mb_s=3000, log_gb_per_hour=20, other_log_backups_24h=96, vdi_log_backups_24h=0)
    lf = make_cfg_lift()
    lf.network_gbps, lf.target_write_mb_s = 10, 2000
    tl = estimate_timeline(p, lf)
    # 100 TB at min(3000 read, 1062 MB/s network x 3 compression) ~ 9.9 h backup; restore at 2000 MB/s ~ 14.6 h
    assert 9 < tl["backup_h"] < 11 and 14 < tl["restore_h"] < 15.5
    assert tl["compressed_gb"] == pytest.approx(34133.3, rel=0.01)
    assert tl["downtime_min"] < 15
    p.timeline = tl
    assert "TOTAL 100.0 TB" in format_plan([p], lf)


def test_ag_script():
    s = ag_seed_script("ag_mig", ["Sales", "HR"], "sql01", "mssql.ocp.svc")
    assert "CLUSTER_TYPE = NONE" in s and "SEEDING_MODE = AUTOMATIC" in s and "DBCC TRACEON (9567, -1)" in s
    assert "FOR DATABASE [Sales], [HR]" in s and "tcp://mssql.ocp.svc:5022" in s


# ------------------------------------------------------------------ simulated servers
class World:
    """Two fake SQL Servers: source with msdb history, target with restore state."""

    def __init__(self):
        self.lsn = 1000
        self.sets = []             # (id, type, first, last, devices, device_type, copy_only)
        self.source_state = "ONLINE"
        self.target_state = None
        self.target_log = []       # executed restore statements
        self.source_log = []
        self.next_id = 1
        self.hist = [(3e12, 1e12, 1200)]  # backup_size, compressed, seconds

    def add_set(self, typ, devices, dtype=2, span=100):
        first = self.lsn
        self.lsn += span
        self.sets.append((self.next_id, typ, first, self.lsn, devices, dtype, False))
        self.next_id += 1


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rows = []

    def execute(self, sql, params=None):
        if sql.startswith("RESTORE FILELISTONLY"):
            self.description = [(c,) for c in ("LogicalName", "PhysicalName", "Type", "FileGroupName", "Size")]
            self.rows = [("Sales", "D:\\Sales.mdf", "D", "PRIMARY", 5e11), ("Sales2", "E:\\Sales2.ndf", "D", "FG2", 4e11),
                         ("Sales_log", "L:\\Sales_log.ldf", "L", None, 1e10)]

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, world, side):
        self.w, self.side = world, side
        self.raw = self

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        w = self.w
        (w.source_log if self.side == "src" else w.target_log).append(sql)
        devs = re.findall(r"(?:DISK|URL) = N'([^']+)'", sql)
        if sql.startswith("BACKUP DATABASE"):
            w.add_set("D", devs, span=5000)
        elif sql.startswith("BACKUP LOG"):
            if w.source_state != "ONLINE" and "READ_ONLY" not in w.source_state:
                raise RuntimeError("database is not online")
            w.add_set("L", devs)
            if "NORECOVERY" in sql:
                w.source_state = "RESTORING"
        elif sql.startswith("ALTER DATABASE") and "READ_ONLY" in sql:
            w.source_state = "ONLINE READ_ONLY"
            w.replayed_read_only = True
        elif sql.startswith("ALTER DATABASE") and "SET READ_WRITE" in sql:
            w.target_read_only = False
        elif sql.startswith("ALTER DATABASE") and "SINGLE_USER" in sql:
            w.replayed_single_user = True
        elif sql.startswith("ALTER DATABASE") and "MULTI_USER" in sql:
            w.target_single_user = False
        elif sql.startswith("RESTORE DATABASE") and "WITH RECOVERY" in sql:
            assert w.target_state == "RESTORING"
            w.target_state = "ONLINE"
            w.target_read_only = getattr(w, "replayed_read_only", False)    # options travel in the log
            w.target_single_user = getattr(w, "replayed_single_user", False)
        elif sql.startswith("RESTORE DATABASE"):
            w.target_state = "RESTORING"
        elif sql.startswith("RESTORE LOG"):
            assert w.target_state == "RESTORING", "log restore on non-restoring database"
        return 0

    def query(self, sql, params=None):
        w = self.w
        if "@@SPID" in sql:
            return [(77,)]
        if "dm_exec_requests" in sql:
            return [(42.0, 120000)]
        if "ProductMajorVersion" in sql:
            return [(16,)] if self.side == "src" else [(getattr(w, "target_major", 16),)]
        if "SERVERPROPERTY('Edition')" in sql:
            return [("Enterprise Edition",)]
        if "recovery_model_desc" in sql:
            return [("FULL", 150, False, "ONLINE")]
        if "database_recovery_status" in sql:     # a full backup started the log chain long ago
            return [(None if getattr(w, "no_log_chain", False) else 1000,)]
        if "FROM sys.master_files" in sql:
            return [("ROWS", 100 * 1024 * 1024 * 1024 / 1024, 8), ("LOG", 200.0, 1)]
        if "type = 'D'" in sql and "TOP 5" in sql:
            return w.hist
        if "bs.type = 'L' AND bs.backup_finish_date" in sql:
            vdi = sum(1 for s in w.sets if s[1] == "L" and s[5] == 7)
            return [(sum(1 for s in w.sets if s[1] == "L"), vdi, 48.0)]
        if "TOP 1 CAST(bs.last_lsn" in sql:
            full = [s for s in w.sets if s[1] == "D" and s[4][0] == params[1]][-1]
            return [(full[3], full[3], 3e12, 1e12)]
        if "bs.type = 'L' AND bs.last_lsn >" in sql:
            after = int(params[1])
            out = []
            for sid, typ, first, last, devs, dtype, co in w.sets:
                if typ == "L" and last > after:
                    for n, d in enumerate(devs, 1):
                        out.append((sid, "L", first, last, 0, "t", d, dtype, co, 1e9, n))
            return out
        if "is_read_only, user_access_desc" in sql:
            return [(getattr(w, "target_read_only", False),
                     "SINGLE_USER" if getattr(w, "target_single_user", False) else "MULTI_USER")]
        if "state_desc FROM sys.databases" in sql:
            return [(w.target_state,)] if w.target_state else []
        if "is_encrypted FROM sys.databases" in sql:
            return [(False,)]
        if "SUSER_SNAME(owner_sid)" in sql:
            return [("app_owner", False, False, False, False, 0)]
        if "SUSER_ID" in sql:
            return [(1,)]
        if "sys.sql_logins" in sql or "authentication_type = 1" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql[:120]}")

    def scalar(self, sql, params=None):
        r = self.query(sql, params)
        return r[0][0] if r else None

    def close(self):
        pass


def fake_connect(world):
    return lambda cfg, autocommit=True: FakeConn(world, "src" if cfg.host == "src" else "tgt")


def test_full_lift_flow_seed_catchup_with_existing_job_logs_and_cutover(tmp_path):
    w = World()
    cfg = make_cfg(tmp_path)
    msgs = []
    lift = Lift(cfg, progress=msgs.append, connect_fn=fake_connect(w))
    lift.seed("Sales")
    assert w.target_state == "RESTORING"
    restore = [s for s in w.target_log if s.startswith("RESTORE DATABASE")][0]
    assert "FROM DISK = N'/mnt/mig/Sales_full_" in restore              # share path mapped to pod mount
    assert "MOVE N'Sales' TO N'/d1/Sales_Sales.mdf'" in restore and "MOVE N'Sales2' TO N'/d2/" in restore
    assert lift.state.db("Sales")["phase"] == "restoring"

    # writes continue on the source; the existing backup job also takes log backups to its own share
    w.add_set("L", ["\\\\nas01\\joblogs\\Sales\\Sales_LOG_1.trn"])
    assert lift.catch_up_once("Sales") == 2                              # job's log + ours, in LSN order
    logs = [s for s in w.target_log if s.startswith("RESTORE LOG")]
    assert "/mnt/joblogs/Sales/Sales_LOG_1.trn" in logs[0] and "/mnt/mig/Sales_log_" in logs[1]

    lift.cutover("Sales")
    assert w.source_state == "RESTORING"                                 # tail log WITH NORECOVERY
    assert w.target_state == "ONLINE" and w.target_single_user is False   # replayed SINGLE_USER undone
    assert any("ALTER AUTHORIZATION ON DATABASE::[Sales] TO [app_owner]" in s for s in w.target_log)
    assert lift.state.db("Sales")["phase"] == "recovered"
    assert any("ONLINE on target" in m for m in msgs)


def test_read_only_cutover_mode(tmp_path):
    w = World()
    lift = Lift(make_cfg(tmp_path, cutover_source="read_only"), progress=lambda m: None, connect_fn=fake_connect(w))
    lift.seed("Sales")
    lift.cutover("Sales")
    assert w.source_state == "ONLINE READ_ONLY" and w.target_state == "ONLINE"
    assert w.target_read_only is False                                   # replayed READ_ONLY undone


def test_vdi_log_backup_breaks_catchup_loudly(tmp_path):
    w = World()
    lift = Lift(make_cfg(tmp_path), progress=lambda m: None, connect_fn=fake_connect(w))
    lift.seed("Sales")
    w.add_set("L", ["{8C1B-VDI-GUID}"], dtype=7)
    with pytest.raises(ChainError, match="third-party"):
        lift.catch_up_once("Sales")


def test_seed_is_resumable(tmp_path):
    w = World()
    cfg = make_cfg(tmp_path)
    Lift(cfg, progress=lambda m: None, connect_fn=fake_connect(w)).seed("Sales")
    n = len(w.source_log)
    Lift(cfg, progress=lambda m: None, connect_fn=fake_connect(w)).seed("Sales")   # new process, same state
    assert len(w.source_log) == n                                        # no second full backup


def test_plan_flags_blockers_and_uses_measured_ratio(tmp_path):
    w = World()
    w.add_set("L", ["{VDI}"], dtype=7)
    lift = Lift(make_cfg(tmp_path, transport="s3", s3_url="s3://minio:9000/b"), progress=lambda m: None,
                connect_fn=fake_connect(w))
    p = lift.plan()[0]
    assert p.compression_ratio == 3.0 and p.ratio_source.startswith("measured")
    assert any("third-party" in b for b in p.blockers)
    assert any("S3 limit" in b for b in p.blockers)                     # ~33 TB compressed > 12.5 TB


def test_newer_target_version_warns_and_statistics_refresh_runs(tmp_path):
    w = World()
    w.target_major = 17
    lift = Lift(make_cfg(tmp_path, update_statistics_after=True), progress=lambda m: None,
                connect_fn=fake_connect(w))
    p = lift.plan()[0]
    assert any("newer SQL Server version (16 -> 17)" in x and "compatibility level 150" in x for x in p.warnings)
    assert not p.blockers
    lift.seed("Sales")
    lift.cutover("Sales")
    assert "EXEC [Sales].sys.sp_updatestats" in w.target_log
    assert "statistics updated (sp_updatestats)" in lift.state.db("Sales")["post_notes"]


def test_cli_lift_ag_script_and_cutover_guard(tmp_path, capsys):
    import json
    from mssql_ocp_migrate.cli import main
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"source": {"database": "master", "host": "sql01"},
                             "target": {"database": "master", "host": "mssql.ocp"},
                             "lift": {"databases": ["Sales"], "state_file": str(tmp_path / "s.json")}}))
    assert main(["lift", "ag-script", "-c", str(c)]) == 0
    assert "tcp://mssql.ocp:5022" in capsys.readouterr().out
    assert main(["lift", "cutover", "-c", str(c)]) == 2          # refuses without --confirm


def test_seed_without_log_chain_takes_a_regular_full_backup(tmp_path):
    w = World()
    w.no_log_chain = True                  # never backed up: COPY_ONLY wouldn't start the log chain
    msgs = []
    Lift(make_cfg(tmp_path), progress=msgs.append, connect_fn=fake_connect(w)).seed("Sales")
    full = [s for s in w.source_log if s.startswith("BACKUP DATABASE")]
    assert full and "COPY_ONLY" not in full[0]
    assert any("log chain" in m for m in msgs)
