"""Building blocks shared by the T-SQL generators (spec sections 4.3, 4.7 and 4.8).

Every generated batch starts with :data:`PARAM_HEADER`, which declares the run parameters from
bound ``?`` parameters; run values are never interpolated into SQL text. Identifiers are validated
and bracket-quoted by :func:`qi`.

``diff_sql`` contract (all generators): one ``SELECT`` usable verbatim as a derived table
(``SELECT * INTO #diffs FROM (<diff_sql>) d``: no top-level ``WITH``, no ``ORDER BY``) returning
``business_key NVARCHAR(400), break_type VARCHAR(30), field_name NVARCHAR(128) NULL,
left_value NVARCHAR(4000) NULL, right_value NVARCHAR(4000) NULL, diff DECIMAL(38,6) NULL``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from recon.config.models import DatasetConfig, MappingConfig, Normalize
from recon.periods import RunPeriod

PARAM_HEADER = "DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;\n"
BATCH_SEPARATOR = "\nGO\n"

# Normalized strings are compared under an explicit binary collation so that results do not depend
# on the database/column collations (a case-insensitive default would otherwise ignore
# ``case_insensitive: false``, and differing left/right column collations would conflict).
COMPARE_COLLATION = "Latin1_General_100_BIN2"

NULL_KEY = "∅"  # stands in for a NULL key part inside business_key

SQL_TYPES = {
    "decimal": "DECIMAL(38,6)",
    "int": "BIGINT",
    "date": "DATE",
    "datetime": "DATETIME2(3)",
    "bool": "BIT",
}

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def params_for(period: RunPeriod | None, today: date) -> tuple[date | None, date | None, date]:
    """Bound values for PARAM_HEADER: ``(@period_start, @period_end_excl, @as_of)``.

    ``None`` (a snapshot rule) leaves the period unset and evaluates mappings as of ``today``.
    """
    if period is None:
        return (None, None, today)
    return (period.start, period.end_excl, period.end)


@dataclass(frozen=True)
class GenContext:
    datasets: dict[str, DatasetConfig]
    sources: dict[str, str]  # dataset name -> parenthesised derived table (Connector.as_sql_source)
    mappings: dict[str, MappingConfig]


@dataclass(frozen=True)
class GeneratedSql:
    checks_sql: str | None
    counts_sql: str
    diff_sql: str
    totals_sql: str | None = None

    def full_text(self) -> str:
        """Every non-null part in execution order, each prefixed by PARAM_HEADER, joined by GO."""
        parts = [self.checks_sql, self.counts_sql, self.totals_sql, self.diff_sql]
        return BATCH_SEPARATOR.join(PARAM_HEADER + p for p in parts if p is not None)


# --------------------------------------------------------------------------- expression helpers


def qi(identifier: str) -> str:
    """Validate a one- or two-part identifier and bracket-quote each part."""
    if not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier {identifier!r}")
    return ".".join(f"[{part}]" for part in identifier.split("."))


def norm_string(expr: str, normalize: Normalize) -> str:
    """``expr`` as a normalized NVARCHAR(4000): optional trim (blank -> NULL) and upper-casing."""
    out = f"CONVERT(NVARCHAR(4000), {expr}) COLLATE {COMPARE_COLLATION}"
    if normalize.trim:
        out = f"NULLIF(LTRIM(RTRIM({out})), N'')"
    if normalize.case_insensitive:
        out = f"UPPER({out})"
    return out


def typed(expr: str, type_: str, normalize: Normalize | None = None) -> str:
    """``expr`` cast to the compare type; NULL when the cast fails.

    Blank text is NULL for non-string types (SQL Server would otherwise turn ``''`` into 0, 1900-01-01
    or false). The CASE keeps ``expr``'s own type, so floats are not round-tripped through text.
    """
    if type_ == "string":
        return norm_string(expr, normalize or Normalize())
    try:
        sql_type = SQL_TYPES[type_]
    except KeyError:
        raise ValueError(f"unknown compare type {type_!r}") from None
    blank_to_null = f"CASE WHEN LTRIM(RTRIM(CONVERT(NVARCHAR(4000), {expr}))) = N'' THEN NULL ELSE {expr} END"
    return f"TRY_CAST({blank_to_null} AS {sql_type})"


def business_key_expr(exprs: list[str]) -> str:
    """Join NVARCHAR key parts with ``|``; ``\\`` and ``|`` inside a part are backslash-escaped.

    NULL parts become ``∅``. The REPLACE search strings are written as plain ``'\\'``/``'|'``
    literals so that ``N'|'`` appears only as the separator.
    """
    if not exprs:
        raise ValueError("business_key_expr needs at least one key expression")
    parts = [f"REPLACE(REPLACE(ISNULL({e}, N'{NULL_KEY}'), '\\', N'\\\\'), '|', N'\\|')" for e in exprs]
    return " + N'|' + ".join(parts)


def pii_hash(expr: str) -> str:
    return f"HASHBYTES('SHA2_256', {expr})"


def mapping_source(m: MappingConfig) -> str:
    """Parenthesised derived table of the mapping rows valid on ``@as_of`` (all rows if undated)."""
    sql = f"(SELECT * FROM {qi(m.object)}"
    if m.effective is not None:
        start, end = qi(m.effective.start), qi(m.effective.end)
        sql += f" WHERE {start} <= @as_of AND ({end} IS NULL OR {end} >= @as_of)"
    return sql + ")"


def mapping_ambiguity_check(m: MappingConfig, normalize: Normalize | None = None) -> str:
    """Statements that THROW 50001 when a ``from`` value has more than one valid mapping row.

    ``from`` values are grouped as the rule's join normalizes them (``normalize``, default
    trim + case-insensitive), so ``cc1`` and ``CC1`` count as the same value.
    """
    var = f"@ambiguous_{m.name}"
    key = norm_string(f"mp.{qi(m.from_)}", normalize or Normalize())
    return (
        f"DECLARE {var} NVARCHAR(2048);\n"
        f"SELECT TOP (1) {var} = N'ambiguous mapping {m.name}: value ' + a.from_value\n"
        f"    + N' has ' + CONVERT(NVARCHAR(20), a.valid_rows) + N' valid rows'\n"
        "FROM (\n"
        f"    SELECT {key} AS from_value, COUNT_BIG(*) AS valid_rows\n"
        f"    FROM {mapping_source(m)} AS mp\n"
        f"    WHERE {key} IS NOT NULL\n"
        f"    GROUP BY {key}\n"
        "    HAVING COUNT_BIG(*) > 1\n"
        ") AS a\n"
        "ORDER BY a.from_value;\n"
        f"IF {var} IS NOT NULL\n"
        f"    THROW 50001, {var}, 1;"
    )
