"""Shared helpers for SQL generator tests: golden-file comparison and a GenContext builder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from recon.config.loader import Project, load_project
from recon.connectors.sqlserver import SqlServerConnector
from recon.sqlgen.common import GenContext

GOLDEN_DIR = Path(__file__).parent / "golden"


def ctx_for(project: Project) -> GenContext:
    """GenContext for a loaded project, with sources rendered by the SQL Server connector."""
    connector = SqlServerConnector(None)  # as_sql_source never touches the database
    return GenContext(
        datasets=project.datasets,
        sources={name: connector.as_sql_source(ds) for name, ds in project.datasets.items()},
        mappings=project.mappings,
    )


@pytest.fixture
def fixture_project(fixture_root) -> Project:
    return load_project(fixture_root)


@pytest.fixture
def golden():
    """``golden(name, text)``: compare ``text`` with ``golden/<name>.sql`` (UPDATE_GOLDEN=1 rewrites)."""

    def check(name: str, text: str) -> None:
        path = GOLDEN_DIR / f"{name}.sql"
        if os.environ.get("UPDATE_GOLDEN") == "1":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            return
        if not path.is_file():
            pytest.fail(
                f"golden file {path} is missing; run with UPDATE_GOLDEN=1 to create it, then review it",
                pytrace=False,
            )
        expected = path.read_text(encoding="utf-8")
        assert text == expected, (
            f"generated SQL differs from golden file {path}; if the change is intended, "
            "rerun with UPDATE_GOLDEN=1 and review the diff"
        )

    return check
