"""Tests for grammar_tokens/metric_is_past_window_open.py —
`is_past_window_open` metric.

Moved verbatim from test_market_cycle_tokens.py's Layer 2
(TestIsPastWindowOpen) — Wave 1, batch 4 of the DSL token-
unification plan.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.api.algo.grammar_tokens.metric_is_past_window_open import (
    _metric_is_past_window_open,
)

IST = ZoneInfo("Asia/Kolkata")

# A plain weekday — not a holiday on NSE or MCX (same fixture date
# test_market_cycle_tokens.py uses).
_PLAIN_WEEKDAY = date(2026, 11, 5)  # Thursday


def _at(hh: int, mm: int) -> datetime:
    return datetime(_PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month, _PLAIN_WEEKDAY.day,
                     hh, mm, tzinfo=IST)


def _ctx(now=None):
    from backend.api.algo.agent_evaluator import Context
    return Context(segment_flags={}, now=now)


def test_before_0800_is_false():
    ctx = _ctx(now=_at(7, 59))
    assert _metric_is_past_window_open(ctx, {}) == 0.0


def test_at_and_after_0800_is_true():
    ctx = _ctx(now=_at(8, 0))
    assert _metric_is_past_window_open(ctx, {}) == 1.0


def test_late_evening_same_session_is_still_true():
    """23:50 IST is still TODAY's session (the closed overnight
    window doesn't start until 23:31) — must read True, proving the
    resolver uses the session-date convention rather than a bare
    `now.time() >= 08:00` check that would coincidentally also pass
    here but for the wrong reason."""
    ctx = _ctx(now=_at(23, 50))
    assert _metric_is_past_window_open(ctx, {}) == 1.0


def test_after_midnight_before_next_0800_is_false():
    """00:30 IST the NEXT calendar day — still inside the closed
    overnight window, must read False. A bare `date()` compare
    against "today" would wrongly flip this True at midnight."""
    next_day = datetime(_PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month,
                         _PLAIN_WEEKDAY.day + 1, 0, 30, tzinfo=IST)
    ctx = _ctx(now=next_day)
    assert _metric_is_past_window_open(ctx, {}) == 0.0


def test_missing_now_is_none():
    assert _metric_is_past_window_open(_ctx(now=None), {}) is None
