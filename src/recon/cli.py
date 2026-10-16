"""Command-line entry point. Later tasks add commands to ``app``."""

from __future__ import annotations

import os
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated

import pyodbc
import typer

from recon.config.loader import ConfigError, load_project
from recon.db import Database, LockBusy, connection_string
from recon.executor import execute_run
from recon.logging_setup import configure_logging
from recon.migrations import apply_migrations
from recon.periods import resolve_period
from recon.repository import SqlRepository

app = typer.Typer(no_args_is_help=True, help="Workday to Great Plains reconciliation engine.")
rules_app = typer.Typer(no_args_is_help=True, help="Work with reconciliation rules.")
app.add_typer(rules_app, name="rules")
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")


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


class Trigger(str, Enum):
    manual = "manual"
    scheduled = "scheduled"


@app.command("run")
def run(
    period: Annotated[str, typer.Option("--period", help="YYYY-MM, current, previous or SNAPSHOT.")],
    domain: Annotated[
        list[str] | None, typer.Option("--domain", help="Run the rules of this domain (repeatable).")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every active rule.")] = False,
    rule: Annotated[str | None, typer.Option("--rule", help="Run a single rule by id.")] = None,
    trigger: Annotated[Trigger, typer.Option("--trigger", help="Who started the run.")] = Trigger.manual,
    root: Annotated[Path, typer.Option("--root", help="Project root containing config/ and rules/.")] = Path("."),
    no_export: Annotated[bool, typer.Option("--no-export", help="Do not write the run workbook.")] = False,
) -> None:
    """Run reconciliation rules for a period."""
    chosen = sum([bool(domain), all_, rule is not None])
    if chosen != 1:
        typer.echo("Specify exactly one of --domain (repeatable), --all or --rule.")
        raise typer.Exit(2)
    today = date.today()
    try:
        run_period = resolve_period(period, today)
    except ValueError as exc:
        typer.echo(str(exc))
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
        # The run workbook export is wired in by Task 11; until then no workbook is written and
        # --no-export is accepted (and has nothing to switch off).
        try:
            outcome = execute_run(
                project, database, repo, domains=list(domain) if domain else None, rule_id=rule,
                period=run_period, trigger=trigger.value, export=None, today=today,
            )
        except Exception as exc:  # exit 1 would read as "breaks found"
            typer.echo(f"Run failed: {exc}")
            raise typer.Exit(2) from exc
        for message in outcome.messages:
            typer.echo(message)
        if outcome.run_id is not None:
            for check in repo.checks_for_run(outcome.run_id):
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
