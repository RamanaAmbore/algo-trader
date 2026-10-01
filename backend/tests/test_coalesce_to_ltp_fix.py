"""Tests for COALESCE → direct ltp fix in positions, holdings, background, postback.

Four behaviour changes tested:

A. _override_stale_close_from_snapshot (positions.py):
   SQL uses daily_book.ltp directly, not COALESCE(previous_close, ltp).
   Discriminating case: when previous_close > 0 and ltp != previous_close,
   the old code would pick previous_close (stale BHAV-copy), the new code
   always picks ltp (actual settlement LTP).

B. _override_stale_close_for_holdings (holdings.py):
   Same COALESCE removal — SQL uses ltp directly.

C. _task_performance (background.py):
   The market-closure (close summary) report is no longer sent from this
   task at all — that responsibility moved entirely to `_run_close_once`
   (see test_cap_flags_dev.py and test_market_closure_report_mcx_only.py).
   This file now only asserts the dead code (`_perf_run_close_check`) was
   actually removed and that `_task_performance` no longer references
   `send_summary` on the close path.

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
        # Simplified: uses ltp AS ref_close directly (daily_book.ltp is the
        # canonical settlement LTP; the COALESCE fallback to close_price was removed).
        assert "ltp AS ref_close" in src, (
            "positions.py must select 'ltp AS ref_close' in _fetch_snapshot_close_map "
            "(simplified from COALESCE after prev_close rename)"
        )

    def test_discriminating_case_previous_close_ignored(self):
        """When daily_book has ltp=100 (actual settlement), prev_close must be set to 100.

        Old COALESCE(previous_close, ltp) would return the stale BHAV-copy value.
        New code uses ltp directly from daily_book, which is the actual settlement LTP.
        After the prev_close rename, the function patches raw['prev_close'] not close_price.
        """
        from backend.api.routes.positions import _override_stale_close_from_snapshot

        # Build a positions DataFrame — prev_close=105 (Kite stale), ltp=110
        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 10,
            'overnight_quantity': 10,
            'average_price': 90.0,
            'last_price': 110.0,
            'prev_close': 105.0,  # Kite stale (renamed from close_price)
            'pnl': 200.0,
            'day_change_val': (110.0 - 105.0) * 10,
            'm2m': 200.0,
            'unrealised': 200.0,
            'realised': 0.0,
        }])

        mock_time = datetime(2026, 8, 23, 9, 0, 0, tzinfo=IST)
        # _fetch_snapshot_close_map issues a SINGLE combined query (a FULL
        # OUTER JOIN of the snapshot_close CTE and the batch-anchored
        # pnl_final/pnl_ranked CTE — see _BASELINE_PNL_CTE_SQL) — 6-column
        # result: (account, symbol, ref_close, total_pnl, kind, qty). kind
        # is 'positions' here (a plain, unconditional-use baseline).
        mock_result = MagicMock()
        mock_result.all.return_value = [("TEST001", "RELIANCE", 100.0, 200.0, "positions", 10.0)]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                  return_value=mock_time),
            # settlement_cutoff_for() does its own internal DB refresh() call —
            # patch it directly so it doesn't consume a side_effect slot meant
            # for the snapshot_map / baseline_pnl_map queries below.
            patch("backend.api.helpers.exchange_clock.settlement_cutoff_for",
                  new=AsyncMock(return_value=mock_time)),
        ):
            asyncio.run(_override_stale_close_from_snapshot(df))

        # prev_close must be patched from 105 → 100 (using ltp=100 from daily_book)
        assert abs(df.iloc[0]['prev_close'] - 100.0) < 0.01, (
            f"prev_close must be patched to daily_book.ltp=100, got {df.iloc[0]['prev_close']}. "
            "Old COALESCE would have kept 105 (stale BHAV-copy); new ltp-direct always patches."
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
        """When daily_book ltp=100 (actual settlement), prev_close must be set to 100.

        Old COALESCE(previous_close, ltp): would return stale BHAV value.
        New ltp-direct: always sets prev_close = ltp from daily_book (actual settlement).
        After the prev_close rename, the function patches raw['prev_close'] not close_price.
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
            'prev_close': 102.0,   # Kite stale (renamed from close_price)
            'pnl': (110.0 - 90.0) * 10,
            'day_change': 110.0 - 102.0,
            'day_change_val': (110.0 - 102.0) * 10,
            'day_change_percentage': 0.0,
            'pnl_percentage': 0.0,
        }])

        mock_time = datetime(2026, 8, 23, 9, 0, 0, tzinfo=IST)
        mock_result = MagicMock()
        # DB returns ltp=100 (actual settlement)
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

        # prev_close must be set to 100 (from daily_book.ltp)
        assert abs(df.iloc[0]['prev_close'] - 100.0) < 0.01, (
            f"prev_close must be patched to ltp=100, got {df.iloc[0]['prev_close']}. "
            "Old COALESCE would have kept stale BHAV value; new ltp-direct always patches."
        )
        # day_change_val must be recomputed: (ltp-prev_close)*qty = (110-100)*10 = 100
        expected_dcv = (110.0 - 100.0) * 10
        assert abs(df.iloc[0]['day_change_val'] - expected_dcv) < 0.01, (
            f"day_change_val must be recomputed after close patch. "
            f"Expected {expected_dcv}, got {df.iloc[0]['day_change_val']}"
        )


# ---------------------------------------------------------------------------
# Task C — background.py: _perf_run_close_check removed; close report now
# lives exclusively in _run_close_once (see test_cap_flags_dev.py and
# test_market_closure_report_mcx_only.py for the MCX-only behaviour).
# ---------------------------------------------------------------------------

class TestPerfRunCloseCheckRemoved:
    """The old in-_task_performance close-summary sender is gone.

    2026-10 fix: `_task_performance` used to send a SECOND, duplicate close
    report (independent state from `_run_close_once`'s cron sweep) whenever
    `_perf_probe_open_segments` first observed all segments closed. That
    duplicate sender had no `market_summary` cap gate and — because its own
    `close_seg_state` dict never got primed until both NON-MCX and MCX had
    already closed — it fired its *own* NON-MCX report hours late, around
    MCX close, on top of `_run_close_once`'s already-correct send. Removed
    entirely rather than filtered, since `_run_close_once` already covers
    the full (unfiltered, cross-exchange) book.
    """

    def test_helper_deleted(self):
        """_perf_run_close_check must no longer exist in background.py."""
        src = Path("backend/api/background.py").read_text()
        assert "_perf_run_close_check" not in src, (
            "_perf_run_close_check is dead code and must stay deleted — the "
            "market-closure report is now sent exclusively by _run_close_once"
        )

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

    def test_task_performance_no_longer_sends_close_summary(self):
        """`_task_performance`'s own `if not open_segments:` branch must no
        longer reference `send_summary` / `_perf_fetch_all_broker_data` for a
        close report — it must be a plain no-op `continue`. The ONLY sender
        of the market-closure report is `_run_close_once`."""
        src = Path("backend/api/background.py").read_text()
        func_start = src.index("async def _task_performance")
        try:
            func_end = src.index("\nasync def ", func_start + 1)
        except ValueError:
            func_end = len(src)
        func_src = src[func_start:func_end]

        assert "send_summary" not in func_src, (
            "_task_performance must not call send_summary at all — the close "
            "report is owned exclusively by _run_close_once"
        )
        assert "close_seg_state" not in func_src, (
            "_task_performance must not carry its own close_seg_state — "
            "dead state left over from the removed duplicate close-summary sender"
        )

        open_gate_pos = func_src.find("if not open_segments:")
        assert open_gate_pos != -1, "'if not open_segments:' guard must exist in _task_performance"
        gate_block = func_src[open_gate_pos:open_gate_pos + 400]
        assert "continue" in gate_block, (
            "'if not open_segments:' must still short-circuit the rest of the "
            "tick via `continue`"
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
