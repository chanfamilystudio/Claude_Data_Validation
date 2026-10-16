"""Versioned DDL migrations for the ``recon`` schema."""

from __future__ import annotations

import logging
import re
from importlib import resources
from pathlib import Path

from recon.db import Database

log = logging.getLogger(__name__)

_FILE_RE = re.compile(r"^V(\d+)__.+\.sql$")
_GO_RE = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)
MIGRATE_LOCK = "recon:migrate"

_BOOTSTRAP = """
IF SCHEMA_ID('recon') IS NULL EXEC('CREATE SCHEMA recon');
GO
IF OBJECT_ID('recon.schema_version', 'U') IS NULL
    CREATE TABLE recon.schema_version (
        version    VARCHAR(20) NOT NULL CONSTRAINT PK_schema_version PRIMARY KEY,
        applied_at DATETIME2(3) NOT NULL CONSTRAINT DF_schema_version_applied DEFAULT SYSUTCDATETIME()
    );
"""


def split_batches(sql: str) -> list[str]:
    """Split a script on lines consisting only of ``GO``; blank batches are dropped."""
    return [b.strip() for b in _GO_RE.split(sql) if b.strip()]


def list_migrations(migrations_dir: Path | None):
    """``[(version, text), ...]`` ordered numerically; ``None`` means the packaged directory."""
    source = resources.files("recon.sql") if migrations_dir is None else Path(migrations_dir)
    found = []
    for entry in source.iterdir():
        m = _FILE_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), f"V{m.group(1)}", entry))
    found.sort(key=lambda t: (t[0], t[2].name))
    return [(version, entry.read_text(encoding="utf-8")) for _, version, entry in found]


def apply_migrations(db: Database, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending ``V###__*.sql`` files in order; return the versions applied this call.

    Each file runs in one transaction together with its ``schema_version`` row. A named
    application lock prevents two migrators racing.
    """
    migrations = list_migrations(migrations_dir)
    with db.applock(MIGRATE_LOCK):
        with db.transaction():
            for batch in split_batches(_BOOTSTRAP):
                db.execute(batch)
        done = {r["version"] for r in db.query("SELECT version FROM recon.schema_version")}
        applied: list[str] = []
        for version, text in migrations:
            if version in done:
                continue
            with db.transaction():
                for batch in split_batches(text):
                    db.execute(batch)
                db.execute("INSERT INTO recon.schema_version (version) VALUES (?)", version)
            log.info("applied migration %s", version)
            applied.append(version)
        return applied
