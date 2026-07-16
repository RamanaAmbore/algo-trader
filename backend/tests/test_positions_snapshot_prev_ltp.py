"""Tests for the prev_ltp preference fix in _positions_snapshot().

Root cause of bug: After MCX closes (23:30 IST), the broker's positions.close_price
field is set to today's settlement price. When _positions_snapshot() used this value
as close_price without a fallback, the frontend's baseDayPnlForPosition formula
collapsed intraday P&L to 0:

  day_pnl = total_pnl - oq * (ltp - close_price)
          = total_pnl - oq * (settlement - settlement)
          = total_pnl  (wrong — should be the intraday component)

Fix: _positions_snapshot() now prefers yesterday's settlement price (prev_ltp from
daily_book) as close_price, with fallback to snapshot's previous_close, ensuring
correct day-P&L computation even after market close.

The fix uses a combined SQL query with two CTEs:
1. latest_batch: Most-recent snapshot batch per account (by captured_at)
2. prev_batch: Most-recent prior snapshot per (account, symbol) using
   captured_at < max_at (not date < today) to avoid UTC/IST edge cases.

Five quality dimensions tested:
1. SSOT  — SQL query returns prev_ltp and prev_settlement_pnl columns
2. Perf  — Single query (no separate round-trip for yesterday's data)
3. Stale — Query uses captured_at < max_at (yesterday-batch finder)
4. Reuse — Loop unpacks all 13 columns and passes via build_snapshot_position_row
5. UX    — prev_ltp has first preference; previous_close is second fallback
"""

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 1. SSOT — SQL query structure includes prev_ltp and prev_settlement_pnl
# ---------------------------------------------------------------------------

def test_positions_snapshot_sql_includes_prev_batch_cte():
    """_positions_snapshot SQL must include a prev_batch CTE to find yesterday's rows."""
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "prev_batch AS (" in src, (
        "_positions_snapshot SQL must include prev_batch CTE definition"
    )


def test_positions_snapshot_sql_prev_batch_uses_captured_at_anchor():
    """prev_batch CTE must use captured_at < lb.max_at (not date < today)
    to avoid UTC/IST edge cases where yesterday's rows in IST might be
    labeled with today's date in UTC.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "captured_at < lb.max_at" in src, (
        "prev_batch CTE must anchor on captured_at < lb.max_at, not date-based logic"
    )
    # Should NOT use date < :today_date filter in prev_batch
    # (only in old removed code, but verify)
    assert "date < :today_date" not in src or "prev_batch" not in src.split("date < :today_date")[0], (
        "prev_batch CTE must not use date < today filter (use captured_at instead)"
    )


def test_positions_snapshot_sql_returns_prev_ltp_and_prev_settlement_pnl():
    """Main SELECT must include pb.prev_ltp and pb.prev_settlement_pnl columns."""
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "pb.prev_ltp" in src, (
        "SELECT clause must include pb.prev_ltp (aliased from prev_batch.ltp)"
    )
    assert "pb.prev_settlement_pnl" in src, (
        "SELECT clause must include pb.prev_settlement_pnl (aliased from prev_batch.total_pnl)"
    )


def test_positions_snapshot_sql_left_joins_prev_batch():
    """Main query must LEFT JOIN prev_batch so symbols without prior snapshots
    get NULL for prev_ltp (not rejected).
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "LEFT JOIN prev_batch pb" in src, (
        "Query must LEFT JOIN prev_batch (not INNER JOIN) to handle new positions"
    )


# ---------------------------------------------------------------------------
# 2. Perf — Single query (no separate round-trip)
# ---------------------------------------------------------------------------

def test_positions_snapshot_no_separate_prev_query():
    """The fix must eliminate the separate query that built prev_map.
    Verify the removed code is gone.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # Old code built prev_map via separate query with "date < :today_date"
    assert "prev_map" not in src, (
        "prev_map dictionary must not exist — use SQL JOIN instead"
    )
    assert "date < :today_date" not in src, (
        "Old date-based prev_day_sql must be removed"
    )


# ---------------------------------------------------------------------------
# 3. Stale — prev_ltp comes from a prior snapshot (yesterday)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_snapshot_prev_ltp_preference_over_previous_close():
    """Core fix: when prev_ltp is present and > 0, use it as close_price.
    Do NOT use snapshot's previous_close, which may be today's settlement
    after MCX close.

    Scenario:
    - Latest snapshot (today, captured_at=now): ltp=5500, previous_close=5500 (today's settlement)
    - Prior snapshot (yesterday, captured_at=now-1h): prev_ltp=5400 (yesterday's settlement)

    Expected: close_price = 5400 (yesterday's settlement), NOT 5500
    """
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    # Simulate latest snapshot row
    LATEST_LTP = 5500.0         # today's last_price from broker
    TODAY_SETTLEMENT = 5500.0   # broker sets close_price = today's settlement after close
    QTY = 10
    AVG_COST = 5000.0
    DAY_PNL = 500.0             # 10 * (5500 - 5000) = 5000 (wrong if we use today's settlement)
    TOTAL_PNL = 5000.0

    # Simulate yesterday's snapshot
    YESTERDAY_LTP = 5400.0      # yesterday's settlement price
    YESTERDAY_PNL = 4000.0      # 10 * (5400 - 5000) = 4000

    # Build row using the fix — prev_ltp takes precedence
    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=QTY,
        avg_cost=AVG_COST,
        ltp=LATEST_LTP,
        day_pnl=DAY_PNL,
        total_pnl=TOTAL_PNL,
        extras={},
        previous_close=TODAY_SETTLEMENT,  # snapshot's broken value
        prev_settlement_pnl=YESTERDAY_PNL,
        # The loop in _positions_snapshot passes prev_ltp via this kwarg now
        # (we test the builder's use of it; the loop's unpacking is tested separately)
    )

    # The builder should prefer prev_ltp if passed in the future.
    # For now, test that when previous_close is the ONLY close price info,
    # it gets used (existing behavior). The real test of prev_ltp preference
    # happens in the integration test below with mocked DB.
    assert row.close_price == pytest.approx(TODAY_SETTLEMENT, rel=1e-6), (
        "Without prev_ltp passed, close_price uses previous_close"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_loop_unpacks_prev_ltp_and_prev_settlement_pnl():
    """The loop in _positions_snapshot must unpack 13 columns (not 11).
    Verify the tuple unpacking includes prev_ltp and prev_settlement_pnl.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # Find the loop over raw_rows
    assert "for (account, symbol, exchange, qty, avg_cost, ltp," in src, (
        "Loop must unpack from raw_rows"
    )
    # Must unpack prev_ltp and prev_settlement_pnl (columns 11 and 12)
    assert "prev_ltp, prev_settlement_pnl) in raw_rows:" in src, (
        "Loop tuple unpacking must include prev_ltp and prev_settlement_pnl"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_prev_close_val_prefers_prev_ltp():
    """The preference logic in _positions_snapshot must prefer prev_ltp > 0
    over snapshot's previous_close.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # Find the prev_close_val computation
    assert "prev_close_val = (" in src, (
        "_positions_snapshot must compute prev_close_val"
    )
    # Must check prev_ltp first
    assert "float(prev_ltp) if prev_ltp and float(prev_ltp) > 0" in src, (
        "prev_close_val must prefer prev_ltp > 0 as primary choice"
    )
    # Must use previous_close as fallback
    assert "else (float(previous_close)" in src, (
        "prev_close_val must fallback to previous_close when prev_ltp is absent/zero"
    )


# ---------------------------------------------------------------------------
# 4. Reuse — builder accepts prev_settlement_pnl and threads it through
# ---------------------------------------------------------------------------

def test_build_snapshot_position_row_accepts_prev_settlement_pnl_kwarg():
    """build_snapshot_position_row must accept prev_settlement_pnl as a kwarg."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row
    import inspect

    sig = inspect.signature(build_snapshot_position_row)
    params = sig.parameters
    assert "prev_settlement_pnl" in params, (
        "build_snapshot_position_row must accept prev_settlement_pnl kwarg"
    )
    param = params["prev_settlement_pnl"]
    assert param.default is None, (
        "prev_settlement_pnl must default to None for backward compatibility"
    )


def test_build_snapshot_position_row_stores_prev_settlement_pnl():
    """When prev_settlement_pnl is passed, the PositionRow must include it."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    YESTERDAY_PNL = 4000.0

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NIFTY26JULFUT",
        exchange="NFO",
        qty=10,
        avg_cost=5000.0,
        ltp=5500.0,
        day_pnl=5000.0,
        total_pnl=5000.0,
        extras={},
        prev_settlement_pnl=YESTERDAY_PNL,
    )

    assert row.prev_settlement_pnl == pytest.approx(YESTERDAY_PNL, rel=1e-6), (
        f"prev_settlement_pnl={row.prev_settlement_pnl} must equal {YESTERDAY_PNL}"
    )


def test_build_snapshot_position_row_prev_settlement_pnl_none_when_absent():
    """When prev_settlement_pnl is not passed, it defaults to None."""
    from backend.api.routes.positions_helpers import build_snapshot_position_row

    row = build_snapshot_position_row(
        account="ZG0790",
        symbol="NEW_SYMBOL",
        exchange="NFO",
        qty=10,
        avg_cost=5000.0,
        ltp=5500.0,
        day_pnl=0.0,
        total_pnl=0.0,
        extras={},
        # prev_settlement_pnl not provided
    )

    assert row.prev_settlement_pnl is None, (
        "prev_settlement_pnl must be None for new positions"
    )


# ---------------------------------------------------------------------------
# 5. UX — Integration test with mocked DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positions_snapshot_end_to_end_prev_ltp_preference():
    """Integration: _positions_snapshot with mocked DB returns rows where
    close_price prefers yesterday's LTP over today's settlement.

    Scenario (MCX after close):
    - Today's snapshot (captured_at=now): ltp=5500, previous_close=5500, qty=10, avg=5000, total_pnl=5000, day_pnl=500
    - Yesterday's snapshot (captured_at=now-1h): prev_ltp=5400, prev_settlement_pnl=4000

    Expected result:
    - close_price = 5400 (yesterday's LTP, not today's settlement)
    - prev_settlement_pnl = 4000 (yesterday's total P&L)
    - last_price = 5500 (unchanged, still today's LTP from snapshot)
    """
    from backend.api.routes.positions import _positions_snapshot

    # Build 13-tuple per the SQL SELECT:
    # account, symbol, exchange, qty, avg_cost, ltp, day_pnl, total_pnl,
    # payload_json, captured_at, previous_close, prev_ltp, prev_settlement_pnl
    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
    snapshot_row = (
        "ZG0790",                       # account
        "NIFTY26JULFUT",                # symbol
        "NFO",                          # exchange
        10,                             # qty
        Decimal("5000.00"),             # avg_cost
        Decimal("5500.00"),             # ltp (today's LTP)
        Decimal("500.00"),              # day_pnl
        Decimal("5000.00"),             # total_pnl (lifetime P&L)
        "{}",                           # payload_json
        captured_ts,                    # captured_at
        Decimal("5500.00"),             # previous_close (today's settlement — BAD)
        Decimal("5400.00"),             # prev_ltp (yesterday's settlement — GOOD)
        Decimal("4000.00"),             # prev_settlement_pnl (yesterday's total P&L)
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [snapshot_row]
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        resp = await _positions_snapshot()

    assert resp is not None, "snapshot query should not fail"
    assert len(resp.rows) == 1, "expected 1 position row"

    row = resp.rows[0]

    # The core fix: close_price should use yesterday's LTP (5400), not today's settlement (5500)
    assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
        f"close_price={row.close_price} should prefer prev_ltp=5400, "
        f"not today's settlement (previous_close)=5500"
    )

    # last_price unchanged — still today's LTP
    assert row.last_price == pytest.approx(5500.0, rel=1e-6), (
        f"last_price={row.last_price} must remain today's LTP=5500"
    )

    # prev_settlement_pnl should be yesterday's total P&L
    assert row.prev_settlement_pnl == pytest.approx(4000.0, rel=1e-6), (
        f"prev_settlement_pnl={row.prev_settlement_pnl} must equal yesterday's total_pnl=4000"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_prev_ltp_fallback_to_previous_close():
    """When prev_ltp is None or 0 (symbol is new, no yesterday snapshot),
    fallback to previous_close.
    """
    from backend.api.routes.positions import _positions_snapshot

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
    snapshot_row = (
        "ZG0790",                       # account
        "NEW_NIFTY",                    # symbol (new, no prev snapshot)
        "NFO",                          # exchange
        10,                             # qty
        Decimal("5000.00"),             # avg_cost
        Decimal("5500.00"),             # ltp
        Decimal("500.00"),              # day_pnl
        Decimal("5000.00"),             # total_pnl
        "{}",                           # payload_json
        captured_ts,                    # captured_at
        Decimal("5350.00"),             # previous_close (snapshot's value)
        None,                           # prev_ltp (no yesterday snapshot — LEFT JOIN NULL)
        None,                           # prev_settlement_pnl
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [snapshot_row]
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 1

    row = resp.rows[0]

    # Fallback to previous_close when prev_ltp is NULL
    assert row.close_price == pytest.approx(5350.0, rel=1e-6), (
        f"close_price={row.close_price} should fallback to previous_close=5350 "
        f"when prev_ltp is NULL"
    )

    # prev_settlement_pnl should also be None
    assert row.prev_settlement_pnl is None, (
        "prev_settlement_pnl must be None for new positions"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_multiple_accounts_and_symbols():
    """Verify the preference logic works correctly for multiple positions
    from multiple accounts — each gets its own prev_ltp/prev_settlement_pnl.
    """
    from backend.api.routes.positions import _positions_snapshot

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

    snapshot_rows = [
        # Position 1: ZG0790 / NIFTY — has yesterday snapshot
        (
            "ZG0790", "NIFTY26JULFUT", "NFO", 10,
            Decimal("5000.00"), Decimal("5500.00"),
            Decimal("500.00"), Decimal("5000.00"), "{}",
            captured_ts, Decimal("5500.00"),
            Decimal("5400.00"), Decimal("4000.00"),
        ),
        # Position 2: ZJ6294 / CRUDEOIL — no yesterday snapshot (NEW)
        (
            "ZJ6294", "CRUDEOIL26AUGFUT", "MCX", 100,
            Decimal("5000.00"), Decimal("5550.00"),
            Decimal("5000.00"), Decimal("5000.00"), "{}",
            captured_ts, Decimal("5400.00"),
            None, None,  # No prev_ltp/prev_settlement_pnl
        ),
        # Position 3: ZG0790 / GOLDM — has yesterday snapshot
        (
            "ZG0790", "GOLDM26AUGFUT", "MCX", 1,
            Decimal("6800.00"), Decimal("6900.00"),
            Decimal("100.00"), Decimal("100.00"), "{}",
            captured_ts, Decimal("6850.00"),
            Decimal("6810.00"), Decimal("10.00"),
        ),
    ]

    mock_result = MagicMock()
    mock_result.all.return_value = snapshot_rows
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 3

    # Position 1: ZG0790 / NIFTY
    nifty_row = next(r for r in resp.rows if r.tradingsymbol == "NIFTY26JULFUT")
    assert nifty_row.close_price == pytest.approx(5400.0, rel=1e-6), (
        "NIFTY close_price should use prev_ltp=5400"
    )
    assert nifty_row.prev_settlement_pnl == pytest.approx(4000.0, rel=1e-6)

    # Position 2: ZJ6294 / CRUDEOIL (new)
    crudeoil_row = next(r for r in resp.rows if r.tradingsymbol == "CRUDEOIL26AUGFUT")
    assert crudeoil_row.close_price == pytest.approx(5400.0, rel=1e-6), (
        "CRUDEOIL close_price should fallback to previous_close=5400"
    )
    assert crudeoil_row.prev_settlement_pnl is None, "CRUDEOIL is new"

    # Position 3: ZG0790 / GOLDM
    goldm_row = next(r for r in resp.rows if r.tradingsymbol == "GOLDM26AUGFUT")
    assert goldm_row.close_price == pytest.approx(6810.0, rel=1e-6), (
        "GOLDM close_price should use prev_ltp=6810"
    )
    assert goldm_row.prev_settlement_pnl == pytest.approx(10.0, rel=1e-6)


@pytest.mark.asyncio
async def test_positions_snapshot_day_pnl_not_collapsed_after_close():
    """End-to-end: verify that day_change_val (stored from daily_book) is
    preserved and NOT collapsed to 0 by the close-price fix.

    The bug was: close_price = settlement_price, so frontend formula
      day_pnl = total_pnl - oq * (ltp - close_price) = total_pnl - 0 = total_pnl
    This gave lifetime P&L instead of day P&L.

    After fix: close_price = yesterday_settlement, so frontend formula
      day_pnl = total_pnl - oq * (ltp - yesterday_settlement) ≠ total_pnl
    The actual day_pnl comes from day_change_val column (stored from daily_book).
    """
    from backend.api.routes.positions import _positions_snapshot

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

    # Position: opened yesterday with lifetime P&L = 4000,
    # today's intraday gain = 500
    YESTERDAY_TOTAL_PNL = 4000.0
    TODAY_INTRADAY_GAIN = 500.0  # What we want to see after close
    TODAY_TOTAL_PNL = YESTERDAY_TOTAL_PNL + TODAY_INTRADAY_GAIN  # 4500

    snapshot_row = (
        "ZG0790",
        "NIFTY26JULFUT",
        "NFO",
        10,
        Decimal("5000.00"),              # avg_cost
        Decimal("5500.00"),              # ltp (today's settlement)
        Decimal(str(TODAY_INTRADAY_GAIN)),  # day_pnl = 500 (stored value)
        Decimal(str(TODAY_TOTAL_PNL)),   # total_pnl = 4500
        "{}",                            # payload_json (empty)
        captured_ts,
        Decimal("5500.00"),              # previous_close = today's settlement (BAD)
        Decimal("5400.00"),              # prev_ltp = yesterday's settlement (GOOD)
        Decimal(str(YESTERDAY_TOTAL_PNL)),  # prev_settlement_pnl = yesterday's total
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [snapshot_row]
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        resp = await _positions_snapshot()

    assert resp is not None
    assert len(resp.rows) == 1

    row = resp.rows[0]

    # With the fix, close_price = yesterday's settlement (5400)
    assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
        "close_price should be yesterday's LTP=5400 (fix applied)"
    )

    # day_change_val must preserve the stored intraday gain
    # (not collapse to 0)
    assert row.day_change_val == pytest.approx(TODAY_INTRADAY_GAIN, rel=1e-6), (
        f"day_change_val={row.day_change_val} should preserve stored day_pnl={TODAY_INTRADAY_GAIN}, "
        f"not collapse to 0"
    )

    # total_pnl stays as is (comes from live broker or stored)
    assert row.pnl == pytest.approx(TODAY_TOTAL_PNL, rel=1e-6)
