"""Behavioural contract every ``Repository`` implementation must honour.

Subclass :class:`RepositoryContract` in a ``Test*`` class, provide a ``repo`` fixture and the
three inspection hooks (they read tables the interface deliberately does not expose).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from recon.breaks import break_key
from recon.model import (
    BreakChange,
    BreakRecord,
    CheckRecord,
    ObservedDiff,
    SignoffRow,
    TotalRow,
    UserEdit,
)

P = "2026-09"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


# -- builders ----------------------------------------------------------------------------------
def add_rule(repo, rule_id="emp_name", domain="employees", severity="high", version_hash=HASH_A):
    repo.upsert_rule(rule_id, version_hash, domain, "record_match", severity, "owner@x", "id: x", True)


def open_run(repo, domain="employees", period=P, trigger="manual"):
    return repo.open_run(trigger, domain, period, date(2026, 9, 1), date(2026, 9, 30), "abc123", "0.1.0")


def observed(bk="E1", left="a", right="b", diff=None, break_type="value_mismatch", field="name"):
    return ObservedDiff(bk, break_type, field, left, right, diff)


def change(kind, *, rule_id="emp_name", domain="employees", period=P, obs=None, existing=None,
           post_signoff=False):
    key = break_key(rule_id, period, obs.business_key if obs else existing.business_key,
                    obs.field_name if obs else existing.field_name,
                    obs.break_type if obs else existing.break_type)
    return BreakChange(kind, key, rule_id, domain, period, obs, existing, post_signoff)


def create_break(repo, run_id, *, bk="E1", rule_id="emp_name", domain="employees", period=P,
                 left="a", right="b", diff=None, post_signoff=False) -> BreakRecord:
    obs = observed(bk, left, right, diff)
    repo.apply_engine_changes(
        [change("create", rule_id=rule_id, domain=domain, period=period, obs=obs,
                post_signoff=post_signoff)],
        run_id,
    )
    key = break_key(rule_id, period, bk, obs.field_name, obs.break_type)
    return next(b for b in repo.load_breaks(rule_id, period) if b.break_key == key)


def signoff_row(run_id, domain="employees", period=P, user="TEST\\user") -> SignoffRow:
    return SignoffRow(None, domain, period, run_id, user, datetime(2026, 10, 1, 12, 0, 0, 123000),
                      "all good", 0, 2, "evidence/x.xlsx", "e" * 64, "signed")


def edit(break_id, status=None, owner=None, comment=None, version=1) -> UserEdit:
    return UserEdit(break_id, version, status, owner, comment)


class RepositoryContract:
    """Mixin: the subclass supplies ``repo`` (fixture) and the three hooks below."""

    # hooks -----------------------------------------------------------------------------------
    failures: tuple[type[Exception], ...]  # what a constraint / not-found failure raises

    def occurrence_rows(self, repo, break_id: int) -> list[dict]:
        """``break_occurrence`` rows of a break as dicts (run_id, left_value, right_value, diff)."""
        raise NotImplementedError

    def audit_rows(self, repo) -> list[dict]:
        """All audit rows ascending: entity, entity_id (int), field, old_value, new_value,
        changed_by, source, run_id, import_batch_id."""
        raise NotImplementedError

    def batch_rows(self, repo) -> list[dict]:
        """import_batch rows: import_batch_id, file_name, file_sha256, imported_by, rows_applied,
        rows_rejected."""
        raise NotImplementedError

    def run_state(self, repo, run_id: int) -> tuple[str, datetime | None]:
        """(status, finished_at) of a run."""
        raise NotImplementedError

    # identity, rules, runs -------------------------------------------------------------------
    def test_current_user_is_stable_text(self, repo):
        assert repo.current_user() and repo.current_user() == repo.current_user()

    def test_open_run_and_close_run(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        row = repo.latest_run("employees", P)
        assert (row.run_id, row.trigger, row.domain, row.period_label) == (run_id, "manual", "employees", P)
        assert row.status == "running" and row.run_by == repo.current_user()
        assert isinstance(row.started_at, datetime) and row.finished_at is None
        repo.close_run(run_id, "partial")
        row = repo.latest_run("employees", P)
        assert row.status == "partial" and isinstance(row.finished_at, datetime)

    def test_open_run_requires_period_dates(self, repo):
        with pytest.raises(ValueError, match="open_run requires period_start and period_end"):
            repo.open_run("manual", "employees", P, None, date(2026, 9, 30), None, "0.1.0")
        with pytest.raises(ValueError, match="open_run requires period_start and period_end"):
            repo.open_run("manual", "employees", P, date(2026, 9, 1), None, None, "0.1.0")
        assert repo.latest_run("employees", P) is None

    def test_open_run_all_domains_stores_null_domain(self, repo):
        run_id = open_run(repo, domain=None)
        row = repo.latest_run("anything", P)
        assert row.run_id == run_id and row.domain is None

    def test_upsert_rule_is_idempotent(self, repo):
        add_rule(repo)
        add_rule(repo)  # same (rule_id, version_hash): no error, no duplicate
        run_id = open_run(repo)
        create_break(repo, run_id)
        assert len(repo.load_breaks("emp_name", P)) == 1

    def test_latest_run_prefers_highest_run_id_and_includes_all_domain_runs(self, repo):
        run1 = open_run(repo, "employees")
        assert repo.latest_run("employees", P).run_id == run1
        run2 = open_run(repo, None)
        run3 = open_run(repo, "payroll")
        run4 = open_run(repo, "employees", "2026-08")
        assert repo.latest_run("employees", P).run_id == run2  # all-domain run beats the older one
        assert repo.latest_run("payroll", P).run_id == run3
        assert repo.latest_run("employees", "2026-08").run_id == run4
        run5 = open_run(repo, "employees")
        assert repo.latest_run("employees", P).run_id == run5
        assert repo.latest_run("employees", "2026-10") is None

    def test_latest_run_none_when_empty(self, repo):
        assert repo.latest_run("employees", P) is None
        assert repo.latest_run(None, P) is None

    def test_latest_run_without_domain_is_the_latest_all_domain_run(self, repo):
        run1 = open_run(repo, None)
        open_run(repo, "employees")
        assert repo.latest_run(None, P).run_id == run1
        run3 = open_run(repo, None)
        assert repo.latest_run(None, P).run_id == run3
        assert repo.latest_run(None, "2026-08") is None

    def test_get_run(self, repo):
        run1 = open_run(repo, "employees")
        run2 = open_run(repo, None, "2026-08")
        row = repo.get_run(run1)
        assert (row.run_id, row.domain, row.period_label, row.status) == (run1, "employees", P, "running")
        assert row.run_by == repo.current_user()
        assert repo.get_run(run2).domain is None
        assert repo.get_run(run2 + 100) is None

    def test_fail_orphan_runs_only_touches_matching_running_runs(self, repo):
        a = open_run(repo, "employees")
        b = open_run(repo, "payroll")
        c = open_run(repo, None)
        d = open_run(repo, "employees", "2026-08")
        e = open_run(repo, "employees")
        repo.close_run(e, "succeeded")
        assert repo.fail_orphan_runs(P, ["employees"]) == 2
        assert self.run_state(repo, a)[0] == "failed" and self.run_state(repo, a)[1] is not None
        assert self.run_state(repo, c)[0] == "failed"
        assert self.run_state(repo, b)[0] == "running" and self.run_state(repo, b)[1] is None
        assert self.run_state(repo, d)[0] == "running"
        assert self.run_state(repo, e)[0] == "succeeded"
        assert repo.fail_orphan_runs(P, ["employees"]) == 0
        assert repo.fail_orphan_runs(P, []) == 0

    def test_fail_orphan_runs_empty_domain_list_still_fails_all_domain_runs(self, repo):
        c = open_run(repo, None)
        b = open_run(repo, "payroll")
        assert repo.fail_orphan_runs(P, []) == 1
        assert self.run_state(repo, c)[0] == "failed"
        assert self.run_state(repo, b)[0] == "running"

    # checks and totals -----------------------------------------------------------------------
    def test_record_check_roundtrip_in_order(self, repo):
        add_rule(repo, "emp_name", "employees")
        add_rule(repo, "pay_total", "payroll", version_hash=HASH_B)
        run_id = open_run(repo, None)
        ok = CheckRecord(run_id, "emp_name", HASH_A, "pass", 10, 10, 0, "SELECT 1", 12, None)
        bad = CheckRecord(run_id, "pay_total", HASH_B, "error", None, None, 0, "SELECT 2", 3, "boom")
        id1 = repo.record_check(ok)
        id2 = repo.record_check(bad)
        assert id1 != id2
        assert repo.checks_for_run(run_id) == [ok, bad]
        assert repo.checks_for_run(run_id + 999) == []

    def test_record_check_unknown_rule_raises(self, repo):
        run_id = open_run(repo)
        with pytest.raises(self.failures):
            repo.record_check(CheckRecord(run_id, "ghost", HASH_A, "pass", 1, 1, 0, "s", 1, None))

    def test_totals_for_run_filters_by_rule_domain(self, repo):
        add_rule(repo, "emp_total", "employees")
        add_rule(repo, "pay_total", "payroll", version_hash=HASH_B)
        run_id = open_run(repo, None)
        c1 = repo.record_check(CheckRecord(run_id, "emp_total", HASH_A, "fail", 1, 1, 1, "s", 1, None))
        c2 = repo.record_check(CheckRecord(run_id, "pay_total", HASH_B, "pass", 1, 1, 0, "s", 1, None))
        t1 = TotalRow("dept=1", "salary", Decimal("100.50"), Decimal("100.00"), Decimal("0.50"),
                      Decimal(0), False)
        t2 = TotalRow("dept=2", "salary", Decimal(5), Decimal(5), Decimal(0), Decimal("0.01"), True)
        t3 = TotalRow("all", "gross", Decimal(1000), Decimal(1000), Decimal(0), Decimal(0), True)
        repo.insert_totals(c1, [t1, t2])
        repo.insert_totals(c2, [t3])
        assert repo.totals_for_run(run_id, None) == [t1, t2, t3]
        got = repo.totals_for_run(run_id, "employees")[0]
        assert (str(got.left_value), str(got.right_value), str(got.diff), str(got.tolerance_applied)) == (
            "100.500000", "100.000000", "0.500000", "0.000000")
        assert repo.totals_for_run(run_id, "employees") == [t1, t2]
        assert repo.totals_for_run(run_id, "payroll") == [t3]
        assert repo.totals_for_run(run_id, "gl") == []
        assert repo.totals_for_run(run_id + 999, None) == []

    # break lifecycle -------------------------------------------------------------------------
    def test_lifecycle_create_seen_auto_clear_reopen(self, repo):
        add_rule(repo)
        r1, r2, r3, r4 = (open_run(repo) for _ in range(4))

        rec = create_break(repo, r1, left="a", right="b", diff=Decimal("1.5"))
        assert (rec.status, rec.version, rec.first_seen_run_id, rec.last_seen_run_id) == ("open", 1, r1, r1)
        assert (rec.left_value, rec.right_value, rec.diff) == ("a", "b", Decimal("1.5"))
        assert str(rec.diff) == "1.500000"  # DECIMAL(38,6) fidelity
        assert (rec.rule_id, rec.domain, rec.period_label, rec.business_key) == ("emp_name", "employees", P, "E1")
        assert (rec.break_type, rec.field_name, rec.owner, rec.resolution_type) == ("value_mismatch", "name", None, None)
        assert rec.resolved_run_id is None and rec.post_signoff is False and rec.severity == "high"
        assert len(self.occurrence_rows(repo, rec.break_id)) == 1

        repo.apply_engine_changes(
            [change("seen", obs=observed(left="a2", right="b2", diff=Decimal("1.23456789")), existing=rec)], r2)
        seen = repo.get_break(rec.break_id)
        assert seen.version == 1 and seen.status == "open"
        assert (seen.left_value, seen.right_value, seen.diff) == ("a2", "b2", Decimal("1.234568"))
        assert str(seen.diff) == "1.234568"
        assert seen.last_seen_run_id == r2 and seen.first_seen_run_id == r1
        assert [o["run_id"] for o in self.occurrence_rows(repo, rec.break_id)] == [r1, r2]

        repo.apply_engine_changes([change("auto_clear", existing=seen)], r3)
        cleared = repo.get_break(rec.break_id)
        assert (cleared.status, cleared.version) == ("resolved", 2)
        assert (cleared.resolution_type, cleared.resolved_run_id) == ("auto_cleared", r3)
        assert cleared.last_seen_run_id == r2
        assert len(self.occurrence_rows(repo, rec.break_id)) == 2  # auto_clear adds no occurrence

        repo.apply_engine_changes(
            [change("reopen", obs=observed(left="a3", right="b3"), existing=cleared)], r4)
        reopened = repo.get_break(rec.break_id)
        assert (reopened.status, reopened.version) == ("open", 3)
        assert (reopened.resolution_type, reopened.resolved_run_id) == (None, None)
        assert (reopened.left_value, reopened.right_value, reopened.diff) == ("a3", "b3", None)
        assert reopened.last_seen_run_id == r4 and reopened.first_seen_run_id == r1
        occ = self.occurrence_rows(repo, rec.break_id)
        assert [o["run_id"] for o in occ] == [r1, r2, r4]
        assert occ[2]["left_value"] == "a3" and occ[2]["diff"] is None

    def test_engine_audit_rows_only_for_status_changes(self, repo):
        add_rule(repo)
        r1, r2, r3, r4 = (open_run(repo) for _ in range(4))
        rec = create_break(repo, r1)
        repo.apply_engine_changes([change("seen", obs=observed(left="x"), existing=rec)], r2)
        repo.apply_engine_changes([change("auto_clear", existing=repo.get_break(rec.break_id))], r3)
        repo.apply_engine_changes(
            [change("reopen", obs=observed(), existing=repo.get_break(rec.break_id))], r4)
        rows = self.audit_rows(repo)
        assert [(r["old_value"], r["new_value"], r["run_id"]) for r in rows] == [
            (None, "open", r1), ("open", "resolved", r3), ("resolved", "open", r4)]
        for r in rows:
            assert (r["entity"], r["entity_id"], r["field"]) == ("break", rec.break_id, "status")
            assert (r["source"], r["changed_by"], r["import_batch_id"]) == ("engine", repo.current_user(), None)

    def test_auto_clear_of_investigating_and_accepted_records_old_status(self, repo):
        add_rule(repo)
        r1, r2 = open_run(repo), open_run(repo)
        rec = create_break(repo, r1)
        repo.apply_user_edits([edit(rec.break_id, status="investigating", comment=None)], "a.xlsx", "1" * 64, 0)
        repo.apply_engine_changes([change("auto_clear", existing=repo.get_break(rec.break_id))], r2)
        last = self.audit_rows(repo)[-1]
        assert (last["old_value"], last["new_value"], last["source"]) == ("investigating", "resolved", "engine")

    def test_decimal_values_round_half_away_from_zero_to_six_places(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        cases = {"E1": ("2.0000005", "2.000001"), "E2": ("-2.0000005", "-2.000001"),
                 "E3": ("2.0000004", "2.000000")}
        for bk, (given, _) in cases.items():
            create_break(repo, run_id, bk=bk, diff=Decimal(given))
        got = {b.business_key: str(b.diff) for b in repo.load_breaks("emp_name", P)}
        assert got == {bk: want for bk, (_, want) in cases.items()}
        add_rule(repo, "emp_total", version_hash=HASH_B)
        check_id = repo.record_check(CheckRecord(run_id, "emp_total", HASH_B, "pass", 1, 1, 0, "s", 1, None))
        repo.insert_totals(check_id, [TotalRow("g", "m", Decimal("1.0000005"), Decimal("-1.0000005"),
                                               Decimal("1.23456789"), Decimal("0.1"), True)])
        (t,) = repo.totals_for_run(run_id, None)
        assert (str(t.left_value), str(t.right_value), str(t.diff), str(t.tolerance_applied)) == (
            "1.000001", "-1.000001", "1.234568", "0.100000")

    def test_create_records_post_signoff(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        rec = create_break(repo, run_id, post_signoff=True)
        assert rec.post_signoff is True
        assert create_break(repo, run_id, bk="E2").post_signoff is False

    def test_create_duplicate_key_raises(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        create_break(repo, run_id)
        with pytest.raises(self.failures):
            create_break(repo, run_id)

    def test_engine_change_on_missing_break_raises(self, repo):
        add_rule(repo)
        r1, r2 = open_run(repo), open_run(repo)
        rec = create_break(repo, r1)
        ghost = dataclasses.replace(rec, break_key="f" * 64, break_id=rec.break_id + 999)
        with pytest.raises(self.failures):
            repo.apply_engine_changes([dataclasses.replace(change("auto_clear", existing=rec),
                                                           break_key="f" * 64, existing=ghost)], r2)

    def test_get_break_missing_is_none(self, repo):
        assert repo.get_break(123456) is None

    def test_load_breaks_scopes_to_rule_and_period(self, repo):
        add_rule(repo, "emp_name")
        add_rule(repo, "emp_dept", version_hash=HASH_B)
        run_id = open_run(repo)
        b1 = create_break(repo, run_id, bk="E1")
        b2 = create_break(repo, run_id, bk="E2")
        create_break(repo, run_id, bk="E1", rule_id="emp_dept")
        create_break(repo, run_id, bk="E1", period="2026-08")
        repo.apply_engine_changes([change("auto_clear", existing=b2)], run_id)
        loaded = repo.load_breaks("emp_name", P)
        assert [b.break_id for b in loaded] == [b1.break_id, b2.break_id]  # all statuses, by break_id
        assert loaded[1].status == "resolved"
        assert repo.load_breaks("emp_name", "2030-01") == []

    def test_severity_comes_from_latest_rule_row(self, repo):
        add_rule(repo, severity="low", version_hash=HASH_A)
        add_rule(repo, severity="high", version_hash=HASH_B)  # later load wins (tie -> larger hash)
        run_id = open_run(repo)
        rec = create_break(repo, run_id)
        assert rec.severity == "high"
        assert repo.get_break(rec.break_id).severity == "high"
        assert repo.breaks_for("employees", P)[0].severity == "high"

    # breaks_for ------------------------------------------------------------------------------
    def _seed_scope(self, repo):
        add_rule(repo, "emp_name", "employees")
        add_rule(repo, "pay_amt", "payroll", version_hash=HASH_B)
        run_id = open_run(repo, None)
        ids = {
            "emp_sep": create_break(repo, run_id, bk="E1").break_id,
            "emp_aug": create_break(repo, run_id, bk="E2", period="2026-08").break_id,
            "emp_snap": create_break(repo, run_id, bk="E3", period="SNAPSHOT").break_id,
            "pay_sep": create_break(repo, run_id, bk="P1", rule_id="pay_amt", domain="payroll").break_id,
            "pay_snap": create_break(repo, run_id, bk="P2", rule_id="pay_amt", domain="payroll",
                                     period="SNAPSHOT").break_id,
        }
        return run_id, ids

    def test_breaks_for_includes_domain_snapshot_breaks(self, repo):
        _, ids = self._seed_scope(repo)
        got = repo.breaks_for("employees", P)
        assert [b.break_id for b in got] == [ids["emp_sep"], ids["emp_snap"]]
        assert [b.period_label for b in got] == [P, "SNAPSHOT"]
        assert [b.break_id for b in repo.breaks_for("payroll", P)] == [ids["pay_sep"], ids["pay_snap"]]
        assert [b.break_id for b in repo.breaks_for("employees", "2026-08")] == [ids["emp_aug"], ids["emp_snap"]]
        assert repo.breaks_for("gl", P) == []

    def test_breaks_for_domain_none_covers_all_domains(self, repo):
        _, ids = self._seed_scope(repo)
        got = repo.breaks_for(None, P)
        assert [b.break_id for b in got] == [ids["emp_sep"], ids["emp_snap"], ids["pay_sep"], ids["pay_snap"]]

    def test_breaks_for_status_and_resolved_in_run_filters(self, repo):
        run1, ids = self._seed_scope(repo)
        run2, run3 = open_run(repo, None), open_run(repo, None)
        repo.apply_engine_changes(
            [change("auto_clear", period="SNAPSHOT", existing=repo.get_break(ids["emp_snap"]))], run2)
        repo.apply_engine_changes(
            [change("auto_clear", rule_id="pay_amt", domain="payroll",
                    existing=repo.get_break(ids["pay_sep"]))], run3)

        def ids_of(**kw):
            return [b.break_id for b in repo.breaks_for(None, P, **kw)]

        assert len(ids_of()) == 4  # both None -> everything
        assert ids_of(statuses={"open"}) == [ids["emp_sep"], ids["pay_snap"]]
        assert ids_of(statuses={"resolved"}) == [ids["emp_snap"], ids["pay_sep"]]
        assert ids_of(statuses={"open", "resolved"}) == [ids["emp_sep"], ids["emp_snap"], ids["pay_sep"], ids["pay_snap"]]
        assert ids_of(statuses={"open"}, resolved_in_run=run2) == [ids["emp_sep"], ids["emp_snap"], ids["pay_snap"]]
        assert ids_of(statuses={"accepted"}, resolved_in_run=run3) == [ids["pay_sep"]]
        assert ids_of(statuses=set(), resolved_in_run=run2) == [ids["emp_snap"]]
        assert ids_of(statuses=set()) == []
        assert ids_of(statuses={"open"}, resolved_in_run=run1) == [ids["emp_sep"], ids["pay_snap"]]

    # user edits ------------------------------------------------------------------------------
    def test_user_edit_writes_status_owner_comment_audit_and_batch(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        rec = create_break(repo, run_id)
        other = create_break(repo, run_id, bk="E2")
        assert not repo.import_seen("1" * 64)

        batch_id = repo.apply_user_edits(
            [edit(rec.break_id, status="resolved", owner="bob", comment="fixed in GP")],
            "recon_export.xlsx", "1" * 64, 2)

        after = repo.get_break(rec.break_id)
        assert (after.status, after.owner, after.resolution_type, after.version) == ("resolved", "bob", "manual", 2)
        assert after.resolved_run_id is None
        assert repo.get_break(other.break_id).version == 1

        comments = repo.comments_for([rec.break_id, other.break_id])
        assert list(comments) == [rec.break_id]
        (c,) = comments[rec.break_id]
        assert (c.break_id, c.text, c.author) == (rec.break_id, "fixed in GP", repo.current_user())
        assert isinstance(c.created_at, datetime)

        audit = [r for r in self.audit_rows(repo) if r["source"] == "import"]
        assert [(r["field"], r["old_value"], r["new_value"]) for r in audit] == [
            ("status", "open", "resolved"), ("owner", None, "bob")]
        for r in audit:
            assert (r["entity"], r["entity_id"], r["changed_by"]) == ("break", rec.break_id, repo.current_user())
            assert r["import_batch_id"] == batch_id and r["run_id"] is None

        (batch,) = self.batch_rows(repo)
        assert batch["import_batch_id"] == batch_id
        assert (batch["file_name"], batch["file_sha256"]) == ("recon_export.xlsx", "1" * 64)
        assert (batch["imported_by"], batch["rows_applied"], batch["rows_rejected"]) == (repo.current_user(), 1, 2)
        assert repo.import_seen("1" * 64)
        assert not repo.import_seen("2" * 64)

    def test_user_edit_leaving_resolved_clears_resolution(self, repo):
        add_rule(repo)
        r1, r2 = open_run(repo), open_run(repo)
        rec = create_break(repo, r1)
        repo.apply_engine_changes([change("auto_clear", existing=rec)], r2)
        cleared = repo.get_break(rec.break_id)
        assert (cleared.resolution_type, cleared.resolved_run_id) == ("auto_cleared", r2)
        repo.apply_user_edits([edit(rec.break_id, status="open")], "a.xlsx", "1" * 64, 0)
        reopened = repo.get_break(rec.break_id)
        assert (reopened.status, reopened.resolution_type, reopened.resolved_run_id) == ("open", None, None)
        assert reopened.version == cleared.version + 1

    def test_user_edit_accepted_keeps_resolution_empty(self, repo):
        add_rule(repo)
        rec = create_break(repo, open_run(repo))
        repo.apply_user_edits([edit(rec.break_id, status="accepted", comment="known")], "a.xlsx", "1" * 64, 0)
        got = repo.get_break(rec.break_id)
        assert (got.status, got.resolution_type, got.version) == ("accepted", None, 2)

    def test_user_edit_comment_only_bumps_version_without_audit(self, repo):
        add_rule(repo)
        rec = create_break(repo, open_run(repo))
        repo.apply_user_edits([edit(rec.break_id, comment="looking")], "a.xlsx", "1" * 64, 0)
        got = repo.get_break(rec.break_id)
        assert (got.status, got.owner, got.version) == ("open", None, 2)
        assert [r for r in self.audit_rows(repo) if r["source"] == "import"] == []
        assert [c.text for c in repo.comments_for([rec.break_id])[rec.break_id]] == ["looking"]

    def test_user_edit_unchanged_values_are_not_audited(self, repo):
        add_rule(repo)
        rec = create_break(repo, open_run(repo))
        repo.apply_user_edits([edit(rec.break_id, status="open", owner="bob")], "a.xlsx", "1" * 64, 0)
        repo.apply_user_edits([edit(rec.break_id, status="open", owner="bob", version=2)], "b.xlsx", "2" * 64, 0)
        fields = [(r["field"], r["old_value"], r["new_value"]) for r in self.audit_rows(repo)
                  if r["source"] == "import"]
        assert fields == [("owner", None, "bob")]

    def test_version_bumps_once_per_edited_break(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        a = create_break(repo, run_id, bk="E1")
        b = create_break(repo, run_id, bk="E2")
        repo.apply_user_edits(
            [edit(a.break_id, status="investigating"), edit(b.break_id, owner="amy"),
             edit(a.break_id, owner="bob", comment="c")],
            "a.xlsx", "1" * 64, 0)
        assert repo.get_break(a.break_id).version == 2
        assert repo.get_break(b.break_id).version == 2
        assert repo.get_break(a.break_id).owner == "bob"
        repo.apply_user_edits([edit(a.break_id, status="open", version=2)], "b.xlsx", "2" * 64, 0)
        assert repo.get_break(a.break_id).version == 3
        (batch1, batch2) = self.batch_rows(repo)
        assert (batch1["rows_applied"], batch2["rows_applied"]) == (3, 1)

    def test_user_edit_unknown_break_raises_and_applies_nothing(self, repo):
        add_rule(repo)
        rec = create_break(repo, open_run(repo))
        with pytest.raises(self.failures):
            repo.apply_user_edits(
                [edit(rec.break_id, status="resolved", owner="bob", comment="x"), edit(rec.break_id + 999, status="open")],
                "a.xlsx", "1" * 64, 0)
        got = repo.get_break(rec.break_id)
        assert (got.status, got.owner, got.version) == ("open", None, 1)
        assert repo.comments_for([rec.break_id]) == {}
        assert self.batch_rows(repo) == []
        assert not repo.import_seen("1" * 64)
        assert [r for r in self.audit_rows(repo) if r["source"] == "import"] == []

    def test_user_edit_duplicate_file_hash_raises_and_changes_nothing(self, repo):
        add_rule(repo)
        a = create_break(repo, open_run(repo))
        repo.apply_user_edits([edit(a.break_id, owner="bob")], "a.xlsx", "1" * 64, 0)
        with pytest.raises(self.failures):
            repo.apply_user_edits([edit(a.break_id, owner="amy", version=2)], "a2.xlsx", "1" * 64, 0)
        got = repo.get_break(a.break_id)
        assert (got.owner, got.version) == ("bob", 2)
        assert len(self.batch_rows(repo)) == 1

    def test_comments_for_orders_by_created_and_omits_missing(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        a = create_break(repo, run_id, bk="E1")
        b = create_break(repo, run_id, bk="E2")
        c = create_break(repo, run_id, bk="E3")
        repo.apply_user_edits([edit(a.break_id, comment="first"), edit(b.break_id, comment="b1")],
                              "1.xlsx", "1" * 64, 0)
        repo.apply_user_edits([edit(a.break_id, comment="second", version=2)], "2.xlsx", "2" * 64, 0)
        repo.apply_user_edits([edit(a.break_id, comment="third", version=3)], "3.xlsx", "3" * 64, 0)
        got = repo.comments_for([a.break_id, b.break_id, c.break_id, 424242])
        assert set(got) == {a.break_id, b.break_id}
        assert [x.text for x in got[a.break_id]] == ["first", "second", "third"]
        assert [x.text for x in got[b.break_id]] == ["b1"]
        stamps = [x.created_at for x in got[a.break_id]]
        assert stamps == sorted(stamps)
        assert repo.comments_for([]) == {}

    # sign-off and audit ----------------------------------------------------------------------
    def test_signoff_insert_active_and_revoke(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        assert repo.active_signoff("employees", P) is None
        before = datetime.now(UTC).replace(tzinfo=None)
        # caller-asserted identity and time are ignored: the store stamps its own
        signoff_id = repo.insert_signoff(signoff_row(run_id, user="SOMEONE\\else"))
        after = datetime.now(UTC).replace(tzinfo=None)
        active = repo.active_signoff("employees", P)
        assert active.signed_by == repo.current_user()
        assert before - timedelta(seconds=60) <= active.signed_at <= after + timedelta(seconds=60)
        assert active.signed_at.microsecond % 1000 == 0
        assert active.signed_at != datetime(2026, 10, 1, 12, 0, 0, 123000)
        expected = dataclasses.replace(signoff_row(run_id), signoff_id=signoff_id,
                                       signed_by=active.signed_by, signed_at=active.signed_at)
        assert active == expected
        assert all(e.changed_by == repo.current_user() for e in repo.audit_for("employees", P))
        assert repo.active_signoff("payroll", P) is None
        assert repo.active_signoff("employees", "2026-08") is None

        repo.revoke_signoff(signoff_id, "wrong period")
        assert repo.active_signoff("employees", P) is None

    def test_active_signoff_ignores_revoked_and_allows_resignoff(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        first = repo.insert_signoff(signoff_row(run_id))
        repo.revoke_signoff(first, "redo")
        second = repo.insert_signoff(signoff_row(run_id))
        assert second != first
        assert repo.active_signoff("employees", P).signoff_id == second

    def test_second_active_signoff_for_same_scope_raises(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        repo.insert_signoff(signoff_row(run_id))
        with pytest.raises(self.failures):
            repo.insert_signoff(signoff_row(run_id))
        assert len([r for r in self.audit_rows(repo) if r["entity"] == "signoff"]) == 1

    def test_revoke_unknown_or_already_revoked_signoff_raises(self, repo):
        add_rule(repo)
        run_id = open_run(repo)
        with pytest.raises(self.failures):
            repo.revoke_signoff(98765, "nope")
        signoff_id = repo.insert_signoff(signoff_row(run_id))
        repo.revoke_signoff(signoff_id, "once")
        with pytest.raises(self.failures):
            repo.revoke_signoff(signoff_id, "twice")

    def test_audit_for_scopes_to_domain_period_and_snapshot_plus_signoffs(self, repo):
        add_rule(repo, "emp_name", "employees")
        add_rule(repo, "pay_amt", "payroll", version_hash=HASH_B)
        run_id = open_run(repo, None)
        b_sep = create_break(repo, run_id, bk="E1")
        create_break(repo, run_id, bk="E2", period="2026-08")  # other period: excluded
        create_break(repo, run_id, bk="P1", rule_id="pay_amt", domain="payroll")  # other domain: excluded
        b_snap = create_break(repo, run_id, bk="E3", period="SNAPSHOT")
        pay_signoff = repo.insert_signoff(signoff_row(run_id, domain="payroll"))  # excluded
        so_id = repo.insert_signoff(signoff_row(run_id))
        repo.apply_user_edits([edit(b_sep.break_id, status="investigating")], "a.xlsx", "1" * 64, 0)
        repo.revoke_signoff(so_id, "oops")
        assert pay_signoff != so_id

        entries = repo.audit_for("employees", P)
        triples = [(e.entity, e.entity_id, e.field, e.old_value, e.new_value, e.source) for e in entries]
        assert triples == [
            ("break", str(b_sep.break_id), "status", None, "open", "engine"),
            ("break", str(b_snap.break_id), "status", None, "open", "engine"),
            ("signoff", str(so_id), "status", None, "signed", "cli"),
            ("break", str(b_sep.break_id), "status", "open", "investigating", "import"),
            ("signoff", str(so_id), "status", "signed", "revoked", "cli"),
        ]
        assert all(e.changed_by == repo.current_user() and isinstance(e.changed_at, datetime) for e in entries)
        assert [e.entity_id for e in repo.audit_for("payroll", P) if e.entity == "signoff"] == [str(pay_signoff)]
        assert repo.audit_for("gl", P) == []
