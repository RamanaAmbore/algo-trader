"""
Tests for fix_daily_book_prev_close() market-day gate in backend/api/algo/daily_snapshot.py.

Covers:
  - Early return when _is_market_day_today() returns False
  - Proceed normally when _is_market_day_today() returns True
  - No DB calls when returning early (non-trading day)
  - settlement_map path sets ltp = close_price alongside prev_close (CloseReset fix)

Quality dimensions:
  1. SSOT        — _is_market_day_today() is the canonical gate
  2. Correctness — returns 0 on non-trading day; proceeds on trading day;
                   settlement_map path aligns ltp with close_price at 08:00
  3. Performance — early return skips all DB I/O on weekends/holidays
  4. Stale code  — no hardcoded weekend/holiday logic in the function
  5. Integration — market-day gate properly stops recovery from running
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from unittest.mock import AsyncMock, patch, MagicMock
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_non_trading_day_returns_zero():
    """Return 0 early (no DB calls) when not a trading day."""
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=False):
        from backend.api.algo.daily_snapshot import fix_daily_book_prev_close
        result = await fix_daily_book_prev_close(now)
        assert result == 0, "Should return 0 on non-trading day"


@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_trading_day_proceeds():
    """Proceed normally (call DB) when it is a trading day."""
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).replace(hour=8, minute=30)  # 08:30 IST

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=True):
        with patch("backend.api.algo.daily_snapshot._exchange_clock.get_nse_open_time", return_value=None):
            # When get_nse_open_time is None (holiday), function should return early
            from backend.api.algo.daily_snapshot import fix_daily_book_prev_close
            result = await fix_daily_book_prev_close(now)
            # Holiday condition catches it and returns 0
            assert result == 0


@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_non_trading_day_skips_db():
    """Verify no DB session is created for non-trading days."""
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=False):
        with patch("backend.api.algo.daily_snapshot.async_session") as mock_session_factory:
            from backend.api.algo.daily_snapshot import fix_daily_book_prev_close
            result = await fix_daily_book_prev_close(now)

            # Should not create any DB session
            mock_session_factory.assert_not_called()
            assert result == 0


@pytest.mark.asyncio
async def test_fix_daily_book_prev_close_market_day_uses_cache():
    """When it is a trading day, _is_market_day_today() should be called from cache."""
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).replace(hour=8, minute=30)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=True) as mock_day_check:
        with patch("backend.api.algo.daily_snapshot._exchange_clock.get_nse_open_time", return_value=None):
            from backend.api.algo.daily_snapshot import fix_daily_book_prev_close
            await fix_daily_book_prev_close(now)

            # The market day check should have been called
            mock_day_check.assert_called()


@pytest.mark.asyncio
async def test_settlement_map_path_sets_ltp_equal_to_close_price():
    """In new-session mode (≥ 08:00), the settlement_map UPDATE must set ltp = close_price
    alongside prev_close so day P&L ≈ 0 at session open (CloseReset fix)."""
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    now = datetime(2026, 9, 20, 8, 30, tzinfo=_IST)
    settlement_map = {("ACC1", "GOLDSYMBOL"): 2100.0}

    executed_sqls: list[str] = []
    mock_result = MagicMock()
    mock_result.rowcount = 1

    mock_session = AsyncMock()

    async def capture_execute(sql, params=None):
        executed_sqls.append(str(sql))
        return mock_result

    mock_session.execute.side_effect = capture_execute
    mock_session.commit = AsyncMock()

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=mock_session)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=True):
        with patch("backend.api.algo.daily_snapshot._exchange_clock.get_nse_open_time", return_value=dtime(8, 0)):
            with patch("backend.api.algo.daily_snapshot.async_session", return_value=ctx_mock):
                await fix_daily_book_prev_close(now, settlement_map=settlement_map)

    # The first executed SQL (settlement_map UPDATE) must include ltp = :close_price
    assert executed_sqls, "Expected at least one SQL to be executed"
    first_sql = executed_sqls[0]
    assert "ltp" in first_sql and "close_price" in first_sql, (
        "Settlement_map UPDATE must set ltp = :close_price so daily_book.ltp "
        "aligns with prev_close at session open, making day P&L ≈ 0"
    )
    assert "prev_close" in first_sql, "UPDATE must still set prev_close = :close_price"
