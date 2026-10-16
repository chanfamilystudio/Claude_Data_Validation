"""Excel export, read back with openpyxl (InMemoryRepository as the data source)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from fakes import InMemoryRepository
from recon.excel import layout
from recon.excel.export import export_workbook
from recon.model import CheckRecord, UserEdit
from recon.model import TotalRow as Total
from repository_contract import HASH_A, add_rule, change, create_break, observed, open_run

P = "2026-09"


_HASHES = {"emp_name": HASH_A, "emp_dept": "d" * 64, "pay_tot": "e" * 64}


def _check(run_id, rule_id="emp_name", result="fail", n=1, sql="SELECT 1", err=None, left=3, right=4):
    return CheckRecord(run_id, rule_id, _HASHES[rule_id], result, left, right, n, sql, 12, err)


def _add_comment(repo, break_id, text, author="TEST\\a", at=None):
    repo.comments.append({"comment_id": len(repo.comments) + 1, "break_id": break_id, "text": text,
                          "author": author, "created_at": at or datetime(2026, 9, 30, 8, 0),
                          "import_batch_id": 1})


@pytest.fixture
def repo():
    r = InMemoryRepository(user="CORP\\exporter")
    add_rule(r, "emp_name", "employees", "high")
    add_rule(r, "emp_dept", "employees", "low", version_hash="d" * 64)
    add_rule(r, "pay_tot", "payroll", "medium", version_hash="e" * 64)
    return r


@pytest.fixture
def out(tmp_path):
    return tmp_path / "x" / "book.xlsx"  # parent does not exist


def _export(repo, out, **kw):
    kw.setdefault("domain", "employees")
    kw.setdefault("period_label", P)
    kw.setdefault("run_id", None)
    return export_workbook(repo, out_path=out, **kw)


def _rows(ws):
    return [[c.value for c in row] for row in ws.iter_rows()]


def test_sheets_headers_and_atomic_write(repo, out):
    run = open_run(repo)
    create_break(repo, run)
    path = _export(repo, out, run_id=run)
    assert path == out and out.is_file()
    assert not out.with_name("book.xlsx.tmp").exists()
    wb = load_workbook(out)
    assert wb.sheetnames == ["Summary", "Totals", "Breaks", "_meta"]
    assert [c.value for c in wb["Breaks"][1]] == layout.BREAK_COLUMNS
    assert [c.value for c in wb["Totals"][1]] == layout.TOTALS_COLUMNS


def test_break_row_values(repo, out):
    run = open_run(repo)
    b = create_break(repo, run, bk="000123", left="1", right="2", diff=Decimal("-1.5"))
    repo.breaks[b.break_id]["owner"] = "alice"
    wb = load_workbook(_export(repo, out, run_id=run))
    row = dict(zip(layout.BREAK_COLUMNS, [c.value for c in wb["Breaks"][2]], strict=True))
    assert row == {
        "break_id": b.break_id, "rule_id": "emp_name", "severity": "high", "domain": "employees",
        "period": P, "business_key": "000123", "break_type": "value_mismatch", "field": "name",
        "left": "1", "right": "2", "diff": -1.5, "first_seen_run": run, "last_seen_run": run,
        "post_signoff": False, "comment_history": None, "Status": "open", "Owner": "alice",
        "New comment": None,
    }
    assert wb["Breaks"]["F2"].data_type == "s"  # leading zeros survive as text


def test_blank_diff_and_post_signoff_true(repo, out):
    run = open_run(repo)
    create_break(repo, run, diff=None, post_signoff=True)
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    assert ws["K2"].value is None and ws["N2"].value is True


def test_formula_like_values_stay_text(repo, out):
    run = open_run(repo)
    create_break(repo, run, bk="=1+1", left="=SUM(A1)", right="@x")
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    assert (ws["F2"].value, ws["I2"].value, ws["J2"].value) == ("=1+1", "=SUM(A1)", "@x")
    assert ws["F2"].data_type == "s" and ws["I2"].data_type == "s"


def test_pii_mask_passes_through(repo, out):
    run = open_run(repo)
    create_break(repo, run, left="***", right="***")
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    assert (ws["I2"].value, ws["J2"].value) == ("***", "***")


def test_resolved_breaks_only_when_resolved_in_this_run(repo, out):
    run1 = open_run(repo)
    create_break(repo, run1, bk="K")
    gone = create_break(repo, run1, bk="G")
    old = create_break(repo, run1, bk="OLD")
    run2 = open_run(repo)
    repo.apply_engine_changes([change("auto_clear", existing=gone)], run2)
    repo.apply_engine_changes([change("auto_clear", existing=old)], run1)  # resolved by an earlier run
    ws = load_workbook(_export(repo, out, run_id=run2))["Breaks"]
    keys = {ws.cell(r, 6).value: ws.cell(r, 16).value for r in range(2, ws.max_row + 1)}
    assert keys == {"K": "open", "G": "resolved"}
    # latest-mode export (no run id) lists what was resolved in the run it shows (run2), not earlier ones
    ws = load_workbook(_export(repo, out))["Breaks"]
    assert {ws.cell(r, 6).value for r in range(2, ws.max_row + 1)} == {"K", "G"}


def test_investigating_and_accepted_included(repo, out):
    run = open_run(repo)
    a = create_break(repo, run, bk="A")
    i = create_break(repo, run, bk="I")
    repo.apply_user_edits([UserEdit(a.break_id, 1, "accepted", None, "ok"),
                           UserEdit(i.break_id, 1, "investigating", "bob", None)], "f.xlsx", "h" * 64, 0)
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    got = {ws.cell(r, 6).value: (ws.cell(r, 16).value, ws.cell(r, 17).value) for r in range(2, 4)}
    assert got == {"A": ("accepted", None), "I": ("investigating", "bob")}


def test_domain_and_snapshot_filtering(repo, out):
    run = open_run(repo)
    create_break(repo, run, bk="E1")
    create_break(repo, run, bk="S1", period="SNAPSHOT")
    create_break(repo, run, bk="P1", rule_id="pay_tot", domain="payroll")
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    assert {(ws.cell(r, 6).value, ws.cell(r, 5).value) for r in range(2, ws.max_row + 1)} == {
        ("E1", P), ("S1", "SNAPSHOT")}
    ws = load_workbook(_export(repo, out, domain=None, run_id=run))["Breaks"]
    assert {ws.cell(r, 6).value for r in range(2, ws.max_row + 1)} == {"E1", "S1", "P1"}


def test_sorted_by_severity_then_type_then_key(repo, out):
    run = open_run(repo)
    create_break(repo, run, bk="Z", rule_id="emp_dept")  # low
    create_break(repo, run, bk="B", rule_id="emp_name")  # high
    create_break(repo, run, bk="A", rule_id="emp_name")  # high
    create_break(repo, run, bk="M", rule_id="pay_tot", domain="employees")  # medium
    repo.apply_engine_changes([change("create", rule_id="emp_name", obs=observed("A", break_type="missing_left",
                                                                                 field=None))], run)
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    got = [(ws.cell(r, 3).value, ws.cell(r, 7).value, ws.cell(r, 6).value) for r in range(2, ws.max_row + 1)]
    assert got == [("high", "missing_left", "A"), ("high", "value_mismatch", "A"),
                   ("high", "value_mismatch", "B"), ("medium", "value_mismatch", "M"),
                   ("low", "value_mismatch", "Z")]


def test_comment_history_latest_five_oldest_first(repo, out):
    run = open_run(repo)
    b = create_break(repo, run)
    base = datetime(2026, 9, 1, 9, 0)
    for n in range(7):
        _add_comment(repo, b.break_id, f"c{n}", author=f"TEST\\u{n}", at=base + timedelta(days=n))
    ws = load_workbook(_export(repo, out, run_id=run))["Breaks"]
    assert ws["O2"].value == "\n".join(f"TEST\\u{n} 2026-09-{n + 1:02d}: c{n}" for n in range(2, 7))


def test_meta_sheet_is_very_hidden_with_versions(repo, out):
    run = open_run(repo)
    a = create_break(repo, run, bk="A")
    b = create_break(repo, run, bk="B")
    repo.apply_user_edits([UserEdit(b.break_id, 1, None, "carol", None)], "f.xlsx", "h" * 64, 0)  # version 2
    wb = load_workbook(_export(repo, out, run_id=run))
    ws = wb["_meta"]
    assert ws.sheet_state == "veryHidden"
    meta = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(1, 6)}
    assert list(meta) == layout.META_KEYS
    assert str(uuid.UUID(meta["export_id"])) == meta["export_id"]
    assert (meta["run_id"], meta["schema_version"], meta["exported_by"]) == (run, "1", "CORP\\exporter")
    assert datetime.fromisoformat(meta["exported_at"]).utcoffset() == timedelta(0)
    assert [c.value for c in ws[7]] == ["break_id", "version"]
    assert sorted((ws.cell(r, 1).value, ws.cell(r, 2).value) for r in range(8, ws.max_row + 1)) == sorted(
        [(a.break_id, 1), (b.break_id, 2)])
    assert wb["Breaks"].sheet_state == "visible"


def test_exports_get_distinct_export_ids(repo, out, tmp_path):
    first = load_workbook(_export(repo, out))["_meta"]["B1"].value
    second = load_workbook(_export(repo, tmp_path / "y.xlsx"))["_meta"]["B1"].value
    assert first != second


def test_protection_and_validation_in_normal_mode(repo, out):
    run = open_run(repo)
    create_break(repo, run, bk="A")
    create_break(repo, run, bk="B")
    wb = load_workbook(_export(repo, out, run_id=run))
    for ws in wb:
        assert ws.protection.sheet is True and not ws.protection.password
    ws = wb["Breaks"]
    assert ws["F2"].protection.locked is True and ws["A2"].protection.locked is True
    for col in "PQR":
        assert ws[f"{col}2"].protection.locked is False and ws[f"{col}3"].protection.locked is False
    assert ws["P1"].protection.locked is True  # header
    dvs = ws.data_validations.dataValidation
    assert len(dvs) == 1 and dvs[0].type == "list"
    assert str(dvs[0].sqref) == "P2:P3"
    assert dvs[0].formula1 == '"' + ",".join(layout.STATUSES) + '"'


def test_empty_breaks_has_no_validation_and_does_not_fail(repo, out):
    wb = load_workbook(_export(repo, out))
    assert [c.value for c in wb["Breaks"][1]] == layout.BREAK_COLUMNS
    assert wb["Breaks"].max_row == 1 and not wb["Breaks"].data_validations.dataValidation


def test_summary_with_run_and_checks(repo, out):
    run = open_run(repo)
    repo.close_run(run, "partial")
    repo.record_check(_check(run, "emp_name", "fail", 2))
    repo.record_check(_check(run, "emp_dept", "error", 0, err="boom", left=None, right=None))
    ws = load_workbook(_export(repo, out, run_id=run))["Summary"]
    rows = _rows(ws)
    meta = {r[0]: r[1] for r in rows[:7]}
    assert list(meta) == layout.SUMMARY_RUN_KEYS
    assert (meta["run_id"], meta["domain"], meta["period"], meta["status"], meta["run_by"]) == (
        run, "employees", P, "partial", "CORP\\exporter")
    r = repo.runs[run]
    assert meta["started_at"] == r["started_at"].isoformat(sep=" ", timespec="seconds")
    assert meta["finished_at"] == r["finished_at"].isoformat(sep=" ", timespec="seconds")
    assert rows[8][:7] == layout.SUMMARY_CHECK_COLUMNS == [
        "rule_id", "severity", "result", "rows_left", "rows_right", "break_count", "error_message"]
    assert rows[9][:7] == ["emp_name", "high", "fail", 3, 4, 2, None]
    assert rows[10][:7] == ["emp_dept", "low", "error", None, None, 0, "boom"]


def test_summary_of_all_domain_run_uses_all(repo, out):
    run = open_run(repo, domain=None)
    ws = load_workbook(_export(repo, out, domain=None, run_id=run))["Summary"]
    assert ws["B2"].value == "all"


def test_summary_without_run_says_no_run(repo, out):
    ws = load_workbook(_export(repo, out))["Summary"]
    assert (ws["A1"].value, ws["B1"].value) == ("run_id", "no run")
    wt = load_workbook(out)["Totals"]
    assert wt.max_row == 1


def test_latest_run_used_when_run_id_omitted(repo, out):
    open_run(repo)
    run2 = open_run(repo)
    repo.record_check(_check(run2))
    wb = load_workbook(_export(repo, out))
    assert wb["Summary"]["B1"].value == run2
    assert wb["Summary"]["A10"].value == "emp_name"
    assert wb["_meta"]["B2"].value == run2


def test_latest_all_domain_run_when_domain_omitted(repo, out):
    open_run(repo, "employees")
    run2 = open_run(repo, None)
    wb = load_workbook(_export(repo, out, domain=None))
    assert wb["Summary"]["B1"].value == run2


def test_totals_sheet_highlights_out_of_tolerance(repo, out):
    run = open_run(repo, "payroll")
    cid = repo.record_check(_check(run, "pay_tot", "fail"))
    repo.insert_totals(cid, [
        Total("G1", "net", Decimal(10), Decimal(10), Decimal(0), Decimal("0.01"), True),
        Total("G2", "net", Decimal("10.5"), Decimal(10), Decimal("0.5"), Decimal("0.01"), False),
    ])
    ws = load_workbook(_export(repo, out, domain="payroll", run_id=run))["Totals"]
    assert ws["A1"].value == "rule_id"
    assert [c.value for c in ws[2]] == ["pay_tot", "G1", "net", 10, 10, 0, 0.01, True]
    assert [c.value for c in ws[3]] == ["pay_tot", "G2", "net", 10.5, 10, 0.5, 0.01, False]
    assert ws["A2"].fill.fill_type is None
    assert all(c.fill.fill_type == "solid" for c in ws[3])
    for sheet in load_workbook(out):
        assert sheet.protection.sheet is True


def test_evidence_mode(repo, out):
    run = open_run(repo)
    repo.close_run(run, "succeeded")
    a = create_break(repo, run, bk="A")
    b = create_break(repo, run, bk="B")
    repo.record_check(_check(run, "emp_name", sql="SELECT 'x'"))
    run2 = open_run(repo)
    repo.apply_engine_changes([change("auto_clear", existing=b)], run2)  # resolved in an older run
    _add_comment(repo, a.break_id, "looked at it", author="TEST\\a", at=datetime(2026, 9, 2, 10, 0, 5))
    _add_comment(repo, b.break_id, "fixed", author="TEST\\b", at=datetime(2026, 9, 3, 11, 30))
    wb = load_workbook(_export(repo, out, run_id=run, evidence=True))
    assert wb.sheetnames == ["Summary", "Totals", "Breaks", "Comments", "Audit", "SQL", "_meta"]
    assert wb["_meta"].sheet_state == "veryHidden"
    ws = wb["Breaks"]
    assert {ws.cell(r, 6).value for r in range(2, ws.max_row + 1)} == {"A", "B"}  # resolved included
    for sheet in wb:
        assert sheet.protection.sheet is True
        for row in sheet.iter_rows():
            for cell in row:
                assert cell.protection.locked is True, (sheet.title, cell.coordinate)
        assert not sheet.data_validations.dataValidation
    assert _rows(wb["Comments"]) == [
        layout.COMMENT_COLUMNS,
        [a.break_id, "TEST\\a", "2026-09-02 10:00:05", "looked at it"],
        [b.break_id, "TEST\\b", "2026-09-03 11:30:00", "fixed"],
    ]
    audit = _rows(wb["Audit"])
    assert audit[0] == layout.AUDIT_COLUMNS
    assert len(audit) == 1 + len(repo.audit_for("employees", P)) and len(audit) > 3
    assert audit[1][:3] == ["break", str(a.break_id), "status"] and audit[1][7] == "engine"
    assert _rows(wb["SQL"]) == [layout.SQL_COLUMNS, ["emp_name", "SELECT 'x'"]]


def test_evidence_requires_a_domain(repo, out):
    with pytest.raises(ValueError, match="domain"):
        _export(repo, out, domain=None, evidence=True)
    assert not out.exists()


def test_failed_export_leaves_no_partial_file(repo, out, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "comments_for", boom)
    open_run(repo)
    create_break(repo, 1)
    with pytest.raises(RuntimeError):
        _export(repo, out)
    assert not out.exists() and not out.with_name("book.xlsx.tmp").exists()


def test_single_domain_workbooks_only_carry_that_domains_checks(repo, out):
    run = open_run(repo, domain=None)
    repo.record_check(_check(run, "emp_name", sql="SELECT employees"))
    repo.record_check(_check(run, "pay_tot", "error", err="secret failure", sql="SELECT payroll"))
    create_break(repo, run, bk="E1")
    for evidence in (False, True):
        wb = load_workbook(_export(repo, out, run_id=run, evidence=evidence))
        assert [r[0] for r in _rows(wb["Summary"])[9:]] == ["emp_name"]
        assert "secret failure" not in {c.value for row in wb["Summary"].iter_rows() for c in row}
        if evidence:
            assert _rows(wb["SQL"])[1:] == [["emp_name", "SELECT employees"]]
    wb = load_workbook(_export(repo, out, domain=None, run_id=run))
    assert [r[0] for r in _rows(wb["Summary"])[9:]] == ["emp_name", "pay_tot"]
