"""mapping and quality SQL executed on SQL Server, the way the executor runs it.

``diff_sql`` is loaded into a ``#diffs`` temp table created from ``DIFFS_DDL`` (parameterless batch)
with ``INSERT INTO #diffs (...) SELECT * FROM (<diff_sql>) d`` under bound parameters.
"""

from __future__ import annotations

from datetime import date

import pyodbc
import pytest

from recon.config.models import DatasetConfig, MappingConfig, MappingRule, QualityRule
from recon.connectors.sqlserver import SqlServerConnector
from recon.sqlgen import generate
from recon.sqlgen.common import DIFF_COLUMNS, DIFFS_DDL, PARAM_HEADER, GenContext, params_for

pytestmark = pytest.mark.integration

PARAMS = params_for(None, date(2026, 9, 30))

_TABLES = (
    "t_mq_wd.workers", "t_mq_gp.depts", "t_mq_xref.cc_map", "t_mq_xref.dated_map",
    "t_mq_gp.emp", "t_mq_gp.pairs",
)
_SCHEMAS = ("t_mq_wd", "t_mq_gp", "t_mq_xref")


def _drop_all(db) -> None:
    db.execute("IF OBJECT_ID('tempdb..#diffs') IS NOT NULL DROP TABLE #diffs")
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


def _ds(name: str, obj: str) -> DatasetConfig:
    return DatasetConfig(name=name, source="s", object=obj)


def _ctx(*datasets: DatasetConfig, mappings: tuple[MappingConfig, ...] = ()) -> GenContext:
    conn = SqlServerConnector(None)
    return GenContext(
        datasets={d.name: d for d in datasets},
        sources={d.name: conn.as_sql_source(d) for d in datasets},
        mappings={m.name: m for m in mappings},
    )


def _map(name: str, obj: str, frm: str, to: str, effective: dict | None = None) -> MappingConfig:
    data = {"name": name, "object": obj, "from": frm, "to": to}
    if effective:
        data["effective"] = effective
    return MappingConfig.model_validate(data)


def _load(db, generated, params=PARAMS) -> set[tuple]:
    """Fill #diffs exactly as the executor does; return the (key, type, field, left, right, diff) set."""
    db.execute("IF OBJECT_ID('tempdb..#diffs') IS NOT NULL DROP TABLE #diffs")
    db.execute(DIFFS_DDL)
    db.execute(
        PARAM_HEADER + f"INSERT INTO #diffs ({DIFF_COLUMNS}) SELECT * FROM (\n{generated.diff_sql}\n) d",
        *params,
    )
    return {
        (r["business_key"], r["break_type"], r["field_name"], r["left_value"], r["right_value"], r["diff"])
        for r in db.query("SELECT * FROM #diffs")
    }


def _counts(db, generated, params=PARAMS) -> tuple[int, int]:
    (row,) = db.query(PARAM_HEADER + generated.counts_sql, *params)
    return row["rows_left"], row["rows_right"]


# --------------------------------------------------------------------------- mapping

WORKERS = _ds("wd_workers", "t_mq_wd.workers")
DEPTS = _ds("gp_depts", "t_mq_gp.depts")
CC_MAP = _map("cc_to_dept", "t_mq_xref.cc_map", "wd_cc", "gp_dept")


def _mapping_rule(mapping: str = "cc_to_dept") -> MappingRule:
    return MappingRule.model_validate(
        {
            "id": "t_map", "domain": "employees", "check_type": "mapping", "severity": "low",
            "dataset": "wd_workers", "column": "cost_center", "mapping": mapping,
            "target": {"dataset": "gp_depts", "column": "DEPRTMNT"},
        }
    )


@pytest.fixture
def mapping_tables(schemas):
    db = schemas
    db.execute("CREATE TABLE t_mq_wd.workers (cost_center NVARCHAR(30) NULL)")
    db.execute("CREATE TABLE t_mq_gp.depts (DEPRTMNT NVARCHAR(30) NULL)")
    db.execute("CREATE TABLE t_mq_xref.cc_map (wd_cc NVARCHAR(30) NULL, gp_dept NVARCHAR(30) NULL)")
    for value in ("CC1", "cc1", "cc2 ", "CC3", "CC4", "CC5", "CC6", "C|C\\7", None, "   ", None):
        db.execute("INSERT INTO t_mq_wd.workers VALUES (?)", value)
    for value in ("D1", "d2 ", "D3", None, "  "):
        db.execute("INSERT INTO t_mq_gp.depts VALUES (?)", value)
    for frm, to in (
        ("CC1", "D1"),
        (" Cc2", "D2"),  # trim + case folding on both the mapping and the target side
        ("CC4", "D9"),  # mapped to a department GP does not have
        ("CC5", None),  # mapped to nothing
        ("CC6", "   "),  # mapped to a blank
        ("CC8", "D3"),  # unused crosswalk row
        (None, "D1"),  # crosswalk row without a from value never matches
    ):
        db.execute("INSERT INTO t_mq_xref.cc_map VALUES (?, ?)", frm, to)
    return db


def test_mapping_missing_and_mismatch(mapping_tables):
    db = mapping_tables
    g = generate(_mapping_rule(), _ctx(WORKERS, DEPTS, mappings=(CC_MAP,)))
    db.query(PARAM_HEADER + g.checks_sql, *PARAMS)  # no ambiguity: must not raise
    assert _load(db, g) == {
        ("CC3", "mapping_missing", "cost_center", "CC3", None, None),
        ("C\\|C\\\\7", "mapping_missing", "cost_center", "C|C\\7", None, None),
        ("CC4", "mapping_mismatch", "cost_center", "CC4", "D9", None),
        ("CC5", "mapping_mismatch", "cost_center", "CC5", None, None),
        ("CC6", "mapping_mismatch", "cost_center", "CC6", None, None),
    }


def test_mapping_counts_are_distinct_non_null_values(mapping_tables):
    g = generate(_mapping_rule(), _ctx(WORKERS, DEPTS, mappings=(CC_MAP,)))
    # CC1/cc1 collapse, null and blank are not values: CC1 CC2 CC3 CC4 CC5 CC6 C|C\7 ; D1 D2 D3
    assert _counts(mapping_tables, g) == (7, 3)


def test_mapping_all_mapped_is_clean(mapping_tables):
    db = mapping_tables
    db.execute("DELETE FROM t_mq_wd.workers WHERE cost_center NOT IN ('CC1', 'cc2 ')")
    g = generate(_mapping_rule(), _ctx(WORKERS, DEPTS, mappings=(CC_MAP,)))
    assert _load(db, g) == set()


def test_mapping_effective_dating_uses_as_of(schemas):
    db = schemas
    db.execute("CREATE TABLE t_mq_wd.workers (cost_center NVARCHAR(30) NULL)")
    db.execute("CREATE TABLE t_mq_gp.depts (DEPRTMNT NVARCHAR(30) NULL)")
    db.execute(
        "CREATE TABLE t_mq_xref.dated_map (wd_cc NVARCHAR(30), gp_dept NVARCHAR(30), eff_start DATE, eff_end DATE NULL)"
    )
    for value in ("CC1", "CC2"):
        db.execute("INSERT INTO t_mq_wd.workers VALUES (?)", value)
    db.execute("INSERT INTO t_mq_gp.depts VALUES ('D1')")
    db.execute("INSERT INTO t_mq_xref.dated_map VALUES ('CC1', 'D1', '2026-01-01', NULL)")
    db.execute("INSERT INTO t_mq_xref.dated_map VALUES ('CC2', 'D1', '2025-01-01', '2026-06-30')")  # expired
    mapping = _map("dated", "t_mq_xref.dated_map", "wd_cc", "gp_dept", {"start": "eff_start", "end": "eff_end"})
    g = generate(_mapping_rule("dated"), _ctx(WORKERS, DEPTS, mappings=(mapping,)))
    assert _load(db, g) == {("CC2", "mapping_missing", "cost_center", "CC2", None, None)}


def test_mapping_ambiguity_is_refused(mapping_tables):
    db = mapping_tables
    db.execute("INSERT INTO t_mq_xref.cc_map VALUES ('cc1 ', 'D3')")
    g = generate(_mapping_rule(), _ctx(WORKERS, DEPTS, mappings=(CC_MAP,)))
    with pytest.raises(pyodbc.Error, match="ambiguous mapping cc_to_dept"):
        db.query(PARAM_HEADER + g.checks_sql, *PARAMS)


# --------------------------------------------------------------------------- quality

EMP = _ds("gp_emp", "t_mq_gp.emp")
EMP_DEPTS = _ds("gp_depts", "t_mq_gp.depts")

# (EMPLOYID, DEPRTMNT, INACTIVE, STATUS, STRTDATE)
EMP_ROWS = [
    ("000001", "D1", 0, "A", "2020-01-01"),  # clean
    ("000002", "d2 ", 1, " a", "2021-05-05"),  # clean after normalization
    ("000003", "  ", 0, "A", "2020-01-01"),  # blank department -> not_null
    (None, "D1", 0, "A", "2020-01-01"),  # null key -> not_null (key shown as the null marker)
    ("000004", "D1", 0, "A", "2020-01-01"),  # duplicated key (twice) -> unique
    ("000004", "d1", 1, "I", "2020-01-01"),
    ("000005", "D1", 5, "A", "2020-01-01"),  # accepted_values INACTIVE
    ("000006", "D9", 0, "A", "2020-01-01"),  # references: no such department
    ("00007x", "D1", 0, "A", "2020-01-01"),  # like
    ("000008", "D1", 0, "A", "2099-01-01"),  # custom_sql: future hire date
    ("000009", "D1", 0, "x", "2020-01-01"),  # accepted_values STATUS
    ("  ", "D1", 0, "A", "2020-01-01"),  # blank key: not_null; skipped by unique and like
]
QUALITY_ASSERTIONS = [
    {"not_null": ["EMPLOYID", "DEPRTMNT"]},
    {"unique": ["EMPLOYID"]},
    {"accepted_values": {"column": "INACTIVE", "values": [0, 1]}},
    {"accepted_values": {"column": "STATUS", "values": ["A", "i"]}},
    {"references": {"column": "DEPRTMNT", "dataset": "gp_depts", "ref_column": "DEPRTMNT"}},
    {"like": {"column": "EMPLOYID", "pattern": "[0-9][0-9][0-9][0-9][0-9][0-9]"}},
    {"custom_sql": {"name": "future_hire_dates", "sql": "SELECT EMPLOYID AS business_key FROM {dataset} WHERE STRTDATE > '2026-09-30'"}},
]


def _quality_rule(assertions, dataset="gp_emp", key=("EMPLOYID",)) -> QualityRule:
    return QualityRule.model_validate(
        {
            "id": "t_quality", "domain": "employees", "check_type": "quality", "severity": "low",
            "dataset": dataset, "key": list(key), "assertions": assertions,
        }
    )


@pytest.fixture
def emp_tables(schemas):
    db = schemas
    db.execute(
        "CREATE TABLE t_mq_gp.emp (EMPLOYID NVARCHAR(20) NULL, DEPRTMNT NVARCHAR(20) NULL, INACTIVE SMALLINT NULL,"
        " STATUS NVARCHAR(5) NULL, STRTDATE DATE NULL)"
    )
    db.execute("CREATE TABLE t_mq_gp.depts (DEPRTMNT NVARCHAR(30) NULL)")
    for row in EMP_ROWS:
        db.execute("INSERT INTO t_mq_gp.emp VALUES (?, ?, ?, ?, ?)", *row)
    for value in ("D1", "d2", None):
        db.execute("INSERT INTO t_mq_gp.depts VALUES (?)", value)
    return db


def test_every_assertion_type_reports_its_planted_violation(emp_tables):
    db = emp_tables
    g = generate(_quality_rule(QUALITY_ASSERTIONS), _ctx(EMP, EMP_DEPTS))
    assert g.checks_sql is None
    assert _load(db, g) == {
        # not_null: blank department; the null key and the blank key both show as the null marker
        ("000003", "quality_violation", "DEPRTMNT", None, None, None),
        ("∅", "quality_violation", "EMPLOYID", None, None, None),
        # unique: the duplicated value with its count
        ("000004", "quality_violation", "unique:EMPLOYID", "2", None, None),
        # accepted_values: actual value
        ("000005", "quality_violation", "INACTIVE", "5", None, None),
        ("000009", "quality_violation", "STATUS", "x", None, None),
        # references: department without a match (only D9; null and blank are not checked)
        ("000006", "quality_violation", "DEPRTMNT", "D9", None, None),
        # like: the key is normalized (upper-case), the value is shown as stored
        ("00007X", "quality_violation", "EMPLOYID", "00007x", None, None),
        # custom_sql
        ("000008", "quality_violation", "future_hire_dates", None, None, None),
    }


def test_quality_counts(emp_tables):
    g = generate(_quality_rule(QUALITY_ASSERTIONS), _ctx(EMP, EMP_DEPTS))
    assert _counts(emp_tables, g) == (len(EMP_ROWS), 0)


def test_clean_data_has_no_violations(emp_tables):
    db = emp_tables
    db.execute("DELETE FROM t_mq_gp.emp WHERE ISNULL(EMPLOYID, '') NOT IN ('000001', '000002')")
    g = generate(_quality_rule(QUALITY_ASSERTIONS), _ctx(EMP, EMP_DEPTS))
    assert _load(db, g) == set()


def test_unique_business_key_escapes_separators(schemas):
    db = schemas
    db.execute("CREATE TABLE t_mq_gp.pairs (A NVARCHAR(20) NULL, B NVARCHAR(20) NULL)")
    for a, b in (
        ("A|B", "C"), ("a|b ", "c"),  # same normalized pair
        ("A", "B|C"), ("A", "b|c"),  # different pair that joins to the same unescaped text
        ("X\\", "Y"), ("x\\", "y"),
        ("U", "V"),  # unique
        ("W", None), ("W", None),  # null parts are not duplicates
    ):
        db.execute("INSERT INTO t_mq_gp.pairs VALUES (?, ?)", a, b)
    pairs = _ds("pairs", "t_mq_gp.pairs")
    rule = _quality_rule([{"unique": ["A", "B"]}], dataset="pairs", key=("A",))
    assert _load(db, generate(rule, _ctx(pairs))) == {
        ("A\\|B|C", "quality_violation", "unique:A,B", "2", None, None),
        ("A|B\\|C", "quality_violation", "unique:A,B", "2", None, None),
        ("X\\\\|Y", "quality_violation", "unique:A,B", "2", None, None),
    }


def test_quality_key_is_composite_and_escaped(schemas):
    db = schemas
    db.execute("CREATE TABLE t_mq_gp.pairs (A NVARCHAR(20) NULL, B NVARCHAR(20) NULL)")
    db.execute("INSERT INTO t_mq_gp.pairs VALUES ('A|B', NULL), ('A', 'B|C'), ('ok', 'x')")
    pairs = _ds("pairs", "t_mq_gp.pairs")
    rule = _quality_rule([{"not_null": ["B"]}, {"like": {"column": "A", "pattern": "ok"}}], dataset="pairs", key=("A", "B"))
    assert _load(db, generate(rule, _ctx(pairs))) == {
        ("A\\|B|∅", "quality_violation", "B", None, None, None),
        ("A\\|B|∅", "quality_violation", "A", "A|B", None, None),
        ("A|B\\|C", "quality_violation", "A", "A", None, None),
    }


def test_accepted_values_not_confused_by_blank_value(emp_tables):
    db = emp_tables
    rule = _quality_rule([{"accepted_values": {"column": "STATUS", "values": ["", "A"]}}])
    got = _load(db, generate(rule, _ctx(EMP, EMP_DEPTS)))
    assert {k[0] for k in got} == {"000004", "000009"}  # the 'I' row and the 'x' row; blank accepts nothing


def test_custom_sql_without_business_key_column_fails(emp_tables):
    db = emp_tables
    rule = _quality_rule([{"custom_sql": {"name": "bad", "sql": "SELECT EMPLOYID AS id FROM {dataset}"}}])
    g = generate(rule, _ctx(EMP, EMP_DEPTS))
    with pytest.raises(pyodbc.Error, match="business_key"):
        _load(db, g)


def test_custom_sql_accepts_explicit_alias(emp_tables):
    db = emp_tables
    rule = _quality_rule(
        [{"custom_sql": {"name": "future", "sql": "SELECT e.EMPLOYID AS business_key FROM {dataset} e WHERE e.STRTDATE > GETDATE()"}}]
    )
    assert _load(db, generate(rule, _ctx(EMP, EMP_DEPTS))) == {
        ("000008", "quality_violation", "future", None, None, None)
    }


def test_diff_columns_have_contract_types(emp_tables):
    db = emp_tables
    g = generate(_quality_rule(QUALITY_ASSERTIONS), _ctx(EMP, EMP_DEPTS))
    cols = db.query(
        PARAM_HEADER
        + f"SELECT * INTO #shape FROM (\n{g.diff_sql}\n) d;\n"
        "SELECT c.name, TYPE_NAME(c.user_type_id) AS type_name, c.max_length, c.precision, c.scale"
        " FROM tempdb.sys.columns c WHERE c.object_id = OBJECT_ID('tempdb..#shape') ORDER BY c.column_id",
        *PARAMS,
    )
    assert [(c["name"], c["type_name"], c["max_length"], c["precision"], c["scale"]) for c in cols] == [
        ("business_key", "nvarchar", 800, 0, 0),
        ("break_type", "varchar", 30, 0, 0),
        ("field_name", "nvarchar", 256, 0, 0),
        ("left_value", "nvarchar", 8000, 0, 0),
        ("right_value", "nvarchar", 8000, 0, 0),
        ("diff", "decimal", 17, 38, 6),
    ]
