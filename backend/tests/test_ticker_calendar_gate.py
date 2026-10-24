"""
Tests for the calendar-gated broker/WebSocket connection lifecycle
(2026-10-24) — stop connecting to the KiteTicker WebSocket at all on a
day nothing will trade, while never missing a genuinely open day
(including a same-day special session discovered after the 05:30
calendar refresh).

Covers:
  - `_ticker_calendar_verdict()` — the tri-state (open/closed/unknown)
    calendar decision, fail-safe-to-OPEN on any lookup error.
  - `_ds_tick_ticker_restart()` — a confirmed-open day connects at 08:00
    unchanged; a confirmed-closed day never connects and shows `idle`;
    a same-day special session appearing later is picked up by the
    recheck loop within the 08:00-23:30 window.
  - `_sg_recover_ticker_connect()` — SessionGuard cold-start catch-up: a
    server restart after 08:00 on an open day connects immediately.
  - `get_ticker_calendar_status()` / `ticker_connected_today()` —
    staleness handling for the health-route idle badge and the
    watchdog's skip-all-inspection gate.
  - `backend.api.app._start_kite_ticker()` — the true unconditional
    startup connect path now also respects the calendar.
  - `backend.api.routes.health._resolve_ticker_calendar_status()` wiring.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")

_SEGMENTS = {
    "equity": {"holiday_exchange": "NSE", "evening_open_on_holidays": False},
    "commodity": {"holiday_exchange": "MCX", "evening_open_on_holidays": True},
}


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


# ---------------------------------------------------------------------------
# _ticker_calendar_verdict
# ---------------------------------------------------------------------------

async def test_verdict_open_on_plain_weekday_no_holiday():
    """A normal weekday with no holiday resolves OPEN without ever
    needing to consult special sessions (guards against the trap of
    using an "is open right now" check, which would read False at the
    08:00 decision point before any session has opened)."""
    import backend.api.background as bg

    monday = date(2026, 10, 19)  # confirmed Monday
    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch(
             "backend.brokers.broker_apis._read_special_sessions_async",
             new_callable=AsyncMock,
             side_effect=AssertionError("must not be called — calendar already says open"),
         ):
        verdict = await bg._ticker_calendar_verdict(monday)
    assert verdict == bg._CAL_OPEN


async def test_verdict_closed_on_weekend_with_no_special_session():
    import backend.api.background as bg

    saturday = date(2026, 10, 17)  # confirmed Saturday
    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
         patch(
             "backend.brokers.broker_apis._read_special_sessions_async",
             new_callable=AsyncMock, return_value=[],
         ):
        verdict = await bg._ticker_calendar_verdict(saturday)
    assert verdict == bg._CAL_CLOSED


async def test_verdict_zero_width_special_session_stays_closed():
    """A zero-width override row (the auto-inserted 'MCX evening closed'
    marker) must NOT be read as an open signal, and must block the
    evening_open_on_holidays default too."""
    import backend.api.background as bg

    a_holiday = date(2026, 10, 20)

    async def _special(exch, today):
        if exch == "MCX":
            return [{"date": today, "start": dtime(0, 0), "end": dtime(0, 0)}]
        return []

    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value={a_holiday}), \
         patch("backend.brokers.broker_apis._read_special_sessions_async", side_effect=_special):
        verdict = await bg._ticker_calendar_verdict(a_holiday)
    assert verdict == bg._CAL_CLOSED


async def test_verdict_non_zero_width_special_session_opens():
    """A late-announced, non-zero-width market_special_sessions row for
    today opens that segment, even though the base calendar says
    closed — this is the mechanism the closed-day recheck loop relies
    on to pick up a same-day special session."""
    import backend.api.background as bg

    a_holiday = date(2026, 10, 20)

    async def _special(exch, today):
        if exch == "NSE":
            return [{"date": today, "start": dtime(10, 0), "end": dtime(14, 0)}]
        return []

    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value={a_holiday}), \
         patch("backend.brokers.broker_apis._read_special_sessions_async", side_effect=_special):
        verdict = await bg._ticker_calendar_verdict(a_holiday)
    assert verdict == bg._CAL_OPEN


async def test_verdict_evening_open_on_holiday_weekday_opens():
    """MCX's evening_open_on_holidays default opens the segment on a
    weekday holiday with no special-session row at all."""
    import backend.api.background as bg

    a_holiday = date(2026, 10, 20)  # a Tuesday
    assert a_holiday.weekday() < 5

    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", return_value={a_holiday}), \
         patch(
             "backend.brokers.broker_apis._read_special_sessions_async",
             new_callable=AsyncMock, return_value=[],
         ):
        verdict = await bg._ticker_calendar_verdict(a_holiday)
    assert verdict == bg._CAL_OPEN


async def test_verdict_extra_trading_days_override_opens_weekend():
    """An operator-managed `market.extra_trading_days` entry (Muhurat
    Saturday) opens a weekend without even consulting fetch_holidays."""
    import backend.api.background as bg

    muhurat_sat = date(2026, 10, 17)

    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch(
             "backend.shared.helpers.date_time_utils._parse_extra_trading_days",
             return_value={muhurat_sat},
         ), \
         patch(
             "backend.brokers.broker_apis.fetch_holidays",
             side_effect=AssertionError("must not be called — extra_trading_days short-circuits"),
         ):
        verdict = await bg._ticker_calendar_verdict(muhurat_sat)
    assert verdict == bg._CAL_OPEN


async def test_verdict_fail_safe_to_unknown_on_error():
    """Any DB/config uncertainty during the check resolves UNKNOWN, which
    every caller treats exactly like OPEN (fail-safe-to-connect)."""
    import backend.api.background as bg

    saturday = date(2026, 10, 17)
    with patch("backend.api.background.config", {"market_segments": _SEGMENTS}), \
         patch("backend.brokers.broker_apis.fetch_holidays", side_effect=RuntimeError("db down")):
        verdict = await bg._ticker_calendar_verdict(saturday)
    assert verdict == bg._CAL_UNKNOWN


async def test_verdict_outer_exception_fails_safe_to_unknown():
    """A failure even constructing the extra_trading_days set (outermost
    try/except) still resolves UNKNOWN, never raises."""
    import backend.api.background as bg

    with patch(
        "backend.shared.helpers.date_time_utils._parse_extra_trading_days",
        side_effect=RuntimeError("boom"),
    ):
        verdict = await bg._ticker_calendar_verdict(date(2026, 10, 17))
    assert verdict == bg._CAL_UNKNOWN


# ---------------------------------------------------------------------------
# _ds_tick_ticker_restart — the main-loop 08:00-23:30 tick
# ---------------------------------------------------------------------------

async def test_tick_connects_on_confirmed_open_day_unchanged():
    import backend.api.background as bg

    today = date(2026, 10, 19)
    now = datetime(2026, 10, 19, 8, 0, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now, today)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == today
    # Check the underlying state dict directly rather than the
    # staleness-filtering public accessor — `today` here is a fixed
    # historical date, not the real IST "now" the accessor compares
    # against (the accessor's own staleness behavior is covered by the
    # dedicated tests below, using the real clock).
    assert bg._TICKER_CALENDAR_STATE["idle"] is False
    assert bg._TICKER_CALENDAR_STATE["connected"] is True


async def test_tick_never_connects_on_confirmed_closed_day_and_marks_idle():
    import backend.api.background as bg

    today = date(2026, 10, 17)
    now = datetime(2026, 10, 17, 8, 0, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now, today)

    m_restart.assert_not_called()
    assert bg._ticker_restart_done_global is None
    assert bg._TICKER_CALENDAR_STATE["idle"] is True
    assert bg._TICKER_CALENDAR_STATE["date"] == today
    assert "closed" in bg._TICKER_CALENDAR_STATE["reason"]


async def test_tick_outside_0800_2330_window_is_noop():
    import backend.api.background as bg

    today = date(2026, 10, 19)
    now = datetime(2026, 10, 19, 23, 45, tzinfo=_IST)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called outside the window"),
    ):
        await bg._ds_tick_ticker_restart(now, today)

    assert bg._ticker_restart_done_global is None


async def test_recheck_loop_picks_up_late_special_session_within_window():
    """A same-day special session appearing after the initial 08:05
    check is picked up by a LATER tick within the 08:00-23:30 window —
    simulates the throttle interval elapsing between two calls."""
    import backend.api.background as bg

    today = date(2026, 10, 17)
    first_tick = datetime(2026, 10, 17, 8, 5, tzinfo=_IST)
    second_tick = datetime(2026, 10, 17, 10, 0, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(first_tick, today)
    m_restart.assert_not_called()
    assert bg._TICKER_CALENDAR_STATE["idle"] is True

    # Force the throttle window to have elapsed so the second tick
    # actually re-queries instead of short-circuiting.
    bg._TICKER_CALENDAR_STATE["checked_at"] = 0.0

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart2:
        await bg._ds_tick_ticker_restart(second_tick, today)

    m_restart2.assert_called_once()
    assert bg._ticker_restart_done_global == today
    assert bg._TICKER_CALENDAR_STATE["idle"] is False


async def test_recheck_loop_throttled_within_interval():
    """Two closed-day ticks within `_TICKER_RECHECK_INTERVAL_S` of each
    other must only hit the DB-backed verdict computation once —
    `_ticker_calendar_verdict` raises on the second call to prove it was
    never invoked again."""
    import backend.api.background as bg

    today = date(2026, 10, 17)
    now = datetime(2026, 10, 17, 8, 5, tzinfo=_IST)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED):
        await bg._ds_tick_ticker_restart(now, today)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not re-query within the throttle window"),
    ):
        await bg._ds_tick_ticker_restart(now, today)  # same instant — well within throttle


async def test_tick_noop_once_already_connected_today():
    """Once `_ticker_restart_done_global` is seeded (e.g. by
    `_start_kite_ticker()` at true process startup), the tick must not
    re-query the calendar at all."""
    import backend.api.background as bg

    today = date(2026, 10, 19)
    now = datetime(2026, 10, 19, 9, 0, tzinfo=_IST)
    bg._ticker_restart_done_global = today

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called — already connected today"),
    ):
        await bg._ds_tick_ticker_restart(now, today)


# ---------------------------------------------------------------------------
# _sg_recover_ticker_connect — SessionGuard cold-start catch-up
# ---------------------------------------------------------------------------

async def test_session_guard_catchup_connects_immediately_after_0800_restart():
    """A server restart at 14:00 IST on a calendar-confirmed-open day
    connects immediately, instead of waiting for the next day's 08:00
    tick."""
    import backend.api.background as bg

    today = date(2026, 10, 19)
    now_t = dtime(14, 0)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._sg_recover_ticker_connect(now_t, today)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == today


async def test_session_guard_catchup_skips_before_0800():
    import backend.api.background as bg

    today = date(2026, 10, 19)
    now_t = dtime(6, 0)

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called before 08:00"),
    ):
        await bg._sg_recover_ticker_connect(now_t, today)

    assert bg._ticker_restart_done_global is None


async def test_session_guard_catchup_noop_when_already_connected():
    """If `_start_kite_ticker()` already connected via its own
    credential path, SessionGuard's backstop must not re-query."""
    import backend.api.background as bg

    today = date(2026, 10, 19)
    bg._ticker_restart_done_global = today

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called — already connected"),
    ):
        await bg._sg_recover_ticker_connect(dtime(14, 0), today)


# ---------------------------------------------------------------------------
# ticker_connected_today / get_ticker_calendar_status — staleness
# ---------------------------------------------------------------------------

def test_ticker_connected_today_false_when_never_computed():
    import backend.api.background as bg
    assert bg.ticker_connected_today() is False


def test_ticker_connected_today_true_right_after_connect():
    import backend.api.background as bg
    today = __import__("backend.shared.helpers.date_time_utils", fromlist=["timestamp_indian"]).timestamp_indian().date()
    bg._ticker_calendar_mark(today, connected=True, idle=False, reason="connected")
    assert bg.ticker_connected_today() is True


def test_ticker_connected_today_false_for_stale_prior_day():
    """A 'connected' flag from yesterday must never suppress today's
    watchdog checks."""
    import backend.api.background as bg
    bg._ticker_calendar_mark(date(2000, 1, 1), connected=True, idle=False, reason="stale")
    assert bg.ticker_connected_today() is False


def test_get_ticker_calendar_status_stale_reads_as_not_idle():
    import backend.api.background as bg
    bg._ticker_calendar_mark(date(2000, 1, 1), connected=False, idle=True, reason="old")
    status = bg.get_ticker_calendar_status()
    assert status == {"idle": False, "reason": ""}


# ---------------------------------------------------------------------------
# backend.api.app._start_kite_ticker — the true unconditional startup
# connect path.
# ---------------------------------------------------------------------------

async def test_start_kite_ticker_skips_on_confirmed_closed_day():
    import backend.api.app as app_mod
    import backend.api.background as bg

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED), \
         patch("backend.brokers.kite_ticker.get_ticker") as m_get_ticker:
        await app_mod._start_kite_ticker()

    m_get_ticker.assert_not_called()
    assert bg.get_ticker_calendar_status()["idle"] is True


async def test_start_kite_ticker_connects_direct_mode_on_open_day():
    import backend.api.app as app_mod
    import backend.api.background as bg
    import os as _os

    mock_ticker = MagicMock()
    mock_connections = MagicMock()
    mock_connections._brokers = []

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch.dict(_os.environ, {"RAMBOQ_USE_CONN_SERVICE": ""}, clear=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch.object(app_mod, "_resolve_creds_local", return_value=("api_key_1", "token_1", "ACC1")):
        await app_mod._start_kite_ticker()

    mock_ticker.start.assert_called_once_with("api_key_1", "token_1", account="ACC1")
    assert bg.get_ticker_calendar_status()["idle"] is False
    assert bg.ticker_connected_today() is True


async def test_start_kite_ticker_conn_service_mode_on_open_day():
    import backend.api.app as app_mod
    import backend.api.background as bg
    import os as _os

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch.dict(_os.environ, {"RAMBOQ_USE_CONN_SERVICE": "1"}, clear=False), \
         patch.object(app_mod, "_start_mmap_reader") as m_start_mmap:
        await app_mod._start_kite_ticker()

    m_start_mmap.assert_called_once()
    assert bg.ticker_connected_today() is True


async def test_start_kite_ticker_early_boot_does_not_suppress_0800_restart():
    """A process boot well before 08:00 IST on a calendar-confirmed-open
    day connects immediately (unchanged — `_start_kite_ticker` has
    always run at any time of day), but must NOT seed
    `_ticker_restart_done_global` — otherwise the later 08:00 fresh-token
    restart (`_ds_tick_ticker_restart`'s whole original purpose: binding
    to the token `_task_holiday_refresh` rotates at 05:30) would be
    silently skipped for the rest of the day."""
    import backend.api.app as app_mod
    import backend.api.background as bg
    import os as _os

    early = datetime(2026, 10, 19, 2, 0, tzinfo=_IST)

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=early), \
         patch.dict(_os.environ, {"RAMBOQ_USE_CONN_SERVICE": "1"}, clear=False), \
         patch.object(app_mod, "_start_mmap_reader") as m_start_mmap:
        await app_mod._start_kite_ticker()

    m_start_mmap.assert_called_once()
    # Check the state dict directly — `ticker_connected_today()` compares
    # against the REAL current IST date, not the fixed `early` fixture.
    assert bg._TICKER_CALENDAR_STATE["connected"] is True  # watchdog may run real checks
    assert bg._ticker_restart_done_global is None  # but 08:00 restart still fires

    # The 08:00 tick on the same day must still connect — the early boot
    # must not have silently satisfied it.
    now_0800 = datetime(2026, 10, 19, 8, 0, tzinfo=_IST)
    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now_0800, date(2026, 10, 19))
    m_restart.assert_called_once()


async def test_start_kite_ticker_boot_after_0800_seeds_sentinel():
    """A boot AT or AFTER 08:00 IST is the normal cold-start-catch-up
    case (item 4) — it DOES seed the sentinel, since this connect IS the
    day's official restart, not a pre-market early boot."""
    import backend.api.app as app_mod
    import backend.api.background as bg
    import os as _os

    late = datetime(2026, 10, 19, 14, 0, tzinfo=_IST)

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=late), \
         patch.dict(_os.environ, {"RAMBOQ_USE_CONN_SERVICE": "1"}, clear=False), \
         patch.object(app_mod, "_start_mmap_reader"):
        await app_mod._start_kite_ticker()

    assert bg._ticker_restart_done_global == date(2026, 10, 19)


# ---------------------------------------------------------------------------
# _snapshot_restart_ticker — defensive set_loop() (the "frozen LTP" class
# of regression this change could otherwise reintroduce via a new path).
# ---------------------------------------------------------------------------

async def test_snapshot_restart_ticker_cutover_mode_sets_loop_defensively():
    """A process that booted on a confirmed-closed day never calls
    `set_loop()` via `_start_kite_ticker()`. The FIRST real connect — via
    the recheck loop's `_snapshot_restart_ticker()` call — must still
    wire the running loop into the ticker (and its BroadcastBus),
    otherwise `publish_many()` silently drops every tick forever."""
    import backend.api.background as bg

    mock_ticker = MagicMock()
    mock_ticker.check_and_heal = MagicMock(return_value=True)

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        bg._snapshot_restart_ticker()

    mock_ticker.set_loop.assert_called_once()
    mock_ticker.check_and_heal.assert_called_once()


async def test_snapshot_restart_ticker_direct_mode_sets_loop_defensively():
    import backend.api.background as bg

    mock_ticker = MagicMock()
    mock_ticker.restart = MagicMock()

    mock_kite = MagicMock()
    mock_kite.api_key = "key_123"
    mock_kite.access_token = "token_456"
    mock_conn = MagicMock()
    mock_conn.kite = mock_kite
    mock_connections_instance = MagicMock()
    mock_connections_instance.conn = {"ACC1": mock_conn}

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("backend.brokers.connections.Connections", return_value=mock_connections_instance):
        bg._snapshot_restart_ticker()

    mock_ticker.set_loop.assert_called_once()
    mock_ticker.restart.assert_called_once_with("key_123", "token_456", "ACC1")


async def test_snapshot_restart_ticker_set_loop_failure_does_not_block_restart():
    """A `set_loop()` exception (unexpected adapter shape, etc.) is
    caught and logged — it must not prevent the restart/heal itself."""
    import backend.api.background as bg

    mock_ticker = MagicMock()
    mock_ticker.set_loop = MagicMock(side_effect=RuntimeError("boom"))
    mock_ticker.check_and_heal = MagicMock(return_value=True)

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.shared.helpers.utils.is_engine_idle", return_value=False), \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker):
        bg._snapshot_restart_ticker()  # must not raise

    mock_ticker.check_and_heal.assert_called_once()


async def test_start_kite_ticker_engine_idle_skips_before_calendar_check():
    """The pre-existing dev-idle gate still short-circuits BEFORE the
    calendar check even runs."""
    import backend.api.app as app_mod

    with patch("backend.shared.helpers.utils.is_engine_idle", return_value=True), \
         patch(
             "backend.api.background._ticker_calendar_verdict",
             new_callable=AsyncMock,
             side_effect=AssertionError("must not be called — engine idle"),
         ):
        await app_mod._start_kite_ticker()  # must not raise


# ---------------------------------------------------------------------------
# health.py wiring
# ---------------------------------------------------------------------------

def test_resolve_ticker_calendar_status_surfaces_idle():
    import backend.api.background as bg
    from backend.api.routes.health import _resolve_ticker_calendar_status

    today = __import__("backend.shared.helpers.date_time_utils", fromlist=["timestamp_indian"]).timestamp_indian().date()
    bg._ticker_calendar_mark(today, connected=False, idle=True, reason="market calendar confirms closed today")
    idle, reason = _resolve_ticker_calendar_status()
    assert idle is True
    assert "closed" in reason


def test_resolve_ticker_calendar_status_fails_open_on_error():
    from backend.api.routes.health import _resolve_ticker_calendar_status
    with patch("backend.api.background.get_ticker_calendar_status", side_effect=RuntimeError("boom")):
        idle, reason = _resolve_ticker_calendar_status()
    assert idle is False
    assert reason == ""


def test_broker_health_response_has_ticker_idle_fields_with_defaults():
    from backend.api.routes.health import BrokerHealthResponse
    resp = BrokerHealthResponse(accounts=[])
    assert resp.ticker_idle is False
    assert resp.ticker_idle_reason == ""


# ---------------------------------------------------------------------------
# _ticker_calendar_refresh_idle_status (2026-10-24 bug fix) — status-only
# calendar check that can run at any hour, outside the 08:00-23:30 connect
# window. Fixes: on a confirmed non-trading day (e.g. Saturday), outside
# that window — or before the first in-window tick after a restart —
# `_TICKER_CALENDAR_STATE` stayed stale/unset, so `get_ticker_calendar_status()`
# fell back to "not idle" and the broker-health badge showed its default
# per-account state instead of idle.
# ---------------------------------------------------------------------------

async def test_refresh_idle_status_marks_idle_on_closed_verdict_outside_window():
    """The core fix: a CLOSED verdict at any hour (here, 02:00 IST on a
    Saturday — well outside the 08:00-23:30 connect window) now marks
    the shared state idle. Before this fix, nothing computed this
    outside the window, so `get_ticker_calendar_status()` stayed
    `{"idle": False}` all day."""
    import backend.api.background as bg

    saturday = date(2026, 10, 17)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED):
        await bg._ticker_calendar_refresh_idle_status(saturday)

    assert bg._TICKER_CALENDAR_STATE["idle"] is True
    assert bg._TICKER_CALENDAR_STATE["date"] == saturday
    assert bg._TICKER_CALENDAR_STATE["connected"] is False
    assert "closed" in bg._TICKER_CALENDAR_STATE["reason"]


async def test_refresh_idle_status_never_calls_connect_even_when_verdict_is_open():
    """Structural guarantee: this function has no `do_connect` parameter
    at all, so it cannot connect anything. Explicitly proves it even
    when the verdict resolves OPEN — `_snapshot_restart_ticker` (the
    real connect function used by the two in-window call sites) must
    never be invoked by this path, and the shared state must be left
    untouched rather than marked misleadingly."""
    import backend.api.background as bg

    monday = date(2026, 10, 19)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ticker_calendar_refresh_idle_status(monday)

    m_restart.assert_not_called()
    assert bg._TICKER_CALENDAR_STATE["date"] is None  # untouched
    assert bg._ticker_restart_done_global is None


async def test_refresh_idle_status_never_calls_connect_on_unknown_verdict():
    """Same guarantee for the UNKNOWN (fail-safe) verdict — leaves state
    untouched, never connects."""
    import backend.api.background as bg

    monday = date(2026, 10, 19)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_UNKNOWN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ticker_calendar_refresh_idle_status(monday)

    m_restart.assert_not_called()
    assert bg._TICKER_CALENDAR_STATE["date"] is None


async def test_refresh_idle_status_skips_verdict_when_already_connected_today():
    """If a real connect call site already marked today as connected,
    this status-only path must not re-query the calendar at all."""
    import backend.api.background as bg

    today = date(2026, 10, 19)
    bg._ticker_calendar_mark(today, connected=True, idle=False, reason="connected")

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not be called — already connected today"),
    ):
        await bg._ticker_calendar_refresh_idle_status(today)


async def test_refresh_idle_status_throttles_repeat_closed_checks_same_day():
    """Two closed-day calls within `_TICKER_RECHECK_INTERVAL_S` of each
    other must only hit the verdict computation once — mirrors
    `_ticker_calendar_decide_and_act`'s own throttle so an overnight
    closed window doesn't hammer `market_special_sessions` every 30s
    for hours."""
    import backend.api.background as bg

    saturday = date(2026, 10, 17)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED):
        await bg._ticker_calendar_refresh_idle_status(saturday)
    assert bg._TICKER_CALENDAR_STATE["idle"] is True

    with patch(
        "backend.api.background._ticker_calendar_verdict",
        new_callable=AsyncMock,
        side_effect=AssertionError("must not re-query within the throttle window"),
    ):
        await bg._ticker_calendar_refresh_idle_status(saturday)  # immediately after — within throttle


async def test_refresh_idle_status_rechecks_after_throttle_window_elapses():
    """Once the recheck interval has elapsed, a stale idle mark for the
    same day IS re-verified (picks up a late special session, matching
    the in-window recheck loop's own behaviour)."""
    import backend.api.background as bg

    saturday = date(2026, 10, 17)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED):
        await bg._ticker_calendar_refresh_idle_status(saturday)
    assert bg._TICKER_CALENDAR_STATE["idle"] is True

    # Force the throttle window to have elapsed.
    bg._TICKER_CALENDAR_STATE["checked_at"] = 0.0

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED) as m_verdict:
        await bg._ticker_calendar_refresh_idle_status(saturday)
    m_verdict.assert_called_once()


# ---------------------------------------------------------------------------
# Wiring into the two ticker-watchdog loop bodies — proves the fix actually
# reaches both the direct-mode loop and the conn-service/cutover loop (prod
# and dev both run with RAMBOQ_USE_CONN_SERVICE=1, so the cutover loop is
# the one that matters most in production).
# ---------------------------------------------------------------------------

def _make_cancelling_sleep(stop_after: int):
    calls = {"n": 0}

    async def _sleep(delay):
        calls["n"] += 1
        if calls["n"] >= stop_after:
            raise asyncio.CancelledError("test escape")
    return _sleep


async def test_cutover_loop_marks_idle_on_closed_day_without_connecting():
    """One real iteration of `_watchdog_cutover_heal_loop` on a
    confirmed-closed day (e.g. Saturday 02:00 IST) marks the shared
    state idle via the new status-only path, and never calls
    check_and_heal() (ticker_connected_today() stays False)."""
    import backend.api.background as bg

    saturday_0200 = datetime(2026, 10, 17, 2, 0, tzinfo=_IST)
    mock_ticker = MagicMock()
    mock_ticker.check_and_heal = MagicMock(return_value=True)

    with patch("backend.api.background.is_cutover_on", return_value=True), \
         patch("backend.api.background.timestamp_indian", return_value=saturday_0200), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart, \
         patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker), \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(2)):
        await bg._task_ticker_watchdog({})
        # Assert the staleness-filtering public accessor WHILE
        # `timestamp_indian` is still patched to the fixed Saturday —
        # `get_ticker_calendar_status()` compares `st["date"]` against
        # the REAL current IST date, which would make this assertion
        # spuriously fail once the patch context exits (same trap the
        # pre-existing tests above comment on).
        assert bg.get_ticker_calendar_status() == {
            "idle": True,
            "reason": bg._TICKER_CALENDAR_CLOSED_REASON,
        }

    mock_ticker.check_and_heal.assert_not_called()
    m_restart.assert_not_called()
    # Check the underlying state dict directly too, outside the patch —
    # it carries the fixed `saturday_0200` date regardless of the real
    # clock, unlike the accessor above.
    assert bg._TICKER_CALENDAR_STATE["idle"] is True
    assert bg._TICKER_CALENDAR_STATE["date"] == saturday_0200.date()


async def test_direct_loop_marks_idle_on_closed_day_without_broker_checks():
    """Same proof for the non-cutover (direct) watchdog loop body — one
    real iteration on a confirmed-closed day marks idle via the new
    status-only path, and the loop's own zero-broker-check invariant on
    this branch is preserved: `_watchdog_deferred_recycle` and
    `_watchdog_check_market_open` (both real broker/market probes) are
    never reached."""
    import backend.api.background as bg

    saturday_0200 = datetime(2026, 10, 17, 2, 0, tzinfo=_IST)

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.api.background.timestamp_indian", return_value=saturday_0200), \
         patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED), \
         patch("backend.api.background._watchdog_deferred_recycle", new_callable=AsyncMock) as m_recycle, \
         patch("backend.api.background._watchdog_check_market_open", new_callable=AsyncMock) as m_market_open, \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(2)):
        await bg._task_ticker_watchdog({})
        assert bg.get_ticker_calendar_status() == {
            "idle": True,
            "reason": bg._TICKER_CALENDAR_CLOSED_REASON,
        }

    m_recycle.assert_not_called()
    m_market_open.assert_not_called()
    assert bg._TICKER_CALENDAR_STATE["idle"] is True
    assert bg._TICKER_CALENDAR_STATE["date"] == saturday_0200.date()


async def test_direct_loop_exception_in_status_refresh_does_not_crash_loop():
    """An exception from the new status-only refresh call must be
    caught and logged, not propagate and kill the supervised task —
    matches every other per-cycle guard in this loop."""
    import backend.api.background as bg

    saturday_0200 = datetime(2026, 10, 17, 2, 0, tzinfo=_IST)

    with patch("backend.api.background.is_cutover_on", return_value=False), \
         patch("backend.api.background.timestamp_indian", return_value=saturday_0200), \
         patch(
             "backend.api.background._ticker_calendar_refresh_idle_status",
             new_callable=AsyncMock, side_effect=RuntimeError("boom"),
         ), \
         patch("asyncio.sleep", side_effect=_make_cancelling_sleep(2)):
        # Must not raise — the per-iteration try/except around the new
        # call swallows it, and the loop's own outer except would catch
        # it anyway even if it didn't.
        await bg._task_ticker_watchdog({})


async def test_0800_connect_tick_still_fires_after_overnight_idle_mark():
    """Interaction check: after the new status-only path marks today
    idle overnight (e.g. 02:00), the real 08:00 connect tick on a day
    that turns out OPEN (a late special session) still connects and
    seeds the restart sentinel — the overnight idle mark must not
    permanently suppress the real connect decision once the throttle
    window has elapsed."""
    import backend.api.background as bg

    today = date(2026, 10, 17)

    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_CLOSED):
        await bg._ticker_calendar_refresh_idle_status(today)
    assert bg._TICKER_CALENDAR_STATE["idle"] is True

    # Age the mark past the shared recheck throttle so the 08:00 tick
    # actually re-queries instead of short-circuiting.
    bg._TICKER_CALENDAR_STATE["checked_at"] = 0.0

    now_0800 = datetime(2026, 10, 17, 8, 0, tzinfo=_IST)
    with patch("backend.api.background._ticker_calendar_verdict", new_callable=AsyncMock, return_value=bg._CAL_OPEN), \
         patch("backend.api.background._snapshot_restart_ticker") as m_restart:
        await bg._ds_tick_ticker_restart(now_0800, today)

    m_restart.assert_called_once()
    assert bg._ticker_restart_done_global == today
    assert bg._TICKER_CALENDAR_STATE["idle"] is False
