import re
from types import SimpleNamespace

import pytest

from recon.config.models import RecordMatchRule
from recon.sqlgen import generate
from recon.sqlgen.common import PARAM_HEADER, mapping_ambiguity_check
from recon.sqlgen.record_match import generate_record_match

from .conftest import ctx_for


def test_record_match_golden(fixture_project, golden):
    rule = fixture_project.rules["emp_master_match"]
    golden("record_match_emp_master", generate(rule, ctx_for(fixture_project)).full_text())


def test_identifiers_quoted_and_params_not_interpolated(fixture_project):
    text = generate(fixture_project.rules["emp_master_match"], ctx_for(fixture_project)).full_text()
    assert "[EMPLOYID]" in text and "2026" not in text and text.startswith(PARAM_HEADER)


def _rule(**overrides) -> RecordMatchRule:
    data = {
        "id": "ck_match",
        "domain": "employees",
        "check_type": "record_match",
        "severity": "low",
        "left": "wd_workers",
        "right": "gp_employees",
        "keys": [{"left": "employee_id", "right": "EMPLOYID"}, {"left": "cost_center", "right": "DEPRTMNT"}],
        "compare": [
            {"left": "annual_salary", "right": "SALARY", "type": "int", "tolerance": {"abs": 2, "pct": "0.05"}},
            {"left": "active", "right": "INACTIVE", "type": "bool"},
        ],
    }
    data.update(overrides)
    return RecordMatchRule.model_validate(data)


def test_record_match_composite_golden(fixture_project, golden):
    """Composite key, int with abs+pct tolerance, bool, case-sensitive normalization, no mapping."""
    rule = _rule(normalize={"trim": True, "case_insensitive": False})
    golden("record_match_composite", generate(rule, ctx_for(fixture_project)).full_text())


def test_diff_sql_is_usable_as_derived_table(fixture_project):
    g = generate(fixture_project.rules["emp_master_match"], ctx_for(fixture_project))
    for sql in (g.diff_sql, g.counts_sql):
        assert not re.match(r"\s*WITH\b", sql, re.IGNORECASE)
        assert "ORDER BY" not in sql.upper()
        assert ";" not in sql
        assert "@" not in sql.replace("@period_start", "").replace("@period_end_excl", "").replace(
            "@as_of", ""
        )


def test_diff_sql_output_contract(fixture_project):
    diff = generate(fixture_project.rules["emp_master_match"], ctx_for(fixture_project)).diff_sql
    head = diff.split("FROM (", 1)[0]
    for col in (
        "CAST(k.business_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key",
        "CAST(b.break_type AS VARCHAR(30)) AS break_type",
        "CAST(b.field_name AS NVARCHAR(128)) AS field_name",
        "CAST(b.left_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value",
        "CAST(b.right_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value",
        "CAST(b.diff AS DECIMAL(38,6)) AS diff",
    ):
        assert col in head


def test_pii_raw_value_never_selected_for_output(fixture_project):
    diff = generate(fixture_project.rules["emp_master_match"], ctx_for(fixture_project)).diff_sql
    assert "N'***' AS f4_left" in diff and "N'***' AS f4_right" in diff
    assert "HASHBYTES('SHA2_256', " in diff


def test_checks_sql_holds_ambiguity_check_for_every_mapping_used(fixture_project):
    ctx = ctx_for(fixture_project)
    rule = fixture_project.rules["emp_master_match"]
    g = generate(rule, ctx)
    assert g.checks_sql == mapping_ambiguity_check(ctx.mappings["cc_to_dept"], rule.normalize)
    assert generate(_rule(), ctx).checks_sql is None
    undated = _rule(compare=[{"left": "ledger", "right": "ACTINDX", "via_mapping": "wd_acct_to_gp"}])
    assert generate(undated, ctx).checks_sql == mapping_ambiguity_check(ctx.mappings["wd_acct_to_gp"], undated.normalize)
    both = _rule(
        compare=[
            {"left": "ledger", "right": "ACTINDX", "via_mapping": "wd_acct_to_gp"},
            {"left": "cc", "right": "DEPT", "via_mapping": "cc_to_dept"},
            {"left": "cc2", "right": "DEPT2", "via_mapping": "cc_to_dept"},
        ]
    )
    assert generate(both, ctx).checks_sql == "\n".join(
        mapping_ambiguity_check(ctx.mappings[name], both.normalize) for name in ("wd_acct_to_gp", "cc_to_dept")
    )


def test_counts_sql(fixture_project):
    ctx = ctx_for(fixture_project)
    g = generate(fixture_project.rules["emp_master_match"], ctx)
    assert g.counts_sql == (
        "SELECT\n"
        f"    (SELECT COUNT_BIG(*) FROM {ctx.sources['wd_workers']} AS s) AS rows_left,\n"
        f"    (SELECT COUNT_BIG(*) FROM {ctx.sources['gp_employees']} AS s) AS rows_right"
    )


def test_periodic_source_is_inlined(fixture_project):
    ctx = ctx_for(fixture_project)
    rule = _rule(left="wd_pay_jrnl", right="gp_gl_pay")
    g = generate_record_match(rule, ctx)
    assert ctx.sources["gp_gl_pay"] in g.diff_sql
    assert "[TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl" in g.diff_sql


def test_dotted_column_rejected(fixture_project):
    rule = _rule(keys=[{"left": "a.b", "right": "B"}])
    with pytest.raises(ValueError, match="column"):
        generate(rule, ctx_for(fixture_project))


def test_generate_rejects_unsupported_check_type(fixture_project):
    bogus = SimpleNamespace(check_type="bogus")  # pydantic would reject an unknown check_type literal
    with pytest.raises(NotImplementedError, match="no SQL generator for check_type 'bogus'"):
        generate(bogus, ctx_for(fixture_project))
