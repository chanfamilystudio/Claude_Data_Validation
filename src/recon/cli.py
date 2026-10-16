"""Command-line entry point. Later tasks add commands to ``app``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import pyodbc
import typer

from recon.config.loader import ConfigError, load_project
from recon.db import Database, LockBusy, connection_string
from recon.migrations import apply_migrations

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
