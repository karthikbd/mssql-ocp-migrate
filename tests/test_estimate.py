"""`estimate` on the SQLite stand-in: sampling, the write probe, projections and notes."""
from mssql_ocp_migrate.estimate import Estimate, _parallel_factor, _write_probe, estimate, format_estimate
from mssql_ocp_migrate.model import Column, Table
from sqlite_adapter import SqliteFactory
from test_engine import cfg, make_dbs


def test_estimate_samples_the_biggest_table_and_projects_time(tmp_path):
    src, tgt = make_dbs(tmp_path)
    c = cfg(tmp_path, workers=4, executor="process")
    est = estimate(c, SqliteFactory(src, tgt), sample_rows=1000)
    assert est.tables == 5 and est.rows > 9000
    assert est.sample_table in ("main.orders", "main.order_lines") and est.sample_rows == 1000
    assert est.read_rows_per_s > 0 and est.checksum_mb_per_s > 0
    assert est.total_minutes == round(est.copy_minutes + est.index_minutes + est.validate_minutes, 1)
    assert est.total_minutes_range[0] <= est.total_minutes <= est.total_minutes_range[1]
    # the SQLite target has no bulk loader: the probe is skipped and said so, the estimate still completes
    assert any(n.startswith("write probe skipped") for n in est.notes)
    text = format_estimate(est)
    assert "TOTAL" in text and "note: write probe skipped" in text
    assert est.to_dict()["tables"] == 5


def test_estimate_without_write_probe_and_with_threads(tmp_path):
    src, tgt = make_dbs(tmp_path)
    c = cfg(tmp_path, workers=8, executor="thread")
    c.source.driver = c.target.driver = "pytds"
    est = estimate(c, SqliteFactory(src, tgt), sample_rows=200, write_probe=False)
    assert est.write_rows_per_s == 0
    assert any("CPU-bound on one core" in n for n in est.notes)


def test_estimate_full_validation_costs_more(tmp_path):
    src, tgt = make_dbs(tmp_path)
    checksum = estimate(cfg(tmp_path), SqliteFactory(src, tgt), sample_rows=500, write_probe=False)
    c = cfg(tmp_path)
    c.validation.mode = "full"
    full = estimate(c, SqliteFactory(src, tgt), sample_rows=500, write_probe=False)
    assert full.validate_minutes >= checksum.validate_minutes


def test_estimate_with_no_data(tmp_path):
    src, tgt = make_dbs(tmp_path)
    c = cfg(tmp_path)
    c.migration.include = ["main.empty_table"]
    est = estimate(c, SqliteFactory(src, tgt))
    assert est.rows == 0 and est.notes == ["no data to move"]


def test_parallel_factor():
    assert _parallel_factor(1, "process") == 1
    assert _parallel_factor(5, "process") == 4
    assert _parallel_factor(8, "thread_gil") == 1.3


class _Target:
    def __init__(self, driver):
        self.driver, self.calls = driver, []

    def execute(self, sql, params=None):
        self.calls.append(sql)

    def _insert_bulk_pytds(self, name, table, cols, batches, tablock):
        self.calls.append(("bulk", name, sum(len(b) for b in batches)))

    def _load_pyodbc(self, table, cols, batches, identity, target_name=None):
        self.calls.append(("odbc", target_name, sum(len(b) for b in batches)))

    def rollback(self):
        self.calls.append("rollback")


def test_write_probe_loads_a_temp_copy_and_rolls_back():
    t = Table("dbo", "T", columns=[Column("id", "int", is_identity=True, nullable=False),
                                   Column("v", "nvarchar", max_length=40, default_definition="(N'x')")])
    for driver, kind in (("pytds", "bulk"), ("pyodbc", "odbc")):
        tgt = _Target(driver)
        rps = _write_probe(tgt, t, [(i, "a") for i in range(10)], batch_rows=4)
        assert rps > 0
        assert tgt.calls[0].startswith("CREATE TABLE #mom_estimate (") and "IDENTITY" not in tgt.calls[0]
        assert tgt.calls[1] == (kind, "#mom_estimate", 10) and tgt.calls[-1] == "rollback"


def test_format_estimate_without_range():
    assert "0.0–0.0 min" in format_estimate(Estimate(tables=0, rows=0, source_mb=0))
