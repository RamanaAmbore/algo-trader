"""Tests for grammar_tokens/metric_is_past_holiday_refresh.py —
`is_past_holiday_refresh` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestIsPastHolidayRefresh) — Wave 1, batch 4 of the DSL token-
unification plan.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.api.algo.grammar_tokens.metric_is_past_holiday_refresh import (
    _metric_is_past_holiday_refresh,
)

IST = ZoneInfo("Asia/Kolkata")

# A plain weekday — not a holiday on NSE or MCX — isolates these tests
# from the evening-holiday branch entirely (same fixture date
# test_market_cycle_tokens.py uses).
_PLAIN_WEEKDAY = date(2026, 11, 5)  # Thursday


def _at(hh: int, mm: int) -> datetime:
    return datetime(_PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month, _PLAIN_WEEKDAY.day,
                     hh, mm, tzinfo=IST)


def _ctx(now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags={}, now=now)


def test_before_boundary_is_false():
    ctx = _ctx(now=_at(5, 29))
    assert _metric_is_past_holiday_refresh(ctx, {}) == 0.0


def test_after_boundary_is_true():
    ctx = _ctx(now=_at(5, 31))
    assert _metric_is_past_holiday_refresh(ctx, {}) == 1.0


def test_at_exact_boundary_is_true():
    ctx = _ctx(now=_at(5, 30))
    assert _metric_is_past_holiday_refresh(ctx, {}) == 1.0


def test_reads_configured_time_not_a_hardcoded_0530():
    """`holiday_refresh_time` is operator-tunable — the resolver must
    read it live, not hardcode 05:30."""
    from backend.shared.helpers.utils import config as _cfg
    with patch.dict(_cfg, {"holiday_refresh_time": "06:00"}):
        ctx = _ctx(now=_at(5, 45))
        assert _metric_is_past_holiday_refresh(ctx, {}) == 0.0
        ctx2 = _ctx(now=_at(6, 1))
        assert _metric_is_past_holiday_refresh(ctx2, {}) == 1.0


def test_missing_now_is_none():
    assert _metric_is_past_holiday_refresh(_ctx(now=None), {}) is None
