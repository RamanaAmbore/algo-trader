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
        ):
            asyncio.run(_run_close_once(state))
            asyncio.run(_run_close_once(state))

        mock_send.assert_called_once()
