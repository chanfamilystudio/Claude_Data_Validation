import re

import pytest

from recon.config.models import QualityRule
from recon.sqlgen import generate
from recon.sqlgen.common import PARAM_HEADER
from recon.sqlgen.quality import generate_quality

from .conftest import ctx_for


def _rule(*assertions, key=("EMPLOYID",), **overrides) -> QualityRule:
    data = {
        "id": "ck_quality",
        "domain": "employees",
        "check_type": "quality",
        "severity": "low",
        "dataset": "gp_employees",
        "key": list(key),
        "assertions": list(assertions),
    }
    data.update(overrides)
    return QualityRule.model_validate(data)


def test_quality_golden(fixture_project, golden):
    rule = fixture_project.rules["gp_emp_quality"]
    golden("quality_gp_emp", generate(rule, ctx_for(fixture_project)).full_text())


def test_dispatch_registers_quality(fixture_project):
    rule = fixture_project.rules["gp_emp_quality"]
    ctx = ctx_for(fixture_project)
    assert generate(rule, ctx) == generate_quality(rule, ctx)


def test_diff_sql_usable_as_derived_table_and_contract(fixture_project):
    g = generate(fixture_project.rules["gp_emp_quality"], ctx_for(fixture_project))
    assert g.checks_sql is None and g.totals_sql is None
    for sql in (g.diff_sql, g.counts_sql):
        assert not re.match(r"\s*WITH\b", sql, re.IGNORECASE)
        assert "ORDER BY" not in sql.upper()
        assert ";" not in sql
    head = g.diff_sql.split("FROM (", 1)[0]
    for alias in ("business_key", "break_type", "field_name", "left_value", "right_value", "diff"):
        assert f"AS {alias}" in head
    assert g.diff_sql.count("UNION ALL") == 5  # six assertions
    assert g.full_text().startswith(PARAM_HEADER)


def test_counts_sql(fixture_project):
    ctx = ctx_for(fixture_project)
    counts = generate(fixture_project.rules["gp_emp_quality"], ctx).counts_sql
    assert counts == (
        "SELECT\n"
        f"    (SELECT COUNT_BIG(*) FROM {ctx.sources['gp_employees']} AS s) AS rows_left,\n"
        "    CAST(0 AS BIGINT) AS rows_right"
    )


def test_field_names(fixture_project):
    diff = generate(fixture_project.rules["gp_emp_quality"], ctx_for(fixture_project)).diff_sql
    for expected in ("N'unique:EMPLOYID'", "N'INACTIVE'", "N'DEPRTMNT'", "N'EMPLOYID'", "N'future_hire_dates'"):
        assert expected in diff


def test_unique_composite_field_name_and_key(fixture_project):
    from recon.config.models import Unique

    rule = _rule({"unique": ["EMPLOYID", "DEPRTMNT"]})
    assert isinstance(rule.assertions[0], Unique)
    diff = generate(rule, ctx_for(fixture_project)).diff_sql
    assert "N'unique:EMPLOYID,DEPRTMNT'" in diff and "u.k1" in diff and "u.k2" in diff


def test_custom_sql_gets_dataset_source_and_alias(fixture_project):
    ctx = ctx_for(fixture_project)
    src = ctx.sources["gp_employees"]
    plain = generate(_rule({"custom_sql": {"name": "c1", "sql": "SELECT a AS business_key FROM {dataset} WHERE x = 1"}}), ctx)
    assert f"FROM {src} AS [dataset] WHERE x = 1" in plain.diff_sql
    aliased = generate(_rule({"custom_sql": {"name": "c1", "sql": "SELECT d.a AS business_key FROM {dataset} d"}}), ctx)
    assert f"FROM {src} d" in aliased.diff_sql and "AS [dataset]" not in aliased.diff_sql
    aliased_as = generate(_rule({"custom_sql": {"name": "c1", "sql": "SELECT d.a AS business_key FROM {dataset} AS d"}}), ctx)
    assert f"FROM {src} AS d" in aliased_as.diff_sql and "AS [dataset]" not in aliased_as.diff_sql


def test_accepted_values_literals_are_escaped_strings(fixture_project):
    rule = _rule({"accepted_values": {"column": "STATUS", "values": ["O'Brien", 1, 2.0, True, "x"]}})
    diff = generate(rule, ctx_for(fixture_project)).diff_sql
    assert "N'O''Brien'" in diff and "N'1'" in diff and "N'2'" in diff and "N'x'" in diff
    assert "N'True'" not in diff and "N'2.0'" not in diff
    assert " IN (" not in diff  # NOT EXISTS over VALUES: a blank accepted value cannot hide violations


def test_references_uses_referenced_source(fixture_project):
    ctx = ctx_for(fixture_project)
    diff = generate(fixture_project.rules["gp_emp_quality"], ctx).diff_sql
    assert ctx.sources["gp_departments"] in diff


def test_unknown_dataset_rejected(fixture_project):
    ctx = ctx_for(fixture_project)
    with pytest.raises(ValueError, match="unknown dataset"):
        generate(_rule({"not_null": ["A"]}, dataset="nope"), ctx)
    with pytest.raises(ValueError, match="unknown dataset"):
        generate(_rule({"references": {"column": "A", "dataset": "nope", "ref_column": "B"}}), ctx)


def test_dotted_column_rejected(fixture_project):
    with pytest.raises(ValueError, match="column"):
        generate(_rule({"not_null": ["a.b"]}), ctx_for(fixture_project))
