"""Tests for previous_close immutability fixes in daily_snapshot.py and exchange_clock.py.

Covers five quality dimensions:
  SSOT        — UPSERT SQL preserves previous_close; writer stores None not ltp
  Correctness — _TODAY_NSE_OPEN set correctly for normal/holiday, fix returns 0 on holiday
  Performance — holiday no-op: fix_daily_book_prev_close returns 0 without DB call
  Reuse       — _effective_gate_rows used directly in load_today_open_time (not filtered getter)
  UX          — correct previous_close prevents wrong day P&L display

Test catalogue:
  1. UPSERT immutability — previous_close = daily_book.previous_close pattern in SQL
  2. Writer None fallback — holdings writer stores None when prev_ltp_map empty and close_price=0
  3. load_today_open_time — normal session → time(8, 0)
  4. load_today_open_time — holiday override → None
  5. fix_daily_book_prev_close — holiday no-op (returns 0, no DB call)
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. UPSERT immutability — SQL text check
# ---------------------------------------------------------------------------

class TestUpsertImmutability:
    """UPSERT SQL must preserve previous_close — no rolling-shift."""

    def test_upsert_sql_preserves_previous_close(self):
        """The ON CONFLICT clause must use `previous_close = daily_book.previous_close`
        and must NOT contain the old CASE expression that overwrites it on ltp change."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_text = str(_UPSERT_SQL)

        # Immutable preserve pattern must be present.
        assert "previous_close = daily_book.previous_close" in sql_text, (
            "UPSERT SQL must preserve previous_close unchanged on update"
        )

        # Old rolling-shift CASE expression must NOT be present.
        assert "THEN daily_book.ltp ELSE daily_book.previous_close END" not in sql_text, (
            "Rolling-shift CASE expression was removed — should not be present"
        )

    def test_upsert_sql_still_updates_ltp(self):
        """ltp update clause must still be present — it updates freely."""
        from backend.api.algo.daily_snapshot import _UPSERT_SQL

        sql_text = str(_UPSERT_SQL)
        assert "COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp)" in sql_text, (
            "ltp COALESCE update must remain untouched"
        )


# ---------------------------------------------------------------------------
# 2. Writer None fallback — holdings _holdings_rows
# ---------------------------------------------------------------------------

class TestHoldingsWriterNoneFallback:
    """_holdings_rows must store None for previous_close when both
    prev_ltp_map and close_price are unavailable — never fall back to ltp."""

    def _make_holding_row(self, close_price=0.0, ltp=407.5) -> dict:
        """Minimal broker holding dict."""
        return {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "opening_quantity": 10,
            "average_price": 350.0,
            "last_price": ltp,
            "close_price": close_price,
            "day_change": 0.0,
            "day_change_percentage": 0.0,
            "pnl": 0.0,
        }

    def test_holdings_previous_close_none_when_no_prev_ltp_and_no_close_price(self):
        """With empty prev_ltp_map and close_price=0, previous_close must be None."""
        from datetime import date
        from backend.api.algo.daily_snapshot import _holdings_rows

        row = self._make_holding_row(close_price=0.0, ltp=407.5)
        now_ist = datetime(2026, 8, 30, 9, 0, tzinfo=_IST)

        # Patch ltp lookup so no live broker call is needed
        with patch("backend.api.algo.daily_snapshot._is_exchange_open_at", return_value=False):
            with patch("backend.api.algo.daily_snapshot._snap_holding_eod_vals",
                       return_value=(407.5, 0.0, 0.0)):
                rows = _holdings_rows(
                    account="ZG0790",
                    target_date=date(2026, 8, 30),
                    raw=[row],
                    now_ist=now_ist,
                    settled=True,
                    market_open=False,
                    prev_ltp_map={},   # empty — no prior daily_book row
                )

        assert len(rows) == 1, "Expected one holdings row"
        prev_close = rows[0]["previous_close"]
        assert prev_close is None, (
            f"previous_close should be None when prev_ltp_map empty and close_price=0, "
            f"got {prev_close!r} instead"
        )

    def test_holdings_previous_close_uses_prev_ltp_map_when_present(self):
        """When prev_ltp_map has a value, it must be used as previous_close."""
        from datetime import date
        from backend.api.algo.daily_snapshot import _holdings_rows

        row = self._make_holding_row(close_price=0.0, ltp=407.5)
        now_ist = datetime(2026, 8, 30, 9, 0, tzinfo=_IST)
        prev_ltp_map = {("ZG0790", "RELIANCE", "holdings"): 374.10}

        with patch("backend.api.algo.daily_snapshot._is_exchange_open_at", return_value=False):
            with patch("backend.api.algo.daily_snapshot._snap_holding_eod_vals",
                       return_value=(407.5, 0.0, 0.0)):
                rows = _holdings_rows(
                    account="ZG0790",
                    target_date=date(2026, 8, 30),
                    raw=[row],
                    now_ist=now_ist,
                    settled=True,
                    market_open=False,
                    prev_ltp_map=prev_ltp_map,
                )

        assert len(rows) == 1
        assert rows[0]["previous_close"] == pytest.approx(374.10), (
            "previous_close must use prev_ltp_map value when available"
        )

    def test_holdings_previous_close_uses_close_price_fallback(self):
        """When prev_ltp_map empty but broker close_price is non-zero, use it."""
        from datetime import date
        from backend.api.algo.daily_snapshot import _holdings_rows

        row = self._make_holding_row(close_price=374.10, ltp=407.5)
        now_ist = datetime(2026, 8, 30, 9, 0, tzinfo=_IST)

        with patch("backend.api.algo.daily_snapshot._is_exchange_open_at", return_value=False):
            with patch("backend.api.algo.daily_snapshot._snap_holding_eod_vals",
                       return_value=(407.5, 0.0, 0.0)):
                rows = _holdings_rows(
                    account="ZG0790",
                    target_date=date(2026, 8, 30),
                    raw=[row],
                    now_ist=now_ist,
                    settled=True,
                    market_open=False,
                    prev_ltp_map={},
                )

        assert len(rows) == 1
        assert rows[0]["previous_close"] == pytest.approx(374.10), (
            "previous_close must use broker close_price when prev_ltp_map is empty"
        )


# ---------------------------------------------------------------------------
# 3. load_today_open_time — normal session
# ---------------------------------------------------------------------------

class TestLoadTodayOpenTimeNormal:
    """load_today_open_time sets _TODAY_NSE_OPEN to row open_time on a normal trading day."""

    async def test_normal_session_sets_open_time(self):
        """NON-MCX row with open_time=time(8,0) → get_nse_open_time() returns time(8, 0)."""
        from backend.api.helpers import exchange_clock

        # Build a mock NON-MCX session row
        mock_row = MagicMock()
        mock_row.gate = "NON-MCX"
        mock_row.date = None   # default row
        mock_row.open_time = time(8, 0)
        mock_row.close_time = time(15, 30)
        mock_row.weekdays = [0, 1, 2, 3, 4]
        mock_row.exchanges = ["NSE", "BSE", "NFO", "BFO", "CDS"]

        with patch.object(exchange_clock, "_CACHE", [mock_row]):
            # Patch refresh so no DB call is made
            with patch.object(exchange_clock, "refresh", new=AsyncMock()):
                # Patch _effective_gate_rows to return the mock row
                with patch.object(exchange_clock, "_effective_gate_rows",
                                   return_value=[mock_row]):
                    await exchange_clock.load_today_open_time()

        result = exchange_clock.get_nse_open_time()
        assert result == time(8, 0), (
            f"Normal session: expected time(8, 0), got {result!r}"
        )

    async def test_custom_open_time_preserved(self):
        """Special session with open_time=time(9, 15) is stored correctly."""
        from backend.api.helpers import exchange_clock

        mock_row = MagicMock()
        mock_row.gate = "NON-MCX"
        mock_row.date = None
        mock_row.open_time = time(9, 15)

        with patch.object(exchange_clock, "refresh", new=AsyncMock()):
            with patch.object(exchange_clock, "_effective_gate_rows",
                               return_value=[mock_row]):
                await exchange_clock.load_today_open_time()

        assert exchange_clock.get_nse_open_time() == time(9, 15)


# ---------------------------------------------------------------------------
# 4. load_today_open_time — holiday (open_time=None)
# ---------------------------------------------------------------------------

class TestLoadTodayOpenTimeHoliday:
    """load_today_open_time sets _TODAY_NSE_OPEN to None on a holiday."""

    async def test_holiday_override_sets_none(self):
        """Holiday override row with open_time=None → get_nse_open_time() returns None."""
        from backend.api.helpers import exchange_clock

        mock_row = MagicMock()
        mock_row.gate = "NON-MCX"
        mock_row.date = None
        mock_row.open_time = None   # holiday

        with patch.object(exchange_clock, "refresh", new=AsyncMock()):
            with patch.object(exchange_clock, "_effective_gate_rows",
                               return_value=[mock_row]):
                await exchange_clock.load_today_open_time()

        result = exchange_clock.get_nse_open_time()
        assert result is None, (
            f"Holiday override: expected None, got {result!r}"
        )

    async def test_no_rows_falls_back_to_default(self):
        """Empty _effective_gate_rows (no schedule config) → defaults to time(8, 0)."""
        from backend.api.helpers import exchange_clock

        with patch.object(exchange_clock, "refresh", new=AsyncMock()):
            with patch.object(exchange_clock, "_effective_gate_rows",
                               return_value=[]):
                await exchange_clock.load_today_open_time()

        result = exchange_clock.get_nse_open_time()
        assert result == time(8, 0), (
            f"No rows: expected default time(8, 0), got {result!r}"
        )


# ---------------------------------------------------------------------------
# 5. fix_daily_book_prev_close — holiday no-op
# ---------------------------------------------------------------------------

class TestFixDailyBookPrevCloseHolidayNoop:
    """fix_daily_book_prev_close must return 0 immediately on a holiday
    without making any DB calls."""

    async def test_holiday_returns_zero_no_db_call(self):
        """When get_nse_open_time() returns None, fix returns 0 and skips DB."""
        from backend.api.algo import daily_snapshot
        from backend.api.helpers import exchange_clock

        now_ist = datetime(2026, 8, 30, 10, 0, tzinfo=_IST)   # any time

        # Patch exchange_clock module used inside daily_snapshot (imported as _exchange_clock)
        with patch.object(exchange_clock, "get_nse_open_time", return_value=None):
            # async_session must NOT be called — patch it to assert zero calls
            with patch("backend.api.algo.daily_snapshot.async_session") as mock_session:
                result = await daily_snapshot.fix_daily_book_prev_close(now_ist=now_ist)

        assert result == 0, f"Expected 0 on holiday, got {result!r}"
        mock_session.assert_not_called()

    async def test_non_holiday_does_not_return_early(self):
        """When get_nse_open_time() returns a valid time, fix proceeds to DB call."""
        from backend.api.algo import daily_snapshot
        from backend.api.helpers import exchange_clock

        now_ist = datetime(2026, 8, 30, 10, 0, tzinfo=_IST)

        mock_result = MagicMock()
        mock_result.rowcount = 5

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.execute = AsyncMock(return_value=mock_result)
        mock_ctx.commit = AsyncMock()

        with patch.object(exchange_clock, "get_nse_open_time", return_value=time(8, 0)):
            with patch("backend.api.algo.daily_snapshot.async_session",
                       return_value=mock_ctx):
                result = await daily_snapshot.fix_daily_book_prev_close(now_ist=now_ist)

        assert result == 5, f"Expected rowcount=5, got {result!r}"
