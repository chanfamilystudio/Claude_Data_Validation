import pytest

from recon.config.loader import ConfigError
from recon.config.models import DatasetConfig
from recon.connectors import get_connector
from recon.connectors.base import Column
from recon.connectors.sqlserver import SqlServerConnector


def test_snapshot_source():
    ds = DatasetConfig(name="wd_workers", source="workday", object="stg_wd.workers")
    assert SqlServerConnector(None).as_sql_source(ds) == "(SELECT * FROM [stg_wd].[workers])"


def test_one_part_name_is_single_bracketed():
    ds = DatasetConfig(name="w", source="workday", object="workers")
    assert SqlServerConnector(None).as_sql_source(ds) == "(SELECT * FROM [workers])"


def test_periodic_source_uses_half_open_range_and_filter():
    ds = DatasetConfig(
        name="gp_gl_pay", source="gp", object="stg_gp.GL20000",
        period_column="TRXDATE", filter="SOURCDOC = 'UPRCC'",
    )
    assert SqlServerConnector(None).as_sql_source(ds) == (
        "(SELECT * FROM [stg_gp].[GL20000] WHERE (SOURCDOC = 'UPRCC') "
        "AND [TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl)"
    )


def test_period_only_and_filter_only():
    c = SqlServerConnector(None)
    period = DatasetConfig(name="a", source="s", object="s.t", period_column="d")
    assert c.as_sql_source(period) == (
        "(SELECT * FROM [s].[t] WHERE [d] >= @period_start AND [d] < @period_end_excl)"
    )
    filt = DatasetConfig(name="a", source="s", object="s.t", filter="x = 1")
    assert c.as_sql_source(filt) == "(SELECT * FROM [s].[t] WHERE (x = 1))"


def test_closing_bracket_is_escaped():
    from recon.connectors.sqlserver import _quote

    assert _quote("a]b.c") == "[a]]b].[c]"


def test_registry():
    conn = get_connector("sqlserver", None)
    assert isinstance(conn, SqlServerConnector)
    assert conn.name == "sqlserver"


def test_unknown_connector():
    with pytest.raises(ConfigError) as exc:
        get_connector("oracle", None)
    assert exc.value.issues == ["unknown connector 'oracle'"]


def test_column_is_value_object():
    assert Column("a", "int") == Column(name="a", data_type="int")
