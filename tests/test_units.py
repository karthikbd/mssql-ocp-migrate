import datetime as dt
import decimal
import json
import random
import re
import sqlite3
import uuid

import pytest

from mssql_ocp_migrate import chunking, ddl
from mssql_ocp_migrate.cli import EXAMPLE_CONFIG, main
from mssql_ocp_migrate.config import Config, ConfigError
from mssql_ocp_migrate.model import CheckConstraint, Column, ForeignKey, Index, Table
from mssql_ocp_migrate.rowhash import RowHasher
from mssql_ocp_migrate.state import State, decode_key, encode_key


# ------------------------------------------------------------------ chunking
@pytest.mark.parametrize("ncols", [1, 2, 3])
def test_chunks_partition_rows_exactly(ncols):
    """Every row falls in exactly one chunk, for single and composite keys."""
    rnd = random.Random(ncols)
    db = sqlite3.connect(":memory:")
    cols = [f"k{i}" for i in range(ncols)]
    db.execute(f"CREATE TABLE t ({', '.join(c + ' INT' for c in cols)}, PRIMARY KEY ({', '.join(cols)}))")
    keys = {tuple(rnd.randint(0, 9) for _ in cols) for _ in range(600)}
    db.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in cols)})", sorted(keys))
    ordered = sorted(keys)
    bounds = ordered[37::37]
    chunks = chunking.build_chunks("t", bounds)
    q = lambda n: '"' + n + '"'
    seen = []
    for ch in chunks:
        where, params = chunking.predicate(ch, cols, q)
        seen.extend(tuple(r) for r in db.execute(f"SELECT * FROM t WHERE {where}", params))
    assert sorted(seen) == ordered            # no gaps, no duplicates
    assert len(chunks) == len(bounds) + 1


def test_unbounded_predicate():
    where, params = chunking.predicate(chunking.build_chunks("t", [])[0], ["id"], str)
    assert (where, params) == ("1=1", [])
    assert chunking.chunk_count(0, 10) == 1 and chunking.chunk_count(21, 10) == 3


# ------------------------------------------------------------------ DDL
def sample_table():
    t = Table(schema="sales", name="Order Lines")
    t.columns = [
        Column("id", "bigint", nullable=False, is_identity=True, identity_seed=1000, identity_increment=5),
        Column("name", "nvarchar", max_length=200, collation="Latin1_General_CI_AS", nullable=False,
               default_name="DF_name", default_definition="(N'x')"),
        Column("notes", "nvarchar", max_length=-1),
        Column("price", "decimal", precision=18, scale=4),
        Column("at", "datetime2", scale=7),
        Column("total", "money", is_computed=True, computed_definition="([price]*(2))", computed_persisted=True),
        Column("rv", "timestamp"),
        Column("phone", "varchar", max_length=20, user_type="Phone", user_type_schema="dbo"),
        Column("geo", "geography"),
    ]
    t.primary_key = Index("PK_OL", [("id", False), ("at", True)], unique=True, clustered=True, is_primary_key=True)
    t.indexes = [Index("IX_name", [("name", False)], included=["price"], filter_definition="([price]>(0))"),
                 Index("UQ_n", [("name", False)], unique=True, is_unique_constraint=True),
                 Index("CCI", [], type_desc="CLUSTERED COLUMNSTORE", clustered=True)]
    t.foreign_keys = [ForeignKey("FK_x", ["id"], "sales", "Orders", ["order_id"], on_delete="CASCADE")]
    t.checks = [CheckConstraint("CK_p", "([price]>=(0))")]
    return t


def test_create_table_sql():
    sql = ddl.create_table_sql(sample_table())
    assert sql.startswith("CREATE TABLE [sales].[Order Lines] (")
    assert "[id] bigint IDENTITY(1000,5) NOT NULL" in sql
    assert "[name] nvarchar(100) COLLATE Latin1_General_CI_AS NOT NULL CONSTRAINT [DF_name] DEFAULT (N'x')" in sql
    assert "[notes] nvarchar(max) NULL" in sql
    assert "[price] decimal(18,4) NULL" in sql
    assert "[at] datetime2(7) NULL" in sql
    assert "[total] AS ([price]*(2)) PERSISTED" in sql
    assert "[phone] [dbo].[Phone] NULL" in sql
    assert "CONSTRAINT [PK_OL] PRIMARY KEY CLUSTERED ([id], [at] DESC)" in sql


def test_post_load_sql():
    t = sample_table()
    stmts = ddl.post_load_sql(t)
    assert stmts[0] == "CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [sales].[Order Lines]"
    assert ("CREATE NONCLUSTERED INDEX [IX_name] ON [sales].[Order Lines] ([name]) INCLUDE ([price]) "
            "WHERE ([price]>(0))") in stmts
    assert "ALTER TABLE [sales].[Order Lines] ADD CONSTRAINT [UQ_n] UNIQUE NONCLUSTERED ([name])" in stmts
    assert "ALTER TABLE [sales].[Order Lines] WITH CHECK ADD CONSTRAINT [CK_p] CHECK ([price]>=(0))" in stmts
    fk = ddl.fk_sql_all(t)[0]
    assert fk.endswith("REFERENCES [sales].[Orders] ([order_id]) ON DELETE CASCADE")


def test_model_column_roles():
    t = sample_table()
    assert [c.name for c in t.insert_columns] == ["id", "name", "notes", "price", "at", "phone", "geo"]
    assert "rv" not in [c.name for c in t.compare_columns]
    assert t.key_columns == ["id", "at"] and t.has_identity


def test_quote_escapes_brackets():
    assert ddl.q("a]b") == "[a]]b]"


# ------------------------------------------------------------------ config
@pytest.mark.skipif(__import__("sys").version_info < (3, 11), reason="TOML needs tomllib (Python 3.11+)")
def test_example_config_loads(tmp_path, monkeypatch):
    p = tmp_path / "m.toml"
    p.write_text(EXAMPLE_CONFIG)
    c = Config.load(str(p))
    assert c.source.database == "SalesDB" and c.migration.workers == 8
    monkeypatch.setenv("SRC_DB_PASSWORD", "s3cr3t")
    assert c.source.resolve_password() == "s3cr3t"
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    c.target.password_file = str(secret)
    assert c.target.resolve_password() == "from-file"
    assert c.to_dict()["source"]["password"] is None


def test_json_config_and_validation_errors(tmp_path):
    base = {"source": {"database": "a"}, "target": {"database": "b"}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(base))
    assert Config.load(str(p)).target.database == "b"
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({**base, "migration": {"wrkers": 2}})
    with pytest.raises(ConfigError, match="encrypt"):
        Config.from_dict({"source": {"database": "a", "encrypt": "maybe"}, "target": {"database": "b"}})
    with pytest.raises(ConfigError, match="database is required"):
        Config.from_dict({"source": {}, "target": {"database": "b"}})


def test_include_exclude_globs():
    c = Config.from_dict({"source": {"database": "a"}, "target": {"database": "b"},
                          "migration": {"include": ["dbo.*", "Sales.Order*"], "exclude": ["dbo.tmp_*"]}})
    assert c.selects("dbo", "Customers") and c.selects("sales", "OrderLines")
    assert not c.selects("dbo", "tmp_load") and not c.selects("hr", "People")


# ------------------------------------------------------------------ state
def test_checkpoint_key_roundtrip(tmp_path):
    key = (42, decimal.Decimal("12.3400"), uuid.uuid4(), b"\x00\xff", dt.datetime(2024, 1, 2, 3, 4, 5, 6),
           dt.date(2024, 1, 2), dt.time(1, 2, 3), "naïve", None, 1.5, True)
    assert decode_key(json.loads(json.dumps(encode_key(key)))) == key
    st = State(str(tmp_path / "s.json"))
    from mssql_ocp_migrate.chunking import build_chunks
    st.set_chunks("dbo.t", build_chunks("dbo.t", [key]))
    st.save()
    again = State.load_or_new(str(tmp_path / "s.json"))
    assert again.chunks("dbo.t")[0].upper == key and again.chunks("dbo.t")[1].lower == key


# ------------------------------------------------------------------ hashing
def test_row_hash_is_driver_independent():
    cols = [Column("id", "int"), Column("g", "uniqueidentifier"), Column("m", "money"),
            Column("f", "float"), Column("b", "bit")]
    h = RowHasher(cols, ["id"])
    u = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
    a = (1, u, decimal.Decimal("10.5000"), 1.25, True)            # what pytds returns
    b = (1, str(u).upper(), decimal.Decimal("10.5"), 1.25, 1)    # what pyodbc returns
    assert h.digest(a) == h.digest(b)
    assert h.digest(a) != h.digest((1, u, decimal.Decimal("10.5001"), 1.25, True))
    assert h.digest((1, None, None, None, None)) != h.digest((1, None, None, None, False))
    assert h.key(a) == h.key(b)


# ------------------------------------------------------------------ cli
def test_cli_init_and_bad_config(capsys, tmp_path):
    assert main(["init-config"]) == 0
    assert "[source]" in capsys.readouterr().out
    assert main(["check", "-c", str(tmp_path / "missing.toml")]) == 2


# ------------------------------------------------------- found by fuzzing
def test_row_fingerprint_fields_cannot_run_together():
    h = RowHasher([Column("a", "nvarchar"), Column("b", "nvarchar")], ["a", "b"])
    r1, r2 = ("x\x1esy", "z"), ("x", "y\x1esz")
    assert h.encode(r1) != h.encode(r2)
    assert h.digest(r1) != h.digest(r2)
    assert h.key(r1) != h.key(r2)
    hb = RowHasher([Column("a", "varbinary"), Column("b", "varbinary")], ["a"])
    assert hb.encode((b"\x1ex", b"")) != hb.encode((b"", b"\x1ex"))


@pytest.mark.parametrize("doc, message", [
    ({"source": 5, "target": {"database": "d"}}, "[source] must be a table"),
    ({"source": {"database": "d", "port": "1433"}, "target": {"database": "d"}}, "[source] port must be a whole number"),
    ({"source": {"database": "d"}, "target": {"database": "d"}, "migration": {"workers": True}}, "workers must be"),
    ({"source": {"database": "d"}, "target": {"database": "d"}, "migration": {"include": "dbo.*"}}, "include must be a list"),
    ({"source": {"database": "d"}, "target": {"database": "d"}, "lift": {"rename": {"a": 1}}}, "rename must be"),
    ({"source": {"database": "d"}, "target": {"database": "d"}, "report_dir": 7}, "report_dir"),
])
def test_config_wrong_types_are_config_errors(doc, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        Config.from_dict(doc)


def test_config_compatibility_level_accepts_number_or_text():
    base = {"source": {"database": "d"}, "target": {"database": "d"}}
    for level in (150, "150"):
        cfg = Config.from_dict({**base, "migration": {"compatibility_level": level}})
        assert cfg.migration.compatibility_level == "150"
    cfg = Config.from_dict({**base, "validation": {"sample_full_pct": 10}})   # an int where a float is expected
    assert cfg.validation.sample_full_pct == 10
