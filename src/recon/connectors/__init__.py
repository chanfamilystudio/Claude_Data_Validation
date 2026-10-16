"""Connector registry. ``sources.<name>.connector`` in config selects an entry by name."""

from __future__ import annotations

from recon.config.loader import ConfigError
from recon.connectors.base import Column, Connector
from recon.connectors.sqlserver import SqlServerConnector
from recon.db import Database

CONNECTORS: dict[str, type[SqlServerConnector]] = {"sqlserver": SqlServerConnector}


def get_connector(name: str, db: Database) -> Connector:
    try:
        cls = CONNECTORS[name]
    except KeyError:
        raise ConfigError([f"unknown connector '{name}'"]) from None
    return cls(db)


__all__ = ["CONNECTORS", "Column", "Connector", "SqlServerConnector", "get_connector"]
