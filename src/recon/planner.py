"""Run planning: which rules run, under which period label, and how results roll up (spec section 5)."""

from __future__ import annotations

from recon.config.loader import ConfigError, Project
from recon.config.models import Rule
from recon.periods import SNAPSHOT, RunPeriod


def select_rules(project: Project, domains: list[str] | None, rule_id: str | None) -> list[Rule]:
    """Active rules for ``domains`` (all when None) or the single ``rule_id``, sorted by id."""
    if domains is not None and rule_id is not None:
        raise ConfigError(["select rules by domain or by rule id, not both"])
    if rule_id is not None:
        rule = project.rules.get(rule_id)
        if rule is None:
            raise ConfigError([f"unknown rule '{rule_id}'"])
        if not rule.active:
            raise ConfigError([f"rule '{rule_id}' is inactive"])
        return [rule]
    if domains is not None:
        known = {r.domain for r in project.rules.values()}
        unknown = sorted(set(domains) - known)
        if unknown:
            raise ConfigError([f"unknown domain '{d}'" for d in unknown])
    wanted = None if domains is None else set(domains)
    return sorted(
        (r for r in project.rules.values() if r.active and (wanted is None or r.domain in wanted)),
        key=lambda r: r.id,
    )


def rule_period_label(rule: Rule, project: Project, period: RunPeriod) -> str:
    """``SNAPSHOT`` iff no dataset the rule reads is period-filtered; else the run period's label."""
    if all(project.datasets[name].period_column is None for name in rule.dataset_names()):
        return SNAPSHOT
    return period.label


def run_status(results: list[str]) -> str:
    """``succeeded`` (no errors), ``failed`` (all errors, or nothing ran), else ``partial``."""
    errors = sum(1 for r in results if r == "error")
    if not results or errors == len(results):
        return "failed"
    return "partial" if errors else "succeeded"


def exit_code(results: list[str], aborted: bool) -> int:
    """``0`` all pass, ``1`` breaks and no errors, ``2`` any error, an abort, or nothing ran."""
    if aborted or not results or "error" in results:
        return 2
    return 1 if "fail" in results else 0
