"""Rule checks for dataset-level ``pii_columns``.

A column listed in a dataset's ``pii_columns`` is masked as ``***`` wherever a check shows its value.
It may not be used where the value itself becomes a break's identifier (``business_key``) or the
subject of the check, because that would store and export the raw value:

- record_match ``keys``; control_total ``group_by``; mapping ``column``;
- quality ``key`` and ``unique`` columns.

``tolerance`` is refused on pii compare fields (they are compared by hash). Free SQL (``filter``,
``measures``, ``custom_sql``) cannot be inspected; see docs/operations.md.
"""

from __future__ import annotations

from recon.config.models import (
    ControlTotalRule,
    DatasetConfig,
    MappingRule,
    QualityRule,
    RecordMatchRule,
    Rule,
    Unique,
)


def _pii(datasets: dict[str, DatasetConfig], dataset: str, column: str) -> bool:
    ds = datasets.get(dataset)
    return ds is not None and ds.is_pii(column)


def _msg(loc: str, dataset: str, column: str, use: str) -> str:
    return f"{loc}: column {column!r} of dataset {dataset!r} is listed in pii_columns and cannot be {use}"


def pii_issues(rule: Rule, datasets: dict[str, DatasetConfig]) -> list[str]:
    """Return ``"<location>: <message>"`` problems for pii columns used where they would leak."""
    out: list[str] = []
    if isinstance(rule, RecordMatchRule):
        for i, k in enumerate(rule.keys):
            for side, ds, col in (("left", rule.left, k.left), ("right", rule.right, k.right)):
                if _pii(datasets, ds, col):
                    out.append(_msg(f"keys.{i}.{side}", ds, col, "a key"))
        for i, c in enumerate(rule.compare):
            if c.tolerance is not None and (_pii(datasets, rule.left, c.left) or _pii(datasets, rule.right, c.right)):
                out.append(f"compare.{i}: tolerance is not allowed on pii field {c.left!r} (compared by hash)")
    elif isinstance(rule, ControlTotalRule):
        for i, g in enumerate(rule.group_by):
            for side, ds, col in (("left", rule.left, g.left), ("right", rule.right, g.right)):
                if _pii(datasets, ds, col):
                    out.append(_msg(f"group_by.{i}.{side}", ds, col, "a group"))
    elif isinstance(rule, MappingRule):
        if _pii(datasets, rule.dataset, rule.column):
            out.append(_msg("column", rule.dataset, rule.column, "checked by a mapping rule"))
    elif isinstance(rule, QualityRule):
        for i, col in enumerate(rule.key):
            if _pii(datasets, rule.dataset, col):
                out.append(_msg(f"key.{i}", rule.dataset, col, "a key"))
        for i, a in enumerate(rule.assertions):
            if isinstance(a, Unique):
                for col in a.columns:
                    if _pii(datasets, rule.dataset, col):
                        out.append(_msg(f"assertions.{i}.unique", rule.dataset, col, "a unique column"))
    return out
