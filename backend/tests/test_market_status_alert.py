"""
Item 3 (2026-10-22) — 05:30/05:35 IST market-status alert agent
(NSE + MCX open/closed + hours).

Covers `backend/api/algo/actions_live.py`:
  - `_market_status_for_segment` — calendar-only open/closed/hours/reason
    determination for one `market_segments` entry.
  - `_format_market_status_message` — message text for both segments.
  - `_action_live_send_market_status` — channel routing: Telegram always;
    ntfy only when at least one segment is open today.

Also covers the new built-in agent (`agent_engine.py:market-status-report`)
and its action-dispatch wiring (`actions.py:_NOOP_ACTION_HANDLERS`).
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import pytest

IST = ZoneInfo("Asia/Kolkata")


def _fake_holidays_factory(holiday_exchanges: set):
    def _fn(exchange: str) -> set:
        return {_TODAY} if exchange.upper() in holiday_exchanges else set()
    return _fn


_TODAY = datetime(2026, 10, 22, 5, 35, tzinfo=IST).date()  # Thursday


class TestMarketStatusForSegment:
    @pytest.mark.asyncio
    async def test_open_weekday_reports_hours(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])):
            eq = await _market_status_for_segment("equity", now)
            co = await _market_status_for_segment("commodity", now)
        assert eq["open"] is True
        assert eq["hours"] == "09:15–15:30 IST"
        assert co["open"] is True
        assert co["hours"] == "09:00–23:30 IST"

    @pytest.mark.asyncio
    async def test_weekend_reports_closed(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])):
            eq = await _market_status_for_segment("equity", now)
        assert eq["open"] is False
        assert eq["reason"] == "Weekend"

    @pytest.mark.asyncio
    async def test_nse_holiday_reports_closed_with_reason(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.brokers.broker_apis.fetch_holidays",
                   side_effect=_fake_holidays_factory({"NSE"})), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
             patch("backend.api.algo.grammar_tokens.action_send_market_status._holiday_reason", AsyncMock(return_value="Diwali")):
            eq = await _market_status_for_segment("equity", now)
        assert eq["open"] is False
        assert eq["reason"] == "Diwali"

    @pytest.mark.asyncio
    async def test_nse_holiday_no_reason_falls_back_to_generic(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.brokers.broker_apis.fetch_holidays",
                   side_effect=_fake_holidays_factory({"NSE"})), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
             patch("backend.api.algo.grammar_tokens.action_send_market_status._holiday_reason", AsyncMock(return_value=None)):
            eq = await _market_status_for_segment("equity", now)
        assert eq["open"] is False
        assert eq["reason"] == "Holiday"

    @pytest.mark.asyncio
    async def test_mcx_holiday_with_evening_open_reports_evening_hours(self):
        """MCX's evening_open_on_holidays=True means a holiday still opens
        for the evening leg absent a zero-width special-session override."""
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.brokers.broker_apis.fetch_holidays",
                   side_effect=_fake_holidays_factory({"MCX"})), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])):
            co = await _market_status_for_segment("commodity", now)
        assert co["open"] is True
        assert co["hours"] == "17:00–23:30 IST"

    @pytest.mark.asyncio
    async def test_special_session_zero_width_overrides_to_closed(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        special_row = {"date": _TODAY, "start": dtime(0, 0), "end": dtime(0, 0)}
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[special_row])):
            co = await _market_status_for_segment("commodity", now)
        assert co["open"] is False
        assert co["reason"] == "Special closure"

    @pytest.mark.asyncio
    async def test_special_session_override_reports_custom_hours(self):
        from backend.api.algo.actions_live import _market_status_for_segment
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        special_row = {"date": _TODAY, "start": dtime(10, 0), "end": dtime(14, 0)}
        with patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[special_row])):
            eq = await _market_status_for_segment("equity", now)
        assert eq["open"] is True
        assert eq["hours"] == "10:00–14:00 IST"


class TestFormatMarketStatusMessage:
    def test_both_open(self):
        from backend.api.algo.actions_live import _format_market_status_message
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        eq = {"open": True, "hours": "09:15–15:30 IST", "reason": None}
        co = {"open": True, "hours": "09:00–23:30 IST", "reason": None}
        msg = _format_market_status_message(now, eq, co)
        assert "NSE/Equity: OPEN 09:15–15:30 IST" in msg
        assert "MCX/Commodity: OPEN 09:00–23:30 IST" in msg

    def test_both_closed(self):
        from backend.api.algo.actions_live import _format_market_status_message
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        eq = {"open": False, "hours": None, "reason": "Diwali"}
        co = {"open": False, "hours": None, "reason": "Diwali"}
        msg = _format_market_status_message(now, eq, co)
        assert "NSE/Equity: CLOSED (Diwali)" in msg
        assert "MCX/Commodity: CLOSED (Diwali)" in msg

    def test_mixed(self):
        from backend.api.algo.actions_live import _format_market_status_message
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        eq = {"open": False, "hours": None, "reason": "Diwali"}
        co = {"open": True, "hours": "17:00–23:30 IST", "reason": None}
        msg = _format_market_status_message(now, eq, co)
        assert "NSE/Equity: CLOSED (Diwali)" in msg
        assert "MCX/Commodity: OPEN 17:00–23:30 IST" in msg


class TestActionLiveSendMarketStatus:
    @pytest.mark.asyncio
    async def test_open_day_fires_telegram_and_ntfy(self):
        from backend.api.algo.actions_live import _action_live_send_market_status
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
             patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
             patch("backend.shared.helpers.alert_utils._send_telegram") as mock_tg, \
             patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_ntfy:
            await _action_live_send_market_status({}, {})
        mock_tg.assert_called_once()
        mock_ntfy.assert_called_once()
        tg_msg = mock_tg.call_args.args[0]
        assert "NSE/Equity: OPEN" in tg_msg
        assert "MCX/Commodity: OPEN" in tg_msg
        ntfy_msg = mock_ntfy.call_args.args[1]
        assert "OPEN" in ntfy_msg
        assert mock_ntfy.call_args.kwargs.get("priority") == "low"

    @pytest.mark.asyncio
    async def test_closed_day_fires_telegram_only(self):
        from backend.api.algo.actions_live import _action_live_send_market_status
        now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday — both closed
        with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
             patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
             patch("backend.shared.helpers.alert_utils._send_telegram") as mock_tg, \
             patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_ntfy:
            await _action_live_send_market_status({}, {})
        mock_tg.assert_called_once()
        mock_ntfy.assert_not_called()
        tg_msg = mock_tg.call_args.args[0]
        assert "NSE/Equity: CLOSED" in tg_msg
        assert "MCX/Commodity: CLOSED" in tg_msg

    @pytest.mark.asyncio
    async def test_mixed_day_fires_both_ntfy_on_either_open(self):
        """NSE holiday (closed), MCX genuinely open (no holiday) — ntfy
        still fires per the "either segment open" default."""
        from backend.api.algo.actions_live import _action_live_send_market_status
        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        with patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=now), \
             patch("backend.brokers.broker_apis.fetch_holidays",
                   side_effect=_fake_holidays_factory({"NSE"})), \
             patch("backend.brokers.broker_apis.fetch_special_sessions_async", new=AsyncMock(return_value=[])), \
             patch("backend.api.algo.grammar_tokens.action_send_market_status._holiday_reason", AsyncMock(return_value="Diwali")), \
             patch("backend.shared.helpers.alert_utils._send_telegram") as mock_tg, \
             patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_ntfy:
            await _action_live_send_market_status({}, {})
        mock_tg.assert_called_once()
        mock_ntfy.assert_called_once()
        tg_msg = mock_tg.call_args.args[0]
        assert "NSE/Equity: CLOSED (Diwali)" in tg_msg
        assert "MCX/Commodity: OPEN" in tg_msg


class TestActionDispatchWiring:
    def test_registered_in_noop_handlers_not_live_handlers(self):
        from backend.api.algo.actions import _NOOP_ACTION_HANDLERS, _LIVE_ACTION_HANDLERS
        assert "send_market_status" in _NOOP_ACTION_HANDLERS
        assert "send_market_status" not in _LIVE_ACTION_HANDLERS

    def test_catalog_validation_passes(self):
        from backend.api.algo.grammar_registry import validate_action_dispatch_catalog
        validate_action_dispatch_catalog()  # raises on drift

    def test_builtin_agent_present_and_shaped(self):
        """The Agent row is metadata-only (status toggle + /agents
        visibility) — it is NOT the real trigger. `schedule="never"` is
        deliberate: `_task_performance`'s `if not open_segments: continue`
        guard means `run_cycle()` is never reachable at 05:35 IST (always
        before every segment's own open) on ANY day, so this agent could
        never fire via the normal mechanism regardless of schedule value.
        See `background.py:_ds_tick_market_status_report`'s docstring."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agents = [a for a in BUILTIN_AGENTS if a["slug"] == "market-status-report"]
        assert len(agents) == 1
        agent = agents[0]
        assert agent["fire_at_time"] == "05:35"
        assert agent["schedule"] == "never"
        assert agent["tier"] == "info"
        assert agent["actions"] == [{"type": "send_market_status"}]
        # events must NOT include telegram/ntfy — the action handler sends
        # those directly; double-listing them would double-send.
        channels = {e["channel"] for e in agent["events"]}
        assert "telegram" not in channels
        assert "ntfy" not in channels
        assert "log" in channels


def _patch_tick_deps(*, active=True, marker_date=None, idle=False):
    """Standard patch set for `_ds_tick_market_status_report` tests —
    bypasses the DB marker read/write and the dev-idle gate so each test
    isolates the ONE condition it's actually checking."""
    return (
        patch("backend.api.background._market_status_agent_is_active", AsyncMock(return_value=active)),
        patch("backend.api.background._msr_marker_date", AsyncMock(return_value=marker_date)),
        patch("backend.api.background._msr_mark_done", AsyncMock()),
        patch("backend.shared.helpers.utils.is_engine_idle", return_value=idle),
    )


class TestMarketStatusBackgroundTick:
    """The REAL trigger — a dedicated background tick independent of
    `run_cycle`/BUILTIN_AGENTS evaluation. See
    `background.py:_ds_tick_market_status_report`'s docstring for why
    `run_cycle` can never reach this agent (the open_segments gate in
    `_task_performance` is unconditionally empty at 05:35 IST, every
    day, open or closed)."""

    @pytest.mark.asyncio
    async def test_fires_at_0535_regardless_of_segment_state(self):
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 24, 5, 35, tzinfo=IST)  # Saturday — fully closed
        today = now.date()
        done_ref = [None]
        p1, p2, p3, p4 = _patch_tick_deps()
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_awaited_once()
        assert done_ref[0] == today

    @pytest.mark.asyncio
    async def test_no_fire_before_0535(self):
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 5, 34, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        p1, p2, p3, p4 = _patch_tick_deps()
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()
        assert done_ref[0] is None

    @pytest.mark.asyncio
    async def test_no_fire_after_window_closes(self):
        """A late tick (restart, long GC pause, clock skew) landing at
        10:00 — well past the ~09:15 window end — must NOT send an
        hours-stale report."""
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        p1, p2, p3, p4 = _patch_tick_deps()
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_only_once_per_day_in_memory_sentinel(self):
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 6, 0, tzinfo=IST)
        today = now.date()
        done_ref = [today]
        p1, p2, p3, p4 = _patch_tick_deps()
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restart_inside_window_does_not_resend(self):
        """The in-memory `done_ref` box resets to None on every process
        restart — the DB marker is what actually prevents a re-send when
        a second process instance ticks inside the same window on the
        same day."""
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 6, 0, tzinfo=IST)
        today = now.date()
        done_ref = [None]  # fresh box, as if the process just restarted
        p1, p2, p3, p4 = _patch_tick_deps(marker_date=today)  # already fired today
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()
        assert done_ref[0] == today  # seeded so this process doesn't re-check the DB all day

    @pytest.mark.asyncio
    async def test_dev_idle_suppresses_fire(self):
        """Mirrors `_task_performance`'s own `is_engine_idle()` skip —
        this tick isn't gated by that task's loop at all, so it must
        apply the same check itself (`send_ntfy_alert` has no
        idle-suppression of its own, unlike `_send_telegram`)."""
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        p1, p2, p3, p4 = _patch_tick_deps(idle=True)
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paused_agent_does_not_fire(self):
        from backend.api.background import _ds_tick_market_status_report

        now = datetime(2026, 10, 22, 5, 35, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        p1, p2, p3, p4 = _patch_tick_deps(active=False)
        with p1, p2, p3, p4, \
             patch("backend.api.algo.actions_live._action_live_send_market_status", AsyncMock()) as mock_action:
            await _ds_tick_market_status_report(now, today, done_ref)
        mock_action.assert_not_awaited()
        # done_ref is still marked — a paused day doesn't retry later today.
        assert done_ref[0] == today

    @pytest.mark.asyncio
    async def test_agent_status_read_defaults_active_on_db_error(self):
        from backend.api.background import _market_status_agent_is_active

        with patch("backend.api.database.async_session", side_effect=RuntimeError("db down")):
            assert await _market_status_agent_is_active() is True

    @pytest.mark.asyncio
    async def test_agent_status_read_respects_inactive(self):
        from backend.api.background import _market_status_agent_is_active

        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = lambda: "inactive"
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("backend.api.database.async_session", return_value=mock_session):
            assert await _market_status_agent_is_active() is False


class TestMarketStatusWindowEnd:
    def test_default_window_end_is_0900(self):
        from backend.api.background import _market_status_window_end
        assert _market_status_window_end() == dtime(9, 0)

    def test_config_error_falls_back_to_0900(self):
        from backend.api.background import _market_status_window_end
        with patch("backend.shared.helpers.utils.config", {}):
            assert _market_status_window_end() == dtime(9, 0)
