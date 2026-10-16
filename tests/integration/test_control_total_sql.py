"""control_total SQL executed on SQL Server, the way the executor runs it.

``totals_sql`` is loaded into a ``#totals`` temp table created from ``TOTALS_DDL`` (parameterless
batch) with ``INSERT INTO #totals (...) SELECT * FROM (<totals_sql>) t`` under bound parameters;
``diff_sql`` then reads ``#totals`` and is loaded into ``#diffs`` the same way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pyodbc
import pytest

from recon.config.models import ControlTotalRule, DatasetConfig, MappingConfig
from recon.connectors.sqlserver import SqlServerConnector
from recon.periods import resolve_period
from recon.sqlgen import generate
from recon.sqlgen.common import (
    DIFF_COLUMNS,
    DIFFS_DDL,
    PARAM_HEADER,
    TOTALS_COLUMNS,
    TOTALS_DDL,
    GenContext,
    params_for,
)

pytestmark = pytest.mark.integration

AS_OF = date(2026, 9, 30)
SNAPSHOT_PARAMS = params_for(None, AS_OF)

_TABLES = (
    "t_ct_wd.jrnl", "t_ct_gp.gl", "t_ct_xref.acct_map", "t_ct_wd.pct", "t_ct_gp.pct",
    "t_ct_wd.dated", "t_ct_gp.dated", "t_ct_xref.dup_map",
)
_SCHEMAS = ("t_ct_wd", "t_ct_gp", "t_ct_xref")


def _drop_all(db) -> None:
    for name in ("#totals", "#diffs"):
        db.execute(f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name}")
    for table in _TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    for schema in _SCHEMAS:
        db.execute(f"DROP SCHEMA IF EXISTS {schema}")


@pytest.fixture
def schemas(db):
    _drop_all(db)
    for schema in _SCHEMAS:
        db.execute(f"CREATE SCHEMA {schema}")
    try:
        yield db
    finally:
        _drop_all(db)


def _ds(name: str, obj: str, period_column: str | None = None) -> DatasetConfig:
    return DatasetConfig(name=name, source="s", object=obj, period_column=period_column)


def _ctx(*datasets: DatasetConfig, mappings: tuple[MappingConfig, ...] = ()) -> GenContext:
    conn = SqlServerConnector(None)
    return GenContext(
        datasets={d.name: d for d in datasets},
        sources={d.name: conn.as_sql_source(d) for d in datasets},
        mappings={m.name: m for m in mappings},
    )


def _rule(**fields) -> ControlTotalRule:
    return ControlTotalRule.model_validate(
        {"id": "t_rule", "domain": "payroll", "check_type": "control_total", "severity": "high", **fields}
    )


def _map(name: str, obj: str, frm: str, to: str) -> MappingConfig:
    return MappingConfig.model_validate({"name": name, "object": obj, "from": frm, "to": to})


def _load(db, generated, params):
    """Fill #totals and #diffs exactly as the executor does; return (totals rows, diff rows)."""
    db.execute("IF OBJECT_ID('tempdb..#totals') IS NOT NULL DROP TABLE #totals")
    db.execute("IF OBJECT_ID('tempdb..#diffs') IS NOT NULL DROP TABLE #diffs")
    db.execute(TOTALS_DDL)
    db.execute(DIFFS_DDL)
    db.execute(
        PARAM_HEADER + f"INSERT INTO #totals ({TOTALS_COLUMNS}) SELECT * FROM (\n{generated.totals_sql}\n) t",
        *params,
    )
    db.execute(
        PARAM_HEADER + f"INSERT INTO #diffs ({DIFF_COLUMNS}) SELECT * FROM (\n{generated.diff_sql}\n) d",
        *params,
    )
    totals = {
        (r["group_key"], r["measure"]): (
            r["left_value"], r["right_value"], r["diff"], r["tolerance_applied"], bool(r["within_tolerance"])
        )
        for r in db.query("SELECT * FROM #totals")
    }
    diffs = {
        (r["business_key"], r["break_type"], r["field_name"], r["left_value"], r["right_value"], r["diff"])
        for r in db.query("SELECT * FROM #diffs")
    }
    return totals, diffs


def _d(value: str) -> Decimal:
    return Decimal(value)


# --------------------------------------------------------------------------- journal vs GL, mapped

WD = _ds("wd", "t_ct_wd.jrnl")
GP = _ds("gp", "t_ct_gp.gl")
ACCT_MAP = _map("wd_acct_to_gp", "t_ct_xref.acct_map", "wd_account", "gp_actindx")
JRNL_RULE = _rule(
    left="wd",
    right="gp",
    group_by=[{"left": "ledger_account", "right": "ACTINDX", "via_mapping": "wd_acct_to_gp"}],
    measures=[
        {
            "name": "net_amount",
            "left": "SUM(debit - credit)",
            "right": "SUM(DEBITAMT - CRDTAMNT)",
            "tolerance": {"abs": "0.01"},
        },
        {"name": "line_count", "left": "COUNT(*)", "right": "COUNT(*)"},
    ],
)

# (ledger_account, debit, credit)
WD_ROWS = [
    ("A6000", "100.00", "0"),
    ("a6000 ", "50.00", "10.00"),  # same account as above after trim + case folding
    ("A6100", "80.00", "0"),
    ("A7000", "5.00", "0"),  # mapped, but GP has no lines for 7000
    ("ZZZ", "9.00", "0"),  # no crosswalk row
]
# (ACTINDX, DEBITAMT, CRDTAMNT)
GP_ROWS = [
    (6000, "100.004", "0"),
    (6000, "50.00", "10.00"),
    (6100, "79.00", "0"),
    (6300, "25.00", "0"),  # only in GP
]
MAP_ROWS = [("A6000", 6000), ("A6100", 6100), ("A7000", 7000)]

EXPECTED_TOTALS = {
    # group, measure: left, right, diff, tolerance_applied, within
    ("6000", "net_amount"): (_d("140"), _d("140.004"), _d("-0.004"), _d("0.01"), True),
    ("6000", "line_count"): (_d("2"), _d("2"), _d("0"), _d("0"), True),
    ("6100", "net_amount"): (_d("80"), _d("79"), _d("1"), _d("0.01"), False),
    ("6100", "line_count"): (_d("1"), _d("1"), _d("0"), _d("0"), True),
    ("7000", "net_amount"): (_d("5"), _d("0"), _d("5"), _d("0.01"), False),
    ("7000", "line_count"): (_d("1"), _d("0"), _d("1"), _d("0"), False),
    ("6300", "net_amount"): (_d("0"), _d("25"), _d("-25"), _d("0.01"), False),
    ("6300", "line_count"): (_d("0"), _d("1"), _d("-1"), _d("0"), False),
    ("UNMAPPED:ZZZ", "net_amount"): (_d("9"), _d("0"), _d("9"), _d("0.01"), False),
    ("UNMAPPED:ZZZ", "line_count"): (_d("1"), _d("0"), _d("1"), _d("0"), False),
}
EXPECTED_DIFFS = {
    ("6100", "total_mismatch", "net_amount", "80.000000", "79.000000", _d("1")),
    ("7000", "total_mismatch", "net_amount", "5.000000", "0.000000", _d("5")),
    ("7000", "total_mismatch", "line_count", "1.000000", "0.000000", _d("1")),
    ("6300", "total_mismatch", "net_amount", "0.000000", "25.000000", _d("-25")),
    ("6300", "total_mismatch", "line_count", "0.000000", "1.000000", _d("-1")),
    ("UNMAPPED:ZZZ", "total_mismatch", "net_amount", "9.000000", "0.000000", _d("9")),
    ("UNMAPPED:ZZZ", "total_mismatch", "line_count", "1.000000", "0.000000", _d("1")),
}


@pytest.fixture
def jrnl_tables(schemas):
    db = schemas
    db.execute(
        "CREATE TABLE t_ct_wd.jrnl (ledger_account NVARCHAR(20), debit DECIMAL(18,4), credit DECIMAL(18,4))"
    )
    db.execute(
        "CREATE TABLE t_ct_gp.gl (ACTINDX INT, DEBITAMT NUMERIC(19,5), CRDTAMNT NUMERIC(19,5))"
    )
    db.execute("CREATE TABLE t_ct_xref.acct_map (wd_account NVARCHAR(20), gp_actindx INT)")
    for row in WD_ROWS:
        db.execute("INSERT INTO t_ct_wd.jrnl VALUES (?, ?, ?)", *row)
    for row in GP_ROWS:
        db.execute("INSERT INTO t_ct_gp.gl VALUES (?, ?, ?)", *row)
    for row in MAP_ROWS:
        db.execute("INSERT INTO t_ct_xref.acct_map VALUES (?, ?)", *row)
    return db


def test_mapped_groups_totals_and_diffs(jrnl_tables):
    db = jrnl_tables
    g = generate(JRNL_RULE, _ctx(WD, GP, mappings=(ACCT_MAP,)))
    db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)  # no ambiguity: must not raise
    totals, diffs = _load(db, g, SNAPSHOT_PARAMS)
    assert totals == EXPECTED_TOTALS
    assert diffs == EXPECTED_DIFFS


def test_counts_sql_counts_source_rows(jrnl_tables):
    g = generate(JRNL_RULE, _ctx(WD, GP, mappings=(ACCT_MAP,)))
    (row,) = jrnl_tables.query(PARAM_HEADER + g.counts_sql, *SNAPSHOT_PARAMS)
    assert (row["rows_left"], row["rows_right"]) == (5, 4)


def test_totals_columns_have_contract_types(jrnl_tables):
    db = jrnl_tables
    g = generate(JRNL_RULE, _ctx(WD, GP, mappings=(ACCT_MAP,)))
    into = PARAM_HEADER + f"SELECT * INTO #shape FROM (\n{g.totals_sql}\n) t;\n"
    cols = db.query(
        into + "SELECT c.name, TYPE_NAME(c.user_type_id) AS type_name, c.max_length, c.precision, c.scale"
        " FROM tempdb.sys.columns c WHERE c.object_id = OBJECT_ID('tempdb..#shape') ORDER BY c.column_id",
        *SNAPSHOT_PARAMS,
    )
    assert [(c["name"], c["type_name"], c["max_length"], c["precision"], c["scale"]) for c in cols] == [
        ("group_key", "nvarchar", 800, 0, 0),
        ("measure", "nvarchar", 256, 0, 0),
        ("left_value", "decimal", 17, 38, 6),
        ("right_value", "decimal", 17, 38, 6),
        ("diff", "decimal", 17, 38, 6),
        ("tolerance_applied", "decimal", 17, 38, 6),
        ("within_tolerance", "bit", 1, 1, 0),
    ]


def test_no_group_by_compares_one_all_group(jrnl_tables):
    db = jrnl_tables
    rule = _rule(
        left="wd",
        right="gp",
        measures=[
            {"name": "debit_total", "left": "SUM(debit)", "right": "SUM(DEBITAMT)", "tolerance": {"abs": 50}},
            {"name": "line_count", "left": "COUNT(*)", "right": "COUNT(*)"},
        ],
    )
    g = generate(rule, _ctx(WD, GP))
    assert g.checks_sql is None
    totals, diffs = _load(db, g, SNAPSHOT_PARAMS)
    assert totals == {
        ("ALL", "debit_total"): (_d("244"), _d("254.004"), _d("-10.004"), _d("50"), True),
        ("ALL", "line_count"): (_d("5"), _d("4"), _d("1"), _d("0"), False),
    }
    assert diffs == {("ALL", "total_mismatch", "line_count", "5.000000", "4.000000", _d("1"))}


def test_one_side_empty_compares_against_zero(jrnl_tables):
    db = jrnl_tables
    db.execute("DELETE FROM t_ct_gp.gl")
    rule = _rule(left="wd", right="gp", measures=[{"name": "n", "left": "COUNT(*)", "right": "COUNT(*)"}])
    totals, _ = _load(db, generate(rule, _ctx(WD, GP)), SNAPSHOT_PARAMS)
    assert totals == {("ALL", "n"): (_d("5"), _d("0"), _d("5"), _d("0"), False)}


def test_ambiguous_mapping_is_refused(jrnl_tables):
    db = jrnl_tables
    db.execute("INSERT INTO t_ct_xref.acct_map VALUES (?, ?)", "a6000", 6999)  # same value after folding
    g = generate(JRNL_RULE, _ctx(WD, GP, mappings=(ACCT_MAP,)))
    with pytest.raises(pyodbc.Error, match="ambiguous mapping wd_acct_to_gp"):
        db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)


# --------------------------------------------------------------------------- tolerance on the server


@pytest.fixture
def pct_tables(schemas):
    db = schemas
    db.execute("CREATE TABLE t_ct_wd.pct (acct NVARCHAR(10), amt DECIMAL(18,2))")
    db.execute("CREATE TABLE t_ct_gp.pct (ACCT CHAR(10), AMT NUMERIC(19,5))")
    for row in [("P1", "1009"), ("P2", "1011"), ("P3", "-1009"), ("P4", "1015"), ("P5", "1025")]:
        db.execute("INSERT INTO t_ct_wd.pct VALUES (?, ?)", *row)
    for row in [("P1", "1000"), ("P2", "1000"), ("P3", "-1000"), ("P4", "1000"), ("P5", "1000")]:
        db.execute("INSERT INTO t_ct_gp.pct VALUES (?, ?)", *row)
    return db


def test_pct_and_combined_tolerance_executed_on_server(pct_tables):
    db = pct_tables
    rule = _rule(
        left="wd",
        right="gp",
        group_by=[{"left": "acct", "right": "ACCT"}],
        measures=[
            {"name": "pct_only", "left": "SUM(amt)", "right": "SUM(AMT)", "tolerance": {"pct": "0.01"}},
            {"name": "abs_pct", "left": "SUM(amt)", "right": "SUM(AMT)", "tolerance": {"abs": 20, "pct": "0.01"}},
            {"name": "exact", "left": "SUM(amt)", "right": "SUM(AMT)"},
        ],
    )
    totals, diffs = _load(db, generate(rule, _ctx(_ds("wd", "t_ct_wd.pct"), _ds("gp", "t_ct_gp.pct"))), SNAPSHOT_PARAMS)
    got = {k: (v[2], v[3], v[4]) for k, v in totals.items()}
    assert got == {
        ("P1", "pct_only"): (_d("9"), _d("10"), True),
        ("P1", "abs_pct"): (_d("9"), _d("20"), True),
        ("P1", "exact"): (_d("9"), _d("0"), False),
        ("P2", "pct_only"): (_d("11"), _d("10"), False),  # |diff| > 1% of right
        ("P2", "abs_pct"): (_d("11"), _d("20"), True),
        ("P2", "exact"): (_d("11"), _d("0"), False),
        ("P3", "pct_only"): (_d("-9"), _d("10"), True),  # tolerance uses |right|
        ("P3", "abs_pct"): (_d("-9"), _d("20"), True),
        ("P3", "exact"): (_d("-9"), _d("0"), False),
        ("P4", "pct_only"): (_d("15"), _d("10"), False),
        ("P4", "abs_pct"): (_d("15"), _d("20"), True),
        ("P4", "exact"): (_d("15"), _d("0"), False),
        ("P5", "pct_only"): (_d("25"), _d("10"), False),
        ("P5", "abs_pct"): (_d("25"), _d("20"), False),
        ("P5", "exact"): (_d("25"), _d("0"), False),
    }
    assert {(d[0], d[2]) for d in diffs} == {k for k, v in got.items() if not v[2]}


def test_each_measure_gets_its_own_tolerance(pct_tables):
    """Tolerances are keyed per measure (not by name lookup): 'net' is loose, 'gross' is exact."""
    db = pct_tables
    rule = _rule(
        left="wd",
        right="gp",
        group_by=[{"left": "acct", "right": "ACCT"}],
        measures=[
            {"name": "net", "left": "SUM(amt)", "right": "SUM(AMT)", "tolerance": {"abs": 50}},
            {"name": "gross", "left": "SUM(amt)", "right": "SUM(AMT)"},
            {"name": "other", "left": "SUM(amt)", "right": "SUM(AMT)", "tolerance": {"pct": "0.5"}},
        ],
    )
    totals, diffs = _load(db, generate(rule, _ctx(_ds("wd", "t_ct_wd.pct"), _ds("gp", "t_ct_gp.pct"))), SNAPSHOT_PARAMS)
    for acct in ("P1", "P2", "P3", "P4", "P5"):
        assert totals[(acct, "net")][3:] == (_d("50"), True)
        assert totals[(acct, "gross")][3:] == (_d("0"), False)
        assert totals[(acct, "other")][3] == _d("500") and totals[(acct, "other")][4] is True
    assert {d[2] for d in diffs} == {"gross"} and len(diffs) == 5


def test_pct_tolerance_boundary_is_inclusive(pct_tables):
    db = pct_tables
    db.execute("UPDATE t_ct_wd.pct SET amt = 1010 WHERE acct = 'P2'")  # exactly 1% of 1000
    rule = _rule(
        left="wd",
        right="gp",
        group_by=[{"left": "acct", "right": "ACCT"}],
        measures=[{"name": "m", "left": "SUM(amt)", "right": "SUM(AMT)", "tolerance": {"pct": "0.01"}}],
    )
    totals, _ = _load(db, generate(rule, _ctx(_ds("wd", "t_ct_wd.pct"), _ds("gp", "t_ct_gp.pct"))), SNAPSHOT_PARAMS)
    assert totals[("P2", "m")][4] is True


# --------------------------------------------------------------------------- periods


def test_period_filter_on_datetime_column(schemas):
    db = schemas
    db.execute("CREATE TABLE t_ct_wd.dated (acct NVARCHAR(10), amt DECIMAL(18,2), pay_date DATE)")
    db.execute("CREATE TABLE t_ct_gp.dated (ACCT NVARCHAR(10), AMT NUMERIC(19,5), TRXDATE DATETIME)")
    for row in [("A", "10", "2026-09-30"), ("A", "1", "2026-10-01"), ("A", "5", "2026-08-31")]:
        db.execute("INSERT INTO t_ct_wd.dated VALUES (?, ?, ?)", *row)
    for row in [
        ("A", "10", "2026-09-30 14:35:00"),  # late on the last day: belongs to September
        ("A", "1", "2026-10-01 00:00:00"),  # first instant of October
        ("A", "7", "2026-08-31 23:59:59"),
    ]:
        db.execute("INSERT INTO t_ct_gp.dated VALUES (?, ?, ?)", *row)
    rule = _rule(
        left="wd",
        right="gp",
        group_by=[{"left": "acct", "right": "ACCT"}],
        measures=[{"name": "amt", "left": "SUM(amt)", "right": "SUM(AMT)"}, {"name": "n", "left": "COUNT(*)", "right": "COUNT(*)"}],
    )
    g = generate(rule, _ctx(_ds("wd", "t_ct_wd.dated", "pay_date"), _ds("gp", "t_ct_gp.dated", "TRXDATE")))

    sep = params_for(resolve_period("2026-09", AS_OF), AS_OF)
    totals, diffs = _load(db, g, sep)
    assert totals == {
        ("A", "amt"): (_d("10"), _d("10"), _d("0"), _d("0"), True),
        ("A", "n"): (_d("1"), _d("1"), _d("0"), _d("0"), True),
    }
    assert diffs == set()
    (counts,) = db.query(PARAM_HEADER + g.counts_sql, *sep)
    assert (counts["rows_left"], counts["rows_right"]) == (1, 1)

    aug = params_for(resolve_period("2026-08", AS_OF), AS_OF)
    totals, diffs = _load(db, g, aug)
    assert totals[("A", "amt")] == (_d("5"), _d("7"), _d("-2"), _d("0"), False)
    assert diffs == {("A", "total_mismatch", "amt", "5.000000", "7.000000", _d("-2"))}
