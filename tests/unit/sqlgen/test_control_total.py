import re

import pytest

from recon.config.models import ControlTotalRule
from recon.sqlgen import generate
from recon.sqlgen.common import PARAM_HEADER, TOTALS_COLUMNS, TOTALS_DDL
from recon.sqlgen.control_total import generate_control_total

from .conftest import ctx_for


def _rule(**overrides) -> ControlTotalRule:
    data = {
        "id": "ck_totals",
        "domain": "payroll",
        "check_type": "control_total",
        "severity": "low",
        "left": "wd_pay_jrnl",
        "right": "gp_gl_pay",
        "measures": [
            {"name": "net", "left": "SUM(debit)", "right": "SUM(DEBITAMT)", "tolerance": {"pct": "0.01"}},
            {"name": "mixed", "left": "SUM(a)", "right": "SUM(b)", "tolerance": {"abs": "0.5", "pct": "0.02"}},
        ],
    }
    data.update(overrides)
    return ControlTotalRule.model_validate(data)


def test_control_total_golden(fixture_project, golden):
    rule = fixture_project.rules["pay_jrnl_totals"]
    golden("control_total_pay_jrnl", generate(rule, ctx_for(fixture_project)).full_text())


def test_dispatch_registers_control_total(fixture_project):
    rule = fixture_project.rules["pay_jrnl_totals"]
    ctx = ctx_for(fixture_project)
    assert generate(rule, ctx) == generate_control_total(rule, ctx)


def test_totals_and_diff_sql_usable_as_derived_tables(fixture_project):
    g = generate(fixture_project.rules["pay_jrnl_totals"], ctx_for(fixture_project))
    for sql in (g.totals_sql, g.diff_sql, g.counts_sql):
        assert not re.match(r"\s*WITH\b", sql, re.IGNORECASE)
        assert "ORDER BY" not in sql.upper()
        assert ";" not in sql


def test_no_run_values_interpolated(fixture_project):
    text = generate(fixture_project.rules["pay_jrnl_totals"], ctx_for(fixture_project)).full_text()
    assert text.startswith(PARAM_HEADER) and "2026" not in text


def test_totals_sql_output_contract(fixture_project):
    totals = generate(fixture_project.rules["pay_jrnl_totals"], ctx_for(fixture_project)).totals_sql
    head = totals.split("FROM (", 1)[0]
    for alias in TOTALS_COLUMNS.split(", "):
        assert f"AS {alias}" in head


def test_totals_ddl_matches_columns():
    names = [m.group(1) for m in re.finditer(r"^\s{4}(\w+) ", TOTALS_DDL, re.MULTILINE)]
    assert names == TOTALS_COLUMNS.split(", ")
    assert TOTALS_DDL.startswith("CREATE TABLE #totals")
    assert TOTALS_DDL.count("COLLATE DATABASE_DEFAULT") == 2


def test_diff_sql_reads_totals_temp_table(fixture_project):
    diff = generate(fixture_project.rules["pay_jrnl_totals"], ctx_for(fixture_project)).diff_sql
    assert "FROM #totals" in diff and "within_tolerance = 0" in diff and "'total_mismatch'" in diff


def test_checks_sql_for_mapping_used(fixture_project):
    g = generate(fixture_project.rules["pay_jrnl_totals"], ctx_for(fixture_project))
    assert "ambiguous mapping wd_acct_to_gp" in g.checks_sql and "THROW 50001" in g.checks_sql


def test_checks_sql_deduplicates_mapping_used_twice(fixture_project):
    rule = _rule(
        group_by=[
            {"left": "a", "right": "A", "via_mapping": "wd_acct_to_gp"},
            {"left": "b", "right": "B", "via_mapping": "wd_acct_to_gp"},
        ]
    )
    g = generate(rule, ctx_for(fixture_project))
    assert g.checks_sql.count("DECLARE @ambiguous_wd_acct_to_gp") == 1
    assert "LEFT JOIN" in g.totals_sql and "AS gm1" in g.totals_sql and "AS gm2" in g.totals_sql


def test_no_group_by_is_single_all_group_and_no_checks(fixture_project):
    g = generate(_rule(), ctx_for(fixture_project))
    assert g.checks_sql is None
    assert g.totals_sql.count("N'ALL' AS __gk") == 2
    assert "LEFT JOIN" not in g.totals_sql


def test_tolerance_pct_and_abs_forms(fixture_project):
    totals = generate(_rule(), ctx_for(fixture_project)).totals_sql
    assert "0.01 * ABS(v.right_value)" in totals
    assert "CASE WHEN 0.5 >= 0.02 * ABS(v.right_value)" in totals
    whens = [line.strip() for line in totals.splitlines() if line.strip().startswith("WHEN ")]
    assert len(whens) == 2 and not any(w.endswith(",") for w in whens)


def test_tolerance_case_is_keyed_on_ordinal_not_name(fixture_project):
    totals = generate(_rule(), ctx_for(fixture_project)).totals_sql
    assert "CASE v.ord" in totals and "CASE v.measure" not in totals
    assert "WHEN 1 THEN 0.01 * ABS(v.right_value)" in totals
    assert "WHEN 2 THEN CASE WHEN 0.5" in totals
    assert "(1, N'net', COALESCE(l.m1, 0), COALESCE(r.m1, 0))" in totals
    assert "AS v (ord, measure, left_value, right_value)" in totals
    assert "WHEN N'" not in totals


def test_counts_sql(fixture_project):
    counts = generate(_rule(), ctx_for(fixture_project)).counts_sql
    assert "AS rows_left" in counts and "AS rows_right" in counts
    assert "@period_start" in counts and "[stg_wd].[payroll_journal]" in counts


def test_unknown_mapping_rejected(fixture_project):
    rule = _rule(group_by=[{"left": "a", "right": "A", "via_mapping": "nope"}])
    with pytest.raises(ValueError, match="unknown mapping"):
        generate(rule, ctx_for(fixture_project))


def test_dotted_group_column_rejected(fixture_project):
    rule = _rule(group_by=[{"left": "a.b", "right": "A"}])
    with pytest.raises(ValueError, match="column"):
        generate(rule, ctx_for(fixture_project))


def test_duplicate_measure_names_rejected_ignoring_case():
    measures = [
        {"name": "Net", "left": "SUM(a)", "right": "SUM(b)"},
        {"name": "net", "left": "SUM(c)", "right": "SUM(d)", "tolerance": {"abs": 1}},
    ]
    with pytest.raises(ValueError, match="duplicate measure"):
        _rule(measures=measures)
    with pytest.raises(ValueError, match="duplicate measure"):
        _rule(measures=[measures[0], dict(measures[0])])
