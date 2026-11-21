"""
test_ticker_window_mcx_dst_aware.py — four residual MCX-DST-season
hardcodes found inside `backend/api/background.py` after the base
`mcx_non_agri_close_time()` fix shipped (see CLAUDE.md "MCX DST-aware
non-agri close time"):

  1. `get_ticker_calendar_status()` — the `/api/admin/broker-health`
     idle-badge clock-window check hardcoded the upper bound at 23:30
     IST year-round. During the winter (US-standard-time) window MCX's
     real close is 23:55 IST, so the badge read "idle" for up to ~25
     minutes while MCX genuinely still traded. DISPLAY-ONLY.

  2. `_ds_tick_ticker_restart()` — the main-loop tick that performs the
     day's FIRST KiteTicker connect (cold start, or the closed-day
     recheck loop discovering a same-day special session) capped its
     own window at the same hardcoded 23:30. A first-connect attempt
     landing in [23:30, 23:55) IST during the winter window was refused
     25 minutes early. FUNCTIONAL — not a continuous mid-session
     disconnect/reconnect mechanism; that is `_task_ticker_watchdog`'s
     job, unaffected by either hardcode.

  3. `_sg_recover_ticker_connect()` — SessionGuard's cold-start catch-up
     (a server restart after 08:00 on a calendar-confirmed-open day
     connects the KiteTicker immediately rather than waiting for the
     next in-window 30s tick) carried the identical hardcoded 23:30
     upper bound. A restart landing in [23:30, 23:55) IST during the
     winter window skipped this backstop 25 minutes early.

  4. `_snapshot_stop_ticker()`'s recorded idle `reason` string was a
     static literal — "ticker stopped for the night at 23:31 IST" —
     regardless of season. COSMETIC ONLY: the real stop TRIGGER
     (`_ds_tick_mcx_stop()`) was already DST-aware; only the displayed
     claim about when it fired was wrong during the winter window.

All four now resolve (or, for #4, describe) the window's upper bound via
the shared `_ticker_window_end(today)` helper, which prefers the
DB-backed `exchange_schedule` MCX row's own `close_time` (the SAME
source `_ds_tick_mcx_stop()` already uses to decide when to actually
tear the WebSocket down) and falls back to the computed
`mcx_non_agri_close_time()` when the DB cache is empty/unsynced.

Reference dates match the convention established in
`test_mcx_non_agri_close_time.py` and `test_order_gate_mcx_dst_aware.py`.
"""

from __future__ import annotations

import types
from datetime import date, datetime, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")

_WINTER_DATE = date(2026, 11, 17)   # real MCX non-agri close 23:55 IST
_DST_DATE = date(2026, 6, 1)        # real MCX non-agri close 23:30 IST


def _reset_state():
    import backend.api.background as bg
    bg._TICKER_CALENDAR_STATE = {
        "date": None, "connected": False, "idle": False, "reason": "", "checked_at": 0.0,
    }
    bg._ticker_restart_done_global = None


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture(autouse=True)
def _empty_exchange_clock_cache():
    """Every test in this file relies on the computed-fallback branch of
    `_ticker_window_end()` unless it explicitly seeds the cache itself —
    force a known-empty starting cache rather than trusting whatever an
    earlier test module left behind, and restore afterward."""
    from backend.api.helpers import exchange_clock
    original = exchange_clock._CACHE
    exchange_clock._CACHE = []
    yield
    exchange_clock._CACHE = original


# ── 1. _ticker_window_end — the shared resolver ────────────────────────


def test_ticker_window_end_winter_computed_fallback_is_23_55():
    import backend.api.background as bg
    assert bg._ticker_window_end(_WINTER_DATE) == dtime(23, 55)


def test_ticker_window_end_dst_computed_fallback_is_23_30():
    import backend.api.background as bg
    assert bg._ticker_window_end(_DST_DATE) == dtime(23, 30)


def test_ticker_window_end_prefers_db_row_over_computed_value():
    """A date-specific DB override (e.g. an unusual MCX late-close) must
    win over the computed zoneinfo value — same precedence
    `_ds_tick_mcx_stop()` already uses."""
    import backend.api.background as bg
    from backend.api.helpers import exchange_clock

    mcx_row = types.SimpleNamespace(
        gate="MCX", date=_WINTER_DATE, weekdays=None,
        open_time=dtime(8, 0), close_time=dtime(23, 40),
        snapshot_time=None, exchanges=["MCX", "NCO"],
        is_open=True, session_name="regular", source="system",
    )
    with patch.object(exchange_clock, "_now_ist", return_value=datetime(
        _WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 12, 0, tzinfo=_IST,
    )):
        exchange_clock._CACHE = [mcx_row]
        assert bg._ticker_window_end(_WINTER_DATE) == dtime(23, 40)


def test_ticker_window_end_falls_back_when_db_row_is_a_holiday_override():
    """A holiday override row (`close_time=None`) must fall back to the
    computed value, not propagate None."""
    import backend.api.background as bg
    from backend.api.helpers import exchange_clock

    holiday_row = types.SimpleNamespace(
        gate="MCX", date=_WINTER_DATE, weekdays=None,
        open_time=None, close_time=None,
        snapshot_time=None, exchanges=["MCX", "NCO"],
        is_open=False, session_name="holiday", source="system",
    )
    with patch.object(exchange_clock, "_now_ist", return_value=datetime(
        _WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 12, 0, tzinfo=_IST,
    )):
        exchange_clock._CACHE = [holiday_row]
        assert bg._ticker_window_end(_WINTER_DATE) == dtime(23, 55)


def test_ticker_window_end_resolver_failure_falls_back_to_pre_fix_literal():
    """A resolver exception (future zoneinfo/import failure) must never
    raise — degrades to the pre-fix static 23:30 literal rather than
    crashing the background tick loop."""
    import backend.api.background as bg

    with patch(
        "backend.api.algo.derivatives.mcx_non_agri_close_time",
        side_effect=RuntimeError("boom"),
    ):
        assert bg._ticker_window_end(_WINTER_DATE) == dtime(23, 30)


# ── 2. get_ticker_calendar_status — the idle-badge clock window ────────


def test_broker_health_badge_winter_2340_is_not_idle_when_connected():
    """THE CONFIRMATION: at 23:40 IST during the winter window (real
    close 23:55), a connected ticker must NOT read idle — before this
    fix the hardcoded 23:30 upper bound made the badge show idle here."""
    import backend.api.background as bg

    bg._ticker_calendar_mark(_WINTER_DATE, connected=True, idle=False, reason="connected")
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)
    assert bg.get_ticker_calendar_status(now=now) == {"idle": False, "reason": "connected"}


def test_broker_health_badge_winter_2356_is_idle():
    """Just after the real 23:55 winter close — correctly idle, with a
    reason string reflecting the REAL resolved close, not a stale
    literal."""
    import backend.api.background as bg

    bg._ticker_calendar_mark(_WINTER_DATE, connected=True, idle=False, reason="connected")
    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)
    assert bg.get_ticker_calendar_status(now=now) == {
        "idle": True,
        "reason": "outside the 08:00–23:55 IST connectivity window",
    }


def test_broker_health_badge_dst_2340_is_still_idle_unchanged():
    """Contrast case — during the DST window the real close (23:30) has
    already passed at 23:40, so the badge must still read idle, proving
    this is genuinely season-aware and not "always later"."""
    import backend.api.background as bg

    bg._ticker_calendar_mark(_DST_DATE, connected=True, idle=False, reason="connected")
    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 40, tzinfo=_IST)
    assert bg.get_ticker_calendar_status(now=now) == {
        "idle": True,
        "reason": "outside the 08:00–23:30 IST connectivity window",
    }


def test_broker_health_badge_dst_season_reason_string_is_byte_identical_to_pre_fix():
    """DST-season text must stay byte-identical to the pre-fix literal
    so this is a genuinely additive fix, not a silent format change for
    most of the year."""
    import backend.api.background as bg

    bg._ticker_calendar_mark(_DST_DATE, connected=True, idle=False, reason="connected")
    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 7, 59, tzinfo=_IST)
    assert bg.get_ticker_calendar_status(now=now)["reason"] == (
        "outside the 08:00–23:30 IST connectivity window"
    )


# ── 3. _ds_tick_ticker_restart — the day's first-connect tick ──────────


async def test_restart_tick_connects_at_2340_during_winter_window():
    """The functional confirmation: a first-connect tick at 23:40 IST on
    a winter-window date must actually attempt the connect, not be
    refused as outside the (stale, hardcoded) window."""
    import backend.api.background as bg

    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 40, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now, _WINTER_DATE)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == _WINTER_DATE


async def test_restart_tick_noop_at_2356_past_the_real_winter_close():
    """Past the real 23:55 winter close, the tick must still be a
    no-op — the fix widens the window, it does not remove the cap."""
    import backend.api.background as bg

    now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called outside the real window"),
    ):
        await bg._ds_tick_ticker_restart(now, _WINTER_DATE)

    assert bg._ticker_restart_done_global is None


async def test_restart_tick_dst_season_2340_still_noop_unchanged():
    """DST-season behaviour at 23:40 — already past the real 23:30
    close — must stay a no-op exactly as before this fix (contrast case,
    proves the DST-season window did not change)."""
    import backend.api.background as bg

    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 40, tzinfo=_IST)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called outside the DST-season window"),
    ):
        await bg._ds_tick_ticker_restart(now, _DST_DATE)

    assert bg._ticker_restart_done_global is None


async def test_restart_tick_dst_season_2329_still_connects_unchanged():
    """One minute before the unchanged DST-season boundary — still
    connects, exactly as before this fix."""
    import backend.api.background as bg

    now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 29, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now, _DST_DATE)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == _DST_DATE


# ── 4. _sg_recover_ticker_connect — SessionGuard cold-start catch-up ───


async def test_session_guard_catchup_connects_at_2340_during_winter_window():
    """A server restart at 23:40 IST on a winter-window date (real MCX
    close 23:55) must still attempt the cold-start catch-up connect —
    before this fix the hardcoded 23:30 upper bound refused it 25
    minutes early."""
    import backend.api.background as bg

    now_t = dtime(23, 40)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._sg_recover_ticker_connect(now_t, _WINTER_DATE)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == _WINTER_DATE


async def test_session_guard_catchup_noop_at_2356_past_the_real_winter_close():
    """Past the real 23:55 winter close, the catch-up must still be a
    no-op — the fix widens the window, it does not remove the cap."""
    import backend.api.background as bg

    now_t = dtime(23, 56)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called outside the real window"),
    ):
        await bg._sg_recover_ticker_connect(now_t, _WINTER_DATE)

    assert bg._ticker_restart_done_global is None


async def test_session_guard_catchup_dst_season_2340_still_noop_unchanged():
    """DST-season contrast case — at 23:40, already past the real 23:30
    close, the catch-up must stay a no-op exactly as before this fix."""
    import backend.api.background as bg

    now_t = dtime(23, 40)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called outside the DST-season window"),
    ):
        await bg._sg_recover_ticker_connect(now_t, _DST_DATE)

    assert bg._ticker_restart_done_global is None


async def test_session_guard_catchup_dst_season_2329_still_connects_unchanged():
    """One minute before the unchanged DST-season boundary — still
    connects, exactly as before this fix."""
    import backend.api.background as bg

    now_t = dtime(23, 29)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._sg_recover_ticker_connect(now_t, _DST_DATE)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == _DST_DATE


# ── 5. _snapshot_stop_ticker — the recorded idle `reason` string ───────


def test_stop_ticker_reason_reflects_real_winter_close_plus_one_minute():
    """The recorded reason string must say 23:56 (real 23:55 winter
    close + 1 min) during the winter window, not the stale "23:31 IST"
    literal."""
    import backend.api.background as bg

    frozen_now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)
    with patch("backend.api.background.timestamp_indian", return_value=frozen_now), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=MagicMock()), \
         patch("backend.api.background.is_cutover_on", return_value=False):
        bg._snapshot_stop_ticker()

    assert bg._TICKER_CALENDAR_STATE["reason"] == "ticker stopped for the night at 23:56 IST"


def test_stop_ticker_reason_dst_season_stays_byte_identical_to_pre_fix():
    """During the DST window the resolved stop time is still 23:31 —
    the reason string must be byte-identical to the pre-fix literal, so
    this is a genuinely additive fix for most of the year."""
    import backend.api.background as bg

    frozen_now = datetime(_DST_DATE.year, _DST_DATE.month, _DST_DATE.day, 23, 31, tzinfo=_IST)
    with patch("backend.api.background.timestamp_indian", return_value=frozen_now), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=MagicMock()), \
         patch("backend.api.background.is_cutover_on", return_value=False):
        bg._snapshot_stop_ticker()

    assert bg._TICKER_CALENDAR_STATE["reason"] == "ticker stopped for the night at 23:31 IST"


def test_stop_ticker_reason_resolver_failure_falls_back_to_pre_fix_literal():
    """A resolver exception while computing the reason text must never
    block the real idle mark — degrades to the pre-fix literal text."""
    import backend.api.background as bg

    frozen_now = datetime(_WINTER_DATE.year, _WINTER_DATE.month, _WINTER_DATE.day, 23, 56, tzinfo=_IST)
    with patch("backend.api.background.timestamp_indian", return_value=frozen_now), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=MagicMock()), \
         patch("backend.api.background.is_cutover_on", return_value=False), \
         patch(
             "backend.api.background._ticker_window_end",
             side_effect=RuntimeError("boom"),
         ):
        bg._snapshot_stop_ticker()

    assert bg._TICKER_CALENDAR_STATE["reason"] == "ticker stopped for the night at 23:31 IST"
    # The real idle mark must still land despite the resolver failure.
    assert bg._TICKER_CALENDAR_STATE["idle"] is True
    assert bg._TICKER_CALENDAR_STATE["connected"] is False
