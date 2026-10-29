"""Market-closure report must fire only after MCX close, never after the
NON-MCX (equity) close.

Operator instruction (2026-10): "remove market closure report after
non-mcx close. generate it only after mcx close."

Root cause found during investigation: TWO independent code paths used to
send a "close" summary —
  1. `_run_close_once` (polled every 30s by `_task_post_market_cron`, the
     documented post-market consolidator, gated on `is_enabled('market_summary')`)
  2. `_perf_run_close_check` (called from `_task_performance`'s own
     `if not open_segments:` branch, ungated, with its own independent
     `close_seg_state` dict)

Both looped over every segment returned by `_get_segments()` — NON-MCX
(equity, closes ~15:30 IST) and MCX (closes ~23:30 IST) — and sent a
"close" summary for EACH, meaning up to four close reports were sent per
trading day (two duplicate NON-MCX sends around 15:45 IST, two duplicate
MCX sends around 23:45 IST). Confirmed in prod logs
(`Background: close summary sent for NON-MCX` + `Background[cron]: close
summary sent for NON-MCX` both appearing on the same day).

Fix: `_perf_run_close_check` and its call site were deleted entirely
(dead/duplicate sender — `_run_close_once` already pulls the full,
unfiltered cross-exchange book, so nothing was lost). `_run_close_once`
now skips every segment whose name isn't 'MCX' before any broker fetch or
send, so the market-closure report fires exactly once per day, tied to
the MCX close trigger (hours_end + close_summary_offset_min), reflecting
the complete day's activity across both NSE and MCX.

Holiday-aware gate (2026-10-28 operator instruction: "even close summary
should happen only on market open day") — the old gate was a bare
`now.weekday() < 5`, with no holiday-calendar lookup at all, so a weekday
MCX holiday fired the close summary anyway. `_run_close_once` now calls
`_market_status_for_segment("commodity", now)` (the same calendar-only
resolver the 05:35 IST market-status report uses) before doing any
broker fetch or send, and skips (marking `last_close` handled) when that
resolver says the commodity segment did not trade today. See
`TestRunCloseOnceHolidayGate` below.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

NON_MCX_SEG = {"name": "NON-MCX", "exchange": "NSE", "hours_end": dtime(15, 30)}
MCX_SEG = {"name": "MCX", "exchange": "MCX", "hours_end": dtime(23, 30)}


class TestRunCloseOnceMcxOnly:
    """`_run_close_once` — the sole remaining close-summary sender."""

    def test_non_mcx_trigger_sends_nothing(self):
        """At 16:00 IST (well past the NON-MCX 15:30+15min trigger, MCX still
        open), no close report is sent and no broker fetch happens at all —
        the NON-MCX segment is skipped before any fetch/send."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 16, 0, 0, tzinfo=IST)  # Friday
        assert now.weekday() == 4
        state = {"close_seg_state": _default_seg_state()}

        mock_send = MagicMock()
        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.api.background._fetch_holdings_direct") as mock_h,
            patch("backend.api.background._fetch_positions_direct") as mock_p,
            patch("backend.api.background._fetch_margins_direct") as mock_m,
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_not_called()
        mock_h.assert_not_called()
        mock_p.assert_not_called()
        mock_m.assert_not_called()
        assert state["close_seg_state"]["NON-MCX"]["last_close"] != now.date(), (
            "NON-MCX must never be marked closed by _run_close_once — it is "
            "skipped unconditionally before any bookkeeping"
        )

    def test_mcx_trigger_sends_exactly_once(self):
        """At 23:50 IST (past the MCX 23:30+15min trigger), the close report
        fires exactly once, with msg_type='close'."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 23, 50, 0, tzinfo=IST)  # Friday
        assert now.weekday() == 4
        state = {"close_seg_state": _default_seg_state()}

        df_empty = pd.DataFrame()
        mock_send = MagicMock()

        async def _mock_run(fn, *a):
            return fn(*a)

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.api.background.timestamp_display", return_value="23:50 IST"),
            patch("backend.api.background._run", side_effect=_mock_run),
            patch("backend.api.background._fetch_holdings_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_positions_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_margins_direct",
                  return_value=df_empty),
            patch("backend.api.routes.positions._override_stale_close_from_snapshot",
                  new=AsyncMock()),
            patch("backend.api.background._rebuild_positions_summary",
                  return_value=df_empty),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
            # Hermetic trading-day gate: a genuine Friday with no holiday/
            # special-session row → "is a trading day" → proceeds to send.
            patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
            patch("backend.brokers.broker_apis.fetch_special_sessions_async",
                  new=AsyncMock(return_value=[])),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        # send_summary(sh, sp, ist_display, 'close', label=..., df_margins=..., df_positions=...)
        assert args[3] == "close"
        assert state["close_seg_state"]["MCX"]["last_close"] == now.date()

    def test_mcx_trigger_does_not_resend_same_day(self):
        """A second call on the same day must not resend — idempotency via
        close_seg_state['MCX']['last_close']."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 23, 50, 0, tzinfo=IST)  # Friday
        state = {"close_seg_state": _default_seg_state()}

        df_empty = pd.DataFrame()
        mock_send = MagicMock()

        async def _mock_run(fn, *a):
            return fn(*a)

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.api.background.timestamp_display", return_value="23:50 IST"),
            patch("backend.api.background._run", side_effect=_mock_run),
            patch("backend.api.background._fetch_holdings_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_positions_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_margins_direct",
                  return_value=df_empty),
            patch("backend.api.routes.positions._override_stale_close_from_snapshot",
                  new=AsyncMock()),
            patch("backend.api.background._rebuild_positions_summary",
                  return_value=df_empty),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
            patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
            patch("backend.brokers.broker_apis.fetch_special_sessions_async",
                  new=AsyncMock(return_value=[])),
        ):
            asyncio.run(_run_close_once(state))
            asyncio.run(_run_close_once(state))

        mock_send.assert_called_once()


class TestRunCloseOnceHolidayGate:
    """2026-10-28 operator instruction: "even close summary should happen
    only on market open day." The old gate was a bare `now.weekday() < 5`
    — no holiday-calendar lookup — so a weekday MCX holiday fired the
    close summary anyway. `_run_close_once` now gates on
    `_market_status_for_segment("commodity", now)` before any broker
    fetch or send.
    """

    # Both tests below drive the REAL `_market_status_for_segment`
    # resolver (patching only its own sub-fetches, `fetch_holidays` and
    # `fetch_special_sessions_async`) rather than mocking the resolver's
    # return value directly — mocking the resolver itself would only
    # prove the gate obeys whatever verdict it's handed, not that a
    # genuine holiday calendar produces the right verdict in the first
    # place (in particular, the evening-open-holiday case below is only
    # a real test of `evening_open_on_holidays` handling when the real
    # resolver runs end to end).

    def test_confirmed_mcx_full_holiday_sends_nothing(self):
        """A weekday that is a CONFIRMED full MCX holiday — a zero-width
        `market_special_sessions` override row for today (the shape
        `_upsert_mcx_evening_closed_sessions_coro` auto-inserts for a
        COM-listed date whose evening_session is also "Closed") — must
        not fetch or send.

        This is the regression test for the fix: against the pre-fix
        code (a bare `now.weekday() < 5` check with no holiday lookup at
        all) this assertion fails, because the old code sends
        unconditionally on any weekday past the close trigger,
        irrespective of the holiday calendar. Confirmed empirically by
        stashing the background.py fix and re-running this exact test —
        it failed (attempted the broker fetch) against the pre-fix code.
        """
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 23, 50, 0, tzinfo=IST)  # Friday
        assert now.weekday() == 4
        today = now.date()
        state = {"close_seg_state": _default_seg_state()}

        mock_send = MagicMock()
        zero_width_row = {"date": today, "start": dtime(0, 0), "end": dtime(0, 0)}

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.brokers.broker_apis.fetch_holidays", return_value={today}),
            patch("backend.brokers.broker_apis.fetch_special_sessions_async",
                  new=AsyncMock(return_value=[zero_width_row])),
            patch("backend.api.background._fetch_holdings_direct") as mock_h,
            patch("backend.api.background._fetch_positions_direct") as mock_p,
            patch("backend.api.background._fetch_margins_direct") as mock_m,
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_not_called()
        mock_h.assert_not_called()
        mock_p.assert_not_called()
        mock_m.assert_not_called()
        # Skip is marked handled so the calendar isn't re-evaluated on
        # every subsequent 30s tick for the rest of the day.
        assert state["close_seg_state"]["MCX"]["last_close"] == today

    def test_mcx_evening_open_holiday_still_sends(self):
        """An MCX evening-open holiday — a COM-listed date in
        `fetch_holidays("MCX")` with NO zero-width override row (the
        real-world shape of a day where only the morning session was
        holiday-closed; evening trades 17:00-23:30 per
        `evening_open_on_holidays: true` in the commodity YAML block) —
        must still send. This exercises the REAL
        `evening_open_on_holidays` branch inside `_market_status_for_
        segment` end to end — the gate must not re-derive a stricter
        (wrong) verdict of its own from a bare holiday-set lookup."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 23, 50, 0, tzinfo=IST)  # Friday
        today = now.date()
        state = {"close_seg_state": _default_seg_state()}

        df_empty = pd.DataFrame()
        mock_send = MagicMock()

        async def _mock_run(fn, *a):
            return fn(*a)

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            # Real holiday for MCX today, NO special-session override row
            # — forces the resolver through its evening_open_on_holidays
            # branch rather than the Special-closure or plain-weekday ones.
            patch("backend.brokers.broker_apis.fetch_holidays", return_value={today}),
            patch("backend.brokers.broker_apis.fetch_special_sessions_async",
                  new=AsyncMock(return_value=[])),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.api.background.timestamp_display", return_value="23:50 IST"),
            patch("backend.api.background._run", side_effect=_mock_run),
            patch("backend.api.background._fetch_holdings_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_positions_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_margins_direct",
                  return_value=df_empty),
            patch("backend.api.routes.positions._override_stale_close_from_snapshot",
                  new=AsyncMock()),
            patch("backend.api.background._rebuild_positions_summary",
                  return_value=df_empty),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_called_once()
        assert state["close_seg_state"]["MCX"]["last_close"] == now.date()

    _PATCH_TARGET = (
        "backend.api.algo.grammar_tokens.action_send_market_status"
        "._market_status_for_segment"
    )

    def test_trading_day_check_failure_fails_open_and_sends(self):
        """If the trading-day resolver itself raises (DB/network hiccup —
        simulated here by mocking the resolver directly, since its own
        internal sub-fetches already fail-open to empty/[] and can't be
        made to propagate an exception through it), the gate fails OPEN
        (sends anyway) rather than silently losing the one report of the
        day."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 21, 23, 50, 0, tzinfo=IST)  # Friday
        state = {"close_seg_state": _default_seg_state()}

        df_empty = pd.DataFrame()
        mock_send = MagicMock()
        mock_status = AsyncMock(side_effect=RuntimeError("DB unavailable"))

        async def _mock_run(fn, *a):
            return fn(*a)

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.api.background.timestamp_display", return_value="23:50 IST"),
            patch("backend.api.background._run", side_effect=_mock_run),
            patch(self._PATCH_TARGET, mock_status),
            patch("backend.api.background._fetch_holdings_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_positions_direct",
                  return_value=(df_empty, df_empty)),
            patch("backend.api.background._fetch_margins_direct",
                  return_value=df_empty),
            patch("backend.api.routes.positions._override_stale_close_from_snapshot",
                  new=AsyncMock()),
            patch("backend.api.background._rebuild_positions_summary",
                  return_value=df_empty),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_called_once()
        assert state["close_seg_state"]["MCX"]["last_close"] == now.date()

    def test_weekend_still_sends_nothing(self):
        """A Saturday must still be skipped (the resolver's own weekend
        branch), preserving the old weekday-only behaviour for the common
        case."""
        from backend.api.background import _run_close_once, _default_seg_state

        now = datetime(2026, 8, 22, 23, 50, 0, tzinfo=IST)  # Saturday
        assert now.weekday() == 5
        state = {"close_seg_state": _default_seg_state()}

        mock_send = MagicMock()

        with (
            patch("backend.shared.helpers.utils.is_enabled", return_value=True),
            patch("backend.api.background._get_segments",
                  return_value=[NON_MCX_SEG, MCX_SEG]),
            patch("backend.api.background.timestamp_indian", return_value=now),
            patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
            patch("backend.brokers.broker_apis.fetch_special_sessions_async",
                  new=AsyncMock(return_value=[])),
            patch("backend.api.background._fetch_holdings_direct") as mock_h,
            patch("backend.api.background._fetch_positions_direct") as mock_p,
            patch("backend.api.background._fetch_margins_direct") as mock_m,
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_run_close_once(state))

        mock_send.assert_not_called()
        mock_h.assert_not_called()
        mock_p.assert_not_called()
        mock_m.assert_not_called()
        assert state["close_seg_state"]["MCX"]["last_close"] == now.date()
