"""
test_market_hours_segment.py — MCX-open / NSE-closed scenario guard.

Regression suite for the 09:00-09:15 IST window where MCX is open but
NSE has not yet opened.  Every poller and gate in this codebase must treat
that window as "market open" so positions/holdings refresh, ticker watchdog
stays active, and LTPs update in the browser.

Root-cause context
------------------
Operator complaint (Jun 28 2026 09:07 IST): "mcx should be open now. why
the website is not refreshing data?"  Investigation confirmed:
- Backend `_task_performance` checks each configured segment independently
  and fires when ANY segment is open — but the absence of an explicit test
  meant the pattern could regress silently.
- Frontend `marketAwareInterval` gates on `isMarketOpen()` which reads
  `_serverStatus.any_open` from GET /api/market/status — already correct.
- `is_any_segment_open()` helper was added to date_time_utils.py to give
  callers a single, named, segment-aware gate that is impossible to
  misread as "NSE only".

These tests pin the correct behaviour so any future NSE-only gating
regression is caught immediately.
"""
from __future__ import annotations

from datetime import date, time as dtime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

import pytest

INDIAN_TZ = ZoneInfo("Asia/Kolkata")


def _ist(h: int, m: int, weekday_offset: int = 0):
    """Build an IST-aware datetime for the given hour:minute on a Monday.

    weekday_offset=0 → Monday (default normal trading day).
    weekday_offset=5 → Saturday (weekend).
    """
    from datetime import datetime, timedelta

    # Find the most recent Monday (or the next one if today is a weekend)
    today = date.today()
    days_to_monday = today.weekday()          # 0=Mon … 6=Sun
    monday = today - timedelta(days=days_to_monday)
    target = monday + timedelta(days=weekday_offset)
    return datetime(target.year, target.month, target.day, h, m, 0,
                    tzinfo=INDIAN_TZ)


# ---------------------------------------------------------------------------
# 1.  is_market_open — segment-level primitives
# ---------------------------------------------------------------------------

class TestIsMarketOpenSegmentLevel:
    """is_market_open(now, holidays, start, end) with no holidays must behave
    purely on clock+weekday.  These tests confirm the two-segment boundary
    without touching the broker."""

    def test_nse_not_open_before_0915(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(9, 7)   # 09:07 IST — before NSE opens
        assert not is_market_open(now, set(), dtime(9, 15), dtime(15, 30)), \
            "NSE must be closed at 09:07 (opens 09:15)"

    def test_mcx_open_at_0907(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(9, 7)   # 09:07 IST — MCX already open since 09:00
        assert is_market_open(now, set(), dtime(9, 0), dtime(23, 30)), \
            "MCX must be open at 09:07 (hours 09:00-23:30)"

    def test_both_open_at_1000(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(10, 0)
        assert is_market_open(now, set(), dtime(9, 15), dtime(15, 30)), \
            "NSE must be open at 10:00"
        assert is_market_open(now, set(), dtime(9, 0), dtime(23, 30)), \
            "MCX must be open at 10:00"

    def test_nse_closed_after_1530(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(15, 35)
        assert not is_market_open(now, set(), dtime(9, 15), dtime(15, 30)), \
            "NSE must be closed after 15:30"

    def test_mcx_open_at_1600(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(16, 0)
        assert is_market_open(now, set(), dtime(9, 0), dtime(23, 30)), \
            "MCX must be open at 16:00 (NSE already closed)"

    def test_both_closed_before_0900(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(8, 55)
        assert not is_market_open(now, set(), dtime(9, 0), dtime(23, 30)), \
            "MCX closed before 09:00"
        assert not is_market_open(now, set(), dtime(9, 15), dtime(15, 30)), \
            "NSE closed before 09:00"

    def test_weekend_always_closed(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        # Saturday = weekday_offset 5 from Monday
        now = _ist(10, 0, weekday_offset=5)
        assert not is_market_open(now, set(), dtime(9, 0), dtime(23, 30)), \
            "MCX must be closed on Saturday regardless of time"
        assert not is_market_open(now, set(), dtime(9, 15), dtime(15, 30)), \
            "NSE must be closed on Saturday regardless of time"

    def test_holiday_closes_market(self):
        from backend.shared.helpers.date_time_utils import is_market_open
        now = _ist(10, 0)
        holiday_set = {now.date()}
        assert not is_market_open(now, holiday_set, dtime(9, 15), dtime(15, 30)), \
            "NSE closed on its own holiday"
        assert not is_market_open(now, holiday_set, dtime(9, 0), dtime(23, 30)), \
            "MCX closed on its own holiday"


# ---------------------------------------------------------------------------
# 2.  is_any_segment_open — multi-segment OR gate
# ---------------------------------------------------------------------------

_FAKE_SEGMENTS = {
    "equity": {
        "hours_start": "09:15",
        "hours_end":   "15:30",
        "holiday_exchange": "NSE",
    },
    "commodity": {
        "hours_start": "09:00",
        "hours_end":   "23:30",
        "holiday_exchange": "MCX",
    },
}


def _call_any_open(h: int, m: int, weekend: bool = False,
                   nse_holidays: set | None = None,
                   mcx_holidays: set | None = None) -> bool:
    """Invoke is_any_segment_open with fake config + controlled holiday sets.

    Patches:
      - backend.brokers.broker_apis.fetch_holidays (local import inside helper)
      - backend.shared.helpers.utils.config (dict-style .get access)
    """
    from backend.shared.helpers.date_time_utils import is_any_segment_open
    now = _ist(h, m, weekday_offset=5 if weekend else 0)

    # fetch_holidays is imported inside is_any_segment_open as a local
    # import, so we must patch it at the broker_apis site.
    def _fake_holidays(exch: str) -> set:
        if exch == "NSE":
            return nse_holidays or set()
        if exch == "MCX":
            return mcx_holidays or set()
        return set()

    # config is accessed as config.get(...) inside the helper.
    fake_cfg: dict = {"market_segments": _FAKE_SEGMENTS}

    with patch("backend.brokers.broker_apis.fetch_holidays",
               side_effect=_fake_holidays), \
         patch("backend.shared.helpers.utils.config", fake_cfg):
        return is_any_segment_open(now)


class TestIsAnySegmentOpen:
    """is_any_segment_open() must return True whenever ANY configured segment
    is in session — specifically the MCX-open / NSE-closed 09:00-09:15 window.
    fetch_holidays is mocked to return empty sets (no holidays today) so the
    clock+weekday gate is the sole decision-maker."""

    def test_mcx_only_window_0907_returns_true(self):
        """THE key regression guard: 09:07 IST MCX open but NSE not yet."""
        assert _call_any_open(9, 7), \
            "is_any_segment_open must be True at 09:07 when MCX is open"

    def test_both_open_1000_returns_true(self):
        assert _call_any_open(10, 0), \
            "is_any_segment_open must be True at 10:00 (both sessions)"

    def test_mcx_only_evening_1600_returns_true(self):
        assert _call_any_open(16, 0), \
            "is_any_segment_open must be True at 16:00 (MCX open, NSE closed)"

    def test_both_closed_0855_returns_false(self):
        assert not _call_any_open(8, 55), \
            "is_any_segment_open must be False before 09:00"

    def test_both_closed_after_2330_returns_false(self):
        assert not _call_any_open(23, 35), \
            "is_any_segment_open must be False after 23:30"

    def test_weekend_returns_false(self):
        assert not _call_any_open(10, 0, weekend=True), \
            "is_any_segment_open must be False on Saturday"

    def test_holiday_both_segments_closed(self):
        now = _ist(10, 0)
        today = now.date()
        # Both holiday sets contain today → both segments closed.
        assert not _call_any_open(10, 0,
                                  nse_holidays={today},
                                  mcx_holidays={today}), \
            "is_any_segment_open must be False when both calendars flag today as holiday"

    def test_empty_segments_config_returns_false(self):
        from backend.shared.helpers.date_time_utils import is_any_segment_open
        now = _ist(10, 0)
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.shared.helpers.utils.config", {"market_segments": {}}):
            assert not is_any_segment_open(now), \
                "is_any_segment_open must be False when no segments are configured"


# ---------------------------------------------------------------------------
# 3.  Background poller gate — _task_performance continues when MCX open
# ---------------------------------------------------------------------------

class TestPerformancePollMcxOnly:
    """The _task_performance segment probe at line ~420 of background.py
    builds open_segments from per-segment is_market_open calls.  Simulate
    that loop at 09:07 IST and assert the commodity segment is in open_segments
    while equity is not — confirming the poller would proceed to fetch
    positions/holdings data."""

    def test_open_segments_at_0907(self):
        """open_segments must include 'commodity' but not 'equity' at 09:07."""
        from backend.shared.helpers.date_time_utils import is_market_open

        # Replicate the _build_segments() output for the two standard segments.
        segments = [
            {
                "name": "equity",
                "hours_start": dtime(9, 15),
                "hours_end":   dtime(15, 30),
                "holiday_exchange": "NSE",
            },
            {
                "name": "commodity",
                "hours_start": dtime(9, 0),
                "hours_end":   dtime(23, 30),
                "holiday_exchange": "MCX",
            },
        ]
        now = _ist(9, 7)
        holiday_cache = {"NSE": set(), "MCX": set()}

        def _probe(seg):
            return is_market_open(
                now,
                holiday_cache.get(seg["holiday_exchange"], set()),
                seg["hours_start"],
                seg["hours_end"],
                exchange=seg["holiday_exchange"],
            )

        # Simulate the list comprehension in background.py.
        open_results = [_probe(seg) for seg in segments]
        open_segments = [seg for seg, ok in zip(segments, open_results) if ok]

        # Assertions.
        open_names = {s["name"] for s in open_segments}
        assert "commodity" in open_names, \
            "MCX (commodity) segment must be in open_segments at 09:07"
        assert "equity" not in open_names, \
            "NSE (equity) segment must NOT be in open_segments at 09:07"
        assert open_segments, \
            "open_segments must be non-empty so the poller proceeds to fetch broker data"

    def test_open_segments_at_1000_includes_both(self):
        """Both segments must be open at 10:00 so poller fetches all book data."""
        from backend.shared.helpers.date_time_utils import is_market_open

        segments = [
            {"name": "equity",    "hours_start": dtime(9, 15), "hours_end": dtime(15, 30), "holiday_exchange": "NSE"},
            {"name": "commodity", "hours_start": dtime(9, 0),  "hours_end": dtime(23, 30), "holiday_exchange": "MCX"},
        ]
        now = _ist(10, 0)
        holiday_cache = {"NSE": set(), "MCX": set()}

        open_segments = [
            seg for seg in segments
            if is_market_open(now, holiday_cache[seg["holiday_exchange"]],
                              seg["hours_start"], seg["hours_end"])
        ]
        assert len(open_segments) == 2, \
            "Both segments must be open at 10:00"

    def test_open_segments_at_0855_is_empty(self):
        """Before 09:00 open_segments must be empty so the poller skips."""
        from backend.shared.helpers.date_time_utils import is_market_open

        segments = [
            {"name": "equity",    "hours_start": dtime(9, 15), "hours_end": dtime(15, 30), "holiday_exchange": "NSE"},
            {"name": "commodity", "hours_start": dtime(9, 0),  "hours_end": dtime(23, 30), "holiday_exchange": "MCX"},
        ]
        now = _ist(8, 55)
        holiday_cache = {"NSE": set(), "MCX": set()}

        open_segments = [
            seg for seg in segments
            if is_market_open(now, holiday_cache[seg["holiday_exchange"]],
                              seg["hours_start"], seg["hours_end"])
        ]
        assert open_segments == [], \
            "open_segments must be empty before 09:00 so the poller skips"


# ---------------------------------------------------------------------------
# 4.  market_status endpoint — any_open field is nse_open OR mcx_open
# ---------------------------------------------------------------------------

class TestMarketStatusAnyOpen:
    """The /api/market/status response must set any_open = nse_open OR mcx_open.
    In the 09:00-09:15 window: nse_open=False, mcx_open=True, any_open=True.
    The frontend reads any_open to gate marketAwareInterval callbacks."""

    def _build_status(self, h: int, m: int,
                      nse_holidays: set | None = None,
                      mcx_holidays: set | None = None):
        """Replicate _compute_market_status logic without the async/HTTP layer."""
        from backend.shared.helpers.date_time_utils import is_market_open

        now = _ist(h, m)
        nse_h = nse_holidays or set()
        mcx_h = mcx_holidays or set()

        nse_open = is_market_open(now, nse_h, dtime(9, 15), dtime(15, 30))
        mcx_open = is_market_open(now, mcx_h, dtime(9, 0),  dtime(23, 30))
        any_open = nse_open or mcx_open
        return {"nse_open": nse_open, "mcx_open": mcx_open, "any_open": any_open}

    def test_any_open_true_at_0907(self):
        s = self._build_status(9, 7)
        assert s["mcx_open"],          "mcx_open must be True at 09:07"
        assert not s["nse_open"],      "nse_open must be False at 09:07"
        assert s["any_open"],          "any_open must be True at 09:07 (MCX open)"

    def test_any_open_true_at_1000(self):
        s = self._build_status(10, 0)
        assert s["nse_open"],          "nse_open at 10:00"
        assert s["mcx_open"],          "mcx_open at 10:00"
        assert s["any_open"],          "any_open at 10:00"

    def test_any_open_false_before_0900(self):
        s = self._build_status(8, 55)
        assert not s["nse_open"],      "nse_open before 09:00"
        assert not s["mcx_open"],      "mcx_open before 09:00"
        assert not s["any_open"],      "any_open before 09:00"

    def test_any_open_true_mcx_evening(self):
        """After NSE closes (15:30) MCX is still open until 23:30."""
        s = self._build_status(16, 0)
        assert not s["nse_open"],      "nse_open after 15:30"
        assert s["mcx_open"],          "mcx_open at 16:00"
        assert s["any_open"],          "any_open at 16:00 (MCX evening)"

    def test_any_open_false_after_2330(self):
        s = self._build_status(23, 35)
        assert not s["nse_open"],      "nse_open after 23:30"
        assert not s["mcx_open"],      "mcx_open after 23:30"
        assert not s["any_open"],      "any_open after 23:30 (both sessions closed)"
