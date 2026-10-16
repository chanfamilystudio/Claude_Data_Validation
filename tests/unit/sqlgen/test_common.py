from datetime import date

import pytest

from recon.config.models import MappingConfig, Normalize
from recon.periods import resolve_period
from recon.sqlgen.common import (
    DIFFS_DDL,
    PARAM_HEADER,
    GeneratedSql,
    business_key_expr,
    mapping_ambiguity_check,
    mapping_source,
    norm_string,
    params_for,
    pii_hash,
    qi,
    text_of,
    typed,
)

TODAY = date(2026, 10, 16)


def test_param_header_is_exact():
    assert PARAM_HEADER == "DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;\n"


def test_params_for_snapshot_and_period():
    assert params_for(None, TODAY) == (None, None, TODAY)
    assert params_for(resolve_period("2026-09", TODAY), TODAY) == (
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2026, 9, 30),
    )


@pytest.mark.parametrize(
    ("ident", "quoted"),
    [("EMPLOYID", "[EMPLOYID]"), ("stg_wd.workers", "[stg_wd].[workers]"), ("_x1", "[_x1]")],
)
def test_qi_quotes(ident, quoted):
    assert qi(ident) == quoted


@pytest.mark.parametrize("bad", ["", "1abc", "a b", "a;b", "a]b", "a.b.c", "abc\n", "a.b\n", "[a]", "a-b"])
def test_qi_rejects_invalid(bad):
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        qi(bad)


def test_norm_string_default_trims_uppercases_and_pins_collation():
    assert norm_string("s.[x]", Normalize()) == (
        "UPPER(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[x], 126) COLLATE Latin1_General_100_BIN2)), N''))"
    )


def test_norm_string_variants():
    assert norm_string("x", Normalize(trim=False, case_insensitive=False)) == (
        "CONVERT(NVARCHAR(4000), x, 126) COLLATE Latin1_General_100_BIN2"
    )
    assert norm_string("x", Normalize(trim=False, case_insensitive=True)) == (
        "UPPER(CONVERT(NVARCHAR(4000), x, 126) COLLATE Latin1_General_100_BIN2)"
    )
    assert norm_string("x", Normalize(trim=True, case_insensitive=False)) == (
        "NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(4000), x, 126) COLLATE Latin1_General_100_BIN2)), N'')"
    )


@pytest.mark.parametrize(
    ("type_", "sql_type"),
    [("decimal", "DECIMAL(38,6)"), ("int", "BIGINT"), ("date", "DATE"), ("datetime", "DATETIME2(3)"), ("bool", "BIT")],
)
def test_typed_try_casts_with_blank_as_null(type_, sql_type):
    assert typed("s.[c]", type_) == (
        "TRY_CAST(CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), s.[c], 126))) = N'' THEN NULL ELSE s.[c] END"
        f" AS {sql_type})"
    )


def test_typed_string_is_norm_string():
    n = Normalize(trim=True, case_insensitive=False)
    assert typed("s.[c]", "string", n) == norm_string("s.[c]", n)
    assert typed("s.[c]", "string") == norm_string("s.[c]", Normalize())


def test_typed_unknown_type():
    with pytest.raises(ValueError, match="unknown compare type"):
        typed("x", "money")


def test_business_key_escapes_separator():
    sql = business_key_expr(["a", "b"])
    assert "N'\\|'" in sql and sql.count("N'|'") == 1


def test_business_key_expr_shape():
    assert business_key_expr(["a"]) == "REPLACE(REPLACE(ISNULL(a, N'∅'), '\\', N'\\\\'), '|', N'\\|')"
    two = business_key_expr(["a", "b"])
    assert two == business_key_expr(["a"]) + " + N'|' + " + business_key_expr(["b"])
    with pytest.raises(ValueError):
        business_key_expr([])


def test_pii_hash():
    assert pii_hash("x") == "HASHBYTES('SHA2_256', x)"


def _mapping(effective: bool) -> MappingConfig:
    data = {"name": "cc_to_dept", "object": "xref.cost_center_map", "from": "wd_cc", "to": "gp_dept"}
    if effective:
        data["effective"] = {"start": "eff_start", "end": "eff_end"}
    return MappingConfig.model_validate(data)


def test_mapping_source_plain_and_effective():
    assert mapping_source(_mapping(False)) == "(SELECT * FROM [xref].[cost_center_map])"
    assert mapping_source(_mapping(True)) == (
        "(SELECT * FROM [xref].[cost_center_map]"
        " WHERE [eff_start] <= @as_of AND ([eff_end] IS NULL OR [eff_end] >= @as_of))"
    )


def test_mapping_ambiguity_check_throws_with_message():
    sql = mapping_ambiguity_check(_mapping(True))
    assert "THROW 50001, @ambiguous_cc_to_dept, 1;" in sql
    assert "N'ambiguous mapping cc_to_dept: value '" in sql
    assert "N' valid rows'" in sql
    assert mapping_source(_mapping(True)) in sql
    assert "HAVING COUNT_BIG(*) > 1" in sql
    # the from value is grouped exactly as the rule's join normalizes it
    assert f"GROUP BY {norm_string('mp.[wd_cc]', Normalize())}" in sql
    cs = mapping_ambiguity_check(_mapping(True), Normalize(case_insensitive=False))
    assert f"GROUP BY {norm_string('mp.[wd_cc]', Normalize(case_insensitive=False))}" in cs


def test_generated_sql_full_text():
    g = GeneratedSql(checks_sql="C", counts_sql="N", diff_sql="D", totals_sql="T")
    assert g.full_text() == "\nGO\n".join(PARAM_HEADER + p for p in ["C", "N", "T", "D"])
    g2 = GeneratedSql(checks_sql=None, counts_sql="N", diff_sql="D")
    assert g2.full_text() == PARAM_HEADER + "N\nGO\n" + PARAM_HEADER + "D"


def test_text_of_uses_lossless_style_126():
    assert text_of("s.[x]") == "CONVERT(NVARCHAR(4000), s.[x], 126)"


def test_diffs_ddl_matches_diff_contract():
    assert DIFFS_DDL == (
        "CREATE TABLE #diffs (\n"
        "    business_key NVARCHAR(400) COLLATE DATABASE_DEFAULT NOT NULL,\n"
        "    break_type VARCHAR(30) COLLATE DATABASE_DEFAULT NOT NULL,\n"
        "    field_name NVARCHAR(128) COLLATE DATABASE_DEFAULT NULL,\n"
        "    left_value NVARCHAR(4000) COLLATE DATABASE_DEFAULT NULL,\n"
        "    right_value NVARCHAR(4000) COLLATE DATABASE_DEFAULT NULL,\n"
        "    diff DECIMAL(38,6) NULL\n"
        ");"
    )
