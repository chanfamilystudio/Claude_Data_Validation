import pytest

from recon.config.loader import ConfigError
from recon.config.models import DatasetConfig
from recon.connectors.sqlserver import SqlServerConnector

pytestmark = pytest.mark.integration


@pytest.fixture
def t_conn(db):
    db.execute("DROP VIEW IF EXISTS t_conn.v")
    db.execute("DROP TABLE IF EXISTS t_conn.x")
    db.execute("DROP SCHEMA IF EXISTS t_conn")
    db.execute("CREATE SCHEMA t_conn")
    db.execute("CREATE TABLE t_conn.x (a INT, b NVARCHAR(10))")
    db.execute("CREATE VIEW t_conn.v AS SELECT b, a FROM t_conn.x")
    try:
        yield
    finally:
        db.execute("DROP VIEW IF EXISTS t_conn.v")
        db.execute("DROP TABLE IF EXISTS t_conn.x")
        db.execute("DROP SCHEMA IF EXISTS t_conn")


def test_describe(db, t_conn):
    cols = SqlServerConnector(db).describe(DatasetConfig(name="x", source="s", object="t_conn.x"))
    assert [(c.name, c.data_type) for c in cols] == [("a", "int"), ("b", "nvarchar")]


def test_describe_view_in_ordinal_order(db, t_conn):
    cols = SqlServerConnector(db).describe(DatasetConfig(name="v", source="s", object="t_conn.v"))
    assert [(c.name, c.data_type) for c in cols] == [("b", "nvarchar"), ("a", "int")]


def test_describe_one_part_name_defaults_to_dbo(db):
    db.execute("CREATE TABLE dbo.t_conn_one (k INT)")
    try:
        cols = SqlServerConnector(db).describe(DatasetConfig(name="o", source="s", object="t_conn_one"))
        assert [(c.name, c.data_type) for c in cols] == [("k", "int")]
    finally:
        db.execute("DROP TABLE IF EXISTS dbo.t_conn_one")


def test_describe_missing_object(db):
    ds = DatasetConfig(name="nope", source="s", object="t_conn.missing")
    with pytest.raises(ConfigError) as exc:
        SqlServerConnector(db).describe(ds)
    assert exc.value.issues == ["dataset 'nope': object 't_conn.missing' not found"]


def test_source_is_selectable(db, t_conn):
    db.execute("INSERT INTO t_conn.x VALUES (1, N'p'), (2, N'q')")
    ds = DatasetConfig(name="x", source="s", object="t_conn.x", filter="a > 1")
    sql = SqlServerConnector(db).as_sql_source(ds)
    assert db.query(f"SELECT t.a FROM {sql} AS t") == [{"a": 2}]
