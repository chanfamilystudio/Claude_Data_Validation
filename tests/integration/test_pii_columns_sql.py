"""Dataset pii_columns on SQL Server: violating values are reported as *** and hashed compares still detect changes."""

from __future__ import annotations

from datetime import date

import pytest

from recon.config.models import DatasetConfig, QualityRule, RecordMatchRule
from recon.connectors.sqlserver import SqlServerConnector
from recon.sqlgen import generate
from recon.sqlgen.common import DIFF_COLUMNS, DIFFS_DDL, PARAM_HEADER, GenContext, params_for

pytestmark = pytest.mark.integration

PARAMS = params_for(None, date(2026, 9, 30))


@pytest.fixture
def tables(db):
    db.execute("IF SCHEMA_ID('t_pii') IS NULL EXEC('CREATE SCHEMA t_pii')")
    db.execute("DROP TABLE IF EXISTS t_pii.gp; DROP TABLE IF EXISTS t_pii.wd")
    db.execute("CREATE TABLE t_pii.gp (EMPLOYID CHAR(15), SOCSCNUM CHAR(15))")
    db.execute("CREATE TABLE t_pii.wd (employee_id NVARCHAR(15), national_id NVARCHAR(20))")
    db.execute("INSERT INTO t_pii.gp VALUES ('1', '111-11-1111'), ('2', 'not-an-ssn'), ('3', '333-33-3333')")
    db.execute("INSERT INTO t_pii.wd VALUES (N'1', N'111-11-1111'), (N'2', N'not-an-ssn'), (N'3', N'333-33-0000')")
    yield
    db.execute("DROP TABLE IF EXISTS t_pii.gp; DROP TABLE IF EXISTS t_pii.wd; DROP SCHEMA t_pii")


def _ctx() -> GenContext:
    datasets = {
        "gp": DatasetConfig(name="gp", source="gp", object="t_pii.gp", pii_columns=["SOCSCNUM"]),
        "wd": DatasetConfig(name="wd", source="wd", object="t_pii.wd", pii_columns=["national_id"]),
    }
    conn = SqlServerConnector(None)
    return GenContext(datasets=datasets, sources={n: conn.as_sql_source(d) for n, d in datasets.items()}, mappings={})


def _diffs(db, rule) -> list[dict]:
    g = generate(rule, _ctx())
    db.execute("DROP TABLE IF EXISTS #diffs")
    db.execute(DIFFS_DDL)
    db.execute(PARAM_HEADER + f"INSERT INTO #diffs ({DIFF_COLUMNS}) SELECT * FROM (\n{g.diff_sql}\n) d", *PARAMS)
    return db.query("SELECT business_key, break_type, field_name, left_value, right_value FROM #diffs")


def test_quality_violations_on_pii_columns_show_masked_values(db, tables):
    rule = QualityRule(id="q", domain="employees", check_type="quality", severity="low", dataset="gp",
                       key=["EMPLOYID"], assertions=[
                           {"like": {"column": "SOCSCNUM", "pattern": "[0-9][0-9][0-9]-%"}},
                           {"accepted_values": {"column": "SOCSCNUM", "values": ["111-11-1111"]}},
                       ])
    rows = _diffs(db, rule)
    assert sorted((r["business_key"], r["field_name"], r["left_value"]) for r in rows) == [
        ("2", "accepted_values:SOCSCNUM", "***"),
        ("2", "like:SOCSCNUM", "***"),
        ("3", "accepted_values:SOCSCNUM", "***"),
    ]


def test_record_match_hashes_dataset_pii_columns_without_field_flag(db, tables):
    rule = RecordMatchRule(id="r", domain="employees", check_type="record_match", severity="high",
                           left="wd", right="gp", keys=[{"left": "employee_id", "right": "EMPLOYID"}],
                           compare=[{"left": "national_id", "right": "SOCSCNUM"}])   # no pii: true here
    rows = _diffs(db, rule)
    assert rows == [{"business_key": "3", "break_type": "value_mismatch", "field_name": "national_id",
                     "left_value": "***", "right_value": "***"}]
