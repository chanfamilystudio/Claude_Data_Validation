"""``control_total``: per-group aggregate comparison of two datasets (spec sections 4.4 and 4.7).

Shape of the generated SQL (nested derived tables, no CTEs, so ``totals_sql`` and ``diff_sql`` can
each be wrapped as ``INSERT INTO #x (...) SELECT * FROM (<sql>) d``):

* ``totals_sql``, per side: an inner derived table ``SELECT s.*, <group key> AS __gk FROM <source> s
  [LEFT JOIN <mapping> ...]`` (so mapping columns never collide with the unqualified columns of the
  rule's measure expressions), grouped by ``__gk`` outside it. The two sides are FULL OUTER JOINed on
  the group key and every measure becomes one row via ``CROSS APPLY (VALUES ...)``; a group missing
  on one side contributes 0 for each of its measures.
* ``diff_sql``: reads the ``#totals`` temp table that the executor fills from ``totals_sql`` and
  emits one ``total_mismatch`` per out-of-tolerance group x measure.

The group key is the ``business_key_expr`` of the normalized group values (``ControlTotalRule`` has no
``normalize`` of its own: trim + case-insensitive). On the left, ``via_mapping`` groups by the mapped
``to`` value; a left value without a valid mapping row groups as ``UNMAPPED:<value>`` so it surfaces
as a mismatch rather than collapsing into one NULL group. Without ``group_by`` there is one group,
``ALL``.
"""

from __future__ import annotations

from dataclasses import dataclass

from recon.config.models import ControlTotalRule, GroupBy, MappingConfig, Normalize
from recon.sqlgen.common import (
    TAB,
    GenContext,
    GeneratedSql,
    block,
    business_key_expr,
    column_ref,
    mapping_ambiguity_check,
    mapping_source,
    norm_string,
    row_counts_sql,
    sub,
    tolerance_sql,
)

_NORMALIZE = Normalize()  # rule-level defaults: ControlTotalRule has no normalize setting
DECIMAL = "DECIMAL(38,6)"


@dataclass(frozen=True)
class _Group:
    n: int
    cfg: GroupBy
    mapping: MappingConfig | None


def _group_key_and_joins(groups: list[_Group], side: str) -> tuple[str, list[str]]:
    """The ``__gk`` expression over alias ``s`` and the LEFT JOINs it needs (left side only)."""
    if not groups:
        return "N'ALL'", []
    parts: list[str] = []
    joins: list[str] = []
    for g in groups:
        value = norm_string(column_ref("s", getattr(g.cfg, side)), _NORMALIZE)
        if side == "left" and g.mapping is not None:
            alias = f"gm{g.n}"
            frm = column_ref(alias, g.mapping.from_)
            joins.append(
                f"LEFT JOIN {mapping_source(g.mapping)} AS {alias}\n"
                f"{TAB}ON {norm_string(frm, _NORMALIZE)} = {value}"
            )
            mapped = norm_string(column_ref(alias, g.mapping.to), _NORMALIZE)
            value = f"CASE WHEN {value} IS NOT NULL AND {frm} IS NULL THEN N'UNMAPPED:' + {value} ELSE {mapped} END"
        parts.append(value)
    return business_key_expr(parts), joins


def _side_sql(rule: ControlTotalRule, source: str, groups: list[_Group], side: str) -> str:
    key, joins = _group_key_and_joins(groups, side)
    inner = f"SELECT s.*, {key} AS __gk\nFROM {source} AS s"
    if joins:
        inner += "\n" + "\n".join(joins)
    select = ["g.__gk AS group_key"] + [
        f"-- {m.name}\nCAST(COALESCE({getattr(m, side)}, 0) AS {DECIMAL}) AS m{i}"
        for i, m in enumerate(rule.measures, start=1)
    ]
    return "SELECT\n" + block(select) + "\nFROM (\n" + sub(inner) + "\n) AS g\nGROUP BY g.__gk"


def _totals_sql(rule: ControlTotalRule, ctx: GenContext, groups: list[_Group]) -> str:
    left = _side_sql(rule, ctx.sources[rule.left], groups, "left")
    right = _side_sql(rule, ctx.sources[rule.right], groups, "right")
    values = ",\n".join(
        f"(N'{m.name}', COALESCE(l.m{i}, 0), COALESCE(r.m{i}, 0))" for i, m in enumerate(rule.measures, start=1)
    )
    tolerance = "\n".join(
        # one WHEN per measure with a tolerance; every other measure is exact (tolerance 0)
        f"WHEN N'{m.name}' THEN {tol}"
        for m in rule.measures
        if (tol := tolerance_sql(m.tolerance, "v.right_value")) is not None
    )
    tolerance_expr = f"CASE v.measure\n{sub(tolerance)}\n{TAB}ELSE 0\nEND" if tolerance else "0"
    paired = (
        "SELECT\n"
        + block(
            [
                "COALESCE(l.group_key, r.group_key) AS group_key",
                "v.measure",
                "v.left_value",
                "v.right_value",
                "v.left_value - v.right_value AS diff",
                f"{tolerance_expr} AS tolerance_applied",
            ]
        )
        + "\nFROM (\n" + sub(left) + "\n) AS l"
        + "\nFULL OUTER JOIN (\n" + sub(right) + "\n) AS r"
        + f"\n{TAB}ON r.group_key = l.group_key"
        + "\nCROSS APPLY (VALUES\n" + sub(values) + "\n) AS v (measure, left_value, right_value)"
    )
    return (
        "SELECT\n"
        + block(
            [
                "CAST(w.group_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS group_key",
                "CAST(w.measure AS NVARCHAR(128)) COLLATE DATABASE_DEFAULT AS measure",
                f"CAST(w.left_value AS {DECIMAL}) AS left_value",
                f"CAST(w.right_value AS {DECIMAL}) AS right_value",
                f"CAST(w.diff AS {DECIMAL}) AS diff",
                f"CAST(w.tolerance_applied AS {DECIMAL}) AS tolerance_applied",
                "CAST(CASE WHEN ABS(w.diff) <= w.tolerance_applied THEN 1 ELSE 0 END AS BIT) AS within_tolerance",
            ]
        )
        + "\nFROM (\n" + sub(paired) + "\n) AS w"
    )


# Reads the temp table the executor fills from totals_sql (see common.TOTALS_DDL).
_DIFF_SQL = (
    "SELECT\n"
    f"{TAB}group_key AS business_key,\n"
    f"{TAB}CAST('total_mismatch' AS VARCHAR(30)) AS break_type,\n"
    f"{TAB}measure AS field_name,\n"
    f"{TAB}CONVERT(NVARCHAR(4000), left_value) AS left_value,\n"
    f"{TAB}CONVERT(NVARCHAR(4000), right_value) AS right_value,\n"
    f"{TAB}diff\n"
    "FROM #totals\n"
    "WHERE within_tolerance = 0"
)


def generate_control_total(rule: ControlTotalRule, ctx: GenContext) -> GeneratedSql:
    groups = []
    for i, cfg in enumerate(rule.group_by, start=1):
        mapping = None
        if cfg.via_mapping:
            if cfg.via_mapping not in ctx.mappings:
                raise ValueError(f"rule {rule.id}: unknown mapping {cfg.via_mapping!r}")
            mapping = ctx.mappings[cfg.via_mapping]
        groups.append(_Group(i, cfg, mapping))

    used: dict[str, MappingConfig] = {}  # every distinct mapping, dated or not (spec 4.1)
    for g in groups:
        if g.mapping is not None:
            used.setdefault(g.mapping.name, g.mapping)
    checks = "\n".join(mapping_ambiguity_check(m, _NORMALIZE) for m in used.values()) or None

    return GeneratedSql(
        checks_sql=checks,
        counts_sql=row_counts_sql(ctx.sources[rule.left], ctx.sources[rule.right]),
        diff_sql=_DIFF_SQL,
        totals_sql=_totals_sql(rule, ctx, groups),
    )
