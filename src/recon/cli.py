"""Command-line entry point. Later tasks add commands to ``app``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from recon.config.loader import ConfigError, load_project

app = typer.Typer(no_args_is_help=True, help="Workday to Great Plains reconciliation engine.")
rules_app = typer.Typer(no_args_is_help=True, help="Work with reconciliation rules.")
app.add_typer(rules_app, name="rules")


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
