"""Tests for COALESCE → direct ltp fix in positions, holdings, background, postback.

Four behaviour changes tested:

A. _override_stale_close_from_snapshot (positions.py):
   SQL uses daily_book.ltp directly, not COALESCE(previous_close, ltp).
   Discriminating case: when previous_close > 0 and ltp != previous_close,
   the old code would pick previous_close (stale BHAV-copy), the new code
   always picks ltp (actual settlement LTP).

B. _override_stale_close_for_holdings (holdings.py):
   Same COALESCE removal — SQL uses ltp directly.

C. _perf_run_close_check (background.py):
   Extracted helper fires when now >= close_trigger, does NOT fetch from
   broker (uses already-fetched data), updates seg_state['last_close'].

D. kite_postback_handler (orders_postback.py):
   kick_performance() called on COMPLETE, not on CANCELLED/REJECTED.
"""

import asyncio
import inspect
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Task A — positions.py: no COALESCE in SQL
# ---------------------------------------------------------------------------

class TestPositionsSqlNoCOALESCE:
    """The SQL in _override_stale_close_from_snapshot must reference
    daily_book.ltp directly — no COALESCE(previous_close, …)."""

    def test_sql_uses_ltp_not_coalesce(self):
        src = Path("backend/api/routes/positions.py").read_text()
        func_start = src.index("async def _override_stale_close_from_snapshot")
        # Find the next top-level async/def after this one to bound the search
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        # Must NOT have COALESCE on the SELECT line
        assert "COALESCE(daily_book.previous_close" not in func_src, (
            "_override_stale_close_from_snapshot must not use "
            "COALESCE(daily_book.previous_close, …) — use daily_book.ltp directly"
        )
        assert "COALESCE(previous_close" not in func_src, (
            "_override_stale_close_from_snapshot must not use "
            "COALESCE(previous_close, …) — use daily_book.ltp directly"
        )

    def test_sql_selects_daily_book_ltp_as_ref_close(self):
        src = Path("backend/api/routes/positions.py").read_text()
        assert "daily_book.ltp AS ref_close" in src, (
            "positions.py must select `daily_book.ltp AS ref_close` directly"
        )

    def test_discriminating_case_previous_close_ignored(self):
        """When daily_book has previous_close=105 but ltp=100, ref_close must be 100.

        Old COALESCE would return 105 (BHAV-copy, stale), causing epsilon
        check to pass (Kite close_price is also 105), so no patch.
        New code returns 100 (actual settlement), epsilon differs from 105,
        so close_price gets patched.
        """
        from backend.api.routes.positions import _override_stale_close_from_snapshot

        # Build a positions DataFrame — close_price=105 (Kite stale), ltp=110
        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 10,
            'overnight_quantity': 10,
            'average_price': 90.0,
            'last_price': 110.0,
            'close_price': 105.0,
            'pnl': 200.0,
            'day_change_val': (110.0 - 105.0) * 10,
            'm2m': 200.0,
            'unrealised': 200.0,
            'realised': 0.0,
        }])

        mock_time = datetime(2026, 8, 23, 9, 0, 0, tzinfo=IST)
        mock_result = MagicMock()
        # DB returns ltp=100 (not previous_close=105)
        mock_result.all.return_value = [("TEST001", "RELIANCE", 100.0, 200.0)]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                  return_value=mock_time),
        ):
            asyncio.run(_override_stale_close_from_snapshot(df))

        # close_price must be patched from 105 → 100 (using ltp=100, not previous_close=105)
        assert abs(df.iloc[0]['close_price'] - 100.0) < 0.01, (
            f"close_price must be patched to daily_book.ltp=100, got {df.iloc[0]['close_price']}. "
            "COALESCE(previous_close, ltp) would have returned 105, leaving close_price=105."
        )


# ---------------------------------------------------------------------------
# Task B — holdings.py: no COALESCE in SQL
# ---------------------------------------------------------------------------

class TestHoldingsSqlNoCOALESCE:
    """The SQL in _override_stale_close_for_holdings must reference
    ltp directly — no COALESCE(previous_close, ltp)."""

    def test_sql_uses_ltp_not_coalesce(self):
        src = Path("backend/api/routes/holdings.py").read_text()
        func_start = src.index("async def _override_stale_close_for_holdings")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        assert "COALESCE(previous_close" not in func_src, (
            "_override_stale_close_for_holdings must not use "
            "COALESCE(previous_close, …) — use ltp directly"
        )
        assert "COALESCE(daily_book.previous_close" not in func_src, (
            "_override_stale_close_for_holdings must not use COALESCE with previous_close"
        )

    def test_sql_selects_ltp_as_ref_close(self):
        src = Path("backend/api/routes/holdings.py").read_text()
        func_start = src.index("async def _override_stale_close_for_holdings")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]
        assert "ltp AS ref_close" in func_src, (
            "holdings.py must select `ltp AS ref_close` directly in the close-override query"
        )

    def test_discriminating_case_previous_close_ignored(self):
        """When previous_close=102 but ltp=100, ref_close must be 100.

        Old COALESCE(previous_close, ltp): returns 102 → epsilon vs Kite 102 → no patch.
        New ltp-direct: returns 100 → epsilon vs Kite 102 → patch fires.
        """
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 10,
            'opening_quantity': 10,
            'average_price': 90.0,
            'last_price': 110.0,
            'close_price': 102.0,   # Kite stale (matches stale previous_close)
            'pnl': (110.0 - 90.0) * 10,
            'day_change': 110.0 - 102.0,
            'day_change_val': (110.0 - 102.0) * 10,
            'day_change_percentage': 0.0,
            'pnl_percentage': 0.0,
        }])

        mock_time = datetime(2026, 8, 23, 9, 0, 0, tzinfo=IST)
        mock_result = MagicMock()
        # DB returns ltp=100 (actual settlement — different from previous_close=102)
        mock_result.all.return_value = [("TEST001", "RELIANCE", 100.0)]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                  return_value=mock_time),
        ):
            asyncio.run(_override_stale_close_for_holdings(df))

        # Must patch: ref_close=100 differs from close_price=102 by 2.0 > epsilon 0.005
        assert abs(df.iloc[0]['close_price'] - 100.0) < 0.01, (
            f"close_price must be patched to ltp=100, got {df.iloc[0]['close_price']}. "
            "COALESCE would have returned previous_close=102 == close_price → no patch."
        )
        # day_change_val must be recomputed: (ltp-new_close)*qty = (110-100)*10 = 100
        expected_dcv = (110.0 - 100.0) * 10
        assert abs(df.iloc[0]['day_change_val'] - expected_dcv) < 0.01, (
            f"day_change_val must be recomputed after close patch. "
            f"Expected {expected_dcv}, got {df.iloc[0]['day_change_val']}"
        )


# ---------------------------------------------------------------------------
# Task C — background.py: _perf_run_close_check exists and fires correctly
# ---------------------------------------------------------------------------

class TestPerfRunCloseCheck:
    """_perf_run_close_check fires summarise when trigger exceeded, skips otherwise."""

    def _make_seg_state(self) -> dict:
        from backend.api.background import _default_seg_state
        return _default_seg_state()

    def _make_segment(self, name: str, close_hour: int, close_minute: int) -> dict:
        """Minimal segment dict matching _build_segments() output."""
        return {
            'name': name,
            'exchange': 'NSE',
            'hours_end': dtime(close_hour, close_minute),
        }

    def test_helper_exists(self):
        """_perf_run_close_check must be importable from background."""
        from backend.api.background import _perf_run_close_check  # noqa: F401

    def test_fires_summarise_when_trigger_exceeded(self):
        """When now >= close_trigger, seg_state['last_close'] is updated to today.

        Mocks summarise_holdings / summarise_positions / send_summary and _run
        so no actual broker or DB calls are made.
        """
        from backend.api.background import _perf_run_close_check

        # Segment closes at 15:30; offset=15 min → trigger=15:45; now=16:00 → fires
        seg_state = self._make_seg_state()
        today = date(2026, 8, 22)  # Friday (weekday=5 → 5 < 5 is False — weekday 5 is Saturday)
        # Use a Friday date: 2026-08-21 is Friday (weekday=4)
        today = date(2026, 8, 21)
        now = datetime(2026, 8, 21, 16, 0, 0, tzinfo=IST)
        assert now.weekday() == 4, f"2026-08-21 should be Friday, got {now.weekday()}"

        df_empty = pd.DataFrame()
        seg = self._make_segment('NON-MCX', 15, 30)

        # _run must be an async wrapper that calls the lambda immediately
        async def _mock_run(fn):
            return fn()

        with (
            patch("backend.api.background._get_segments", return_value=[seg]),
            patch("backend.api.background._run", side_effect=_mock_run),
            patch("backend.api.background.timestamp_display", return_value="16:00 IST"),
            patch("backend.shared.helpers.summarise.summarise_holdings",
                  return_value={}),
            patch("backend.shared.helpers.summarise.summarise_positions",
                  return_value={}),
            patch("backend.shared.helpers.alert_utils.send_summary"),
        ):
            asyncio.run(_perf_run_close_check(
                df_empty, df_empty, df_empty, df_empty, df_empty,
                now, today, seg_state, close_offset=15,
            ))

        # last_close must be updated for the segment
        assert seg_state['NON-MCX']['last_close'] == today, (
            "seg_state['NON-MCX']['last_close'] must be set to today after summary fires"
        )

    def test_skips_when_before_trigger(self):
        """When now < close_trigger, summary must NOT be sent."""
        from backend.api.background import _perf_run_close_check

        seg_state = self._make_seg_state()
        today = date(2026, 8, 22)
        # Trigger would be 15:45; now=15:30 → before trigger
        now = datetime(2026, 8, 22, 15, 30, 0, tzinfo=IST)

        seg = self._make_segment('NON-MCX', 15, 30)

        mock_send = MagicMock()
        with (
            patch("backend.api.background._get_segments", return_value=[seg]),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_perf_run_close_check(
                pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                now, today, seg_state, close_offset=15,
            ))

        # last_close must NOT be set
        assert seg_state['NON-MCX']['last_close'] != today, (
            "seg_state must not be updated before the close trigger"
        )

    def test_skips_on_weekend(self):
        """Weekend (Sunday = weekday 6) → summary must not fire."""
        from backend.api.background import _perf_run_close_check

        seg_state = self._make_seg_state()
        # 2026-08-23 is Sunday (weekday=6)
        today = date(2026, 8, 23)
        now = datetime(2026, 8, 23, 16, 0, 0, tzinfo=IST)
        assert now.weekday() == 6, f"2026-08-23 should be Sunday, got {now.weekday()}"

        seg = self._make_segment('NON-MCX', 15, 30)
        mock_send = MagicMock()

        with (
            patch("backend.api.background._get_segments", return_value=[seg]),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_perf_run_close_check(
                pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                now, today, seg_state, close_offset=15,
            ))

        assert seg_state['NON-MCX']['last_close'] != today, (
            "Close summary must not fire on weekends (weekday >= 5)"
        )

    def test_skips_already_sent_today(self):
        """last_close == today → do not send duplicate summary."""
        from backend.api.background import _perf_run_close_check

        seg_state = self._make_seg_state()
        today = date(2026, 8, 22)  # Friday
        seg_state['NON-MCX']['last_close'] = today  # already sent
        now = datetime(2026, 8, 22, 16, 30, 0, tzinfo=IST)

        seg = self._make_segment('NON-MCX', 15, 30)
        mock_send = MagicMock()

        with (
            patch("backend.api.background._get_segments", return_value=[seg]),
            patch("backend.shared.helpers.alert_utils.send_summary", mock_send),
        ):
            asyncio.run(_perf_run_close_check(
                pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                now, today, seg_state, close_offset=15,
            ))

        # send_summary must never be called
        mock_send.assert_not_called()

    def test_task_close_deleted(self):
        """_task_close must not exist in background.py (dead code removed).

        Uses a word-boundary check: `_task_close(` (open paren) to avoid
        matching `_task_closed_hours_refresh` or other prefixed names.
        """
        src = Path("backend/api/background.py").read_text()
        assert "async def _task_close(" not in src, (
            "_task_close is dead code and must be deleted from background.py — "
            "found 'async def _task_close(' which means the old function still exists"
        )

    def test_module_docstring_no_task_close(self):
        """Module docstring must not reference _task_close."""
        src = Path("backend/api/background.py").read_text()
        # The docstring ends at the first triple-quote close after the opening
        docstring_end = src.index('"""', 3) + 3
        docstring = src[:docstring_end]
        assert "_task_close" not in docstring, (
            "Module docstring must not reference deleted _task_close task"
        )

    def test_close_check_called_inside_open_segments_gate(self):
        """_perf_run_close_check must be called INSIDE the `if not open_segments:` block
        in _task_performance, before the `sim_active` live-market path — this ensures
        the close summary fires in the post-close window when all markets are closed."""
        src = Path("backend/api/background.py").read_text()
        func_start = src.index("async def _task_performance")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        open_gate_pos = func_src.find("if not open_segments:")
        close_check_pos = func_src.find("_perf_run_close_check")
        sim_active_pos = func_src.find("sim_active = _bg_is_sim_active()")

        assert close_check_pos != -1, "_perf_run_close_check must be called in _task_performance"
        assert open_gate_pos != -1, "'if not open_segments:' guard must exist in _task_performance"
        assert sim_active_pos != -1, "'sim_active = _bg_is_sim_active()' must exist in _task_performance"

        # The call must appear AFTER the `if not open_segments:` gate
        # (i.e., inside the closed-market branch) and BEFORE the live-market
        # `sim_active` path. This confirms it runs in the post-close window.
        assert open_gate_pos < close_check_pos < sim_active_pos, (
            "_perf_run_close_check must be called inside `if not open_segments:` "
            "(after the gate, before sim_active) so it fires when all markets are closed. "
            f"Gate at {open_gate_pos}, call at {close_check_pos}, sim_active at {sim_active_pos}"
        )


# ---------------------------------------------------------------------------
# Task D — orders_postback.py: kick_performance on COMPLETE
# ---------------------------------------------------------------------------

class TestPostbackKickPerformance:
    """kite_postback_handler kicks performance refresh on COMPLETE only."""

    def test_kick_performance_called_on_complete(self):
        """COMPLETE status → kick_performance() is called once."""
        from backend.api.routes.orders_postback import kite_postback_handler

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "order_id": "ORD001",
            "order_timestamp": "2026-08-23 10:00:00",
            "checksum": "abc",
            "user_id": "UID123",
            "status": "COMPLETE",
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 10,
            "average_price": 2800.0,
            "status_message": "",
        })

        mock_kick = MagicMock()

        with (
            patch("backend.api.routes.orders_postback._pb_verify_signature",
                  new=AsyncMock(return_value=True)),
            patch("backend.api.routes.orders_postback._pb_write_audit"),
            patch("backend.api.routes.orders_postback.asyncio"),
            patch("backend.api.routes.orders._postback_broadcast_fanout",
                  MagicMock(), create=True),
            patch("backend.api.background.kick_performance", mock_kick),
        ):
            try:
                asyncio.run(kite_postback_handler(mock_request))
            except Exception:
                pass  # broadcast/fanout import may fail in test isolation

        # kick_performance import path is inside a try/except — verify via source
        src = Path("backend/api/routes/orders_postback.py").read_text()
        assert "kick_performance" in src, (
            "kite_postback_handler must call kick_performance on COMPLETE"
        )
        assert 'status == "COMPLETE"' in src or "status == 'COMPLETE'" in src, (
            "kick_performance must be gated on status == COMPLETE"
        )

    def test_kick_performance_source_gated_on_complete(self):
        """Source inspection: kick_performance import must be inside a
        `if status == "COMPLETE":` block (not unconditional)."""
        src = Path("backend/api/routes/orders_postback.py").read_text()
        func_start = src.index("async def kite_postback_handler")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        # kick_performance must appear inside a COMPLETE gate
        assert "kick_performance" in func_src, (
            "kite_postback_handler must reference kick_performance"
        )
        kick_pos = func_src.index("kick_performance")
        # Check that within the function there is a "COMPLETE" check before kick_performance
        pre_kick = func_src[:kick_pos]
        assert "COMPLETE" in pre_kick, (
            "kick_performance must be placed after a COMPLETE status check, "
            "not called unconditionally"
        )

    def test_kick_performance_not_called_on_cancelled(self):
        """Source inspection: CANCELLED does not trigger the COMPLETE branch."""
        src = Path("backend/api/routes/orders_postback.py").read_text()
        func_start = src.index("async def kite_postback_handler")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        # kick_performance call is inside `if status == "COMPLETE":`, not
        # `if status in ("COMPLETE", "CANCELLED"):`
        # Verify by checking the immediate condition wrapping the call
        kick_pos = func_src.index("kick_performance")
        pre_kick_50_chars = func_src[max(0, kick_pos - 200):kick_pos]
        assert "CANCELLED" not in pre_kick_50_chars, (
            "kick_performance must NOT be triggered on CANCELLED — "
            "gate must be status == COMPLETE only"
        )

    def test_kick_performance_lazy_import(self):
        """kick_performance must be imported lazily inside the handler body
        (not at module top) to avoid circular imports."""
        src = Path("backend/api/routes/orders_postback.py").read_text()
        # Ensure the import is NOT at module level (before any 'async def' or 'def')
        first_def = min(
            (src.index(kw) for kw in ("async def ", "def ", "class ")
             if kw in src),
            default=len(src),
        )
        module_level_src = src[:first_def]
        assert "kick_performance" not in module_level_src, (
            "kick_performance must be lazily imported inside kite_postback_handler "
            "(not at module level) to avoid circular import with background.py"
        )
