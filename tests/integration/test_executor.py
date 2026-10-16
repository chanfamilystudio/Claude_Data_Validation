"""``execute_run`` and ``recon run`` against SQL Server with the fixture project's staging tables."""

from __future__ import annotations

import logging
from datetime import date

import pyodbc
import pytest
from typer.testing import CliRunner

import recon
from conftest import edit_rule
from recon.cli import app
from recon.config.loader import load_project
from recon.db import Database
from recon.executor import execute_run
from recon.periods import resolve_period
from recon.repository import SqlRepository

pytestmark = pytest.mark.integration

TODAY = date.today()
PERIOD = resolve_period("2026-09", TODAY)

_TABLES = (
    "stg_wd.workers", "stg_gp.UPR00100", "stg_gp.UPR40300", "stg_wd.payroll_journal", "stg_gp.GL20000",
    "xref.cost_center_map", "xref.account_map",
)
_SCHEMAS = ("stg_wd", "stg_gp", "xref")

_SETUP = [
    ("CREATE TABLE stg_wd.workers (employee_id VARCHAR(20), cost_center VARCHAR(20), hire_date DATE,"
    " annual_salary DECIMAL(18,2), national_id VARCHAR(20))"),
    ("INSERT INTO stg_wd.workers VALUES ('000001', 'CC1', '2020-01-01', 50000.00, '111-11-1111'),"
    " ('000002', 'cc2 ', '2021-02-01', 60000.00, '222-22-2222')"),
    ("CREATE TABLE stg_gp.UPR00100 (EMPLOYID CHAR(15), DEPRTMNT CHAR(6), STRTDATE DATETIME,"
    " SALARY NUMERIC(19,5), SOCSCNUM CHAR(15), INACTIVE TINYINT)"),
    ("INSERT INTO stg_gp.UPR00100 VALUES ('000001', 'D1', '2020-01-01', 50000, '111-11-1111', 0),"
    " ('000002', 'D2', '2021-02-01', 60000, '222-22-2222', 0)"),
    "CREATE TABLE stg_gp.UPR40300 (DEPRTMNT CHAR(6), DSCRIPTN VARCHAR(30))",
    "INSERT INTO stg_gp.UPR40300 VALUES ('D1', 'One'), ('D2', 'Two')",
    ("CREATE TABLE xref.cost_center_map (wd_cost_center VARCHAR(20), gp_dept VARCHAR(6), eff_start DATE,"
    " eff_end DATE NULL)"),
    "INSERT INTO xref.cost_center_map VALUES ('CC1', 'D1', '2000-01-01', NULL), ('CC2', 'D2', '2000-01-01', NULL)",
    "CREATE TABLE xref.account_map (wd_account VARCHAR(20), gp_actindx INT)",
    "INSERT INTO xref.account_map VALUES ('A6000', 100)",
    ("CREATE TABLE stg_wd.payroll_journal (ledger_account VARCHAR(20), debit DECIMAL(18,2),"
    " credit DECIMAL(18,2), pay_date DATE, load_ts DATETIME2)"),
    ("INSERT INTO stg_wd.payroll_journal VALUES ('A6000', 100.00, 0, '2026-09-15', SYSDATETIME()),"
    " ('A6000', 900.00, 0, '2026-10-15', SYSDATETIME())"),
    ("CREATE TABLE stg_gp.GL20000 (ACTINDX INT, DEBITAMT NUMERIC(19,5), CRDTAMNT NUMERIC(19,5),"
    " TRXDATE DATETIME, SOURCDOC CHAR(11))"),
    ("INSERT INTO stg_gp.GL20000 VALUES (100, 100, 0, '2026-09-30 14:35', 'UPRCC'),"
    " (100, 5, 0, '2026-09-10', 'GJ')"),
]


def _drop_all(db) -> None:
    for name in ("#diffs", "#totals"):
        db.execute(f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name}")
    for table in _TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    for schema in _SCHEMAS:
        db.execute(f"DROP SCHEMA IF EXISTS {schema}")


@pytest.fixture
def staging(db):
    _drop_all(db)
    for schema in _SCHEMAS:
        db.execute(f"CREATE SCHEMA {schema}")
    for stmt in _SETUP:
        db.execute(stmt)
    try:
        yield db
    finally:
        _drop_all(db)


@pytest.fixture
def repo(db):
    return SqlRepository(db)


def _run(root, db, *, domains=None, rule_id=None, period=PERIOD, export=None):
    return execute_run(
        load_project(root), db, SqlRepository(db), domains=domains, rule_id=rule_id, period=period,
        trigger="manual", export=export, today=TODAY,
    )


def _checks(repo, run_id):
    return {c.rule_id: c for c in repo.checks_for_run(run_id)}


def _count(db, table):
    return db.scalar(f"SELECT COUNT(*) FROM recon.{table}")


# --------------------------------------------------------------------------- happy path


def test_all_rules_pass_on_clean_data(staging, repo, tmp_fixture):
    db = staging
    out = _run(tmp_fixture, db)
    assert (out.status, out.exit_code, out.workbook) == ("succeeded", 0, None)
    checks = _checks(repo, out.run_id)
    assert {k: c.result for k, c in checks.items()} == {
        "emp_cost_center_map": "pass", "emp_master_match": "pass", "gp_emp_quality": "pass",
        "pay_jrnl_totals": "pass",
    }
    assert (checks["emp_master_match"].rows_left, checks["emp_master_match"].rows_right) == (2, 2)
    # the GP datetime 2026-09-30 14:35 falls in 2026-09; October and non-UPRCC rows are excluded
    assert (checks["pay_jrnl_totals"].rows_left, checks["pay_jrnl_totals"].rows_right) == (1, 1)
    assert checks["pay_jrnl_totals"].generated_sql.startswith("DECLARE @period_start")
    assert all(c.duration_ms is not None and c.error_message is None for c in checks.values())

    run = db.query("SELECT * FROM recon.run WHERE run_id = ?", out.run_id)[0]
    assert run["status"] == "succeeded" and run["finished_at"] is not None
    assert (run["period_label"], run["period_start"], run["period_end"]) == ("2026-09", date(2026, 9, 1),
                                                                             date(2026, 9, 30))
    assert run["domain"] is None and run["trigger"] == "manual"
    assert run["run_by"] == db.current_user()
    assert run["engine_version"] == recon.__version__
    assert run["git_commit"] is None  # the tmp project copy is not a git checkout

    totals = repo.totals_for_run(out.run_id, "payroll")
    assert sorted((t.measure, t.within_tolerance) for t in totals) == [("line_count", True), ("net_amount", True)]
    assert _count(db, "[break]") == 0
    # every selected rule was registered; temp tables are gone
    assert {r["rule_id"] for r in db.query("SELECT rule_id FROM recon.[rule]")} == set(checks)
    assert db.scalar("SELECT OBJECT_ID('tempdb..#diffs')") is None
    assert db.scalar("SELECT OBJECT_ID('tempdb..#totals')") is None


def test_single_domain_run_records_domain(staging, tmp_fixture):
    out = _run(tmp_fixture, staging, domains=["payroll"])
    assert out.exit_code == 0
    assert staging.scalar("SELECT domain FROM recon.run WHERE run_id = ?", out.run_id) == "payroll"


# --------------------------------------------------------------------------- break lifecycle


def test_fail_autoclear_reopen_cycle(staging, repo, tmp_fixture):
    db = staging
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 50100 WHERE EMPLOYID = '000001'")
    first = _run(tmp_fixture, db, domains=["employees"])
    assert (first.status, first.exit_code) == ("succeeded", 1)
    check = _checks(repo, first.run_id)["emp_master_match"]
    assert (check.result, check.break_count) == ("fail", 1)
    [brk] = repo.load_breaks("emp_master_match", "SNAPSHOT")  # snapshot datasets -> SNAPSHOT label
    assert (brk.status, brk.break_type, brk.field_name) == ("open", "value_mismatch", "annual_salary")
    assert (brk.business_key, brk.first_seen_run_id) == ("000001", first.run_id)

    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 50000 WHERE EMPLOYID = '000001'")
    fixed = _run(tmp_fixture, db, domains=["employees"])
    assert (fixed.status, fixed.exit_code) == ("succeeded", 0)
    [brk] = repo.load_breaks("emp_master_match", "SNAPSHOT")
    assert (brk.status, brk.resolution_type, brk.resolved_run_id) == ("resolved", "auto_cleared", fixed.run_id)

    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 50100 WHERE EMPLOYID = '000001'")
    again = _run(tmp_fixture, db, domains=["employees"])
    assert again.exit_code == 1
    [brk] = repo.load_breaks("emp_master_match", "SNAPSHOT")
    assert (brk.status, brk.resolution_type, brk.last_seen_run_id) == ("open", None, again.run_id)
    assert brk.first_seen_run_id == first.run_id
    statuses = [r["new_value"] for r in db.query(
        "SELECT new_value FROM recon.audit_log WHERE entity = 'break' ORDER BY audit_id")]
    assert statuses == ["open", "resolved", "open"]


def test_signed_off_period_warns_and_flags_new_breaks(staging, repo, tmp_fixture):
    from recon.model import SignoffRow

    db = staging
    first = _run(tmp_fixture, db, domains=["employees"])
    repo.insert_signoff(SignoffRow(None, "employees", "2026-09", first.run_id, "", None, None, 0, 0,
                                   "evidence.zip", "0" * 64, "signed"))
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    out = _run(tmp_fixture, db, domains=["employees"])
    assert out.exit_code == 1
    assert any("employees" in m and "2026-09" in m and "signed off" in m for m in out.messages)
    [brk] = repo.load_breaks("emp_master_match", "SNAPSHOT")
    assert brk.post_signoff is True


# --------------------------------------------------------------------------- errors


def test_failing_rule_sql_gives_partial_run(staging, repo, tmp_fixture):
    db = staging
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {
        "assertions": [{"custom_sql": {"name": "no_key", "sql": "SELECT EMPLOYID FROM {dataset}"}}],
    })
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    out = _run(tmp_fixture, db, domains=["employees"])
    assert (out.status, out.exit_code) == ("partial", 2)
    checks = _checks(repo, out.run_id)
    assert {k: c.result for k, c in checks.items()} == {
        "emp_cost_center_map": "pass", "emp_master_match": "fail", "gp_emp_quality": "error",
    }
    err = checks["gp_emp_quality"]
    assert "business_key" in err.error_message and err.break_count == 0
    assert "no_key" in err.generated_sql
    assert len(repo.load_breaks("emp_master_match", "SNAPSHOT")) == 1  # other rules still merged
    assert repo.load_breaks("gp_emp_quality", "SNAPSHOT") == []
    assert db.scalar("SELECT status FROM recon.run WHERE run_id = ?", out.run_id) == "partial"
    assert db.scalar("SELECT OBJECT_ID('tempdb..#diffs')") is None


def test_max_breaks_errors_and_leaves_breaks_untouched(staging, repo, tmp_fixture):
    db = staging
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    first = _run(tmp_fixture, db, rule_id="emp_master_match")
    [before] = repo.load_breaks("emp_master_match", "SNAPSHOT")

    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"max_breaks": 2})
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 60000 WHERE EMPLOYID = '000002'")  # would auto-clear
    db.execute("INSERT INTO stg_wd.workers VALUES ('000003', 'CC1', '2022-01-01', 1, NULL),"
               " ('000004', 'CC1', '2022-01-01', 1, NULL), ('000005', 'CC1', '2022-01-01', 1, NULL)")
    out = _run(tmp_fixture, db, rule_id="emp_master_match")
    assert (out.status, out.exit_code) == ("failed", 2)
    check = _checks(repo, out.run_id)["emp_master_match"]
    assert check.result == "error"
    assert check.error_message == "suspected load failure: 3 > max_breaks 2"
    assert repo.load_breaks("emp_master_match", "SNAPSHOT") == [before]
    assert before.last_seen_run_id == first.run_id and before.status == "open"
    assert _count(db, "break_occurrence") == 1


def test_stale_source_is_an_error(staging, repo, tmp_fixture):
    db = staging
    db.execute("UPDATE stg_wd.payroll_journal SET load_ts = DATEADD(hour, -30, SYSDATETIME())")
    db.execute("UPDATE stg_gp.GL20000 SET DEBITAMT = 1")  # would be a total_mismatch
    out = _run(tmp_fixture, db, domains=["payroll"])
    assert (out.status, out.exit_code) == ("failed", 2)
    check = _checks(repo, out.run_id)["pay_jrnl_totals"]
    assert (check.result, check.error_message) == ("error", "stale source wd_pay_jrnl")
    assert _count(db, "total_result") == 0 and _count(db, "[break]") == 0


def test_freshness_ignores_the_period_predicate(staging, repo, tmp_fixture):
    db = staging
    # a closed period whose rows were loaded long ago; the table's latest load is recent (other periods)
    db.execute("INSERT INTO stg_wd.payroll_journal VALUES ('A6000', 70.00, 0, '2026-08-20',"
               " DATEADD(day, -40, SYSDATETIME()))")
    db.execute("INSERT INTO stg_gp.GL20000 VALUES (100, 70, 0, '2026-08-20', 'UPRCC')")
    out = _run(tmp_fixture, db, domains=["payroll"], period=resolve_period("2026-08", TODAY))
    assert out.exit_code == 0
    check = _checks(repo, out.run_id)["pay_jrnl_totals"]
    assert (check.result, check.rows_left, check.rows_right) == ("pass", 1, 1)
    # a period with no rows at all is not "stale" either: the dataset as a whole is fresh
    out = _run(tmp_fixture, db, domains=["payroll"], period=resolve_period("2026-07", TODAY))
    assert _checks(repo, out.run_id)["pay_jrnl_totals"].result == "pass"


def test_executor_rejects_snapshot_period(staging, tmp_fixture):
    with pytest.raises(ValueError, match="SNAPSHOT"):
        _run(tmp_fixture, staging, period=resolve_period("SNAPSHOT", TODAY))
    assert _count(staging, "run") == 0


def test_lock_held_elsewhere_refuses_run(staging, dsn, tmp_fixture):
    other = Database(dsn)
    try:
        with other.applock("recon:payroll:2026-09"):
            out = _run(tmp_fixture, staging)
        # the lock taken before the busy one (employees sorts first) was released again
        with other.applock("recon:employees:2026-09"):
            pass
    finally:
        other.close()
    assert (out.run_id, out.status, out.exit_code) == (None, "failed", 2)
    assert out.messages == ["run already in progress for recon:payroll:2026-09"]
    assert _count(staging, "run") == 0 and _count(staging, "[rule]") == 0
    assert _run(tmp_fixture, staging).exit_code == 0


def test_snapshot_lock_held_elsewhere_refuses_snapshot_rules_only(staging, dsn, tmp_fixture):
    other = Database(dsn)
    try:
        with other.applock("recon:employees:SNAPSHOT"):
            refused = _run(tmp_fixture, staging, rule_id="emp_master_match")
            periodic = _run(tmp_fixture, staging, domains=["payroll"])  # no snapshot rules: unaffected
    finally:
        other.close()
    assert (refused.run_id, refused.exit_code) == (None, 2)
    assert refused.messages == ["run already in progress for recon:employees:SNAPSHOT"]
    assert periodic.exit_code == 0
    assert [r["domain"] for r in staging.query("SELECT domain FROM recon.run")] == ["payroll"]


def test_failure_after_check_written_leaves_only_the_error_check(staging, repo, tmp_fixture, monkeypatch):
    db = staging
    original = SqlRepository.apply_engine_changes

    def failing(self, changes, run_id):
        if any(c.rule_id == "pay_jrnl_totals" for c in changes):
            raise RuntimeError("injected failure")
        return original(self, changes, run_id)

    monkeypatch.setattr(SqlRepository, "apply_engine_changes", failing)
    db.execute("UPDATE stg_gp.GL20000 SET DEBITAMT = 1")  # total_mismatch rows -> changes to apply
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    out = _run(tmp_fixture, db)
    assert (out.status, out.exit_code) == ("partial", 2)
    rows = db.query("SELECT result, error_message FROM recon.run_check WHERE rule_id = 'pay_jrnl_totals'")
    assert rows == [{"result": "error", "error_message": "injected failure"}]
    assert _count(db, "total_result") == 0
    assert db.scalar("SELECT COUNT(*) FROM recon.[break] WHERE rule_id = 'pay_jrnl_totals'") == 0
    # the other rules ran and merged normally
    checks = _checks(repo, out.run_id)
    assert {k: c.result for k, c in checks.items()} == {
        "emp_cost_center_map": "pass", "emp_master_match": "fail", "gp_emp_quality": "pass",
        "pay_jrnl_totals": "error",
    }
    assert _count(db, "[break]") == 1 and _count(db, "break_occurrence") == 1


def test_orphaned_running_run_is_failed(staging, repo, tmp_fixture):
    orphan = repo.open_run("scheduled", "payroll", "2026-09", PERIOD.start, PERIOD.end, None, "0")
    other = repo.open_run("scheduled", "payroll", "2026-08", PERIOD.start, PERIOD.end, None, "0")
    _run(tmp_fixture, staging, domains=["payroll"])
    status = {r["run_id"]: r["status"] for r in staging.query("SELECT run_id, status FROM recon.run")}
    assert (status[orphan], status[other]) == ("failed", "running")


def test_preflight_failure_writes_nothing(staging, tmp_fixture):
    db = staging
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"keys": [{"left": "emp_no", "right": "EMPLOYID"}]})
    db.execute("ALTER TABLE stg_wd.payroll_journal DROP COLUMN load_ts")
    out = _run(tmp_fixture, db)
    assert (out.run_id, out.status, out.exit_code) == (None, "failed", 2)
    assert "rule emp_master_match: keys.0.left: column 'emp_no' not found in dataset wd_workers" in out.messages
    assert ("dataset wd_pay_jrnl: freshness.column: column 'load_ts' not found in stg_wd.payroll_journal"
            in out.messages)
    for table in ("run", "[rule]", "run_check"):
        assert _count(db, table) == 0


def test_unknown_domain_is_refused(staging, tmp_fixture):
    out = _run(tmp_fixture, staging, domains=["gl"])
    assert (out.run_id, out.exit_code, out.messages) == (None, 2, ["unknown domain 'gl'"])


def test_export_called_with_run_id(staging, tmp_fixture, tmp_path):
    seen = []

    def export(run_id):
        seen.append(run_id)
        return tmp_path / f"run_{run_id}.xlsx"

    out = _run(tmp_fixture, staging, domains=["payroll"], export=export)
    assert seen == [out.run_id] and out.workbook == tmp_path / f"run_{out.run_id}.xlsx"


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def _reset_recon_logger():
    yield
    logger = logging.getLogger("recon")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def _last_run(db):
    return db.query("SELECT TOP 1 run_id, domain, [trigger] FROM recon.run ORDER BY run_id DESC")[0]


def test_cli_run(staging, dsn, tmp_fixture, monkeypatch, _reset_recon_logger):
    monkeypatch.setenv("RECON_DB", dsn)
    staging.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    res = CliRunner().invoke(app, ["run", "--domain", "employees", "--period", "2026-09",
                                   "--root", str(tmp_fixture), "--no-export"])
    assert res.exit_code == 1, res.output
    lines = res.output.splitlines()
    assert "emp_master_match: fail (1 breaks)" in lines
    assert "gp_emp_quality: pass (0 breaks)" in lines
    run = _last_run(staging)
    assert f"Run {run['run_id']}: succeeded" in lines
    assert (run["domain"], run["trigger"]) == ("employees", "manual")
    assert (tmp_fixture / "logs" / "recon.log").is_file()

    res = CliRunner().invoke(app, ["run", "--rule", "pay_jrnl_totals", "--period", "2026-09",
                                   "--root", str(tmp_fixture), "--trigger", "scheduled"])
    assert res.exit_code == 0, res.output
    assert "pay_jrnl_totals: pass (0 breaks)" in res.output.splitlines()
    run = _last_run(staging)
    assert (run["domain"], run["trigger"]) == ("payroll", "scheduled")

    res = CliRunner().invoke(app, ["run", "--all", "--period", "2026-09", "--root", str(tmp_fixture)])
    assert res.exit_code == 1, res.output
    assert _last_run(staging)["domain"] is None  # NULL only for --all


def test_cli_run_reports_failed_check_read(staging, dsn, tmp_fixture, monkeypatch, _reset_recon_logger):
    def broken(self, run_id):
        raise pyodbc.OperationalError("08S01", "connection lost")

    monkeypatch.setenv("RECON_DB", dsn)
    monkeypatch.setattr(SqlRepository, "checks_for_run", broken)
    res = CliRunner().invoke(app, ["run", "--domain", "payroll", "--period", "2026-09", "--root", str(tmp_fixture)])
    assert res.exit_code == 2, res.output
    run_id = _last_run(staging)["run_id"]
    assert f"Run {run_id}: succeeded (cannot read its checks:" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
