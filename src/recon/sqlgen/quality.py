"""``quality``: assertions on one dataset (spec section 4.6).

``diff_sql`` is a ``UNION ALL`` of one ``SELECT`` per assertion, each returning the six diff columns
with ``break_type = 'quality_violation'`` and ``business_key`` built from the rule's ``key`` columns
(except ``unique``, which reports the duplicated value). Values are compared as normalized strings
(trim, upper-case), so a blank counts as null. ``field_name`` is ``<assertion>:<column>`` (e.g.
``not_null:DEPRTMNT``, ``references:DEPRTMNT``), ``unique:<columns>``, or the ``custom_sql`` name. Nulls: only ``not_null`` reports them; ``unique``, ``accepted_values``,
``references`` and ``like`` look at non-null values only.

``like`` matches the trimmed value (CHAR columns keep their padding) under the BIN2 collation, so the
pattern is case-sensitive exactly as written (``[a-z]`` matches lower-case letters only);
``left_value`` shows the raw value. The outer SELECT collapses rows with the same
``(business_key, break_type, field_name)`` (``recon.break.break_key`` must be unique), keeping
``MIN()`` of the values.
"""

from __future__ import annotations

import re

from recon.config.models import (
    AcceptedValues,
    CustomSql,
    Like,
    Normalize,
    NotNull,
    QualityRule,
    References,
    Unique,
)
from recon.sqlgen.common import (
    COMPARE_COLLATION,
    TAB,
    GenContext,
    GeneratedSql,
    business_key_expr,
    column_ref,
    norm_string,
    source_for,
    sub,
    text_of,
)

_NORMALIZE = Normalize()  # quality rules carry no normalize option: trim + case-insensitive
_VIOLATION = "'quality_violation'"
_NULL = "CAST(NULL AS NVARCHAR(4000))"

# {dataset} is followed by a derived-table alias; these words after it mean the user gave none.
_NOT_ALIAS = (
    r"(?!(?:WHERE|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|OUTER|ON|GROUP|ORDER|HAVING|UNION|EXCEPT|INTERSECT"
    r"|OPTION|FOR|WITH)\b)"
)
_DATASET_PLACEHOLDER = re.compile(r"\{dataset\}(\s+(?:AS\s+)?" + _NOT_ALIAS + r"[A-Za-z_]\w*)?", re.IGNORECASE)


def _nlit(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def _accepted_text(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _branch(key: str, field_name: str, left: str, right: str, body: str) -> str:
    return (
        "SELECT\n"
        f"{TAB}{key} AS business_key,\n"
        f"{TAB}{_VIOLATION} AS break_type,\n"
        f"{TAB}{field_name} AS field_name,\n"
        f"{TAB}{left} AS left_value,\n"
        f"{TAB}{right} AS right_value,\n"
        f"{TAB}CAST(NULL AS DECIMAL(38,6)) AS diff\n"
        f"{body}"
    )


class _Builder:
    def __init__(self, rule: QualityRule, ctx: GenContext) -> None:
        self.rule = rule
        self.ctx = ctx
        self.source = source_for(ctx, rule.dataset)
        self.key = business_key_expr([norm_string(column_ref("s", k), _NORMALIZE) for k in rule.key])

    def _value(self, column: str) -> str:
        return norm_string(column_ref("s", column), _NORMALIZE)

    def _shown(self, column: str) -> str:
        """The value displayed as left_value: the raw text, or ``***`` for a dataset pii column."""
        ds = self.ctx.datasets.get(self.rule.dataset)
        if ds is not None and ds.is_pii(column):
            return _nlit("***")
        return text_of(column_ref("s", column))

    def not_null(self, a: NotNull) -> str:
        values = ",\n".join(f"({_nlit('not_null:' + c)}, {self._value(c)})" for c in a.columns)
        body = (
            f"FROM {self.source} AS s\n"
            f"CROSS APPLY (VALUES\n{sub(values)}\n) AS v (field_name, value)\n"
            "WHERE v.value IS NULL"
        )
        return _branch(self.key, "v.field_name", _NULL, _NULL, body)

    def unique(self, a: Unique) -> str:
        cols = [f"k{i}" for i, _ in enumerate(a.columns, start=1)]
        inner = ",\n".join(f"{self._value(c)} AS {k}" for c, k in zip(a.columns, cols, strict=True))
        refs = [f"u.{k}" for k in cols]
        body = (
            "FROM (\n"
            f"{TAB}SELECT\n{sub(sub(inner))}\n"
            f"{TAB}FROM {self.source} AS s\n"
            ") AS u\n"
            f"WHERE {' AND '.join(f'{r} IS NOT NULL' for r in refs)}\n"
            f"GROUP BY {', '.join(refs)}\n"
            "HAVING COUNT_BIG(*) > 1"
        )
        return _branch(
            business_key_expr(refs),
            _nlit("unique:" + ",".join(a.columns)),
            "CONVERT(NVARCHAR(4000), COUNT_BIG(*))",
            _NULL,
            body,
        )

    def accepted_values(self, a: AcceptedValues) -> str:
        values = ",\n".join(f"({norm_string(_nlit(_accepted_text(v)), _NORMALIZE)})" for v in a.values)
        value = self._value(a.column)
        body = (
            f"FROM {self.source} AS s\n"
            f"WHERE {value} IS NOT NULL\n"
            f"{TAB}AND NOT EXISTS (\n"
            f"{TAB}{TAB}SELECT 1 FROM (VALUES\n{sub(sub(sub(values)))}\n{TAB}{TAB}) AS a (value)\n"
            f"{TAB}{TAB}WHERE a.value = {value}\n"
            f"{TAB})"
        )
        return _branch(self.key, _nlit("accepted_values:" + a.column), self._shown(a.column), _NULL, body)

    def references(self, a: References) -> str:
        ref_source = source_for(self.ctx, a.dataset)
        ref_value = norm_string(column_ref("r", a.ref_column), _NORMALIZE)
        value = self._value(a.column)
        body = (
            f"FROM {self.source} AS s\n"
            f"WHERE {value} IS NOT NULL\n"
            f"{TAB}AND NOT EXISTS (\n"
            f"{TAB}{TAB}SELECT 1 FROM {ref_source} AS r WHERE {ref_value} = {value}\n"
            f"{TAB})"
        )
        return _branch(self.key, _nlit("references:" + a.column), self._shown(a.column), _NULL, body)

    def like(self, a: Like) -> str:
        raw = text_of(column_ref("s", a.column))
        trimmed = f"LTRIM(RTRIM({raw})) COLLATE {COMPARE_COLLATION}"
        body = (
            f"FROM {self.source} AS s\n"
            f"WHERE {self._value(a.column)} IS NOT NULL\n"
            f"{TAB}AND {trimmed} NOT LIKE {_nlit(a.pattern)} COLLATE {COMPARE_COLLATION}"
        )
        return _branch(self.key, _nlit("like:" + a.column), self._shown(a.column), _NULL, body)

    def custom_sql(self, a: CustomSql) -> str:
        sql = _DATASET_PLACEHOLDER.sub(
            lambda m: self.source + (m.group(1) or " AS [dataset]"), a.sql.strip()
        )
        return (
            "SELECT\n"
            f"{TAB}CONVERT(NVARCHAR(400), c.business_key) AS business_key,\n"
            f"{TAB}{_VIOLATION} AS break_type,\n"
            f"{TAB}{_nlit(a.name)} AS field_name,\n"
            f"{TAB}{_NULL} AS left_value,\n"
            f"{TAB}{_NULL} AS right_value,\n"
            f"{TAB}CAST(NULL AS DECIMAL(38,6)) AS diff\n"
            "FROM (\n" + sub(sql) + "\n) AS c"
        )

    def build(self, a: object) -> str:
        for cls, method in (
            (NotNull, self.not_null),
            (Unique, self.unique),
            (AcceptedValues, self.accepted_values),
            (References, self.references),
            (Like, self.like),
            (CustomSql, self.custom_sql),
        ):
            if isinstance(a, cls):
                return method(a)
        raise NotImplementedError(f"no SQL generator for assertion {type(a).__name__}")


def _diff_sql(rule: QualityRule, ctx: GenContext) -> str:
    builder = _Builder(rule, ctx)
    branches = [builder.build(a) for a in rule.assertions]
    union = "\nUNION ALL\n".join(f"-- assertion {i}\n{b}" for i, b in enumerate(branches, start=1))
    return (
        "SELECT\n"
        f"{TAB}CAST(q.business_key AS NVARCHAR(400)) COLLATE DATABASE_DEFAULT AS business_key,\n"
        f"{TAB}CAST(q.break_type AS VARCHAR(30)) AS break_type,\n"
        f"{TAB}CAST(q.field_name AS NVARCHAR(128)) AS field_name,\n"
        f"{TAB}CAST(MIN(q.left_value) AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS left_value,\n"
        f"{TAB}CAST(MIN(q.right_value) AS NVARCHAR(4000)) COLLATE DATABASE_DEFAULT AS right_value,\n"
        f"{TAB}CAST(MIN(q.diff) AS DECIMAL(38,6)) AS diff\n"
        "FROM (\n" + sub(union) + "\n) AS q\n"
        "GROUP BY q.business_key, q.break_type, q.field_name"
    )


def generate_quality(rule: QualityRule, ctx: GenContext) -> GeneratedSql:
    source = source_for(ctx, rule.dataset)
    counts = (
        "SELECT\n"
        f"{TAB}(SELECT COUNT_BIG(*) FROM {source} AS s) AS rows_left,\n"
        f"{TAB}CAST(0 AS BIGINT) AS rows_right"
    )
    return GeneratedSql(checks_sql=None, counts_sql=counts, diff_sql=_diff_sql(rule, ctx))
