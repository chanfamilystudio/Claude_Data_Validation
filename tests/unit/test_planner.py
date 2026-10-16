from datetime import date

import pytest

from conftest import edit_rule
from recon.config.loader import ConfigError, load_project
from recon.periods import SNAPSHOT, resolve_period
from recon.planner import exit_code, rule_period_label, run_status, select_rules

PERIOD = resolve_period("2026-09", date(2026, 10, 16))


@pytest.mark.parametrize(
    ("results", "status"),
    [
        ([], "failed"),
        (["pass"], "succeeded"),
        (["pass", "fail"], "succeeded"),
        (["fail", "fail"], "succeeded"),
        (["pass", "error"], "partial"),
        (["fail", "error", "pass"], "partial"),
        (["error"], "failed"),
        (["error", "error"], "failed"),
    ],
)
def test_run_status(results, status):
    assert run_status(results) == status


@pytest.mark.parametrize(
    ("results", "aborted", "code"),
    [
        (["pass", "pass"], False, 0),
        (["pass", "fail"], False, 1),
        (["fail"], False, 1),
        (["pass", "error"], False, 2),
        (["fail", "error"], False, 2),
        (["error"], False, 2),
        ([], False, 2),
        (["pass"], True, 2),
        (["fail"], True, 2),
    ],
)
def test_exit_code(results, aborted, code):
    assert exit_code(results, aborted) == code


def test_rule_period_label(fixture_root):
    project = load_project(fixture_root)
    # every dataset of the employee rules has period_column null
    assert rule_period_label(project.rules["emp_master_match"], project, PERIOD) == SNAPSHOT
    assert rule_period_label(project.rules["gp_emp_quality"], project, PERIOD) == SNAPSHOT
    assert rule_period_label(project.rules["emp_cost_center_map"], project, PERIOD) == SNAPSHOT
    # both payroll datasets are period-filtered
    assert rule_period_label(project.rules["pay_jrnl_totals"], project, PERIOD) == "2026-09"


def test_rule_period_label_mixed_datasets_is_periodic(tmp_fixture):
    # one periodic dataset is enough to make the rule periodic
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"right": "wd_pay_jrnl"})
    project = load_project(tmp_fixture)
    assert rule_period_label(project.rules["emp_master_match"], project, PERIOD) == "2026-09"


def test_select_rules_all_active_sorted(tmp_fixture):
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {"active": False})
    project = load_project(tmp_fixture)
    assert [r.id for r in select_rules(project, None, None)] == [
        "emp_cost_center_map", "emp_master_match", "pay_jrnl_totals",
    ]
    assert [r.id for r in select_rules(project, ["employees"], None)] == [
        "emp_cost_center_map", "emp_master_match",
    ]
    assert [r.id for r in select_rules(project, ["payroll", "employees"], None)] == [
        "emp_cost_center_map", "emp_master_match", "pay_jrnl_totals",
    ]
    assert [r.id for r in select_rules(project, None, "pay_jrnl_totals")] == ["pay_jrnl_totals"]


def test_select_rules_errors(tmp_fixture):
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {"active": False})
    project = load_project(tmp_fixture)
    with pytest.raises(ConfigError, match="unknown domain 'gl'"):
        select_rules(project, ["employees", "gl"], None)
    with pytest.raises(ConfigError, match="unknown rule 'nope'"):
        select_rules(project, None, "nope")
    with pytest.raises(ConfigError, match="rule 'gp_emp_quality' is inactive"):
        select_rules(project, None, "gp_emp_quality")
    with pytest.raises(ConfigError, match="not both"):
        select_rules(project, ["employees"], "emp_master_match")


def test_execute_run_rejects_snapshot_period(fixture_root):
    from recon.executor import execute_run

    with pytest.raises(ValueError, match="SNAPSHOT"):
        execute_run(load_project(fixture_root), None, None, domains=None, rule_id=None,
                    period=resolve_period("SNAPSHOT", date(2026, 10, 16)), trigger="manual", export=None,
                    today=date(2026, 10, 16))
