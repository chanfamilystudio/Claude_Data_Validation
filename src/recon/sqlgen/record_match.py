"""``record_match``: row-level comparison of two datasets on normalized keys (spec section 4.3).

Shape of the generated ``diff_sql`` (nested derived tables, no CTEs, so it can be wrapped as
``SELECT * INTO #diffs FROM (<diff_sql>) d``):

1. per side, a base select over the dataset source: ``business_key`` from the normalized keys and,
   per compare field ``fN``, the typed value ``fN_val`` (PII: its SHA2_256 hash), the raw text
   ``fN_raw`` when the cast can fail, and on the left for ``via_mapping`` the source text
   ``fN_src`` and the flag ``fN_unmapped`` (non-null value without a valid crosswalk row);
2. per side, one row per ``business_key`` (``key_rows`` = row count; values via MAX, which is the
   row's own value whenever ``key_rows = 1``);
3. ``FULL OUTER JOIN`` of the two sides on ``business_key`` with each field's comparison;
4. ``CROSS APPLY`` emitting the breaks of each key: ``quality_violation`` (``__key__``) for duplicate
   keys (and nothing else for them), ``missing_left`` / ``missing_right``, else one row per
   breaking field (``mapping_missing`` or ``value_mismatch``).
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import indent

from recon.config.models import CompareField, MappingConfig, RecordMatchRule
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
    pii_hash,
    row_counts_sql,
    sub,
    text_of,
    tolerance_sql,
    typed,
)

_NULL_DECIMAL = "CAST(NULL AS DECIMAL(38,6))"


@dataclass(frozen=True)
class _Field:
    n: int
    cfg: CompareField
    mapping: MappingConfig | None

    @property
    def p(self) -> str:
        return f"f{self.n}"

    @property
    def can_fail_cast(self) -> bool:
        return self.cfg.type != "string"

    @property
    def numeric(self) -> bool:
        return self.cfg.type in ("decimal", "int") and not self.cfg.pii

    @property
    def label(self) -> str:
        via = f" via {self.mapping.name}" if self.mapping else ""
        pii = ", pii" if self.cfg.pii else ""
        return f"-- {self.p}: {self.cfg.left}{via} vs {self.cfg.right} ({self.cfg.type}{pii})"


# --------------------------------------------------------------------------- per side


def _side_sql(rule: RecordMatchRule, source: str, fields: list[_Field], side: str) -> str:
    """One row per business_key of one side: key_rows plus the per-field columns."""
    norm = rule.normalize
    keys = [norm_string(column_ref("s", getattr(k, side)), norm) for k in rule.keys]
    base = [f"{business_key_expr(keys)} AS business_key"]
    agg = ["base.business_key", "COUNT_BIG(*) AS key_rows"]
    joins: list[str] = []

    for f in fields:
        col = column_ref("s", getattr(f.cfg, side))
        value_src = col
        entries: list[tuple[str, str]] = []
        if side == "left" and f.mapping is not None:
            alias = f"m{f.n}"
            frm = column_ref(alias, f.mapping.from_)
            value_src = column_ref(alias, f.mapping.to)
            joins.append(
                f"LEFT JOIN {mapping_source(f.mapping)} AS {alias}\n"
                f"{TAB}ON {norm_string(frm, norm)} = {norm_string(col, norm)}"
            )
            if not f.cfg.pii:
                entries.append((text_of(col), "src"))
            entries.append(
                (f"CASE WHEN {norm_string(col, norm)} IS NOT NULL AND {frm} IS NULL THEN 1 ELSE 0 END", "unmapped")
            )
        value = typed(value_src, f.cfg.type, norm)
        if f.cfg.pii:
            value = pii_hash(value if f.cfg.type == "string" else text_of(value))
        entries.append((value, "val"))
        if f.can_fail_cast:
            entries.append((text_of(value_src), "raw"))

        for i, (expr, suffix) in enumerate(entries):
            name = f"{f.p}_{suffix}"
            base.append((f.label + "\n" if i == 0 else "") + f"{expr} AS {name}")
            if suffix == "val" and f.cfg.type == "bool" and not f.cfg.pii:  # MAX() rejects BIT
                agg.append(f"CONVERT(BIT, MAX(CONVERT(TINYINT, base.{name}))) AS {name}")
            else:
                agg.append(f"MAX(base.{name}) AS {name}")

    base_sql = "SELECT\n" + block(base) + f"\nFROM {source} AS s"
    if joins:
        base_sql += "\n" + "\n".join(joins)
    return (
        "SELECT\n" + block(agg) + "\nFROM (\n" + sub(base_sql) + "\n) AS base\nGROUP BY base.business_key"
    )


# --------------------------------------------------------------------------- comparison


def _bad(a: str, f: _Field) -> str:
    """The cast failed: no typed value although the raw text is not blank."""
    return f"({a}.{f.p}_val IS NULL AND NULLIF(LTRIM(RTRIM({a}.{f.p}_raw)), N'') IS NOT NULL)"


def _display(a: str, f: _Field) -> str:
    if f.cfg.pii:
        return "N'***'"
    value = f"{a}.{f.p}_val" if f.cfg.type == "string" else f"CONVERT(NVARCHAR(4000), {a}.{f.p}_val)"
    whens = []
    if a == "l" and f.mapping is not None:
        whens.append(f"WHEN l.{f.p}_unmapped = 1 THEN l.{f.p}_src")
    if f.can_fail_cast:
        whens.append(f"WHEN {_bad(a, f)} THEN {a}.{f.p}_raw")
    if not whens:
        return value
    return f"CASE {' '.join(whens)} ELSE {value} END"


def _compare_columns(f: _Field) -> list[str]:
    lv, rv = f"l.{f.p}_val", f"r.{f.p}_val"
    diff = None
    if f.numeric:
        diff = (
            f"{lv} - {rv}"
            if f.cfg.type == "decimal"
            else f"CONVERT(DECIMAL(19,0), {lv}) - CONVERT(DECIMAL(19,0), {rv})"
        )
    tol = tolerance_sql(f.cfg.tolerance, rv) if f.numeric else None
    if tol is not None:
        equal = f"({lv} IS NULL AND {rv} IS NULL) OR ABS({diff}) <= {tol}"
    else:
        equal = f"{lv} = {rv} OR ({lv} IS NULL AND {rv} IS NULL)"

    whens = []
    if f.mapping is not None:
        whens.append(f"WHEN l.{f.p}_unmapped = 1 THEN 'mapping_missing'")
    if f.can_fail_cast:
        whens.append(f"WHEN {_bad('l', f)} OR {_bad('r', f)} THEN 'value_mismatch'")
    whens.append(f"WHEN {equal} THEN NULL")
    whens.append("ELSE 'value_mismatch'")
    break_expr = "CASE\n" + indent("\n".join(whens), TAB) + "\nEND"
    return [
        f"{f.label}\n{break_expr} AS {f.p}_break",
        f"{_display('l', f)} AS {f.p}_left",
        f"{_display('r', f)} AS {f.p}_right",
        f"{diff or _NULL_DECIMAL} AS {f.p}_diff",
    ]


# --------------------------------------------------------------------------- public


def _fields(rule: RecordMatchRule, ctx: GenContext) -> list[_Field]:
    fields = []
    for i, cfg in enumerate(rule.compare, start=1):
        mapping = None
        if cfg.via_mapping:
            if cfg.via_mapping not in ctx.mappings:
                raise ValueError(f"rule {rule.id}: unknown mapping {cfg.via_mapping!r}")
            mapping = ctx.mappings[cfg.via_mapping]
        fields.append(_Field(i, cfg, mapping))
    return fields


def _diff_sql(rule: RecordMatchRule, ctx: GenContext, fields: list[_Field]) -> str:
    left = _side_sql(rule, ctx.sources[rule.left], fields, "left")
    right = _side_sql(rule, ctx.sources[rule.right], fields, "right")
    keyed_cols = [
        "COALESCE(l.business_key, r.business_key) AS business_key",
        "l.key_rows AS l_rows",
        "r.key_rows AS r_rows",
    ]
    for f in fields:
        keyed_cols += _compare_columns(f)
    keyed = (
        "SELECT\n" + block(keyed_cols)
        + "\nFROM (\n" + sub(left) + "\n) AS l"
        + "\nFULL OUTER JOIN (\n" + sub(right) + "\n) AS r"
        + f"\n{TAB}ON r.business_key = l.business_key"
    )
    values = ",\n".join(
        f"(k.{f.p}_break, N'{f.cfg.left}', k.{f.p}_left, k.{f.p}_right, k.{f.p}_diff)" for f in fields
    )
    breaks = (
        "-- duplicate keys: one quality_violation each (row counts), excluded from all other checks\n"
        "SELECT 'quality_violation', N'__key__', CONVERT(NVARCHAR(4000), ISNULL(k.l_rows, 0)),"
        f" CONVERT(NVARCHAR(4000), ISNULL(k.r_rows, 0)), {_NULL_DECIMAL}\n"
        "WHERE k.l_rows > 1 OR k.r_rows > 1\n"
        "UNION ALL\n"
        "SELECT 'missing_left', NULL, NULL, NULL, NULL\n"
        "WHERE k.l_rows IS NULL AND k.r_rows = 1\n"
        "UNION ALL\n"
        "SELECT 'missing_right', NULL, NULL, NULL, NULL\n"
        "WHERE k.l_rows = 1 AND k.r_rows IS NULL\n"
        "UNION ALL\n"
        "-- matched keys: one row per breaking compare field\n"
        "SELECT f.break_type, f.field_name, f.left_value, f.right_value, f.diff\n"
        "FROM (VALUES\n" + sub(values) + "\n) AS f (break_type, field_name, left_value, right_value, diff)\n"
        "WHERE k.l_rows = 1 AND k.r_rows = 1 AND f.break_type IS NOT NULL"
    )
    return (
        "SELECT\n"
        f"{TAB}CAST(k.business_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,\n"
        f"{TAB}CAST(b.break_type AS VARCHAR(30)) AS break_type,\n"
        f"{TAB}CAST(b.field_name AS NVARCHAR(128)) AS field_name,\n"
        f"{TAB}CAST(b.left_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,\n"
        f"{TAB}CAST(b.right_value AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,\n"
        f"{TAB}CAST(b.diff AS DECIMAL(38,6)) AS diff\n"
        "FROM (\n" + sub(keyed) + "\n) AS k\n"
        "CROSS APPLY (\n" + sub(breaks) + "\n) AS b (break_type, field_name, left_value, right_value, diff)"
    )


def generate_record_match(rule: RecordMatchRule, ctx: GenContext) -> GeneratedSql:
    fields = _fields(rule, ctx)

    used: dict[str, MappingConfig] = {}  # every distinct mapping, dated or not (spec 4.1)
    for f in fields:
        if f.mapping is not None:
            used.setdefault(f.mapping.name, f.mapping)
    checks = "\n".join(mapping_ambiguity_check(m, rule.normalize) for m in used.values()) or None

    counts = row_counts_sql(ctx.sources[rule.left], ctx.sources[rule.right])
    return GeneratedSql(checks_sql=checks, counts_sql=counts, diff_sql=_diff_sql(rule, ctx, fields))
