"""``mapping``: crosswalk coverage of one source column (spec section 4.5).

The distinct normalized non-null values of ``dataset.column`` are looked up in the mapping (rows
valid on ``@as_of``, ``from`` normalized like every other comparison):

* no crosswalk row -> ``mapping_missing`` (``left_value`` = source value, ``right_value`` NULL);
* a crosswalk row whose ``to`` is NULL/blank, or whose ``to`` is not among the normalized non-null
  values of ``target.dataset.column`` -> ``mapping_mismatch`` (``left_value`` = source value,
  ``right_value`` = mapped value, NULL when mapped to nothing).

``business_key`` is the (escaped) source value and ``field_name`` the source column. Ambiguous
mappings are refused by ``checks_sql`` before ``diff_sql`` runs, so the join cannot fan out.
"""

from __future__ import annotations

from recon.config.models import MappingRule, Normalize
from recon.sqlgen.common import (
    TAB,
    GenContext,
    GeneratedSql,
    business_key_expr,
    column_ref,
    mapping_ambiguity_check,
    mapping_source,
    norm_string,
    source_for,
    sub,
)

_NORMALIZE = Normalize()  # mapping rules carry no normalize option: trim + case-insensitive


def _diff_sql(rule: MappingRule, ctx: GenContext) -> str:
    mapping = ctx.mappings.get(rule.mapping)
    if mapping is None:
        raise ValueError(f"rule {rule.id}: unknown mapping {rule.mapping!r}")
    source = source_for(ctx, rule.dataset)
    target = source_for(ctx, rule.target.dataset)
    norm = _NORMALIZE
    src_value = norm_string(column_ref("s", rule.column), norm)
    from_value = norm_string(column_ref("m", mapping.from_), norm)
    to_value = norm_string(column_ref("m", mapping.to), norm)
    target_value = norm_string(column_ref("t", rule.target.column), norm)

    looked_up = (
        "SELECT\n"
        f"{TAB}sv.src_value,\n"
        f"{TAB}mp.to_value AS mapped_value,\n"
        f"{TAB}CASE\n"
        f"{TAB}{TAB}WHEN mp.from_value IS NULL THEN 'mapping_missing'\n"
        f"{TAB}{TAB}WHEN mp.to_value IS NULL OR NOT EXISTS (\n"
        f"{TAB}{TAB}{TAB}SELECT 1 FROM {target} AS t WHERE {target_value} = mp.to_value\n"
        f"{TAB}{TAB}) THEN 'mapping_mismatch'\n"
        f"{TAB}END AS break_type\n"
        "FROM (\n"
        f"{TAB}SELECT DISTINCT {src_value} AS src_value\n"
        f"{TAB}FROM {source} AS s\n"
        ") AS sv\n"
        "LEFT JOIN (\n"
        f"{TAB}SELECT {from_value} AS from_value, {to_value} AS to_value\n"
        f"{TAB}FROM {mapping_source(mapping)} AS m\n"
        ") AS mp\n"
        f"{TAB}ON mp.from_value = sv.src_value\n"
        "WHERE sv.src_value IS NOT NULL"
    )
    return (
        "SELECT\n"
        f"{TAB}CAST({business_key_expr(['x.src_value'])} AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,\n"
        f"{TAB}CAST(x.break_type AS VARCHAR(30)) AS break_type,\n"
        f"{TAB}CAST(N'{rule.column}' AS NVARCHAR(128)) AS field_name,\n"
        f"{TAB}CAST(x.src_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,\n"
        f"{TAB}CAST(x.mapped_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,\n"
        f"{TAB}CAST(NULL AS DECIMAL(38,6)) AS diff\n"
        "FROM (\n" + sub(looked_up) + "\n) AS x\n"
        "WHERE x.break_type IS NOT NULL"
    )


def _counts_sql(rule: MappingRule, ctx: GenContext) -> str:
    """Distinct normalized non-null values: source column (left) and target column (right)."""
    source = source_for(ctx, rule.dataset)
    target = source_for(ctx, rule.target.dataset)
    left = norm_string(column_ref("s", rule.column), _NORMALIZE)
    right = norm_string(column_ref("s", rule.target.column), _NORMALIZE)
    return (
        "SELECT\n"
        f"{TAB}(SELECT COUNT_BIG(DISTINCT {left}) FROM {source} AS s) AS rows_left,\n"
        f"{TAB}(SELECT COUNT_BIG(DISTINCT {right}) FROM {target} AS s) AS rows_right"
    )


def generate_mapping(rule: MappingRule, ctx: GenContext) -> GeneratedSql:
    diff = _diff_sql(rule, ctx)  # validates the references first
    mapping = ctx.mappings[rule.mapping]
    return GeneratedSql(
        checks_sql=mapping_ambiguity_check(mapping, _NORMALIZE),
        counts_sql=_counts_sql(rule, ctx),
        diff_sql=diff,
    )
