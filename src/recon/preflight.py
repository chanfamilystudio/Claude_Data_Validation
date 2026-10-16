"""Run preflight (spec section 5, step 1): every column a rule names must exist in its dataset.

Each referenced dataset and mapping object is described once. Free SQL (measures, dataset filters,
``custom_sql``) is not checked here; errors in it surface when the rule runs.
"""

from __future__ import annotations

from recon.config.loader import ConfigError, Project
from recon.config.models import (
    AcceptedValues,
    ControlTotalRule,
    CustomSql,
    DatasetConfig,
    Like,
    MappingRule,
    NotNull,
    QualityRule,
    RecordMatchRule,
    References,
    Rule,
    Unique,
)
from recon.connectors import SqlServerConnector, get_connector
from recon.db import Database


def _rule_columns(rule: Rule) -> list[tuple[str, str, str]]:
    """``(location, dataset name, column)`` for every column the rule names outside free SQL."""
    out: list[tuple[str, str, str]] = []
    if isinstance(rule, RecordMatchRule):
        for i, k in enumerate(rule.keys):
            out += [(f"keys.{i}.left", rule.left, k.left), (f"keys.{i}.right", rule.right, k.right)]
        for i, c in enumerate(rule.compare):
            out += [(f"compare.{i}.left", rule.left, c.left), (f"compare.{i}.right", rule.right, c.right)]
    elif isinstance(rule, ControlTotalRule):
        for i, g in enumerate(rule.group_by):
            out += [(f"group_by.{i}.left", rule.left, g.left), (f"group_by.{i}.right", rule.right, g.right)]
    elif isinstance(rule, MappingRule):
        out += [("column", rule.dataset, rule.column), ("target.column", rule.target.dataset, rule.target.column)]
    elif isinstance(rule, QualityRule):
        out += [(f"key.{i}", rule.dataset, k) for i, k in enumerate(rule.key)]
        for i, a in enumerate(rule.assertions):
            loc = f"assertions.{i}"
            if isinstance(a, NotNull | Unique):
                tag = "not_null" if isinstance(a, NotNull) else "unique"
                out += [(f"{loc}.{tag}.columns.{j}", rule.dataset, c) for j, c in enumerate(a.columns)]
            elif isinstance(a, AcceptedValues):
                out.append((f"{loc}.accepted_values.column", rule.dataset, a.column))
            elif isinstance(a, Like):
                out.append((f"{loc}.like.column", rule.dataset, a.column))
            elif isinstance(a, References):
                out += [
                    (f"{loc}.references.column", rule.dataset, a.column),
                    (f"{loc}.references.ref_column", a.dataset, a.ref_column),
                ]
            elif not isinstance(a, CustomSql):  # pragma: no cover - new assertion types must be added here
                raise NotImplementedError(f"preflight does not know assertion {type(a).__name__}")
    return out


def _describe(connector, cfg: DatasetConfig, issues: list[str], missing_msg: str | None = None) -> set[str] | None:
    try:
        return {c.name.lower() for c in connector.describe(cfg)}
    except ConfigError as exc:
        issues.extend([missing_msg] if missing_msg else exc.issues)
        return None


def preflight(project: Project, db: Database, rules: list[Rule]) -> list[str]:
    """Every problem found (empty when the rules can run)."""
    issues: list[str] = []

    columns: dict[str, set[str] | None] = {}
    for name in sorted({n for r in rules for n in r.dataset_names()}):
        ds = project.datasets[name]
        columns[name] = cols = _describe(get_connector(project.sources[ds.source].connector, db), ds, issues)
        if cols is None:
            continue
        checks = [("period_column", ds.period_column)]
        if ds.freshness is not None:
            checks.append(("freshness.column", ds.freshness.column))
        issues += [
            f"dataset {name}: {loc}: column '{col}' not found in {ds.object}"
            for loc, col in checks
            if col is not None and col.lower() not in cols
        ]

    for rule in rules:
        for loc, dataset, col in _rule_columns(rule):
            cols = columns[dataset]
            if cols is not None and col.lower() not in cols:
                issues.append(f"rule {rule.id}: {loc}: column '{col}' not found in dataset {dataset}")

    mapping_connector = SqlServerConnector(db)
    for name in sorted({m for r in rules for _, m in r.mapping_refs()}):
        mp = project.mappings[name]
        as_dataset = DatasetConfig(name=mp.name, source="mapping", object=mp.object)
        cols = _describe(mapping_connector, as_dataset, issues, f"mapping {name}: object '{mp.object}' not found")
        if cols is None:
            continue
        checks = [("from", mp.from_), ("to", mp.to)]
        if mp.effective is not None:
            checks += [("effective.start", mp.effective.start), ("effective.end", mp.effective.end)]
        issues += [
            f"mapping {name}: {loc}: column '{col}' not found in {mp.object}"
            for loc, col in checks
            if col.lower() not in cols
        ]
    return issues
