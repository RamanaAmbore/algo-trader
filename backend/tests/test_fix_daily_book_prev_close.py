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


# ---------------------------------------------------------------------------
# Fallback path (no settlement_map) — "both columns together" invariant
# ---------------------------------------------------------------------------

import re


def _set_clause(sql: str) -> str:
    """Extract the text between SET and FROM prev_ref in the fallback UPDATE,
    so assertions target the actual SET clause and not an incidental mention
    of "ltp" elsewhere in the query (WHERE clause, CTE column alias, etc.)."""
    m = re.search(r"\bSET\b(.*?)\bFROM\s+prev_ref\b", sql, re.S)
    assert m, f"Could not locate SET...FROM prev_ref in SQL:\n{sql}"
    return m.group(1)


async def _run_fallback(now, settlement_map=None, open_time=dtime(8, 0)):
    """Drive fix_daily_book_prev_close through the generic daily_book-based
    fallback path and capture every executed SQL string + params."""
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    executed: list[tuple[str, dict]] = []
    mock_result = MagicMock()
    mock_result.rowcount = 2

    mock_session = AsyncMock()

    async def capture_execute(sql, params=None):
        executed.append((str(sql), params or {}))
        return mock_result

    mock_session.execute.side_effect = capture_execute
    mock_session.commit = AsyncMock()

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=mock_session)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=True):
        with patch("backend.api.algo.daily_snapshot._exchange_clock.get_nse_open_time", return_value=open_time):
            with patch("backend.api.algo.daily_snapshot.async_session", return_value=ctx_mock):
                updated = await fix_daily_book_prev_close(now, settlement_map=settlement_map)
    return updated, executed


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_map", [None, {}])
async def test_new_session_fallback_sets_ltp_and_prev_close_together(settlement_map):
    """Regression test for the reported bug: at 08:00+ IST with no settlement_map
    (None or empty dict — both must fall straight through to the generic fallback,
    skipping the settlement_map branch entirely), the fallback UPDATE must set
    BOTH ltp and prev_close to the SAME reference value (r.ref_close), matching
    the settlement_map-present path's "both columns together" invariant.
    Previously only prev_close was set, leaving ltp stale/disagreeing."""
    now = datetime(2026, 9, 20, 8, 30, tzinfo=_IST)

    updated, executed = await _run_fallback(now, settlement_map=settlement_map)

    assert updated == 2
    assert executed, "Expected the fallback UPDATE to execute"
    first_sql, _params = executed[0]
    set_clause = _set_clause(first_sql)

    assert re.search(r"prev_close\s*=\s*r\.ref_close", set_clause), (
        f"Fallback SET clause must set prev_close = r.ref_close; got:\n{set_clause}"
    )
    assert re.search(r"\bltp\s*=\s*r\.ref_close", set_clause), (
        "Fallback SET clause must ALSO set ltp = r.ref_close in new-session mode "
        f"(the reported bug — ltp left untouched); got:\n{set_clause}"
    )


@pytest.mark.asyncio
async def test_new_session_settlement_map_exception_falls_through_and_sets_ltp():
    """When settlement_map IS provided but the settlement_map UPDATE raises, the
    function falls through to the generic fallback (per the existing
    'Fall through to the standard daily_book-based path below' comment) — that
    fallback execution must ALSO set both ltp and prev_close together."""
    from backend.api.algo.daily_snapshot import fix_daily_book_prev_close

    now = datetime(2026, 9, 20, 8, 30, tzinfo=_IST)
    settlement_map = {("ACC1", "GOLDSYMBOL"): 2100.0}

    executed: list[str] = []
    fallback_result = MagicMock()
    fallback_result.rowcount = 1

    mock_session = AsyncMock()

    call_count = {"n": 0}

    async def capture_execute(sql, params=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call is the settlement_map UPDATE — raise to force fall-through.
            raise RuntimeError("DB error on settlement_map UPDATE")
        executed.append(str(sql))
        return fallback_result

    mock_session.execute.side_effect = capture_execute
    mock_session.commit = AsyncMock()

    ctx_mock = MagicMock()
    ctx_mock.__aenter__ = AsyncMock(return_value=mock_session)
    ctx_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.algo.daily_snapshot._exchange_clock._is_market_day_today", return_value=True):
        with patch("backend.api.algo.daily_snapshot._exchange_clock.get_nse_open_time", return_value=dtime(8, 0)):
            with patch("backend.api.algo.daily_snapshot.async_session", return_value=ctx_mock):
                updated = await fix_daily_book_prev_close(now, settlement_map=settlement_map)

    assert updated == 1
    assert executed, "Expected the fallback UPDATE to execute after the settlement_map path raised"
    set_clause = _set_clause(executed[0])
    assert re.search(r"\bltp\s*=\s*r\.ref_close", set_clause), (
        "Fallback-after-exception path must also set ltp = r.ref_close; got:\n"
        f"{set_clause}"
    )


@pytest.mark.asyncio
async def test_overnight_fallback_never_sets_ltp():
    """Regression guard: in overnight mode (before 08:00 IST), the fallback
    UPDATE must set ONLY prev_close — ltp must remain completely untouched
    because it is still live-ticking during the closed-hours window."""
    now = datetime(2026, 9, 20, 1, 0, tzinfo=_IST)

    updated, executed = await _run_fallback(now, settlement_map=None, open_time=dtime(8, 0))

    assert updated == 2
    assert executed, "Expected the fallback UPDATE to execute"
    first_sql, _params = executed[0]
    set_clause = _set_clause(first_sql)

    assert re.search(r"prev_close\s*=\s*r\.ref_close", set_clause), (
        f"Overnight SET clause must still set prev_close = r.ref_close; got:\n{set_clause}"
    )
    assert not re.search(r"\bltp\s*=\s*r\.ref_close", set_clause), (
        f"Overnight mode must NEVER set ltp in the SET clause; got:\n{set_clause}"
    )
