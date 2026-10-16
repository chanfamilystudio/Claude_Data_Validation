"""Command-line entry point. Later tasks add commands to ``app``."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import pyodbc
import typer

from recon.config.loader import ConfigError, Project, load_project
from recon.db import Database, LockBusy, connection_string
from recon.excel.export import export_path, export_workbook
from recon.excel.importer import WorkbookError, import_workbook
from recon.executor import execute_run
from recon.logging_setup import configure_logging
from recon.migrations import apply_migrations
from recon.periods import SNAPSHOT, resolve_period
from recon.repository import Repository, SqlRepository, VersionConflict
from recon.signoff import SignoffRefused, revoke, sign_off

app = typer.Typer(no_args_is_help=True, help="Workday to Great Plains reconciliation engine.")
rules_app = typer.Typer(no_args_is_help=True, help="Work with reconciliation rules.")
app.add_typer(rules_app, name="rules")
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")
signoff_app = typer.Typer(invoke_without_command=True, help="Sign off a reviewed period, or revoke a sign-off.")
app.add_typer(signoff_app, name="signoff")


@rules_app.command("validate")
def validate(
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
) -> None:
    """Validate config/ and rules/ without touching a database."""
    try:
        project = load_project(root)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    typer.echo(f"OK: {len(project.rules)} rules")


@db_app.command("migrate")
def migrate(
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/connection.yaml.")] = Path("."),
) -> None:
    """Create or upgrade the recon schema."""
    try:
        conn_str = connection_string(root, os.environ)
        database = Database(conn_str)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    except pyodbc.Error as exc:
        typer.echo(f"Cannot connect to the database: {exc}")
        raise typer.Exit(2) from exc
    try:
        applied = apply_migrations(database)
    except LockBusy as exc:
        typer.echo(f"Another migration is already running ({exc.resource})")
        raise typer.Exit(2) from exc
    except pyodbc.Error as exc:
        typer.echo(f"Migration failed: {exc}")
        raise typer.Exit(2) from exc
    finally:
        database.close()
    typer.echo("Applied: " + ", ".join(applied) if applied else "Up to date")


def run_exporter(repo: Repository, export_dir: Path, domain: str | None,
                 period_label: str) -> Callable[[int], Path | None]:
    """Export callable for ``execute_run``: a failed export is a warning, never a different exit code."""

    def export(run_id: int) -> Path | None:
        try:
            return export_workbook(repo, domain=domain, period_label=period_label, run_id=run_id,
                                   out_path=export_path(export_dir, domain, period_label, run_id))
        except Exception as exc:
            logging.getLogger("recon").warning("export of run %s failed: %s", run_id, exc, exc_info=True)
            typer.echo(f"Warning: workbook export failed: {exc}")
            return None

    return export


class Trigger(str, Enum):
    manual = "manual"
    scheduled = "scheduled"


@app.command("run")
def run(
    period: Annotated[str, typer.Option("--period", help="YYYY-MM, current or previous.")],
    domain: Annotated[str | None, typer.Option("--domain", help="Run the rules of this domain.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every active rule.")] = False,
    rule: Annotated[str | None, typer.Option("--rule", help="Run a single rule by id.")] = None,
    trigger: Annotated[Trigger, typer.Option("--trigger", help="Who started the run.")] = Trigger.manual,
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
    no_export: Annotated[bool, typer.Option("--no-export", help="Do not write the run workbook.")] = False,
) -> None:
    """Run reconciliation rules for a period."""
    chosen = sum([domain is not None, all_, rule is not None])
    if chosen != 1:
        typer.echo("Specify exactly one of --domain, --all or --rule.")
        raise typer.Exit(2)
    if period.strip().upper() == SNAPSHOT:
        typer.echo("--period must be YYYY-MM, current or previous (snapshot rules run within every period)")
        raise typer.Exit(2)
    today = date.today()
    try:
        run_period = resolve_period(period, today)
    except ValueError as exc:
        typer.echo(f"invalid period {period!r}: expected YYYY-MM, current or previous")
        raise typer.Exit(2) from exc
    try:
        project = load_project(root)
        conn_str = connection_string(root, os.environ)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    configure_logging(project.settings.log_dir)
    try:
        database = Database(conn_str)
    except pyodbc.Error as exc:
        typer.echo(f"Cannot connect to the database: {exc}")
        raise typer.Exit(2) from exc
    try:
        repo = SqlRepository(database)
        export = None
        if not no_export:
            export_domain = domain if domain is not None else (
                project.rules[rule].domain if rule is not None and rule in project.rules else None)
            export = run_exporter(repo, project.settings.export_dir, export_domain, run_period.label)
        try:
            outcome = execute_run(
                project, database, repo, domains=[domain] if domain is not None else None, rule_id=rule,
                period=run_period, trigger=trigger.value, export=export, today=today,
            )
        except Exception as exc:  # exit 1 would read as "breaks found"
            typer.echo(f"Run failed: {exc}")
            raise typer.Exit(2) from exc
        for message in outcome.messages:
            typer.echo(message)
        if outcome.run_id is not None:
            try:
                checks = repo.checks_for_run(outcome.run_id)
            except Exception as exc:  # exit 1 would read as "breaks found"
                typer.echo(f"Run {outcome.run_id}: {outcome.status} (cannot read its checks: {exc})")
                raise typer.Exit(2) from exc
            for check in checks:
                if check.result == "error":
                    typer.echo(f"{check.rule_id}: error: {check.error_message}")
                else:
                    typer.echo(f"{check.rule_id}: {check.result} ({check.break_count} breaks)")
            typer.echo(f"Run {outcome.run_id}: {outcome.status}")
        if outcome.workbook is not None:
            typer.echo(f"Workbook: {outcome.workbook}")
    finally:
        database.close()
    raise typer.Exit(outcome.exit_code)


@app.command("export")
def export(
    period: Annotated[str, typer.Option("--period", help="YYYY-MM, current or previous.")],
    domain: Annotated[str | None, typer.Option("--domain", help="Export this domain.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Export every domain.")] = False,
    run_id: Annotated[int | None, typer.Option("--run-id", help="Show this run (default: the latest).")] = None,
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
) -> None:
    """Write the review workbook for a domain (or all domains) and period."""
    if (domain is not None) == all_:
        typer.echo("Specify exactly one of --domain or --all.")
        raise typer.Exit(2)
    if period.strip().upper() == SNAPSHOT:
        typer.echo("--period must be YYYY-MM, current or previous (snapshot breaks are listed in every period)")
        raise typer.Exit(2)
    try:
        label = resolve_period(period, date.today()).label
    except ValueError as exc:
        typer.echo(f"invalid period {period!r}: expected YYYY-MM, current or previous")
        raise typer.Exit(2) from exc
    try:
        project = load_project(root)
        conn_str = connection_string(root, os.environ)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    if domain is not None and domain not in {r.domain for r in project.rules.values()}:
        typer.echo(f"unknown domain {domain!r}")
        raise typer.Exit(2)
    try:
        database = Database(conn_str)
    except pyodbc.Error as exc:
        typer.echo(f"Cannot connect to the database: {exc}")
        raise typer.Exit(2) from exc
    try:
        repo = SqlRepository(database)
        try:
            if run_id is not None:
                run_row = repo.get_run(run_id)
                if run_row is None or run_row.period_label != label:
                    typer.echo(f"run {run_id} not found for period {label}")
                    raise typer.Exit(2)
                if domain is not None and run_row.domain not in (None, domain):
                    typer.echo(f"run {run_id} covers domain {run_row.domain!r}, not {domain!r}")
                    raise typer.Exit(2)
            path = export_workbook(repo, domain=domain, period_label=label, run_id=run_id,
                                   out_path=export_path(project.settings.export_dir, domain, label, run_id))
        except typer.Exit:
            raise
        except Exception as exc:  # exit 1 would read as "breaks found"
            typer.echo(f"Export failed: {exc}")
            raise typer.Exit(2) from exc
    finally:
        database.close()
    typer.echo(str(path))


@app.command("import")
def import_(
    file: Annotated[Path, typer.Argument(help="Edited export workbook (.xlsx).")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and write the rejects file only.")] = False,
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/connection.yaml.")] = Path("."),
) -> None:
    """Validate an edited export workbook and apply its Status / Owner / comment changes."""
    if not file.is_file():
        typer.echo(f"file not found: {file}")
        raise typer.Exit(2)
    try:
        conn_str = connection_string(root, os.environ)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    try:
        database = Database(conn_str)
    except pyodbc.Error as exc:
        typer.echo(f"Cannot connect to the database: {exc}")
        raise typer.Exit(2) from exc
    try:
        try:
            result = import_workbook(SqlRepository(database), file, dry_run=dry_run)
        except (WorkbookError, VersionConflict) as exc:
            typer.echo(str(exc))
            raise typer.Exit(2) from exc
        except Exception as exc:  # exit 1 would read as "rows rejected"
            typer.echo(f"Import failed: {exc}")
            raise typer.Exit(2) from exc
    finally:
        database.close()
    typer.echo(f"{'dry run: ' if dry_run else ''}applied {result.applied}, rejected {result.rejected}")
    if result.rejects_path is not None:
        typer.echo(f"rejects: {result.rejects_path}")
    raise typer.Exit(1 if result.rejected else 0)


def _signoff_context(domain: str, period: str, root: Path) -> tuple[Project, str, Database]:
    """Shared validation for ``signoff`` / ``signoff revoke``: (project, period label, open database)."""
    if period.strip().upper() == SNAPSHOT:
        typer.echo("--period must be YYYY-MM, current or previous (sign-off covers a calendar period)")
        raise typer.Exit(2)
    try:
        label = resolve_period(period, date.today()).label
    except ValueError as exc:
        typer.echo(f"invalid period {period!r}: expected YYYY-MM, current or previous")
        raise typer.Exit(2) from exc
    try:
        project = load_project(root)
        conn_str = connection_string(root, os.environ)
    except ConfigError as exc:
        for issue in exc.issues:
            typer.echo(issue)
        raise typer.Exit(2) from exc
    if domain not in {r.domain for r in project.rules.values()}:
        typer.echo(f"unknown domain {domain!r}")
        raise typer.Exit(2)
    try:
        database = Database(conn_str)
    except pyodbc.Error as exc:
        typer.echo(f"Cannot connect to the database: {exc}")
        raise typer.Exit(2) from exc
    return project, label, database


@signoff_app.callback()
def signoff_command(
    ctx: typer.Context,
    domain: Annotated[str | None, typer.Option("--domain", help="Domain to sign off.")] = None,
    period: Annotated[str | None, typer.Option("--period", help="YYYY-MM, current or previous.")] = None,
    comment: Annotated[str | None, typer.Option("--comment", help="Sign-off comment.")] = None,
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
) -> None:
    """Sign off a domain and period: freeze an evidence workbook and record the approval."""
    if ctx.invoked_subcommand is not None:
        return
    missing = [name for name, value in (("--domain", domain), ("--period", period), ("--comment", comment))
               if value is None]
    if missing:
        typer.echo(f"Missing option(s): {', '.join(missing)}")
        raise typer.Exit(2)
    assert domain is not None and period is not None and comment is not None
    project, label, database = _signoff_context(domain, period, root)
    try:
        try:
            with ExitStack() as locks:  # same resources as `recon run`, so a run cannot interleave
                for resource in sorted({f"recon:{domain}:{label}", f"recon:{domain}:{SNAPSHOT}"}):
                    locks.enter_context(database.applock(resource))
                row = sign_off(project, SqlRepository(database), domain=domain, period_label=label,
                               comment=comment, now=datetime.now(UTC))
        except LockBusy as exc:
            typer.echo(f"run already in progress for {exc.resource}")
            raise typer.Exit(2) from exc
        except SignoffRefused as exc:
            for reason in exc.reasons:
                typer.echo(reason)
            raise typer.Exit(2) from exc
        except Exception as exc:
            typer.echo(f"Sign-off failed: {exc}")
            raise typer.Exit(2) from exc
    finally:
        database.close()
    typer.echo(f"Signed off {domain} {label} (sign-off {row.signoff_id}): {row.evidence_path}")


@signoff_app.command("revoke")
def signoff_revoke(
    domain: Annotated[str, typer.Option("--domain", help="Domain to revoke.")],
    period: Annotated[str, typer.Option("--period", help="YYYY-MM, current or previous.")],
    reason: Annotated[str, typer.Option("--reason", help="Why the sign-off is revoked.")],
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
) -> None:
    """Revoke the active sign-off of a domain and period."""
    project, label, database = _signoff_context(domain, period, root)
    try:
        try:
            revoke(project, SqlRepository(database), domain=domain, period_label=label, reason=reason)
        except SignoffRefused as exc:
            for text in exc.reasons:
                typer.echo(text)
            raise typer.Exit(2) from exc
        except Exception as exc:
            typer.echo(f"Revoke failed: {exc}")
            raise typer.Exit(2) from exc
    finally:
        database.close()
    typer.echo(f"Revoked {domain} {label}")
