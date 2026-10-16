"""Preflight column checks against a fake INFORMATION_SCHEMA (no database)."""

import pytest

from conftest import edit_rule
from recon.config.loader import load_project
from recon.planner import select_rules
from recon.preflight import preflight

FIXTURE_TABLES = {
    "stg_wd.workers": ["EMPLOYEE_ID", "Cost_Center", "hire_date", "annual_salary", "national_id"],
    "stg_gp.UPR00100": ["EMPLOYID", "DEPRTMNT", "STRTDATE", "SALARY", "SOCSCNUM", "INACTIVE"],
    "stg_gp.UPR40300": ["DEPRTMNT", "DSCRIPTN"],
    "stg_wd.payroll_journal": ["ledger_account", "debit", "credit", "pay_date", "load_ts"],
    "stg_gp.GL20000": ["ACTINDX", "DEBITAMT", "CRDTAMNT", "TRXDATE", "SOURCDOC"],
    "xref.cost_center_map": ["wd_cost_center", "gp_dept", "eff_start", "eff_end"],
    "xref.account_map": ["wd_account", "gp_actindx"],
}


class FakeDb:
    """Answers SqlServerConnector.describe()'s INFORMATION_SCHEMA query from a dict."""

    def __init__(self, tables):
        self.tables = tables
        self.described = []

    def query(self, sql, schema, table):
        assert "INFORMATION_SCHEMA.COLUMNS" in sql
        self.described.append(f"{schema}.{table}")
        return [{"name": c, "data_type": "varchar"} for c in self.tables.get(f"{schema}.{table}", [])]


def _without(table, column):
    tables = {k: list(v) for k, v in FIXTURE_TABLES.items()}
    tables[table] = [c for c in tables[table] if c.lower() != column.lower()]
    return tables


def _run(root, tables, domains=None, rule_id=None):
    project = load_project(root)
    db = FakeDb(tables)
    return preflight(project, db, select_rules(project, domains, rule_id)), db


def test_clean_project_passes_case_insensitively(fixture_root):
    issues, db = _run(fixture_root, FIXTURE_TABLES)
    assert issues == []
    # every dataset and mapping object is described exactly once
    assert sorted(db.described) == sorted(FIXTURE_TABLES)


def test_only_referenced_objects_are_described(fixture_root):
    issues, db = _run(fixture_root, FIXTURE_TABLES, domains=["payroll"])
    assert issues == []
    assert sorted(db.described) == ["stg_gp.GL20000", "stg_wd.payroll_journal", "xref.account_map"]


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("stg_wd.workers", "employee_id",
         "rule emp_master_match: keys.0.left: column 'employee_id' not found in dataset wd_workers"),
        ("stg_gp.UPR00100", "SALARY",
         "rule emp_master_match: compare.2.right: column 'SALARY' not found in dataset gp_employees"),
        ("stg_wd.payroll_journal", "ledger_account",
         "rule pay_jrnl_totals: group_by.0.left: column 'ledger_account' not found in dataset wd_pay_jrnl"),
        ("stg_wd.workers", "cost_center",
         "rule emp_cost_center_map: column: column 'cost_center' not found in dataset wd_workers"),
        ("stg_gp.UPR40300", "DEPRTMNT",
         "rule emp_cost_center_map: target.column: column 'DEPRTMNT' not found in dataset gp_departments"),
        ("stg_gp.UPR00100", "INACTIVE",
         ("rule gp_emp_quality: assertions.2.accepted_values.column: column 'INACTIVE' not found"
         " in dataset gp_employees")),
        ("stg_gp.UPR40300", "DEPRTMNT",
         ("rule gp_emp_quality: assertions.3.references.ref_column: column 'DEPRTMNT' not found"
         " in dataset gp_departments")),
        ("stg_wd.payroll_journal", "load_ts",
         "dataset wd_pay_jrnl: freshness.column: column 'load_ts' not found in stg_wd.payroll_journal"),
        ("stg_gp.GL20000", "TRXDATE",
         "dataset gp_gl_pay: period_column: column 'TRXDATE' not found in stg_gp.GL20000"),
        ("xref.cost_center_map", "gp_dept",
         "mapping cc_to_dept: to: column 'gp_dept' not found in xref.cost_center_map"),
        ("xref.cost_center_map", "eff_end",
         "mapping cc_to_dept: effective.end: column 'eff_end' not found in xref.cost_center_map"),
        ("xref.account_map", "wd_account",
         "mapping wd_acct_to_gp: from: column 'wd_account' not found in xref.account_map"),
    ],
)
def test_missing_column_reported(fixture_root, table, column, expected):
    issues, _ = _run(fixture_root, _without(table, column))
    assert expected in issues


def test_quality_key_and_list_assertions(tmp_fixture):
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {"key": ["EMPLOYID", "nope_key"]})
    issues, _ = _run(tmp_fixture, _without("stg_gp.UPR00100", "DEPRTMNT"), rule_id="gp_emp_quality")
    assert "rule gp_emp_quality: key.1: column 'nope_key' not found in dataset gp_employees" in issues
    assert ("rule gp_emp_quality: assertions.0.not_null.columns.1: column 'DEPRTMNT' not found"
            " in dataset gp_employees") in issues
    assert ("rule gp_emp_quality: assertions.3.references.column: column 'DEPRTMNT' not found"
            " in dataset gp_employees") in issues


def test_missing_objects_and_all_problems_collected(fixture_root):
    tables = {k: v for k, v in FIXTURE_TABLES.items() if k not in ("stg_gp.UPR40300", "xref.account_map")}
    tables["stg_wd.workers"] = ["employee_id"]
    issues, _ = _run(fixture_root, tables)
    assert "dataset 'gp_departments': object 'stg_gp.UPR40300' not found" in issues
    assert "mapping wd_acct_to_gp: object 'xref.account_map' not found" in issues
    # several missing worker columns, each reported; nothing reported against the missing objects' columns
    assert sum("in dataset wd_workers" in i for i in issues) == 5
    assert not any("in dataset gp_departments" in i for i in issues)
