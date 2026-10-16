import re

import pytest

from recon.config.models import MappingRule
from recon.sqlgen import generate
from recon.sqlgen.common import PARAM_HEADER, mapping_ambiguity_check
from recon.sqlgen.mapping import generate_mapping

from .conftest import ctx_for


def _rule(**overrides) -> MappingRule:
    data = {
        "id": "ck_map",
        "domain": "employees",
        "check_type": "mapping",
        "severity": "low",
        "dataset": "wd_workers",
        "column": "cost_center",
        "mapping": "cc_to_dept",
        "target": {"dataset": "gp_departments", "column": "DEPRTMNT"},
    }
    data.update(overrides)
    return MappingRule.model_validate(data)


def test_mapping_golden(fixture_project, golden):
    rule = fixture_project.rules["emp_cost_center_map"]
    golden("mapping_emp_cost_center", generate(rule, ctx_for(fixture_project)).full_text())


def test_dispatch_registers_mapping(fixture_project):
    rule = fixture_project.rules["emp_cost_center_map"]
    ctx = ctx_for(fixture_project)
    assert generate(rule, ctx) == generate_mapping(rule, ctx)


def test_checks_sql_is_ambiguity_check_of_the_mapping(fixture_project):
    ctx = ctx_for(fixture_project)
    g = generate(fixture_project.rules["emp_cost_center_map"], ctx)
    assert g.checks_sql == mapping_ambiguity_check(ctx.mappings["cc_to_dept"])
    assert "THROW 50001" in g.checks_sql and g.totals_sql is None


def test_diff_sql_usable_as_derived_table_and_contract(fixture_project):
    g = generate(fixture_project.rules["emp_cost_center_map"], ctx_for(fixture_project))
    for sql in (g.diff_sql, g.counts_sql):
        assert not re.match(r"\s*WITH\b", sql, re.IGNORECASE)
        assert "ORDER BY" not in sql.upper()
        assert ";" not in sql
    head = g.diff_sql.split("FROM (", 1)[0]
    for alias in ("business_key", "break_type", "field_name", "left_value", "right_value", "diff"):
        assert f"AS {alias}" in head
    assert "N'cost_center'" in head and "2026" not in g.full_text()
    assert g.full_text().startswith(PARAM_HEADER)


def test_counts_sql_counts_distinct_values(fixture_project):
    counts = generate(fixture_project.rules["emp_cost_center_map"], ctx_for(fixture_project)).counts_sql
    assert counts.count("COUNT_BIG(DISTINCT") == 2
    assert "AS rows_left" in counts and "AS rows_right" in counts
    assert "[stg_wd].[workers]" in counts and "[stg_gp].[UPR40300]" in counts and "[DEPRTMNT]" in counts


def test_unknown_references_rejected(fixture_project):
    ctx = ctx_for(fixture_project)
    with pytest.raises(ValueError, match="unknown mapping"):
        generate(_rule(mapping="nope"), ctx)
    with pytest.raises(ValueError, match="unknown dataset"):
        generate(_rule(target={"dataset": "nope", "column": "X"}), ctx)
    with pytest.raises(ValueError, match="unknown dataset"):
        generate(_rule(dataset="nope"), ctx)


def test_dotted_column_rejected(fixture_project):
    with pytest.raises(ValueError, match="column"):
        generate(_rule(column="a.b"), ctx_for(fixture_project))
