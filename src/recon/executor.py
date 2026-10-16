"""Run engine (spec section 5): preflight, lock, run every selected rule, merge breaks, close the run.

Each rule runs in its own ``Database.transaction()``. Its generated SQL fills the ``#totals`` /
``#diffs`` temp tables (created from fixed DDL in a parameterless batch, then filled with
``PARAM_HEADER + INSERT ... SELECT`` under bound run parameters). A rule that fails for any reason
is rolled back and recorded as one ``error`` check in a fresh transaction; no totals or break
changes are written for it.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pyodbc

import recon
from recon.breaks import plan_merge
from recon.config.loader import ConfigError, Project, rule_version_hash
from recon.config.models import Rule
from recon.connectors import get_connector
from recon.db import Database, LockBusy
from recon.logging_setup import log_context
from recon.model import CheckRecord, ObservedDiff, TotalRow
from recon.periods import SNAPSHOT, RunPeriod
from recon.planner import exit_code, rule_period_label, run_status, select_rules
from recon.preflight import preflight
from recon.repository import Repository
from recon.sqlgen import generate
from recon.sqlgen.common import (
    DIFF_COLUMNS,
    DIFFS_DDL,
    PARAM_HEADER,
    TOTALS_COLUMNS,
    TOTALS_DDL,
    GenContext,
    GeneratedSql,
    params_for,
)

log = logging.getLogger(__name__)

ERROR_MESSAGE_LIMIT = 4000


@dataclass
class RunOutcome:
    run_id: int | None
    status: str
    exit_code: int
    workbook: Path | None = None
    messages: list[str] = field(default_factory=list)


class CheckError(Exception):
    """A rule-level failure detected by the engine itself (stale source, too many breaks)."""


def _refused(messages: list[str]) -> RunOutcome:
    return RunOutcome(run_id=None, status="failed", exit_code=2, workbook=None, messages=list(messages))


def git_commit(root: Path) -> str | None:
    """``git rev-parse HEAD`` in the project root; None when unavailable."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = res.stdout.strip()
    return sha if res.returncode == 0 and sha else None


def _gen_context(project: Project, db: Database, rules: list[Rule]) -> GenContext:
    names = {n for r in rules for n in r.dataset_names()}
    sources = {
        n: get_connector(project.sources[project.datasets[n].source].connector, db).as_sql_source(
            project.datasets[n]
        )
        for n in names
    }
    return GenContext(datasets=project.datasets, sources=sources, mappings=project.mappings)


# --------------------------------------------------------------------------- one rule


def _drop_temp_tables(db: Database) -> None:
    for name in ("#diffs", "#totals"):
        try:
            db.execute(f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name}")
        except pyodbc.Error:  # best effort: the tables also vanish with the session
            log.warning("could not drop temp table %s", name)


class _RuleRunner:
    """Executes one rule inside the caller's transaction; fills ``rows_*`` and ``gen`` as it goes."""

    def __init__(self, project: Project, db: Database, repo: Repository, ctx: GenContext, rule: Rule,
                 run_id: int, period: RunPeriod, today: date):
        self.project, self.db, self.repo, self.ctx, self.rule = project, db, repo, ctx, rule
        self.run_id = run_id
        self.label = rule_period_label(rule, project, period)
        self.period = period
        self.params = params_for(None if self.label == SNAPSHOT else period, today)
        self.gen: GeneratedSql | None = None
        self.rows_left: int | None = None
        self.rows_right: int | None = None

    def _with_params(self, sql: str) -> list[dict[str, Any]]:
        return self.db.query(PARAM_HEADER + sql, *self.params)

    def check_freshness(self) -> None:
        for name in self.rule.dataset_names():
            ds = self.project.datasets[name]
            if ds.freshness is None:
                continue
            col = "[" + ds.freshness.column.replace("]", "]]") + "]"
            rows = self._with_params(
                f"SELECT DATEDIFF(hour, MAX({col}), SYSDATETIME()) AS age_hours FROM {self.ctx.sources[name]} s"
            )
            age = rows[0]["age_hours"] if rows else None
            if age is None or age > ds.freshness.max_age_hours:
                raise CheckError(f"stale source {name}")

    def load_temp(self, table: str, ddl: str, columns: str, sql: str) -> None:
        self.db.execute(f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}")
        self.db.execute(ddl)
        self.db.execute(PARAM_HEADER + f"INSERT INTO {table} ({columns}) SELECT * FROM (\n{sql}\n) d",
                        *self.params)

    def run(self, started: float) -> CheckRecord:
        rule, db = self.rule, self.db
        self.gen = gen = generate(rule, self.ctx)
        self.check_freshness()
        if gen.checks_sql is not None:
            db.execute(PARAM_HEADER + gen.checks_sql, *self.params)
        counts = self._with_params(gen.counts_sql)[0]
        self.rows_left, self.rows_right = counts["rows_left"], counts["rows_right"]

        totals: list[TotalRow] = []
        if gen.totals_sql is not None:
            self.load_temp("#totals", TOTALS_DDL, TOTALS_COLUMNS, gen.totals_sql)
            totals = [
                TotalRow(r["group_key"], r["measure"], r["left_value"], r["right_value"], r["diff"],
                         r["tolerance_applied"], bool(r["within_tolerance"]))
                for r in db.query(f"SELECT {TOTALS_COLUMNS} FROM #totals ORDER BY group_key, measure")
            ]
        self.load_temp("#diffs", DIFFS_DDL, DIFF_COLUMNS, gen.diff_sql)
        n = int(db.scalar("SELECT COUNT_BIG(*) FROM #diffs"))
        if n > rule.max_breaks:
            raise CheckError(f"suspected load failure: {n} > max_breaks {rule.max_breaks}")
        observed = [
            ObservedDiff(**r)
            for r in db.query(
                f"SELECT {DIFF_COLUMNS} FROM #diffs ORDER BY business_key, break_type, field_name"
            )
        ]

        rec = CheckRecord(
            run_id=self.run_id, rule_id=rule.id, version_hash=rule_version_hash(rule),
            result="fail" if observed else "pass", rows_left=self.rows_left, rows_right=self.rows_right,
            break_count=len(observed), generated_sql=gen.full_text(),
            duration_ms=int((time.monotonic() - started) * 1000), error_message=None,
        )
        run_check_id = self.repo.record_check(rec)
        if totals:
            self.repo.insert_totals(run_check_id, totals)
        existing = self.repo.load_breaks(rule.id, self.label)
        signed_off = self.repo.active_signoff(rule.domain, self.period.label) is not None
        changes = plan_merge(existing, observed, rule_id=rule.id, domain=rule.domain,
                             period_label=self.label, signed_off=signed_off)
        self.repo.apply_engine_changes(changes, self.run_id)
        return rec


def _run_rule(project: Project, db: Database, repo: Repository, ctx: GenContext, rule: Rule, run_id: int,
              period: RunPeriod, today: date) -> CheckRecord:
    runner = _RuleRunner(project, db, repo, ctx, rule, run_id, period, today)
    started = time.monotonic()
    db.set_timeout(rule.timeout_s)
    try:
        try:
            with db.transaction():
                return runner.run(started)
        except Exception as exc:  # noqa: BLE001 - any rule failure becomes an error check (spec 5.5)
            rec = CheckRecord(
                run_id=run_id, rule_id=rule.id, version_hash=rule_version_hash(rule), result="error",
                rows_left=runner.rows_left, rows_right=runner.rows_right, break_count=0,
                generated_sql=runner.gen.full_text() if runner.gen is not None else "",
                duration_ms=int((time.monotonic() - started) * 1000),
                error_message=str(exc)[:ERROR_MESSAGE_LIMIT],
            )
            db.set_timeout(0)
            with db.transaction():
                repo.record_check(rec)
            return rec
    finally:
        db.set_timeout(0)
        _drop_temp_tables(db)


# --------------------------------------------------------------------------- the run


def execute_run(
    project: Project,
    db: Database,
    repo: Repository,
    *,
    domains: list[str] | None,
    rule_id: str | None,
    period: RunPeriod,
    trigger: Literal["scheduled", "manual"],
    export: Callable[[int], Path] | None,
    today: date,
) -> RunOutcome:
    try:
        rules = select_rules(project, domains, rule_id)
    except ConfigError as exc:
        return _refused(exc.issues)
    if not rules:
        return _refused(["no active rules selected"])

    try:
        issues = preflight(project, db, rules)
    except ConfigError as exc:
        issues = exc.issues
    if issues:
        return _refused(issues)

    run_domains = sorted({r.domain for r in rules})
    with ExitStack() as locks:
        for resource in (f"recon:{d}:{period.label}" for d in run_domains):
            try:
                locks.enter_context(db.applock(resource))
            except LockBusy:
                return _refused([f"run already in progress for {resource}"])
        return _locked_run(project, db, repo, rules, run_domains, period, trigger, export, today)


def _locked_run(project: Project, db: Database, repo: Repository, rules: list[Rule], run_domains: list[str],
                period: RunPeriod, trigger: str, export: Callable[[int], Path] | None,
                today: date) -> RunOutcome:
    messages: list[str] = []
    orphans = repo.fail_orphan_runs(period.label, run_domains)
    if orphans:
        log.warning("marked %d orphaned run(s) for %s as failed", orphans, period.label)
    with db.transaction():
        for rule in rules:
            repo.upsert_rule(rule.id, rule_version_hash(rule), rule.domain, rule.check_type, rule.severity,
                             rule.owner, project.rule_text[rule.id], rule.active)
    for d in run_domains:
        if repo.active_signoff(d, period.label) is not None:
            messages.append(f"warning: {d} {period.label} is signed off; new breaks are flagged post_signoff")

    run_id = repo.open_run(trigger, run_domains[0] if len(run_domains) == 1 else None, period.label,
                           period.start, period.end, git_commit(project.root), recon.__version__)
    results: list[str] = []
    aborted = False
    with log_context(run_id=run_id):
        log.info("run %s started: %d rule(s), period %s, trigger %s", run_id, len(rules), period.label, trigger)
        try:
            ctx = _gen_context(project, db, rules)
            for rule in rules:
                with log_context(rule_id=rule.id):
                    rec = _run_rule(project, db, repo, ctx, rule, run_id, period, today)
                    if rec.result == "error":
                        log.warning("check %s: error after %d ms: %s", rule.id, rec.duration_ms, rec.error_message)
                    else:
                        log.info("check %s: %s, %d break(s), rows %s/%s, %d ms", rule.id, rec.result,
                                 rec.break_count, rec.rows_left, rec.rows_right, rec.duration_ms)
                results.append(rec.result)
        except Exception as exc:
            log.exception("run %s aborted", run_id)
            messages.append(f"run aborted: {exc}")
            aborted = True
        status = "failed" if aborted else run_status(results)
        repo.close_run(run_id, status)
        log.info("run %s finished: %s", run_id, status)

        workbook = None
        if export is not None:
            try:
                workbook = export(run_id)
            except Exception as exc:
                log.exception("export of run %s failed", run_id)
                messages.append(f"export failed: {exc}")
                aborted = True
    return RunOutcome(run_id=run_id, status=status, exit_code=exit_code(results, aborted),
                      workbook=workbook, messages=messages)
