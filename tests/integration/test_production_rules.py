"""The shipped production rules (config/ + rules/) run end to end on the ASSUMED staging layout.

docs/ddl/assumed_staging.sql creates the layout; tests/fixtures/production_sample.sql loads one
period (2026-09) with the breaks listed in PLANTED, plus rows every rule must ignore (other
periods, voided/non-invoice GP documents, non-payroll GL lines, inactive Workday master data,
case-only name differences, an AP amount inside tolerance). When the real staging DDL replaces
the assumed one, update the sample and this list with it.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pyodbc
import pytest
from typer.testing import CliRunner

from recon.cli import app
from recon.db import Database

from .conftest import run_sql_file

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
PERIOD = "2026-09"

# (rule_id, break_type, business_key, field_name) for every planted break, and nothing else.
PLANTED = {
    # employees
    ("emp_master_match", "value_mismatch", "1002", "legal_last_name"),
    ("emp_master_match", "value_mismatch", "1003", "cost_center"),      # CC200 -> D200 since 2026 (effective-dated)
    ("emp_master_match", "mapping_missing", "1004", "cost_center"),
    ("emp_master_match", "missing_right", "1005", None),
    ("emp_master_match", "missing_left", "1006", None),
    ("emp_master_match", "value_mismatch", "1007", "national_id"),      # PII: values stored as ***
    ("emp_master_match", "value_mismatch", "1008", "worker_status"),
    ("emp_cost_center_map", "mapping_missing", "CC999", "cost_center"),
    ("org_cost_center_map", "mapping_mismatch", "CC300", "cost_center_id"),
    ("gp_employee_quality", "quality_violation", "1006", "references:DEPRTMNT"),
    # chart of accounts
    ("gl_account_map", "mapping_mismatch", "7000", "ledger_account"),
    ("gl_account_map", "mapping_missing", "8000", "ledger_account"),
    ("gp_account_string_quality", "quality_violation", "5", "references:ACTINDX"),
    # payroll: J2 credits 199.95 in Workday, GP posted 200.00
    ("pay_jrnl_by_account", "total_mismatch", "2100", "net_amount"),
    ("pay_jrnl_by_account", "total_mismatch", "2100", "credit_total"),
    ("pay_jrnl_by_department", "total_mismatch", "D100", "net_amount"),
    ("pay_jrnl_grand_total", "total_mismatch", "ALL", "net_amount"),
    ("pay_jrnl_grand_total", "total_mismatch", "ALL", "credit_total"),
    ("wd_pay_jrnl_quality", "quality_violation", "J2", "unbalanced_journal"),
    # AP
    ("vendor_master_match", "value_mismatch", "V002", "city"),
    ("vendor_master_match", "value_mismatch", "V003", "supplier_status"),
    ("vendor_master_match", "missing_left", "V004", None),
    ("ap_invoice_match", "value_mismatch", "V002|INV-9", "invoice_amount"),
    ("ap_invoice_match", "missing_left", "V002|INV-10", None),
    ("ap_invoice_match", "missing_right", "V005|INV-77", None),
    ("ap_invoice_totals", "total_mismatch", "V002", "invoice_amount"),
    ("ap_invoice_totals", "total_mismatch", "V002", "invoice_count"),
    ("ap_invoice_totals", "total_mismatch", "V005", "invoice_amount"),
    ("ap_invoice_totals", "total_mismatch", "V005", "invoice_count"),
    ("wd_invoice_quality", "quality_violation", "V005|INV-77", "references:supplier_id"),
}
CLEAN_RULES = {"emp_job_profile_map", "wd_worker_quality", "gp_account_segment_quality",
               "pay_jrnl_account_map", "gp_vendor_quality"}


@pytest.fixture
def prod_db(server_dsn):
    name = f"recon_prod_{uuid.uuid4().hex[:8]}"
    master = pyodbc.connect(server_dsn, autocommit=True)
    try:
        master.execute(f"CREATE DATABASE [{name}]")
        dsn = f"{server_dsn.rstrip(';')};Database={name}"
        database = Database(dsn)
        try:
            run_sql_file(database, REPO / "docs" / "ddl" / "assumed_staging.sql")
            run_sql_file(database, REPO / "tests" / "fixtures" / "production_sample.sql")
            yield dsn, database
        finally:
            database.close()
    finally:
        try:
            master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{name}]")
        finally:
            master.close()


@pytest.fixture
def project_root(tmp_path):
    """A copy of the shipped config/ and rules/, so logs/exports land in tmp_path."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "rules", tmp_path / "rules")
    return tmp_path


def test_shipped_rules_find_exactly_the_planted_breaks(prod_db, project_root, monkeypatch):
    dsn, database = prod_db
    monkeypatch.setenv("RECON_DB", dsn)
    runner = CliRunner()

    migrated = runner.invoke(app, ["db", "migrate", "--root", str(project_root)])
    assert migrated.exit_code == 0, migrated.output

    result = runner.invoke(app, ["run", "--all", "--period", PERIOD, "--root", str(project_root), "--no-export"])
    assert result.exit_code == 1, result.output                      # breaks found, no errors
    assert "Run 1: succeeded" in result.output

    checks = {r["rule_id"]: r["result"] for r in database.query("SELECT rule_id, result FROM recon.run_check")}
    assert len(checks) == 19 and "error" not in checks.values()
    assert {rule for rule, res in checks.items() if res == "pass"} == CLEAN_RULES

    found = {
        (r["rule_id"], r["break_type"], r["business_key"], r["field_name"])
        for r in database.query("SELECT rule_id, break_type, business_key, field_name FROM recon.[break]")
    }
    assert found == PLANTED

    pii = database.query(
        "SELECT left_value, right_value FROM recon.[break] WHERE field_name = 'national_id'")
    assert pii == [{"left_value": "***", "right_value": "***"}]

    # No raw identity number from the sample (national IDs, SSNs, tax IDs) is stored anywhere.
    stored = database.query(
        "SELECT left_value AS v FROM recon.[break] UNION ALL SELECT right_value FROM recon.[break] "
        "UNION ALL SELECT left_value FROM recon.break_occurrence UNION ALL SELECT right_value FROM recon.break_occurrence "
        "UNION ALL SELECT business_key FROM recon.[break]")
    values = {r["v"] for r in stored if r["v"] is not None}
    raw_ids = {"111-11-1111", "222-22-2222", "333-33-3333", "444-44-4444", "555-55-5555", "666-66-6666",
               "777-77-7777", "777-77-7770", "888-88-8888", "12-3456789", "98-7654321", "55-5555555", "44-4444444"}
    assert not {v.strip() for v in values} & raw_ids
