"""Connector for datasets that already live in SQL Server staging tables or views."""

from __future__ import annotations

from recon.config.loader import ConfigError
from recon.config.models import DatasetConfig
from recon.connectors.base import Column
from recon.db import Database

DEFAULT_SCHEMA = "dbo"


def _quote(name: str) -> str:
    """Bracket-quote a one- or two-part identifier (``]`` escaped as ``]]``)."""
    return ".".join("[" + part.replace("]", "]]") + "]" for part in name.split("."))


def _split(name: str) -> tuple[str, str]:
    parts = name.split(".")
    if len(parts) == 1:
        return DEFAULT_SCHEMA, parts[0]
    return parts[0], parts[1]


class SqlServerConnector:
    name = "sqlserver"

    def __init__(self, db: Database):
        self.db = db

    def describe(self, dataset: DatasetConfig) -> list[Column]:
        schema, table = _split(dataset.object)
        rows = self.db.query(
            "SELECT COLUMN_NAME AS name, DATA_TYPE AS data_type"
            " FROM INFORMATION_SCHEMA.COLUMNS"
            " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
            " ORDER BY ORDINAL_POSITION",
            schema,
            table,
        )
        if not rows:
            raise ConfigError([f"dataset '{dataset.name}': object '{dataset.object}' not found"])
        return [Column(r["name"], r["data_type"]) for r in rows]

    def as_sql_source(self, dataset: DatasetConfig) -> str:
        predicates: list[str] = []
        if dataset.filter:
            predicates.append(f"({dataset.filter})")
        if dataset.period_column:
            col = _quote(dataset.period_column)
            predicates.append(f"{col} >= @period_start AND {col} < @period_end_excl")
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        return f"(SELECT * FROM {_quote(dataset.object)}{where})"
