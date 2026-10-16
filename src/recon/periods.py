"""Reconciliation run periods (calendar months or the literal SNAPSHOT)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

SNAPSHOT = "SNAPSHOT"

_MONTH_RE = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")


@dataclass(frozen=True)
class RunPeriod:
    label: str
    start: date
    end: date  # inclusive

    @property
    def end_excl(self) -> date:
        return self.end + timedelta(days=1)


def _month_period(year: int, month: int) -> RunPeriod:
    start = date(year, month, 1)
    next_start = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return RunPeriod(label=f"{year:04d}-{month:02d}", start=start, end=next_start - timedelta(days=1))


def resolve_period(token: str, today: date) -> RunPeriod:
    """Resolve ``YYYY-MM``, ``current``, ``previous`` or ``SNAPSHOT`` to a RunPeriod."""
    if token == "current":
        return _month_period(today.year, today.month)
    if token == "previous":
        if today.month == 1:
            return _month_period(today.year - 1, 12)
        return _month_period(today.year, today.month - 1)
    if token == SNAPSHOT:
        # A snapshot has no calendar window; it is evaluated as of today.
        return RunPeriod(label=SNAPSHOT, start=today, end=today)
    m = _MONTH_RE.fullmatch(token)
    if not m:
        raise ValueError(f"invalid period {token!r}: expected YYYY-MM, 'current', 'previous' or 'SNAPSHOT'")
    try:
        return _month_period(int(m.group(1)), int(m.group(2)))
    except ValueError:
        raise ValueError(f"invalid period {token!r}: year out of range") from None
