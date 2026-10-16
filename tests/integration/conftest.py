"""Integration harness: a throwaway ``recon_test_<hex>`` database per session."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pyodbc
import pytest

from recon.db import Database
from recon.migrations import apply_migrations, split_batches

SERVER_DSN_ENV = "RECON_TEST_DSN"


def run_sql_file(db: Database, path: Path) -> None:
    """Execute a .sql file on ``db``, one batch per ``GO`` separator, in a single transaction."""
    text = Path(path).read_text(encoding="utf-8")
    with db.transaction():
        for batch in split_batches(text):
            db.execute(batch)


@pytest.fixture(scope="session")
def server_dsn() -> str:
    value = os.environ.get(SERVER_DSN_ENV, "").strip()
    if not value:
        pytest.skip(f"{SERVER_DSN_ENV} not set; integration tests need a SQL Server")
    return value


@pytest.fixture(scope="session")
def dsn(server_dsn):
    name = f"recon_test_{uuid.uuid4().hex[:8]}"
    master = pyodbc.connect(server_dsn, autocommit=True)
    try:
        master.execute(f"CREATE DATABASE [{name}]")
        yield f"{server_dsn.rstrip(';')};Database={name}"
    finally:
        try:
            master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{name}]")
        finally:
            master.close()


@pytest.fixture(scope="session")
def db(dsn):
    database = Database(dsn)
    apply_migrations(database)
    try:
        yield database
    finally:
        database.close()


# FK-safe order: children before parents. schema_version is deliberately kept.
_CLEAN_ORDER = (
    "audit_log", "break_comment", "break_occurrence", "total_result", "run_check",
    "signoff", "[break]", "import_batch", "run", "[rule]",
)


@pytest.fixture(autouse=True)
def clean_recon_tables(request):
    """Leave the shared session database with empty recon tables after every test."""
    database = request.getfixturevalue("db")
    yield
    with database.transaction():
        for table in _CLEAN_ORDER:
            database.execute(f"DELETE FROM recon.{table}")
