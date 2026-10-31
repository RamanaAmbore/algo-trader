"""
test_chart_1d_session_trim.py

Covers the "1D" range weekday double-session bug fixed alongside the shipped
5minute-for-1D fix (2026-10-31).

Problem: ``_historical_intraday_store`` computes ``from_d = to_d -
timedelta(days=days)``, so ``days=1`` sweeps [today-1, today] INCLUSIVE —
two calendar dates, not one. On a weekday mid-session this silently merged
yesterday's full session with today's partial one into a single "1D"
response. The bug was invisible in a same-day (Saturday) manual spot-check
because "today" had zero bars, so only one non-empty date (Friday) survived
by coincidence — not because the window was actually one day wide.

Fix: ``_trim_to_latest_session_day()`` keeps only the bars whose own ``ts``
date is the LATEST calendar date present, applied only when ``days == 1``.

Five quality dimensions (feedback_test_dimensions.md):
  1. SSOT        — the trim helper is the one place date-filtering for "1D"
                   happens; _historical_intraday_store calls it, nothing
                   else re-implements the logic.
  2. Performance — pure in-memory list filter, no I/O; not a hot-path
                   concern worth a dedicated perf test.
  3. Stale code  — source-grep confirms _historical_intraday_store calls
                   _trim_to_latest_session_day only when days == 1.
  4. Reusable    — _trim_to_latest_session_day is a pure function, tested
                   directly and independently of the store/broker plumbing.
  5. Correctness — weekday double-session case (yesterday+today merged)
                   trims to today only; weekend/holiday fallback (only one
                   non-empty date) is preserved unchanged — the "never
                   blank during closed hours" invariant still holds.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.routes import options_helpers
from backend.api.routes.options_helpers import (
    _historical_intraday_store,
    _trim_to_latest_session_day,
)


@dataclass
class _Bar:
    ts: str
    open: float = 100.0
    high: float = 101.0
    low: float = 99.0
    close: float = 100.5
    volume: int = 1000


# ── Dimension 4: Reusable — pure-function tests ─────────────────────────────

class TestTrimToLatestSessionDay:
    def test_empty_input_returns_empty(self):
        assert _trim_to_latest_session_day([]) == []

    def test_single_date_unchanged(self):
        bars = [_Bar(ts=f"2026-10-30T0{i}:45:00+00:00") for i in range(3)]
        out = _trim_to_latest_session_day(bars)
        assert out == bars

    def test_two_dates_keeps_latest_only(self):
        """The exact weekday repro: yesterday (full) + today (partial)."""
        yesterday = [_Bar(ts=f"2026-10-29T0{i}:45:00+00:00") for i in range(6)]
        today     = [_Bar(ts=f"2026-10-30T0{i}:45:00+00:00") for i in range(3)]
        merged = yesterday + today
        out = _trim_to_latest_session_day(merged)
        assert len(out) == 3
        assert all(b.ts.startswith("2026-10-30") for b in out)

    def test_weekend_fallback_single_date_preserved(self):
        """Saturday sweep: Sat(empty, already excluded upstream) + Fri(full)
        — only Friday's bars ever reach this function, so trimming to the
        'latest' date is a no-op that still returns Friday's bars (the
        'never blank during closed hours' invariant)."""
        friday = [_Bar(ts=f"2026-10-30T0{i}:45:00+00:00") for i in range(6)]
        out = _trim_to_latest_session_day(friday)
        assert out == friday
        assert len(out) == 6

    def test_unparseable_ts_does_not_crash(self):
        bars = [_Bar(ts=""), _Bar(ts="2026-10-30T03:45:00+00:00")]
        out = _trim_to_latest_session_day(bars)
        assert len(out) == 1
        assert out[0].ts == "2026-10-30T03:45:00+00:00"


# ── Dimension 1/3: SSOT + stale-code — wired in only for days == 1 ─────────

def test_historical_intraday_store_calls_trim_only_for_days_1():
    source = inspect.getsource(options_helpers._historical_intraday_store)
    assert "_trim_to_latest_session_day(merged)" in source
    assert "if days == 1:" in source


@pytest.mark.asyncio
async def test_days_1_request_trims_two_day_sweep_to_one():
    """End-to-end: get_or_fetch_intraday returns two real calendar dates
    (reproducing the weekday from_d=yesterday..to_d=today sweep); the store
    helper must return only today's bars, not both days merged."""
    yesterday_bars = [
        {"bar_ts": f"2026-10-29T0{i}:45:00+00:00", "open": 100, "high": 101,
         "low": 99, "close": 100.5, "volume": 1000}
        for i in range(6)
    ]
    today_bars = [
        {"bar_ts": f"2026-10-30T0{i}:45:00+00:00", "open": 100, "high": 101,
         "low": 99, "close": 100.5, "volume": 1000}
        for i in range(3)
    ]

    call_log = []

    async def _fake_fetch(sym, exch, day, interval, bypass_cache=False):
        call_log.append(day)
        # from_d (yesterday) returns a full day; to_d (today) returns partial
        if str(day) == "2026-10-29":
            return yesterday_bars
        if str(day) == "2026-10-30":
            return today_bars
        return []

    import datetime as _dt

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2026, 10, 30)

    with patch(
        "backend.api.persistence.intraday_store.get_or_fetch_intraday",
        new=AsyncMock(side_effect=_fake_fetch),
    ), patch("datetime.date", _FixedDate):
        hist_cache_put = lambda *a, **k: None
        self_heal_log_once = lambda *a, **k: None

        result = await _historical_intraday_store(
            "RELIANCE", "NSE", 1, "5minute",
            hist_cache_put, self_heal_log_once, 60,
            _Bar, lambda **kw: type("R", (), kw)(),
        )

    assert result is not None
    bars = result.bars
    # Must contain ONLY today's bars (3), never the merged 9.
    assert len(bars) == 3, (
        f"Expected only today's 3 bars after trim, got {len(bars)} — "
        "the weekday double-session bug is back if this merges both dates."
    )
    assert all(b.ts.startswith("2026-10-30") for b in bars)
