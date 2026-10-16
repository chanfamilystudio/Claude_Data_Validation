"""record_match SQL executed on SQL Server: every scenario of the Task 5 table plus period bounds."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pyodbc
import pytest

from recon.config.models import DatasetConfig, MappingConfig, RecordMatchRule
from recon.connectors.sqlserver import SqlServerConnector
from recon.periods import resolve_period
from recon.sqlgen import generate
from recon.sqlgen.common import DIFF_COLUMNS, DIFFS_DDL, PARAM_HEADER, GenContext, params_for

pytestmark = pytest.mark.integration

AS_OF = date(2026, 9, 30)  # "today" for the snapshot scenarios
SNAPSHOT_PARAMS = params_for(None, AS_OF)

_TABLES = (
    "t_wd.workers", "t_gp.employees", "t_xref.cc_map", "t_wd.ck", "t_gp.ck",
    "t_wd.jrnl", "t_gp.gl", "t_wd.cs", "t_gp.cs", "t_wd.lossy", "t_gp.lossy", "t_xref.plain_map",
    "t_wd.typed", "t_gp.typed",
)
_SCHEMAS = ("t_wd", "t_gp", "t_xref")


def _drop_all(db) -> None:
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


def _rule(**fields) -> RecordMatchRule:
    return RecordMatchRule.model_validate(
        {"id": "t_rule", "domain": "employees", "check_type": "record_match", "severity": "high", **fields}
    )


def _diffs(db, sql, params) -> set[tuple]:
    return _rows_as_set(db.query(PARAM_HEADER + sql, *params))


def _rows_as_set(rows) -> set[tuple]:
    return {
        (r["business_key"], r["break_type"], r["field_name"], r["left_value"], r["right_value"], r["diff"])
        for r in rows
    }


def _diffs_via_temp_table(db, sql, params) -> set[tuple]:
    """Run diff_sql exactly as the executor wraps it: SELECT * INTO #diffs FROM (<diff_sql>) d.

    A batch with bound parameters runs as an RPC (sp_executesql-like), so a temp table it creates
    is dropped when the call returns: the read must happen in the same batch.
    """
    into = PARAM_HEADER + f"SELECT * INTO #diffs FROM (\n{sql}\n) d;\n"
    cols = db.query(
        into + "SELECT c.name, TYPE_NAME(c.user_type_id) AS type_name, c.max_length, c.precision, c.scale"
        " FROM tempdb.sys.columns c WHERE c.object_id = OBJECT_ID('tempdb..#diffs') ORDER BY c.column_id",
        *params,
    )
    assert [(c["name"], c["type_name"], c["max_length"], c["precision"], c["scale"]) for c in cols] == [
        ("business_key", "nvarchar", 800, 0, 0),
        ("break_type", "varchar", 30, 0, 0),
        ("field_name", "nvarchar", 256, 0, 0),
        ("left_value", "nvarchar", 8000, 0, 0),
        ("right_value", "nvarchar", 8000, 0, 0),
        ("diff", "decimal", 17, 38, 6),
    ]
    return _rows_as_set(db.query(into + "SELECT * FROM #diffs", *params))


# --------------------------------------------------------------------------- employee master

WD = _ds("wd", "t_wd.workers")
GP = _ds("gp", "t_gp.employees")
CC_MAP = MappingConfig.model_validate(
    {
        "name": "cc_to_dept",
        "object": "t_xref.cc_map",
        "from": "wd_cost_center",
        "to": "gp_dept",
        "effective": {"start": "eff_start", "end": "eff_end"},
    }
)
EMP_RULE = _rule(
    left="wd",
    right="gp",
    keys=[{"left": "employee_id", "right": "EMPLOYID"}],
    compare=[
        {"left": "cost_center", "right": "DEPRTMNT", "via_mapping": "cc_to_dept"},
        {"left": "hire_date", "right": "STRTDATE", "type": "date"},
        {"left": "annual_salary", "right": "SALARY", "type": "decimal", "tolerance": {"abs": "0.01"}},
        {"left": "national_id", "right": "SOCSCNUM", "pii": True},
    ],
)

# (employee_id, cost_center, hire_date, annual_salary, national_id)
WD_ROWS = [
    ("E100", "CC1", "2020-01-01", "50000.00", "111-11-1111"),  # identical row
    ("e001 ", "cc1", "2020-01-01", "100.00", "222"),  # key differs only by case/space
    ("E200", "CC1", "2020-01-01", "100.00", "333"),  # text vs float, within tolerance
    ("E300", "CC1", "2020-01-01", "100.00", "444"),  # salary out of tolerance
    ("E400", "CC1", "2020-01-01", "abc", "555"),  # failed cast
    ("E600", "CC1", "2020-01-01", "1.00", "666"),  # only in Workday
    ("E700", "CC1", "2020-01-01", "1.00", "777-77-7777"),  # national id differs
    ("E800", "CC_NONE", "2020-01-01", "1.00", "888"),  # no crosswalk row
    ("E900", "CC2", "2020-01-01", "1.00", "999"),  # maps to a different department
    ("E950", "CC1", "2019-12-31", "9.00", "000"),  # duplicate in GP, fields differ too
]
# (EMPLOYID, DEPRTMNT, STRTDATE, SALARY, SOCSCNUM) -- GP-style padded CHAR, DATETIME and FLOAT
GP_ROWS = [
    ("E100", "D1", "2020-01-01", 50000.0, "111-11-1111"),
    ("E001", "D1", "2020-01-01", 100.0, "222"),
    ("E200", "D1", "2020-01-01", 100.004, "333"),
    ("E300", "D1", "2020-01-01", 100.02, "444"),
    ("E400", "D1", "2020-01-01", 5.0, "555"),
    ("E500", "D1", "2020-01-01", 1.0, "500"),  # only in GP
    ("E700", "D1", "2020-01-01", 1.0, "777-77-0000"),
    ("E800", "D1", "2020-01-01", 1.0, "888"),
    ("E900", "D1", "2020-01-01", 1.0, "999"),
    ("E950", "D1", "2020-01-01", 1.0, "000"),
    ("E950", "D1", "2020-01-01", 2.0, "000"),
]
# (wd_cost_center, gp_dept, eff_start, eff_end)
MAP_ROWS = [
    ("CC1", "D_OLD", "2000-01-01", "2019-12-31"),  # expired: must not be used
    ("CC1", "D1", "2020-01-01", None),
    ("CC2", "D2", "2020-01-01", None),
    ("CC3", "D3", "2030-01-01", None),  # not yet valid
]

EXPECTED_EMP = {
    ("E300", "value_mismatch", "annual_salary", "100.000000", "100.020000", Decimal("-0.020000")),
    ("E400", "value_mismatch", "annual_salary", "abc", "5.000000", None),
    ("E500", "missing_left", None, None, None, None),
    ("E600", "missing_right", None, None, None, None),
    ("E700", "value_mismatch", "national_id", "***", "***", None),
    ("E800", "mapping_missing", "cost_center", "CC_NONE", "D1", None),
    ("E900", "value_mismatch", "cost_center", "D2", "D1", None),
    ("E950", "quality_violation", "__key__", "1", "2", None),
}


@pytest.fixture
def emp_tables(schemas):
    db = schemas
    db.execute(
        "CREATE TABLE t_wd.workers (employee_id NVARCHAR(20), cost_center NVARCHAR(20),"
        " hire_date NVARCHAR(20), annual_salary NVARCHAR(20), national_id NVARCHAR(20))"
    )
    db.execute(
        "CREATE TABLE t_gp.employees (EMPLOYID CHAR(15), DEPRTMNT CHAR(6), STRTDATE DATETIME,"
        " SALARY FLOAT, SOCSCNUM CHAR(15))"
    )
    db.execute(
        "CREATE TABLE t_xref.cc_map (wd_cost_center NVARCHAR(20), gp_dept NVARCHAR(20),"
        " eff_start DATE, eff_end DATE)"
    )
    for row in WD_ROWS:
        db.execute("INSERT INTO t_wd.workers VALUES (?, ?, ?, ?, ?)", *row)
    for row in GP_ROWS:
        db.execute("INSERT INTO t_gp.employees VALUES (?, ?, ?, ?, ?)", *row)
    for row in MAP_ROWS:
        db.execute("INSERT INTO t_xref.cc_map VALUES (?, ?, ?, ?)", *row)
    return db


def test_employee_scenarios_exact_diff_set(emp_tables):
    db = emp_tables
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)  # no ambiguity: must not raise
    assert _diffs(db, g.diff_sql, SNAPSHOT_PARAMS) == EXPECTED_EMP


def test_diff_sql_runs_as_derived_table_with_contract_types(emp_tables):
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    assert _diffs_via_temp_table(emp_tables, g.diff_sql, SNAPSHOT_PARAMS) == EXPECTED_EMP


def test_executor_temp_table_pattern(emp_tables):
    """DDL in a parameterless batch, INSERT ... SELECT with bound params, read in a separate call."""
    db = emp_tables
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    db.execute("DROP TABLE IF EXISTS #diffs")
    db.execute(DIFFS_DDL)
    try:
        db.execute(
            PARAM_HEADER + f"INSERT INTO #diffs ({DIFF_COLUMNS}) SELECT * FROM (\n{g.diff_sql}\n) d",
            *SNAPSHOT_PARAMS,
        )
        assert _rows_as_set(db.query("SELECT * FROM #diffs")) == EXPECTED_EMP
    finally:
        db.execute("DROP TABLE IF EXISTS #diffs")


def test_counts_sql(emp_tables):
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    rows = emp_tables.query(PARAM_HEADER + g.counts_sql, *SNAPSHOT_PARAMS)
    assert rows == [{"rows_left": len(WD_ROWS), "rows_right": len(GP_ROWS)}]


def test_full_text_batches_execute(emp_tables):
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    batches = g.full_text().split("\nGO\n")
    assert len(batches) == 3
    for batch in batches:
        assert batch.startswith(PARAM_HEADER)
        emp_tables.query(batch, *SNAPSHOT_PARAMS)


def test_mapping_as_of_selects_the_valid_row(emp_tables):
    """As of 2019-06-30 only the expired CC1 -> D_OLD row is valid, so every CC1 row mismatches."""
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    diffs = _diffs(emp_tables, g.diff_sql, params_for(None, date(2019, 6, 30)))
    cc = {d[0]: d for d in diffs if d[2] == "cost_center"}
    assert cc["E100"] == ("E100", "value_mismatch", "cost_center", "D_OLD", "D1", None)
    assert cc["E001"][3] == "D_OLD"
    # CC2 -> D2 only becomes valid in 2020
    assert cc["E900"] == ("E900", "mapping_missing", "cost_center", "CC2", "D1", None)


def test_ambiguous_effective_mapping_raises(emp_tables):
    db = emp_tables
    db.execute("INSERT INTO t_xref.cc_map VALUES (N'cc1 ', N'D9', '2026-01-01', NULL)")
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    with pytest.raises(pyodbc.Error, match="ambiguous mapping cc_to_dept: value CC1 has 2 valid rows"):
        db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)
    # the same rows are not ambiguous before the new row becomes valid
    db.query(PARAM_HEADER + g.checks_sql, *params_for(None, date(2025, 12, 31)))


def test_null_and_blank_semantics(emp_tables):
    db = emp_tables
    db.execute("DELETE FROM t_wd.workers")
    db.execute("DELETE FROM t_gp.employees")
    # blank text is NULL (trim on); NULL = NULL; NULL vs value is a mismatch
    db.execute("INSERT INTO t_wd.workers VALUES (N'N1', NULL, N' ', N'', N'  ')")
    db.execute("INSERT INTO t_gp.employees VALUES ('N1', NULL, NULL, NULL, NULL)")
    db.execute("INSERT INTO t_wd.workers VALUES (N'N2', N'CC1', NULL, N'7', N'x')")
    db.execute("INSERT INTO t_gp.employees VALUES ('N2', NULL, '2020-01-01', NULL, 'x')")
    g = generate(EMP_RULE, _ctx(WD, GP, mappings=(CC_MAP,)))
    assert _diffs(db, g.diff_sql, SNAPSHOT_PARAMS) == {
        ("N2", "value_mismatch", "cost_center", "D1", None, None),
        ("N2", "value_mismatch", "hire_date", None, "2020-01-01", None),
        ("N2", "value_mismatch", "annual_salary", "7.000000", None, None),
    }


def test_undated_mapping_with_duplicate_from_rows_raises(emp_tables):
    db = emp_tables
    db.execute("CREATE TABLE t_xref.plain_map (wd_cc NVARCHAR(20), gp_dept NVARCHAR(20))")
    db.execute("INSERT INTO t_xref.plain_map VALUES (N'CC1', N'D1'), (N'CC2', N'D2')")
    plain = MappingConfig.model_validate(
        {"name": "cc_plain", "object": "t_xref.plain_map", "from": "wd_cc", "to": "gp_dept"}
    )
    rule = EMP_RULE.model_copy(
        update={"compare": [EMP_RULE.compare[0].model_copy(update={"via_mapping": "cc_plain"})]}
    )
    g = generate(rule, _ctx(WD, GP, mappings=(plain,)))
    db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)  # unique: must not raise
    db.execute("INSERT INTO t_xref.plain_map VALUES (N' cc1', N'D9')")
    with pytest.raises(pyodbc.Error, match="ambiguous mapping cc_plain: value CC1 has 2 valid rows"):
        db.query(PARAM_HEADER + g.checks_sql, *SNAPSHOT_PARAMS)


# --------------------------------------------------------------------------- lossless text


def test_float_and_datetime_compared_as_string_are_lossless(schemas):
    """Default `string` type: floats keep full precision and datetimes keep seconds (style 126)."""
    db = schemas
    for t in ("t_wd.lossy", "t_gp.lossy"):
        db.execute(f"CREATE TABLE {t} (k FLOAT, f FLOAT, d DATETIME)")
    db.execute(
        "INSERT INTO t_wd.lossy VALUES (1234567, 123456.78, '2026-09-30T14:35:07'),"
        " (1234568, 1, '2026-01-01T00:00:00')"
    )
    db.execute(
        "INSERT INTO t_gp.lossy VALUES (1234567, 123456.79, '2026-09-30T14:35:08'),"
        " (1234568, 1, '2026-01-01T00:00:00')"
    )
    rule = _rule(
        left="wd_lossy",
        right="gp_lossy",
        keys=[{"left": "k", "right": "k"}],
        compare=[{"left": "f", "right": "f"}, {"left": "d", "right": "d"}],
    )
    g = generate(rule, _ctx(_ds("wd_lossy", "t_wd.lossy"), _ds("gp_lossy", "t_gp.lossy")))
    key = "1.234567000000000E+006"  # 1234567 and 1234568 stay distinct keys (no __key__ duplicate)
    assert _diffs(db, g.diff_sql, SNAPSHOT_PARAMS) == {
        (key, "value_mismatch", "f", "1.234567800000000E+005", "1.234567900000000E+005", None),
        (key, "value_mismatch", "d", "2026-09-30T14:35:07", "2026-09-30T14:35:08", None),
    }


def test_int_and_bool_casts_are_not_lossy(schemas):
    """`int` goes through DECIMAL(38,6): a fractional value is a failed cast, not rounded or truncated.
    `bool` accepts 0/1 numbers and true/false/yes/no/y/n/1/0 text only; anything else is a failed cast."""
    db = schemas
    db.execute("CREATE TABLE t_wd.typed (k NVARCHAR(10), i INT, b NVARCHAR(10))")
    db.execute("CREATE TABLE t_gp.typed (k NVARCHAR(10), i NUMERIC(19,5), b BIT)")
    rows = (  # k, left int, right numeric(19,5), left bool text, right bit
        ("K1", 100, "100.70000", "1", 1),
        ("K2", 100, "100.00000", "y", 1),
        ("K3", 5, "5", "2", 1),
        ("K4", 5, "5", "TRUE", 1),
        ("K5", 5, "5", " No ", 0),
        ("K6", 5, "5", "maybe", 0),
        ("K7", 5, "5.00001", "false", 0),
    )
    for k, li, ri, lb, rb in rows:
        db.execute("INSERT INTO t_wd.typed VALUES (?, ?, ?)", k, li, lb)
        db.execute("INSERT INTO t_gp.typed VALUES (?, ?, ?)", k, Decimal(ri), rb)
    rule = _rule(
        left="wd_typed",
        right="gp_typed",
        keys=[{"left": "k", "right": "k"}],
        compare=[{"left": "i", "right": "i", "type": "int"}, {"left": "b", "right": "b", "type": "bool"}],
    )
    g = generate(rule, _ctx(_ds("wd_typed", "t_wd.typed"), _ds("gp_typed", "t_gp.typed")))
    assert _diffs(db, g.diff_sql, SNAPSHOT_PARAMS) == {
        ("K1", "value_mismatch", "i", "100", "100.70000", None),  # was truncated to 100 = equal
        ("K7", "value_mismatch", "i", "5", "5.00001", None),  # 6th decimal still counts
        ("K3", "value_mismatch", "b", "2", "1", None),  # was any non-zero = true
        ("K6", "value_mismatch", "b", "maybe", "0", None),
    }


# --------------------------------------------------------------------------- composite keys


def test_composite_keys_containing_separator_match_themselves(schemas):
    db = schemas
    for t in ("t_wd.ck", "t_gp.ck"):
        db.execute(f"CREATE TABLE {t} (k1 NVARCHAR(10), k2 NVARCHAR(10), v INT)")
        for row in (("A|B", "C", 1), ("A", "B|C", 2), ("A\\", "|C", 3), ("A\\|", "C", 4)):
            db.execute(f"INSERT INTO {t} VALUES (?, ?, ?)", *row)
    rule = _rule(
        left="wd_ck",
        right="gp_ck",
        keys=[{"left": "k1", "right": "k1"}, {"left": "k2", "right": "k2"}],
        compare=[{"left": "v", "right": "v", "type": "int"}],
    )
    g = generate(rule, _ctx(_ds("wd_ck", "t_wd.ck"), _ds("gp_ck", "t_gp.ck")))
    assert _diffs(db, g.diff_sql, SNAPSHOT_PARAMS) == set()
    keys = db.query(
        PARAM_HEADER + "SELECT DISTINCT business_key FROM (" + g.diff_sql.replace(
            "WHERE k.l_rows = 1 AND k.r_rows = 1 AND f.break_type IS NOT NULL",
            "WHERE k.l_rows = 1 AND k.r_rows = 1",
        ) + ") d",
        *SNAPSHOT_PARAMS,
    )
    assert {r["business_key"] for r in keys} == {"A\\|B|C", "A|B\\|C", "A\\\\|\\|C", "A\\\\\\||C"}


# --------------------------------------------------------------------------- normalization


def test_case_sensitive_rule_under_case_insensitive_collation(schemas):
    db = schemas
    assert db.scalar("SELECT CONVERT(NVARCHAR(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation'))").find("_CI_") > 0
    for t in ("t_wd.cs", "t_gp.cs"):
        db.execute(f"CREATE TABLE {t} (k NVARCHAR(10), v NVARCHAR(10))")
    db.execute("INSERT INTO t_wd.cs VALUES (N'a', N'x'), (N'K1', N'abc')")
    db.execute("INSERT INTO t_gp.cs VALUES (N'A', N'x'), (N'K1', N'ABC')")
    ctx = _ctx(_ds("wd_cs", "t_wd.cs"), _ds("gp_cs", "t_gp.cs"))
    common = {
        "left": "wd_cs",
        "right": "gp_cs",
        "keys": [{"left": "k", "right": "k"}],
        "compare": [{"left": "v", "right": "v"}],
    }
    sensitive = generate(_rule(normalize={"case_insensitive": False}, **common), ctx)
    assert _diffs(db, sensitive.diff_sql, SNAPSHOT_PARAMS) == {
        ("a", "missing_right", None, None, None, None),
        ("A", "missing_left", None, None, None, None),
        ("K1", "value_mismatch", "v", "abc", "ABC", None),
    }
    insensitive = generate(_rule(**common), ctx)
    assert _diffs(db, insensitive.diff_sql, SNAPSHOT_PARAMS) == set()


# --------------------------------------------------------------------------- periods


def test_period_datetime_boundary(schemas):
    """A GP TRXDATE of 2026-09-30 14:35 belongs to 2026-09, not 2026-10 (Review Focus 1)."""
    db = schemas
    db.execute("CREATE TABLE t_wd.jrnl (id NVARCHAR(10), amount DECIMAL(19,5), pay_date DATE)")
    db.execute("CREATE TABLE t_gp.gl (JRNENTRY INT, AMOUNT DECIMAL(19,5), TRXDATE DATETIME)")
    db.execute("INSERT INTO t_wd.jrnl VALUES (N'1', 10, '2026-09-30'), (N'1', 10, '2026-10-01')")
    db.execute("INSERT INTO t_gp.gl VALUES (1, 10, '2026-09-30T14:35:00')")
    rule = _rule(
        left="wd_jrnl",
        right="gp_gl",
        keys=[{"left": "id", "right": "JRNENTRY"}],
        compare=[{"left": "amount", "right": "AMOUNT", "type": "decimal"}],
    )
    ctx = _ctx(_ds("wd_jrnl", "t_wd.jrnl", "pay_date"), _ds("gp_gl", "t_gp.gl", "TRXDATE"))
    g = generate(rule, ctx)
    sep = params_for(resolve_period("2026-09", AS_OF), AS_OF)
    oct_ = params_for(resolve_period("2026-10", AS_OF), AS_OF)

    assert db.query(PARAM_HEADER + g.counts_sql, *sep) == [{"rows_left": 1, "rows_right": 1}]
    assert _diffs(db, g.diff_sql, sep) == set()

    assert db.query(PARAM_HEADER + g.counts_sql, *oct_) == [{"rows_left": 1, "rows_right": 0}]
    assert _diffs(db, g.diff_sql, oct_) == {("1", "missing_right", None, None, None, None)}
