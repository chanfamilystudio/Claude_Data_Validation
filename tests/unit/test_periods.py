from datetime import date

import pytest

from recon.periods import SNAPSHOT, RunPeriod, resolve_period


def test_month_token():
    p = resolve_period("2026-09", today=date(2026, 10, 16))
    assert (p.label, p.start, p.end, p.end_excl) == (
        "2026-09",
        date(2026, 9, 1),
        date(2026, 9, 30),
        date(2026, 10, 1),
    )


def test_current_and_previous_cross_year():
    assert resolve_period("current", today=date(2026, 1, 5)).label == "2026-01"
    assert resolve_period("previous", today=date(2026, 1, 5)).label == "2025-12"


def test_december_end_excl_crosses_year():
    p = resolve_period("2025-12", today=date(2026, 1, 5))
    assert (p.end, p.end_excl) == (date(2025, 12, 31), date(2026, 1, 1))


def test_leap_february():
    assert resolve_period("2024-02", today=date(2026, 1, 5)).end == date(2024, 2, 29)


def test_snapshot_token():
    p = resolve_period("SNAPSHOT", today=date(2026, 10, 16))
    assert p.label == SNAPSHOT == "SNAPSHOT"


def test_run_period_is_frozen():
    p = resolve_period("2026-09", today=date(2026, 10, 16))
    assert isinstance(p, RunPeriod)
    with pytest.raises(AttributeError):
        p.label = "x"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["2026-13", "2026-9", "sept", "", "2026-00", "0000-01"])
def test_invalid_tokens(bad):
    with pytest.raises(ValueError):
        resolve_period(bad, today=date(2026, 10, 16))
