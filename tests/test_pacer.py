"""Pacer cadence policy — quiet hours + per-run cap (offline)."""

from __future__ import annotations

from datetime import datetime

from daily_agent.feed.pacer import Pacer


def _at(hour: int) -> datetime:
    return datetime(2026, 6, 7, hour, 30)


def test_wrapping_quiet_hours_22_to_8():
    p = Pacer(max_per_run=3, quiet_start=22, quiet_end=8)
    assert p.in_quiet_hours(_at(23))   # late night
    assert p.in_quiet_hours(_at(2))    # small hours
    assert p.in_quiet_hours(_at(22))   # start is inclusive
    assert not p.in_quiet_hours(_at(8))   # end is exclusive
    assert not p.in_quiet_hours(_at(12))  # midday
    assert not p.in_quiet_hours(_at(21))  # just before quiet


def test_non_wrapping_quiet_hours():
    p = Pacer(max_per_run=3, quiet_start=1, quiet_end=6)
    assert p.in_quiet_hours(_at(3))
    assert not p.in_quiet_hours(_at(7))
    assert not p.in_quiet_hours(_at(0))


def test_disabled_quiet_hours_when_equal():
    p = Pacer(max_per_run=5, quiet_start=0, quiet_end=0)
    assert not p.in_quiet_hours(_at(3))
    assert p.allowance(_at(3)) == 5


def test_allowance_is_zero_in_quiet_hours_else_cap():
    p = Pacer(max_per_run=3, quiet_start=22, quiet_end=8)
    assert p.allowance(_at(2)) == 0     # quiet
    assert p.allowance(_at(10)) == 3    # active → cap
