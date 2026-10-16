import re
from importlib import resources
from pathlib import Path

import pyodbc
import pytest

from recon.db import Database, LockBusy
from recon.migrations import apply_migrations, list_migrations

pytestmark = pytest.mark.integration

MIGRATIONS = Path(str(resources.files("recon.sql")))  # the packaged migrations directory


def test_migrations_idempotent(db):
    assert apply_migrations(db, MIGRATIONS) == []  # fixture already applied V001
    tables = {r["name"] for r in db.query("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('recon')")}
    assert {
        "rule", "run", "run_check", "total_result", "break", "break_occurrence", "break_comment",
        "audit_log", "import_batch", "signoff", "schema_version",
    } <= tables
    assert db.scalar("SELECT COUNT(*) FROM recon.schema_version WHERE version = 'V001'") == 1


def test_first_apply_reports_versions(dsn):
    # a fresh database on the same server so the first application is observable
    import uuid

    server = dsn.rsplit(";Database=", 1)[0]
    name = f"recon_test_{uuid.uuid4().hex[:8]}"
    master = pyodbc.connect(server, autocommit=True)
    master.execute(f"CREATE DATABASE [{name}]")
    d = Database(f"{server};Database={name}")
    try:
        assert apply_migrations(d) == [v for v, _ in list_migrations(None)]
        assert apply_migrations(d) == []
    finally:
        d.close()
        master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        master.execute(f"DROP DATABASE [{name}]")
        master.close()


def test_applock_is_exclusive(db, dsn):
    other = Database(dsn)
    try:
        with db.applock("recon:payroll:2026-09"), pytest.raises(LockBusy), other.applock("recon:payroll:2026-09"):
            pass
        with other.applock("recon:payroll:2026-09"):  # released on exit
            pass
    finally:
        other.close()


def test_applock_released_on_exception(db, dsn):
    other = Database(dsn)
    try:
        with pytest.raises(ValueError), db.applock("recon:x"):
            raise ValueError
        with other.applock("recon:x"):
            pass
    finally:
        other.close()


def test_current_user_matches_login(db):
    assert db.current_user() == db.scalar("SELECT SUSER_SNAME()")


def test_transaction_rollback_and_commit(db):
    db.execute("CREATE TABLE recon.tx_probe (n INT)")
    try:
        with pytest.raises(RuntimeError), db.transaction():
            db.execute("INSERT INTO recon.tx_probe VALUES (1)")
            raise RuntimeError
        assert db.scalar("SELECT COUNT(*) FROM recon.tx_probe") == 0
        with db.transaction():
            db.execute("INSERT INTO recon.tx_probe VALUES (2)")
        assert db.scalar("SELECT COUNT(*) FROM recon.tx_probe") == 1
        db.execute("INSERT INTO recon.tx_probe VALUES (3)")  # committed immediately
        other_count = Database(db_dsn(db)).scalar("SELECT COUNT(*) FROM recon.tx_probe")
        assert other_count == 2
    finally:
        db.execute("DROP TABLE recon.tx_probe")


def db_dsn(db):
    return db.conn_str


def test_deferred_error_in_second_statement_is_raised_and_not_committed(db):
    db.execute("CREATE TABLE recon.multi_probe (n INT)")
    try:
        sql = "INSERT INTO recon.multi_probe VALUES (1); INSERT INTO recon.multi_probe VALUES ('x');"
        with pytest.raises(pyodbc.Error):
            db.execute(sql)
        assert db.scalar("SELECT COUNT(*) FROM recon.multi_probe") == 0
        with pytest.raises(pyodbc.Error):
            db.query(sql + " SELECT 1 AS a")
        assert db.scalar("SELECT COUNT(*) FROM recon.multi_probe") == 0
        with pytest.raises(pyodbc.Error), db.transaction():
            db.execute(sql)
        assert db.scalar("SELECT COUNT(*) FROM recon.multi_probe") == 0
        # a healthy multi-statement batch still works; query returns the first result set
        assert db.query("INSERT INTO recon.multi_probe VALUES (7); SELECT n FROM recon.multi_probe;") == [{"n": 7}]
    finally:
        db.execute("DROP TABLE recon.multi_probe")


def test_query_params_and_scalar(db):
    assert db.query("SELECT ? AS a, ? AS b", 1, "x") == [{"a": 1, "b": "x"}]
    assert db.scalar("SELECT ?", 41) == 41
    assert db.scalar("SELECT 1 WHERE 1 = 0") is None


# --- schema constraints ------------------------------------------------------------------------


def _run(db, status="running"):
    return db.scalar(
        "INSERT INTO recon.run ([trigger], period_label, period_start, period_end, status, run_by, engine_version)"
        " OUTPUT INSERTED.run_id VALUES ('manual', '2026-09', '2026-09-01', '2026-09-30', ?, 'u', '0.1.0')",
        status,
    )


def _break(db, run_id, key, status="open", btype="value_mismatch", resolution=None):
    db.execute(
        "INSERT INTO recon.[break] (break_key, rule_id, domain, period_label, business_key, break_type, status,"
        " resolution_type, first_seen_run_id, last_seen_run_id) VALUES (?, 'r', 'd', '2026-09', 'k', ?, ?, ?, ?, ?)",
        key, btype, status, resolution, run_id, run_id,
    )


def test_break_defaults_and_uniqueness(db):
    run_id = _run(db)
    key = "a" * 64
    _break(db, run_id, key)
    row = db.query("SELECT version, post_signoff, status FROM recon.[break] WHERE break_key = ?", key)[0]
    assert row == {"version": 1, "post_signoff": False, "status": "open"}
    with pytest.raises(pyodbc.IntegrityError):
        _break(db, run_id, key)
    cols = {r["name"] for r in db.query("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('recon.[break]')")}
    assert "version" in cols and "row_version" not in cols


@pytest.mark.parametrize(
    "kwargs",
    [{"status": "bogus"}, {"btype": "bogus"}, {"resolution": "bogus"}],
)
def test_break_check_constraints(db, kwargs):
    run_id = _run(db)
    with pytest.raises(pyodbc.IntegrityError):
        _break(db, run_id, "b" * 63 + "1", **kwargs)


def test_every_enumerated_break_value_accepted(db):
    run_id = _run(db)
    types = ["missing_left", "missing_right", "value_mismatch", "mapping_missing", "mapping_mismatch",
             "quality_violation", "total_mismatch"]
    statuses = ["open", "investigating", "resolved", "accepted"]
    for i, t in enumerate(types):
        _break(db, run_id, f"t{i}".ljust(64, "0"), btype=t)
    for i, s in enumerate(statuses):
        _break(db, run_id, f"s{i}".ljust(64, "0"), status=s)
    for i, r in enumerate(["auto_cleared", "manual"]):
        _break(db, run_id, f"r{i}".ljust(64, "0"), resolution=r)


def test_run_status_and_trigger_checks(db):
    for s in ("running", "succeeded", "partial", "failed"):
        _run(db, s)
    with pytest.raises(pyodbc.IntegrityError):
        _run(db, "bogus")
    with pytest.raises(pyodbc.IntegrityError):
        db.execute(
            "INSERT INTO recon.run ([trigger], period_label, period_start, period_end, status, run_by, engine_version)"
            " VALUES ('cron', 'p', '2026-09-01', '2026-09-30', 'running', 'u', 'v')"
        )


def test_run_check_result_check(db):
    run_id = _run(db)
    db.execute("INSERT INTO recon.[rule] (rule_id, version_hash, domain, check_type, severity, yaml_text)"
               " VALUES ('rc', 'h', 'd', 'row_match', 'high', 'x')")
    for res in ("pass", "fail", "error"):
        db.execute("INSERT INTO recon.run_check (run_id, rule_id, version_hash, result) VALUES (?, 'rc', 'h', ?)",
                   run_id, res)
    with pytest.raises(pyodbc.IntegrityError):
        db.execute("INSERT INTO recon.run_check (run_id, rule_id, version_hash, result) VALUES (?, 'rc', 'h', 'x')",
                   run_id)


def test_signoff_unique_signed_per_period_but_revoked_allowed(db):
    run_id = _run(db)

    def signoff(domain, period, status):
        db.execute(
            "INSERT INTO recon.signoff (domain, period_label, run_id, signed_by, status, open_break_count,"
            " accepted_break_count) VALUES (?, ?, ?, 'u', ?, 0, 0)", domain, period, run_id, status)

    signoff("payroll", "2026-08", "signed")
    with pytest.raises(pyodbc.IntegrityError):
        signoff("payroll", "2026-08", "signed")
    signoff("payroll", "2026-08", "revoked")
    signoff("payroll", "2026-08", "revoked")
    signoff("gl", "2026-08", "signed")
    with pytest.raises(pyodbc.IntegrityError):
        signoff("gl", "2026-09", "bogus")


def test_import_batch_sha_unique_and_audit_source_check(db):
    sha = "c" * 64
    db.execute("INSERT INTO recon.import_batch (file_name, file_sha256, imported_by, rows_applied, rows_rejected)"
               " VALUES ('f.xlsx', ?, 'u', 0, 0)", sha)
    with pytest.raises(pyodbc.IntegrityError):
        db.execute("INSERT INTO recon.import_batch (file_name, file_sha256, imported_by, rows_applied, rows_rejected)"
                   " VALUES ('g.xlsx', ?, 'u', 0, 0)", sha)
    for src in ("engine", "import", "cli"):
        db.execute("INSERT INTO recon.audit_log (entity, entity_id, field, changed_by, source)"
                   " VALUES ('break', 1, 'status', 'u', ?)", src)
    with pytest.raises(pyodbc.IntegrityError):
        db.execute("INSERT INTO recon.audit_log (entity, entity_id, field, changed_by, source)"
                   " VALUES ('break', 1, 'status', 'u', 'web')")


def test_roles_exist_with_expected_permissions(db):
    roles = {r["name"] for r in db.query("SELECT name FROM sys.database_principals WHERE type = 'R'")}
    assert {"recon_engine", "recon_reader"} <= roles
    perms = {
        (r["role"], r["permission_name"])
        for r in db.query(
            "SELECT USER_NAME(grantee_principal_id) AS role, permission_name FROM sys.database_permissions"
            " WHERE class = 4 OR (class = 3 AND major_id = SCHEMA_ID('recon'))"
            " OR USER_NAME(grantee_principal_id) IN ('recon_engine','recon_reader')"
        )
    }
    assert ("recon_reader", "SELECT") in perms
    assert {("recon_engine", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")} <= perms


def test_timestamps_default_to_utc(db):
    run_id = _run(db)
    started = db.scalar("SELECT started_at FROM recon.run WHERE run_id = ?", run_id)
    assert abs((db.scalar("SELECT SYSUTCDATETIME()") - started).total_seconds()) < 120


def test_migration_files_are_named_in_sequence():
    names = [v for v, _ in list_migrations(None)]
    assert names[0] == "V001" and all(re.fullmatch(r"V\d{3}", n) for n in names)


def test_recon_tables_start_empty_for_each_test(db):
    for table in ("[rule]", "run", "run_check", "total_result", "[break]", "break_occurrence", "break_comment",
                  "audit_log", "import_batch", "signoff"):
        assert db.scalar(f"SELECT COUNT(*) FROM recon.{table}") == 0, table
