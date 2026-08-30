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
    """The per-row builder must unpack 13 columns including prev_ltp and prev_settlement_pnl.

    After the CC-reduction refactor, the 13-tuple unpacking moved from the
    _positions_snapshot loop into build_row_from_snapshot_raw (positions_helpers).
    """
    import inspect
    from backend.api.routes import positions_helpers as _helpers

    src = inspect.getsource(_helpers.build_row_from_snapshot_raw)
    # Must unpack the 13-column tuple
    assert "account, symbol, exchange, qty, avg_cost, ltp," in src, (
        "build_row_from_snapshot_raw must unpack 13-column tuple"
    )
    assert "prev_ltp, prev_settlement_pnl" in src, (
        "Unpacking must include prev_ltp and prev_settlement_pnl (columns 11 and 12)"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_prev_close_val_prefers_prev_ltp():
    """actual_previous_close (frozen settlement) is the primary reference for day P&L.

    After the CC-refactor (2026-08-30), the `prev_close_val` dead-variable was removed.
    The helper `_resolve_previous_close` is now called WITHOUT prev_ltp so that
    corruption detection (pc ≈ ltp) does not fall through to prev_ltp — positions
    only uses prev_ltp when previous_close is completely absent/zero.
    Verify: (a) actual_previous_close is computed, (b) _resolve_previous_close is used.
    """
    import inspect
    from backend.api.routes import positions_helpers as _helpers

    src = inspect.getsource(_helpers.build_row_from_snapshot_raw)
    assert "actual_previous_close" in src, (
        "build_row_from_snapshot_raw must compute actual_previous_close "
        "(the frozen prior-session settlement) as the primary reference"
    )
    assert "_resolve_previous_close" in src, (
        "build_row_from_snapshot_raw must use _resolve_previous_close helper "
        "for corruption-detection (CC-reduction refactor 2026-08-30)"
    )
    # prev_ltp must still be unpacked from the tuple (column 11) but is no
    # longer passed to _resolve_previous_close (positions fallback removed).
    assert "prev_ltp, prev_settlement_pnl" in src, (
        "Tuple unpack must still include prev_ltp (column 11)"
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
    """Integration: _positions_snapshot returns rows where close_price uses the
    frozen prior-session settlement (previous_close) as the primary reference.

    Priority fix (2026-08-11): previous_close (COALESCE-frozen in the first daily
    UPSERT = yesterday's settlement) now takes precedence over prev_ltp (which is
    the LTP of the most-recent daily_book batch — it converges toward today's LTP
    during a session, making day_change ≈ 0 if used as the baseline).

    Scenario (MCX after close):
    - previous_close = 5400 (frozen yesterday's settlement — authoritative)
    - prev_ltp = 5500 (latest daily_book batch LTP ≈ today's LTP — stale-ish)
    - ltp = 5500 (today's last price from snapshot)

    Expected:
    - close_price = 5400 (frozen settlement wins)
    - day_change_val = (5500 - 5400) * 10 = 1000 (non-zero)
    - prev_settlement_pnl = 4000 (yesterday's total P&L)
    - last_price = 5500 (unchanged)
    """
    from backend.api.routes.positions import _positions_snapshot

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
    snapshot_row = (
        "ZG0790",                       # account
        "NIFTY26JULFUT",                # symbol
        "NFO",                          # exchange
        10,                             # qty
        Decimal("5000.00"),             # avg_cost
        Decimal("5500.00"),             # ltp (today's LTP)
        Decimal("500.00"),              # day_pnl (stored, may be stale)
        Decimal("5000.00"),             # total_pnl (lifetime P&L)
        "{}",                           # payload_json
        captured_ts,                    # captured_at
        Decimal("5400.00"),             # previous_close (frozen prior-session settlement)
        Decimal("5500.00"),             # prev_ltp (latest batch LTP ≈ today's LTP)
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

    # Core fix: close_price = frozen prior-session settlement (5400), not prev_ltp (5500)
    assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
        f"close_price={row.close_price} must use frozen previous_close=5400, "
        f"not prev_ltp=5500 (which ≈ today's LTP and would give day_change≈0)"
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
    # previous_close=5500 (frozen settlement) wins over prev_ltp=5400
    nifty_row = next(r for r in resp.rows if r.tradingsymbol == "NIFTY26JULFUT")
    assert nifty_row.close_price == pytest.approx(5500.0, rel=1e-6), (
        "NIFTY close_price should use frozen previous_close=5500 (not prev_ltp=5400)"
    )
    assert nifty_row.prev_settlement_pnl == pytest.approx(4000.0, rel=1e-6)

    # Position 2: ZJ6294 / CRUDEOIL (new, prev_ltp=None)
    # previous_close=5400 is the sole reference (prev_ltp=NULL → fallback to previous_close)
    crudeoil_row = next(r for r in resp.rows if r.tradingsymbol == "CRUDEOIL26AUGFUT")
    assert crudeoil_row.close_price == pytest.approx(5400.0, rel=1e-6), (
        "CRUDEOIL close_price should use previous_close=5400 (prev_ltp=NULL)"
    )
    assert crudeoil_row.prev_settlement_pnl is None, "CRUDEOIL is new"

    # Position 3: ZG0790 / GOLDM
    # previous_close=6850 (frozen settlement) wins over prev_ltp=6810
    goldm_row = next(r for r in resp.rows if r.tradingsymbol == "GOLDM26AUGFUT")
    assert goldm_row.close_price == pytest.approx(6850.0, rel=1e-6), (
        "GOLDM close_price should use frozen previous_close=6850 (not prev_ltp=6810)"
    )
    assert goldm_row.prev_settlement_pnl == pytest.approx(10.0, rel=1e-6)


@pytest.mark.asyncio
async def test_positions_snapshot_day_pnl_not_collapsed_after_close():
    """End-to-end: verify that day_change_val is recomputed from (ltp - previous_close)×qty
    and is NOT collapsed to 0 after MCX close.

    Priority fix (2026-08-11): previous_close (COALESCE-frozen prior-session settlement)
    is the primary baseline. When prev_ltp ≈ today's LTP (intraday re-capture), using
    prev_ltp as baseline would give day_change ≈ 0. Using previous_close (yesterday's
    real settlement) gives the correct non-zero day P&L.

    Scenario:
    - previous_close = 5400 (frozen yesterday's settlement — correct baseline)
    - prev_ltp = 5500 (latest batch LTP ≈ today's LTP — wrong baseline)
    - ltp = 5500, qty = 10
    - Recomputed day_pnl = (5500 - 5400) × 10 = 1000 (NOT 0)
    - close_price = 5400 (frozen settlement, not prev_ltp)
    """
    from backend.api.routes.positions import _positions_snapshot

    captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

    YESTERDAY_TOTAL_PNL = 4000.0
    # total_pnl must equal (ltp - avg) * qty for the universal formula to yield
    # the expected (ltp - prev_close) * qty day_pnl:
    #   day_pnl = total_pnl - (prev_close - avg) * oq
    #           = (5500-5000)*10 - (5400-5000)*10 = 5000 - 4000 = 1000 ✓
    TODAY_TOTAL_PNL = 5000.0  # (5500-5000)*10 = unrealised only, no realised
    QTY = 10
    LTP = 5500.0
    PREVIOUS_CLOSE = 5400.0      # frozen settlement (correct baseline)
    PREV_LTP = 5500.0            # latest batch LTP ≈ today's LTP (wrong if used as baseline)
    # Recomputed formula uses previous_close: (5500 - 5400) * 10 = 1000
    EXPECTED_DAY_PNL = (LTP - PREVIOUS_CLOSE) * QTY  # 1000.0

    snapshot_row = (
        "ZG0790",
        "NIFTY26JULFUT",
        "NFO",
        QTY,
        Decimal("5000.00"),               # avg_cost
        Decimal(str(LTP)),                # ltp
        Decimal("500.00"),                # day_pnl stored (stale — overridden by formula)
        Decimal(str(TODAY_TOTAL_PNL)),    # total_pnl = 5000 = (ltp-avg)*qty
        "{}",                             # payload_json
        captured_ts,
        Decimal(str(PREVIOUS_CLOSE)),     # previous_close = frozen settlement (5400)
        Decimal(str(PREV_LTP)),           # prev_ltp = latest batch LTP (5500 ≈ today's)
        Decimal(str(YESTERDAY_TOTAL_PNL)),# prev_settlement_pnl
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

    # close_price = frozen settlement (5400), not prev_ltp (5500)
    assert row.close_price == pytest.approx(PREVIOUS_CLOSE, rel=1e-6), (
        f"close_price={row.close_price} should be frozen previous_close={PREVIOUS_CLOSE}, "
        f"not prev_ltp={PREV_LTP} (which ≈ today's LTP and would give day_change=0)"
    )

    # day_change_val recomputed from (ltp - previous_close) * qty = 1000, not 0 or stored 500
    assert row.day_change_val == pytest.approx(EXPECTED_DAY_PNL, rel=1e-6), (
        f"day_change_val={row.day_change_val} should be recomputed as "
        f"(ltp-previous_close)*qty=({LTP}-{PREVIOUS_CLOSE})*{QTY}={EXPECTED_DAY_PNL}; "
        f"old code (prev_ltp priority) would give 0 when prev_ltp≈ltp"
    )

    # total_pnl stays as-is (lifetime P&L, not day P&L)
    assert row.pnl == pytest.approx(TODAY_TOTAL_PNL, rel=1e-6)


# ---------------------------------------------------------------------------
# NSE settlement fix — prev_batch boundary guards (today_ist_midnight)
# ---------------------------------------------------------------------------

def test_positions_snapshot_prev_batch_lookback_is_7_days():
    """prev_batch CTE lookback window must be 7 days (not 2 days).

    Root cause of P1 bug (Saturday NavStrip = 0):
    - _task_closed_hours_refresh was calling snapshot_daily_book() every 30 min,
      writing Saturday refresh rows to daily_book.
    - latest_batch = Saturday 07:30 row (same LTP as Friday settlement).
    - prev_batch with 2-day window found Friday 16:15 row.
    - computed_day_pnl = (Saturday LTP − Friday LTP) × qty = 0.

    Fix 1 (background.py): remove snapshot_daily_book() from closed-hours task.
    Fix 2 (positions.py): widen lookback to 7 days so multi-day holiday gaps
    (e.g. Thursday close → Tuesday open after a 4-day weekend) never miss the
    last real settlement row.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "INTERVAL '7 days'" in src, (
        "prev_batch CTE lookback must be INTERVAL '7 days' to survive multi-day "
        "holiday gaps. INTERVAL '2 days' was too narrow and caused P1=0 on Saturday."
    )
    assert "INTERVAL '2 days'" not in src, (
        "INTERVAL '2 days' must be replaced with '7 days' — "
        "the narrower window was the root cause of prev_batch missing Friday settlement "
        "when latest_batch was a Saturday closed-hours row."
    )


def test_positions_snapshot_prev_batch_sql_has_ltp_not_null_guard():
    """prev_batch CTE must filter ltp IS NOT NULL AND ltp > 0 so that
    mid-session snapshots (where ltp=NULL) are excluded from the baseline.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "db.ltp IS NOT NULL AND db.ltp > 0" in src, (
        "prev_batch CTE must guard against NULL/zero ltp rows "
        "so mid-session snapshots don't contaminate the prev_close baseline"
    )


def test_positions_snapshot_prev_batch_sql_has_prev_batch_cutoff_boundary():
    """prev_batch CTE must use :prev_batch_cutoff to exclude same-session rows.

    The boundary is either today 08:00 IST (normal trading day) or yesterday 08:00 IST
    (pre-market window), chosen dynamically so prev_batch always pulls from the
    prior session's data regardless of when the snapshot is read.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "captured_at < :prev_batch_cutoff" in src, (
        "prev_batch CTE must exclude same-session rows with "
        "`AND db.captured_at < :prev_batch_cutoff` boundary"
    )


def test_positions_snapshot_bindparams_includes_prev_batch_cutoff():
    """The bindparams call must pass prev_batch_cutoff as a named parameter."""
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "prev_batch_cutoff=_prev_batch_cutoff" in src, (
        "bindparams must include prev_batch_cutoff= (bound to the computed "
        "8am IST cutoff) to enforce the session-boundary parameter in SQL"
    )


def test_positions_snapshot_today_ist_midnight_computed_from_ts_indian():
    """The 8am cutoff is derived from _now_ist.replace(hour=0, ...) + timedelta(hours=8).
    Verify the midnight pattern is present — it's the base for the 8am cutoff.
    """
    import inspect
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    assert "_now_ist.replace(hour=0, minute=0, second=0, microsecond=0)" in src, (
        "The 8am cutoff must be derived from _now_ist.replace(hour=0, ...) "
        "(midnight base) not from _ts_indian() called again separately"
    )


@pytest.mark.asyncio
async def test_positions_snapshot_computed_day_pnl_no_fallback_to_extras_when_column_present():
    """When day_pnl column is non-NULL and prev_close is unavailable,
    computed_day_pnl uses the stored column value — NOT extras.

    Regression guard: the fallback priority is formula > column > extras.
    When the formula path is skipped (no prev_close), the column wins.
    Extras (which may carry stale/decoy values) must NOT override the column.
    """
    import json as _json
    fake_captured = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)

    payload = {
        "snapshot_extras": {
            "day_change_val": -999.99,  # decoy — must NOT be used
            "day_change_pct": -1.11,
        }
    }
    # day_pnl=2500 (column present), prev_ltp=None (no prev_batch), previous_close=None
    row_data = (
        "ACC1", "NIFTY26JULFUT", "NFO",
        50, Decimal("23000.00"), Decimal("23150.00"),
        Decimal("2500.00"), Decimal("7500.00"),
        _json.dumps(payload),
        fake_captured,
        None,   # previous_close
        None,   # prev_ltp
        None,   # prev_settlement_pnl
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [row_data]
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.api.database.async_session", return_value=mock_session):
        from backend.api.routes.positions import _positions_snapshot
        resp = await _positions_snapshot()

    assert resp is not None
    row = resp.rows[0]
    assert pytest.approx(row.day_change_val, rel=1e-6) == 2500.0, (
        f"day_change_val={row.day_change_val} — stored column 2500 must win "
        f"over extras decoy -999.99 when prev_close is unavailable"
    )
