import hashlib
import itertools
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes import InMemoryRepository
from recon import cli, signoff
from recon.breaks import plan_merge
from recon.cli import app
from recon.config.loader import load_project
from recon.model import CheckRecord
from recon.signoff import SignoffRefused, revoke, sign_off
from repository_contract import HASH_A, add_rule, create_break, edit, observed, open_run

P = "2026-09"
NOW = datetime(2026, 10, 2, 14, 5, 9, tzinfo=UTC)
USER = "TEST\\signer"
EMPLOYEE_RULES = ["emp_cost_center_map", "emp_master_match", "gp_emp_quality"]


@pytest.fixture
def project(tmp_fixture):
    return load_project(tmp_fixture)


@pytest.fixture
def repo():
    return InMemoryRepository(user=USER)


def ready(repo, rules=None, status="succeeded", domain="employees", period=P):
    """A finished run that executed ``rules`` (default: every active employees rule)."""
    rules = EMPLOYEE_RULES if rules is None else rules
    for rule_id in rules:
        add_rule(repo, rule_id, domain)
    run = open_run(repo, domain, period)
    for rule_id in rules:
        repo.record_check(CheckRecord(run, rule_id, HASH_A, "pass", 1, 1, 0, "SELECT 1", 5, None))
    repo.close_run(run, status)
    return run


def refused(project, repo, **kw) -> list[str]:
    args = {"domain": "employees", "period_label": P, "comment": "ok", "now": NOW}
    args.update(kw)
    with pytest.raises(SignoffRefused) as exc:
        sign_off(project, repo, **args)
    return exc.value.reasons


def do_sign(project, repo, **kw):
    args = {"domain": "employees", "period_label": P, "comment": "ok", "now": NOW}
    args.update(kw)
    return sign_off(project, repo, **args)


# -- refusals -------------------------------------------------------------------------------
def test_refused_without_a_run(project, repo):
    assert refused(project, repo) == ["no run found for employees 2026-09"]


@pytest.mark.parametrize("status", ["failed", "partial", "running"])
def test_refused_when_latest_run_not_succeeded(project, repo, status):
    run = ready(repo, status=status)
    assert refused(project, repo) == [f"latest run {run} status is {status}"]


def test_refused_when_rules_not_executed(project, repo):
    run = ready(repo, rules=["emp_master_match"])
    assert refused(project, repo) == [
        f"latest run {run} did not execute rules: emp_cost_center_map, gp_emp_quality"]


def test_inactive_rules_are_not_required(project, repo):
    project.rules["gp_emp_quality"].active = False
    ready(repo, rules=["emp_cost_center_map", "emp_master_match"])
    assert do_sign(project, repo).status == "signed"


_edit_counter = itertools.count(1)


def set_status(repo, brk, status):
    n = next(_edit_counter)
    repo.apply_user_edits([edit(brk.break_id, status=status, comment="c")], f"f{n}.xlsx", f"{n:064x}", 0)


def test_refused_with_open_and_investigating_breaks(project, repo):
    run = ready(repo)
    investigating = create_break(repo, run, bk="E1", rule_id="emp_master_match")
    create_break(repo, run, bk="E2", rule_id="emp_master_match")
    resolved = create_break(repo, run, bk="E3", rule_id="emp_master_match")
    set_status(repo, investigating, "investigating")
    set_status(repo, resolved, "resolved")
    assert refused(project, repo) == ["2 breaks are open or investigating"]


def test_snapshot_breaks_count(project, repo):
    run = ready(repo)
    create_break(repo, run, rule_id="emp_master_match", period="SNAPSHOT")
    assert refused(project, repo) == ["1 breaks are open or investigating"]


def test_refused_for_non_signer(project, repo):
    ready(repo)
    project.signers["employees"] = ["TEST\\someone_else"]
    assert refused(project, repo) == [f"{USER} is not an authorized signer for employees"]
    project.signers.pop("employees")
    assert refused(project, repo) == [f"{USER} is not an authorized signer for employees"]


def test_signer_match_is_case_insensitive(project, repo):
    ready(repo)
    project.signers["employees"] = [USER.upper()]
    assert do_sign(project, repo).status == "signed"


def test_all_reasons_collected_in_order(project, repo):
    run = ready(repo, rules=["emp_master_match"], status="partial")
    create_break(repo, run, rule_id="emp_master_match")
    project.signers["employees"] = ["TEST\\other"]
    assert refused(project, repo) == [
        f"latest run {run} status is partial",
        f"latest run {run} did not execute rules: emp_cost_center_map, gp_emp_quality",
        "1 breaks are open or investigating",
        f"{USER} is not an authorized signer for employees",
    ]


def test_refusal_leaves_no_evidence(project, repo):
    refused(project, repo)
    assert not project.settings.evidence_dir.exists() or not list(project.settings.evidence_dir.rglob("*.xlsx"))


# -- success --------------------------------------------------------------------------------
def test_sign_off_writes_read_only_evidence_and_records_it(project, repo):
    run = ready(repo)
    accepted = create_break(repo, run, rule_id="emp_master_match")
    set_status(repo, accepted, "accepted")

    row = do_sign(project, repo, comment="September done")

    path = Path(row.evidence_path)
    assert path == (project.settings.evidence_dir / "employees" / P
                    / "recon_evidence_employees_2026-09_20261002T140509Z.xlsx")
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IREAD
    assert row.evidence_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (row.domain, row.period_label, row.run_id, row.status) == ("employees", P, run, "signed")
    assert (row.signed_by, row.comment) == (USER, "September done")
    assert (row.open_break_count, row.accepted_break_count) == (0, 1)
    assert row.signoff_id is not None and repo.active_signoff("employees", P) == row


def test_second_sign_off_is_refused(project, repo):
    ready(repo)
    do_sign(project, repo)
    assert refused(project, repo, now=datetime(2026, 10, 3, tzinfo=UTC)) == [
        "employees 2026-09 is already signed off"]


def test_naive_now_is_treated_as_utc(project, repo):
    ready(repo)
    row = do_sign(project, repo, now=datetime(2026, 10, 2, 14, 5, 9))
    assert row.evidence_path.endswith("recon_evidence_employees_2026-09_20261002T140509Z.xlsx")


def test_revoke_then_sign_again(project, repo):
    ready(repo)
    first = do_sign(project, repo)
    revoke(project, repo, domain="employees", period_label=P, reason="found a late adjustment")
    assert repo.active_signoff("employees", P) is None
    assert repo.signoffs[first.signoff_id]["revoke_reason"] == "found a late adjustment"
    second = do_sign(project, repo, now=datetime(2026, 10, 3, 9, 0, 0, tzinfo=UTC))
    assert second.signoff_id != first.signoff_id
    assert second.evidence_path != first.evidence_path
    assert Path(first.evidence_path).is_file() and Path(second.evidence_path).is_file()


def test_non_signer_cannot_revoke(project, repo):
    ready(repo)
    do_sign(project, repo)
    project.signers["employees"] = ["TEST\\other"]
    with pytest.raises(SignoffRefused) as exc:
        revoke(project, repo, domain="employees", period_label=P, reason="because")
    assert exc.value.reasons == [f"{USER} is not an authorized signer for employees"]
    assert repo.active_signoff("employees", P) is not None


def test_revoke_without_signoff(project, repo):
    project.signers["employees"] = ["TEST\\other"]
    with pytest.raises(SignoffRefused) as exc:
        revoke(project, repo, domain="employees", period_label=P, reason="because")
    assert exc.value.reasons == ["no active sign-off for employees 2026-09",
                                 f"{USER} is not an authorized signer for employees"]


@pytest.mark.parametrize("reason", ["", "   "])
def test_revoke_requires_a_reason(project, repo, reason):
    ready(repo)
    do_sign(project, repo)
    with pytest.raises(SignoffRefused) as exc:
        revoke(project, repo, domain="employees", period_label=P, reason=reason)
    assert exc.value.reasons == ["a reason is required"]
    assert repo.active_signoff("employees", P) is not None


def test_breaks_created_after_sign_off_are_flagged_post_signoff(project, repo):
    run = ready(repo)
    do_sign(project, repo)
    signed_off = repo.active_signoff("employees", P) is not None
    changes = plan_merge(repo.load_breaks("emp_master_match", P), [observed("LATE")],
                         rule_id="emp_master_match", domain="employees", period_label=P,
                         signed_off=signed_off)
    repo.apply_engine_changes(changes, run)
    (late,) = repo.load_breaks("emp_master_match", P)
    assert late.post_signoff is True


def test_insert_failure_deletes_the_evidence_file(project, repo, monkeypatch):
    ready(repo)

    def boom(row):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "insert_signoff", boom)
    with pytest.raises(RuntimeError, match="db down"):
        do_sign(project, repo)
    assert list(project.settings.evidence_dir.rglob("*.xlsx")) == []


# -- CLI ------------------------------------------------------------------------------------
class _FakeDb:
    def close(self):
        pass


@pytest.fixture
def cli_env(tmp_fixture, repo, monkeypatch):
    monkeypatch.setenv("RECON_DB", "Driver={x};Server=none")
    monkeypatch.setattr(cli, "Database", lambda conn: _FakeDb())
    monkeypatch.setattr(cli, "SqlRepository", lambda db: repo)
    return tmp_fixture


def test_cli_signoff_and_revoke_route_correctly(cli_env, repo):
    ready(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", P, "--comment", "done",
                              "--root", str(cli_env)])
    assert res.exit_code == 0, res.output
    row = repo.active_signoff("employees", P)
    assert res.output.strip() == f"Signed off employees {P} (sign-off {row.signoff_id}): {row.evidence_path}"
    assert row.comment == "done"

    res = runner.invoke(app, ["signoff", "revoke", "--domain", "employees", "--period", P,
                              "--reason", "oops", "--root", str(cli_env)])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == f"Revoked employees {P}"
    assert repo.active_signoff("employees", P) is None


def test_cli_signoff_refusal_prints_each_reason(cli_env, repo):
    res = CliRunner().invoke(app, ["signoff", "--domain", "employees", "--period", P, "--comment", "x",
                                   "--root", str(cli_env)])
    assert res.exit_code == 2
    assert res.output.splitlines() == ["no run found for employees 2026-09"]


def test_cli_revoke_refusal(cli_env, repo):
    res = CliRunner().invoke(app, ["signoff", "revoke", "--domain", "employees", "--period", P,
                                   "--reason", "x", "--root", str(cli_env)])
    assert res.exit_code == 2
    assert res.output.splitlines() == ["no active sign-off for employees 2026-09"]


def test_cli_rejects_snapshot_bad_period_and_unknown_domain(cli_env):
    runner = CliRunner()
    base = ["--comment", "x", "--root", str(cli_env)]
    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "SNAPSHOT", *base])
    assert res.exit_code == 2 and "--period must be YYYY-MM, current or previous" in res.output
    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "2026-13", *base])
    assert res.exit_code == 2 and "invalid period" in res.output
    res = runner.invoke(app, ["signoff", "--domain", "nope", "--period", P, *base])
    assert res.exit_code == 2 and "unknown domain 'nope'" in res.output


def test_cli_signoff_requires_its_options(cli_env):
    res = CliRunner().invoke(app, ["signoff", "--domain", "employees", "--root", str(cli_env)])
    assert res.exit_code == 2 and "Missing option(s): --period, --comment" in res.output


def test_cli_unexpected_error_exits_2(cli_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "sign_off", boom)
    res = CliRunner().invoke(app, ["signoff", "--domain", "employees", "--period", P, "--comment", "x",
                                   "--root", str(cli_env)])
    assert res.exit_code == 2 and "Sign-off failed: disk full" in res.output


def test_module_exports():
    assert signoff.SignoffRefused is SignoffRefused
