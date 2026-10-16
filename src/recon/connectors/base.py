"""Connector protocol: how a dataset is described and exposed to generated SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from recon.config.models import DatasetConfig


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str


class Connector(Protocol):
    name: str

    def describe(self, dataset: DatasetConfig) -> list[Column]:
        """Columns of the dataset's object, in ordinal order."""
        ...

    def as_sql_source(self, dataset: DatasetConfig) -> str:
        """Parenthesised derived table (no alias) the generator can select from.

        The text may reference ``@period_start`` / ``@period_end_excl``, which every generated
        batch declares from bound parameters.
        """
        ...
