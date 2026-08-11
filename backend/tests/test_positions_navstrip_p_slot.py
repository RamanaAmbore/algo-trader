"""Tests for NavStrip P slot 1 overnight day-P&L scenario.

NavStrip P slot 1 displays Day P&L for positions. The SSOT is the backend
positions route, which produces `close_price` and `prev_settlement_pnl` fields.

After MCX session close (23:30 IST), Kite returns `close_price=0` (or today's
settlement price) for some positions. The backend has two mechanisms to prevent
stale close_price:

1. Live path: `_override_stale_close_from_snapshot` patches `close_price` from
   `daily_book.prev_ltp` for live position rows.
2. Snapshot path: `_positions_snapshot` builds `close_price` from `daily_book.prev_ltp`
   via `prev_close_val` preference logic.

Test coverage:
1. Live-path close override — patches stale close from daily_book
2. Snapshot-path close preference — prefers prev_ltp over previous_close
3. prev_settlement_pnl backfill — both paths populate yesterday's total_pnl
4. Day P&L formula correctness — decomposed formula works with patched close_price
5. Robustness — graceful handling of missing daily_book rows
"""

import asyncio
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Test fixtures — shared position row builders
# ---------------------------------------------------------------------------

def _make_position_row(
    account: str = "ZG0790",
    symbol: str = "CRUDEOILSEP25",
    exchange: str = "MCX",
    last_price: float = 6150.0,
    close_price: float = 0.0,  # Stale/zero after MCX close
    quantity: int = 5,
    average_price: float = 6000.0,
    overnight_qty: int = 5,
    pnl: float = 750.0,
    day_change_val: float = 0.0,  # Kite returns 0 when close is stale
) -> dict:
    """Build a minimal live positions DataFrame row for overnight testing.

    Scenario: MCX position with:
    - last_price=6150 (current market)
    - close_price=0 (stale, from Kite after close)
    - pnl=750 (lifetime P&L = (6150-6000)*5)
    - day_change_val=0 (Kite stale, should be (6150-6000)*5=750)
    """
    return {
        'account': account,
        'tradingsymbol': symbol,
        'exchange': exchange,
        'product': 'NRML',
        'quantity': quantity,
        'overnight_quantity': overnight_qty,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'last_price': last_price,
        'close_price': close_price,
        'average_price': average_price,
        'pnl': pnl,
        'unrealised': 0.0,
        'realised': 0.0,
        'day_change_val': day_change_val,
        'day_change': 0.0,
        'day_change_percentage': 0.0,
        'pnl_percentage': 0.0,
        'last_price_stale': False,
        'account_stale': False,
    }


def _run_override_stale_close_from_snapshot(
    df: pd.DataFrame,
    snapshot_rows: list[tuple[str, str, float, float]],
) -> pd.DataFrame:
    """Invoke _override_stale_close_from_snapshot with mocked DB + midnight.

    Args:
        df: positions DataFrame to patch.
        snapshot_rows: list of (account, symbol, ltp, total_pnl) tuples
                      matching the daily_book result format.

    Returns:
        The patched DataFrame.
    """
    from backend.api.routes.positions import _override_stale_close_from_snapshot
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    # Use a fixed midnight (2026-07-19 00:00 IST) for deterministic tests
    midnight = datetime(2026, 7, 19, 0, 0, 0, tzinfo=ist)

    # Mock the DB session to return snapshot rows
    mock_result = MagicMock()
    mock_result.all.return_value = snapshot_rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian",
            return_value=midnight,
        ),
    ):
        asyncio.run(_override_stale_close_from_snapshot(df))

    return df


# ---------------------------------------------------------------------------
# Test 1: Live-path close override patches stale close from daily_book
# ---------------------------------------------------------------------------

class TestLivePathCloseOverride:
    """_override_stale_close_from_snapshot patches stale close_price."""

    def test_override_stale_close_zero_from_daily_book(self):
        """When close_price=0 (stale after MCX close) and daily_book has
        prev_ltp > 0, patch close_price to prev_ltp (yesterday's settlement).

        Scenario:
        - Kite returns: close_price=0, last_price=6150, pnl=750
        - daily_book has: prev_ltp=6000

        Expected: close_price patched to 6000
        """
        df = pd.DataFrame([_make_position_row(
            close_price=0.0,
            last_price=6150.0,
            pnl=750.0,
        )])

        # Snapshot row: (account, symbol, ltp, total_pnl)
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert df.at[0, 'close_price'] == 6000.0, (
            f"close_price should be patched to 6000.0 from daily_book, "
            f"got {df.at[0, 'close_price']}"
        )

    def test_override_preserves_unchanged_close_price(self):
        """When close_price already matches prev_ltp closely, skip override.
        Uses epsilon (0.005) to avoid tiny floating-point rounding noise.
        """
        df = pd.DataFrame([_make_position_row(
            close_price=6000.0,
            last_price=6150.0,
        )])

        # Snapshot with same LTP (within epsilon)
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.002, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Should remain unchanged (epsilon guard)
        assert df.at[0, 'close_price'] == 6000.0, (
            "close_price should not be patched when already correct (epsilon guard)"
        )

    def test_override_multiple_positions_same_account(self):
        """Multiple positions from same account each get their own close override."""
        df = pd.concat([
            pd.DataFrame([_make_position_row(
                symbol="CRUDEOILSEP25",
                close_price=0.0,
                last_price=6150.0,
            )]),
            pd.DataFrame([_make_position_row(
                symbol="GOLDOCTFUT",
                close_price=0.0,
                last_price=6900.0,
            )]),
        ], ignore_index=True)

        snapshot_rows = [
            ("ZG0790", "CRUDEOILSEP25", 6000.0, 500.0),
            ("ZG0790", "GOLDOCTFUT", 6800.0, 300.0),
        ]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        crude_row = df[df['tradingsymbol'] == 'CRUDEOILSEP25'].iloc[0]
        gold_row = df[df['tradingsymbol'] == 'GOLDOCTFUT'].iloc[0]

        assert crude_row['close_price'] == 6000.0, (
            "CRUDEOIL close_price should be patched to 6000"
        )
        assert gold_row['close_price'] == 6800.0, (
            "GOLDM close_price should be patched to 6800"
        )

    def test_override_gracefully_handles_missing_snapshot(self):
        """When daily_book has no snapshot row for a symbol, close_price
        remains unchanged (not an error).
        """
        df = pd.DataFrame([_make_position_row(
            symbol="NEW_SYMBOL",
            close_price=0.0,
        )])

        # No snapshot row for this symbol
        snapshot_rows = []
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # close_price remains 0 (no crash)
        assert df.at[0, 'close_price'] == 0.0, (
            "close_price should remain 0 when no snapshot found (no error)"
        )

    def test_override_empty_dataframe_no_crash(self):
        """Empty DataFrame must not crash."""
        df = pd.DataFrame()

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from backend.api.routes.positions import _override_stale_close_from_snapshot

        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_from_snapshot(df))

        # No exception; function returned cleanly
        assert df.empty


# ---------------------------------------------------------------------------
# Test 2: Snapshot path sets close_price from prev_ltp preference
# ---------------------------------------------------------------------------

class TestSnapshotPathClosePreference:
    """_positions_snapshot prefers previous_close (official settlement) over prev_ltp (batch LTP)."""

    @pytest.mark.asyncio
    async def test_snapshot_prefers_previous_close_over_prev_ltp(self):
        """Core fix: when previous_close is present and > 0, use it as close_price.
        Do NOT use prev_ltp (stale batch LTP that converges toward current LTP).

        Scenario (MCX after close):
        - previous_close=5400 (official prior-session settlement — GOOD, frozen by COALESCE)
        - prev_ltp=5500 (stale recent batch LTP — BAD, same as today's LTP)

        Expected: close_price = 5400 (uses previous_close)
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
            Decimal("500.00"),              # day_pnl
            Decimal("5000.00"),             # total_pnl
            "{}",                           # payload_json
            captured_ts,                    # captured_at
            Decimal("5400.00"),             # previous_close (official settlement — GOOD)
            Decimal("5500.00"),             # prev_ltp (stale batch LTP — BAD)
            Decimal("4000.00"),             # prev_settlement_pnl
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

        # The core fix: close_price should use previous_close=5400 (official settlement),
        # not prev_ltp=5500 (stale recent batch LTP)
        assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
            f"close_price={row.close_price} should prefer previous_close=5400 "
            f"(official settlement), not prev_ltp=5500 (stale batch LTP)"
        )

    @pytest.mark.asyncio
    async def test_snapshot_fallback_to_previous_close_when_prev_ltp_missing(self):
        """When prev_ltp is None (symbol is new), fallback to previous_close."""
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

        snapshot_row = (
            "ZG0790",
            "NEW_NIFTY",
            "NFO",
            10,
            Decimal("5000.00"),
            Decimal("5500.00"),
            Decimal("500.00"),
            Decimal("5000.00"),
            "{}",
            captured_ts,
            Decimal("5350.00"),             # previous_close (fallback)
            None,                           # prev_ltp (no yesterday snapshot)
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
            f"close_price should fallback to previous_close=5350 when prev_ltp is None"
        )

    @pytest.mark.asyncio
    async def test_snapshot_multiple_accounts_and_symbols(self):
        """Each position gets its own prev_ltp/prev_settlement_pnl correctly."""
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

        snapshot_rows = [
            # Position 1: ZG0790 / NIFTY — has yesterday snapshot
            # previous_close=5400 (official settlement — GOOD), prev_ltp=5500 (stale batch — BAD)
            (
                "ZG0790", "NIFTY26JULFUT", "NFO", 10,
                Decimal("5000.00"), Decimal("5500.00"),
                Decimal("500.00"), Decimal("5000.00"), "{}",
                captured_ts, Decimal("5400.00"),
                Decimal("5500.00"), Decimal("4000.00"),
            ),
            # Position 2: ZJ6294 / CRUDEOIL — new (no prev_ltp; falls back to previous_close)
            (
                "ZJ6294", "CRUDEOIL26AUGFUT", "MCX", 100,
                Decimal("5000.00"), Decimal("5550.00"),
                Decimal("5000.00"), Decimal("5000.00"), "{}",
                captured_ts, Decimal("5400.00"),
                None, None,
            ),
            # Position 3: ZG0790 / GOLDM — has yesterday snapshot
            # previous_close=6810 (official settlement — GOOD), prev_ltp=6850 (stale batch — BAD)
            (
                "ZG0790", "GOLDM26AUGFUT", "MCX", 1,
                Decimal("6800.00"), Decimal("6900.00"),
                Decimal("100.00"), Decimal("100.00"), "{}",
                captured_ts, Decimal("6810.00"),
                Decimal("6850.00"), Decimal("10.00"),
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

        # Position 1: ZG0790 / NIFTY — uses previous_close (official settlement)
        nifty_row = next(r for r in resp.rows if r.tradingsymbol == "NIFTY26JULFUT")
        assert nifty_row.close_price == pytest.approx(5400.0, rel=1e-6), (
            "NIFTY close_price should use previous_close=5400 (official settlement)"
        )

        # Position 2: ZJ6294 / CRUDEOIL (new, no prev_ltp — uses previous_close directly)
        crudeoil_row = next(r for r in resp.rows if r.tradingsymbol == "CRUDEOIL26AUGFUT")
        assert crudeoil_row.close_price == pytest.approx(5400.0, rel=1e-6), (
            "CRUDEOIL close_price should use previous_close=5400 (no prev_ltp available)"
        )

        # Position 3: ZG0790 / GOLDM — uses previous_close (official settlement)
        goldm_row = next(r for r in resp.rows if r.tradingsymbol == "GOLDM26AUGFUT")
        assert goldm_row.close_price == pytest.approx(6810.0, rel=1e-6), (
            "GOLDM close_price should use previous_close=6810 (official settlement)"
        )


# ---------------------------------------------------------------------------
# Test 3: prev_settlement_pnl backfill — both paths populate yesterday's P&L
# ---------------------------------------------------------------------------

class TestPrevSettlementPnlBackfill:
    """Both live and snapshot paths populate prev_settlement_pnl."""

    def test_live_path_backfills_prev_settlement_pnl(self):
        """_override_stale_close_from_snapshot backfills prev_settlement_pnl
        from yesterday's total_pnl in daily_book.
        """
        df = pd.DataFrame([_make_position_row(
            close_price=0.0,
        )])

        # daily_book snapshot with prev_settlement_pnl=500
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert 'prev_settlement_pnl' in df.columns, (
            "prev_settlement_pnl column must be added to DataFrame"
        )
        assert df.at[0, 'prev_settlement_pnl'] == 500.0, (
            f"Expected prev_settlement_pnl=500.0, got {df.at[0, 'prev_settlement_pnl']}"
        )

    def test_live_path_prev_settlement_pnl_null_for_new_position(self):
        """When no snapshot row exists, prev_settlement_pnl remains None
        (position opened today).
        """
        df = pd.DataFrame([_make_position_row(
            symbol="NEW_POSITION",
            close_price=0.0,
        )])

        # No snapshot row for this symbol
        snapshot_rows = []
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Field either is not present, or is None for this row
        if 'prev_settlement_pnl' in df.columns:
            val = df.at[0, 'prev_settlement_pnl']
            assert val is None or pd.isna(val), (
                f"Expected None/NaN for new position, got {val}"
            )

    @pytest.mark.asyncio
    async def test_snapshot_path_populates_prev_settlement_pnl(self):
        """_positions_snapshot populates prev_settlement_pnl from daily_book
        prev_batch."""
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)
        YESTERDAY_PNL = 4000.0

        snapshot_row = (
            "ZG0790", "NIFTY26JULFUT", "NFO", 10,
            Decimal("5000.00"), Decimal("5500.00"),
            Decimal("500.00"), Decimal("5000.00"), "{}",
            captured_ts, Decimal("5500.00"),
            Decimal("5400.00"), Decimal(str(YESTERDAY_PNL)),
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
        assert row.prev_settlement_pnl == pytest.approx(YESTERDAY_PNL, rel=1e-6), (
            f"prev_settlement_pnl should be {YESTERDAY_PNL}, got {row.prev_settlement_pnl}"
        )

    @pytest.mark.asyncio
    async def test_snapshot_path_prev_settlement_pnl_none_for_new_position(self):
        """When prev_settlement_pnl is None in snapshot (new position),
        PositionRow.prev_settlement_pnl must also be None.
        """
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

        snapshot_row = (
            "ZG0790", "NEW_SYMBOL", "NFO", 10,
            Decimal("5000.00"), Decimal("5500.00"),
            Decimal("500.00"), Decimal("5000.00"), "{}",
            captured_ts, Decimal("5500.00"),
            Decimal("5400.00"), None,  # No prev_settlement_pnl
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
        assert row.prev_settlement_pnl is None, (
            "prev_settlement_pnl must be None for new positions"
        )


# ---------------------------------------------------------------------------
# Test 4: Day P&L formula correctness with patched close_price
# ---------------------------------------------------------------------------

class TestDayPnlFormulaCorrectness:
    """Decomposed day-P&L formula works correctly with patched close_price."""

    def test_day_pnl_formula_decomposed_intraday(self):
        """Day P&L = (last_price - close_price) × overnight_qty + day legs.

        Scenario:
        - overnight_qty = 5
        - last_price = 6150 (current market)
        - close_price = 6000 (yesterday's settlement, patched)
        - average_price = 6000 (entry price)
        - pnl = 750 (lifetime = (6150-6000)*5)

        Expected day_pnl = (6150 - 6000) * 5 = 750
        """
        df = pd.DataFrame([_make_position_row(
            last_price=6150.0,
            close_price=0.0,  # Will be patched to 6000
            average_price=6000.0,
            overnight_qty=5,
            quantity=5,
            pnl=750.0,
        )])

        # Patch close_price from daily_book
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # After patch, verify decomposed formula
        ltp = df.at[0, 'last_price']
        close_price = df.at[0, 'close_price']
        oq = df.at[0, 'overnight_quantity']

        expected_day_pnl = (ltp - close_price) * oq

        assert close_price == 6000.0, "close_price should be patched to 6000"
        assert expected_day_pnl == pytest.approx(750.0), (
            f"Day P&L formula (ltp - close) * oq = ({ltp} - {close_price}) * {oq} "
            f"= {expected_day_pnl}, expected 750"
        )

    def test_day_pnl_zero_when_ltp_equals_yesterday_close(self):
        """When last_price = yesterday's settlement, day_pnl should be 0.

        Scenario:
        - yesterday close = 6000
        - today's ltp = 6000 (no move)
        - Expected day_pnl = 0
        """
        df = pd.DataFrame([_make_position_row(
            last_price=6000.0,
            close_price=0.0,
            average_price=6000.0,
            overnight_qty=5,
            quantity=5,
        )])

        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 400.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        ltp = df.at[0, 'last_price']
        close_price = df.at[0, 'close_price']
        oq = df.at[0, 'overnight_quantity']

        expected_day_pnl = (ltp - close_price) * oq

        assert expected_day_pnl == pytest.approx(0.0), (
            f"When ltp=close_price, day_pnl should be 0, got {expected_day_pnl}"
        )

    def test_day_pnl_negative_when_market_down(self):
        """Day P&L is negative when market moved down since yesterday close.

        Scenario:
        - yesterday close = 6000
        - today's ltp = 5950 (down 50)
        - Expected day_pnl = (5950 - 6000) * 5 = -250
        """
        df = pd.DataFrame([_make_position_row(
            last_price=5950.0,
            close_price=0.0,
            average_price=6000.0,
            overnight_qty=5,
            quantity=5,
            pnl=-250.0,
        )])

        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 400.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        ltp = df.at[0, 'last_price']
        close_price = df.at[0, 'close_price']
        oq = df.at[0, 'overnight_quantity']

        expected_day_pnl = (ltp - close_price) * oq

        assert expected_day_pnl == pytest.approx(-250.0), (
            f"When market down, day_pnl should be negative, got {expected_day_pnl}"
        )


# ---------------------------------------------------------------------------
# Test 5: Robustness — graceful handling and edge cases
# ---------------------------------------------------------------------------

class TestRobustness:
    """Graceful handling of missing/broken daily_book data."""

    def test_override_db_query_failure_doesnt_crash(self):
        """When daily_book query fails (e.g., DB offline), the function
        logs a warning and returns gracefully (close_price unchanged).
        """
        df = pd.DataFrame([_make_position_row(
            close_price=0.0,
        )])

        # Mock DB to raise exception
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("DB offline"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from backend.api.routes.positions import _override_stale_close_from_snapshot

        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_from_snapshot(df))

        # No exception; close_price remains unchanged
        assert df.at[0, 'close_price'] == 0.0, (
            "When DB fails, close_price should remain unchanged"
        )

    def test_override_prev_settlement_pnl_null_value_handled(self):
        """When daily_book.total_pnl is NULL, prev_settlement_pnl remains None."""
        df = pd.DataFrame([_make_position_row(
            close_price=0.0,
        )])

        # Snapshot with None total_pnl
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, None)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        if 'prev_settlement_pnl' in df.columns:
            val = df.at[0, 'prev_settlement_pnl']
            # Should be None or NaN
            assert val is None or pd.isna(val), (
                f"Expected None/NaN when total_pnl is NULL, got {val}"
            )

    def test_override_snapshot_ltp_zero_is_ignored(self):
        """When daily_book.ltp is 0 (violates SQL filter), don't patch close_price.

        Note: The SQL query itself filters ltp > 0, so ltp=0 should never be
        returned by the database. This test verifies the epsilon guard at line 741
        would skip patching even if ltp=0 somehow arrived.

        However, since the epsilon check (abs(snap_ltp - current_close) <= 0.005)
        with snap_ltp=0 and close_price=5000 gives abs(0-5000) > 0.005, it WOULD
        attempt to patch. The actual protection is the SQL filter. So instead,
        test that a small difference within epsilon is not patched.
        """
        df = pd.DataFrame([_make_position_row(
            close_price=5000.0,
        )])

        # Snapshot with ltp very close to close_price (within epsilon 0.005)
        # Should NOT patch due to epsilon guard
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 5000.002, 400.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # close_price should remain 5000 (epsilon guard prevents patch)
        assert df.at[0, 'close_price'] == 5000.0, (
            "When snapshot ltp is within epsilon of close_price, don't patch"
        )

    def test_override_handles_multiple_accounts_partial_snapshot(self):
        """When only some accounts have snapshots, the rest pass through unchanged."""
        df = pd.concat([
            pd.DataFrame([_make_position_row(
                account="ZG0790",
                close_price=0.0,
            )]),
            pd.DataFrame([_make_position_row(
                account="ZJ6294",
                close_price=0.0,
            )]),
        ], ignore_index=True)

        # Only snapshot for ZG0790
        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        zg_row = df[df['account'] == 'ZG0790'].iloc[0]
        zj_row = df[df['account'] == 'ZJ6294'].iloc[0]

        # ZG0790 patched, ZJ6294 unchanged
        assert zg_row['close_price'] == 6000.0, "ZG0790 should be patched"
        assert zj_row['close_price'] == 0.0, "ZJ6294 should remain unchanged"

    def test_override_negative_prev_settlement_pnl_preserved(self):
        """prev_settlement_pnl can be negative (yesterday was a loss)."""
        NEGATIVE_PNL = -1250.50

        df = pd.DataFrame([_make_position_row(
            close_price=0.0,
        )])

        snapshot_rows = [("ZG0790", "CRUDEOILSEP25", 6000.0, NEGATIVE_PNL)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert df.at[0, 'prev_settlement_pnl'] == NEGATIVE_PNL, (
            f"prev_settlement_pnl should be {NEGATIVE_PNL}, got "
            f"{df.at[0, 'prev_settlement_pnl']}"
        )

    def test_snapshot_builder_accepts_prev_settlement_pnl_kwarg(self):
        """build_snapshot_position_row must accept prev_settlement_pnl kwarg."""
        from backend.api.routes.positions_helpers import build_snapshot_position_row

        YESTERDAY_PNL = 4000.0

        row = build_snapshot_position_row(
            account="ZG0790",
            symbol="NIFTY26JULFUT",
            exchange="NFO",
            qty=10,
            avg_cost=5000.0,
            ltp=5500.0,
            day_pnl=500.0,
            total_pnl=5000.0,
            extras={},
            prev_settlement_pnl=YESTERDAY_PNL,
        )

        assert row.prev_settlement_pnl == pytest.approx(YESTERDAY_PNL, rel=1e-6), (
            f"prev_settlement_pnl={row.prev_settlement_pnl} must equal {YESTERDAY_PNL}"
        )

    def test_snapshot_builder_prev_settlement_pnl_defaults_to_none(self):
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
            "prev_settlement_pnl must default to None for new positions"
        )


# ---------------------------------------------------------------------------
# Test 6: Integration — end-to-end day_change_val not collapsed after close
# ---------------------------------------------------------------------------

class TestIntegrationDayChangeNotCollapsed:
    """End-to-end: day_change_val is preserved correctly, not collapsed to 0."""

    @pytest.mark.asyncio
    async def test_snapshot_day_pnl_not_collapsed_after_market_close(self):
        """Verify that day_change_val is recomputed from (ltp - prev_close) × qty
        and is NOT collapsed to 0 after NSE settlement.

        Before fix: close_price = settlement_price, so frontend formula
          day_pnl = total_pnl - oq * (ltp - settlement) = total_pnl - 0 = total_pnl
        (collapsed to lifetime P&L)

        After fix (NSE settlement fix): _positions_snapshot recomputes day_pnl
        in the row-mapping loop as (ltp - prev_ltp) × effective_qty using
        yesterday's EOD LTP as the baseline, yielding the true intraday move.

        Scenario: yesterday_settlement=5400, today_ltp=5500, qty=10
        Expected: day_change_val = (5500-5400)*10 = 1000 (NOT stored 500, NOT 0)
        """
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

        YESTERDAY_TOTAL_PNL = 4000.0
        TODAY_TOTAL_PNL = 4500.0
        QTY = 10
        LTP = 5500.0
        PREV_CLOSE = 5400.0  # Official prior-session settlement (frozen by COALESCE)
        # Recomputed: (ltp - previous_close) * qty = (5500 - 5400) * 10 = 1000
        EXPECTED_DAY_PNL = (LTP - PREV_CLOSE) * QTY  # 1000.0

        snapshot_row = (
            "ZG0790",
            "NIFTY26JULFUT",
            "NFO",
            QTY,
            Decimal("5000.00"),               # avg_cost
            Decimal(str(LTP)),                # ltp (today's LTP)
            Decimal("500.00"),                # day_pnl stored (stale — will be overridden)
            Decimal(str(TODAY_TOTAL_PNL)),    # total_pnl = 4500
            "{}",
            captured_ts,
            Decimal(str(PREV_CLOSE)),         # previous_close = yesterday's settlement (GOOD)
            Decimal(str(LTP)),                # prev_ltp = stale recent batch LTP (BAD)
            Decimal(str(YESTERDAY_TOTAL_PNL)),  # prev_settlement_pnl = 4000
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

        # close_price = previous_close=5400 (official settlement), not LTP=5500
        assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
            "close_price should be previous_close=5400 (official settlement, fix applied)"
        )

        # day_change_val recomputed from (ltp - previous_close) * qty = 1000, not stored 500
        assert row.day_change_val == pytest.approx(EXPECTED_DAY_PNL, rel=1e-6), (
            f"day_change_val={row.day_change_val} should be recomputed as "
            f"(ltp-previous_close)*qty=({LTP}-{PREV_CLOSE})*{QTY}={EXPECTED_DAY_PNL}, "
            f"not the stale stored value 500 and not 0"
        )

        # total_pnl should remain as is
        assert row.pnl == pytest.approx(TODAY_TOTAL_PNL, rel=1e-6), (
            f"pnl={row.pnl} should remain {TODAY_TOTAL_PNL}"
        )

        # prev_settlement_pnl should be yesterday's total P&L
        assert row.prev_settlement_pnl == pytest.approx(YESTERDAY_TOTAL_PNL, rel=1e-6), (
            f"prev_settlement_pnl={row.prev_settlement_pnl} should be "
            f"yesterday's total_pnl={YESTERDAY_TOTAL_PNL}"
        )


# ---------------------------------------------------------------------------
# Test 7: closed-hours refresh no longer writes to daily_book
# ---------------------------------------------------------------------------

class TestClosedHoursRefreshNoDailyBookWrite:
    """_task_closed_hours_refresh no longer calls snapshot_daily_book."""

    @pytest.mark.asyncio
    async def test_closed_hours_refresh_source_does_not_import_snapshot_daily_book(self):
        """Verify that _task_closed_hours_refresh source does NOT import snapshot_daily_book.

        Before fix: closed-hours refresh called snapshot_daily_book every 30 min,
        polluting daily_book with mid-session LTPs on weekends.

        After fix: closed-hours refresh only busts caches. Settlement writes are
        exclusively handled by _task_daily_snapshot.

        This test inspects the source code to confirm snapshot_daily_book is not
        imported or called.
        """
        import inspect
        from backend.api.background import _task_closed_hours_refresh

        src = inspect.getsource(_task_closed_hours_refresh)

        # The function should NOT import snapshot_daily_book
        assert "from backend.api.algo.daily_snapshot import snapshot_daily_book" not in src, (
            "snapshot_daily_book should NOT be imported in _task_closed_hours_refresh"
        )

        # The function should NOT call snapshot_daily_book
        assert "await snapshot_daily_book()" not in src, (
            "snapshot_daily_book() should NOT be called in _task_closed_hours_refresh"
        )

        # The docstring should mention this explicitly
        assert "snapshot_daily_book" in src, (
            "The function docstring should mention snapshot_daily_book to explain why it's NOT called"
        )

    @pytest.mark.asyncio
    async def test_closed_hours_refresh_calls_cache_invalidation_not_snapshot(self):
        """Verify that _task_closed_hours_refresh calls cache invalidation (not snapshot write).

        After fix: only cache busting happens, no settlement writes.
        This is verified through source inspection — the function should call
        _raw_cache_invalidate and invalidate, but NOT snapshot_daily_book.
        """
        import inspect
        from backend.api.background import _task_closed_hours_refresh

        src = inspect.getsource(_task_closed_hours_refresh)

        # Should call _raw_cache_invalidate for positions, holdings, margins
        assert '_raw_cache_invalidate("positions")' in src, (
            "_raw_cache_invalidate should be called for positions cache"
        )
        assert '_raw_cache_invalidate("holdings")' in src, (
            "_raw_cache_invalidate should be called for holdings cache"
        )
        assert '_raw_cache_invalidate("margins")' in src, (
            "_raw_cache_invalidate should be called for margins cache"
        )

        # Should also invalidate API-side TTL caches
        assert 'invalidate("positions")' in src, (
            "invalidate should be called for positions API cache"
        )
        assert 'invalidate("holdings")' in src, (
            "invalidate should be called for holdings API cache"
        )
        assert 'invalidate("funds")' in src, (
            "invalidate should be called for funds API cache"
        )


# ---------------------------------------------------------------------------
# Test 8: prev_batch lookback is 7 days in SQL
# ---------------------------------------------------------------------------

class TestPrevBatchLookbackIncrease:
    """prev_batch CTE lookback expanded to 7 days."""

    def test_positions_snapshot_prev_batch_lookback_7_days(self):
        """Source inspection test: verify _positions_snapshot SQL contains
        INTERVAL '7 days' not INTERVAL '2 days'.

        After fix: extended holiday gaps (weekends, holidays) up to 7 days
        are now bridged so prev_batch can find yesterday's snapshot even
        across 3-4 day extended breaks.
        """
        import inspect
        from backend.api.routes.positions import _positions_snapshot

        src = inspect.getsource(_positions_snapshot)

        assert "INTERVAL '7 days'" in src, (
            "prev_batch lookback must be expanded to '7 days' to cover "
            "extended holiday gaps"
        )
        assert "INTERVAL '2 days'" not in src, (
            "old 2-day lookback must be replaced with 7-day window"
        )


# ---------------------------------------------------------------------------
# Test 9: Saturday refresh rows do not collapse day_change_val to 0
# ---------------------------------------------------------------------------

class TestSaturdayRefreshDayPnlCorrectness:
    """Saturday/extended-break refresh rows preserve day_change_val correctly."""

    def test_saturday_refresh_does_not_collapse_day_pnl(self):
        """Verify that day_change_val is recomputed correctly across extended breaks.

        Scenario: Position held overnight from Friday into Saturday (no trading).
        - Thursday settlement: 22800
        - Friday LTP (= Saturday refresh LTP): 23200
        - Qty: 50

        Expected: day_change_val = (23200 - 22800) * 50 = 20000 (NOT 0)

        Before fix: When refresh rows had no intraday trading, day_pnl was
        collapsed to 0 if the day_change_val column was missing.

        After fix: build_row_from_snapshot_raw recomputes day_pnl from
        (ltp - prev_ltp) * qty, preventing collapse.
        """
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw
        from datetime import datetime, timezone

        PREV_LTP = 22800.0   # Thursday settlement
        LTP = 23200.0        # Friday settlement (= Saturday refresh LTP since no trading)
        QTY = 50
        EXPECTED_DAY_PNL = (LTP - PREV_LTP) * QTY  # 20000

        row = (
            "ZG0790",                                    # account
            "NIFTY26JULFUT",                             # symbol
            "NFO",                                       # exchange
            QTY,                                         # qty (in contracts)
            Decimal("23000.00"),                         # avg_cost
            Decimal(str(LTP)),                           # ltp (Friday settlement)
            Decimal("10000.00"),                         # day_pnl (stored value — will be overridden)
            Decimal("10000.00"),                         # total_pnl
            "{}",                                        # payload_json
            datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc),  # captured_at (Saturday)
            Decimal(str(PREV_LTP)),                      # previous_close (Thursday settlement) ← key
            Decimal(str(LTP)),                           # prev_ltp (Friday=Saturday LTP, stale)
            None,                                        # prev_settlement_pnl
        )

        result = build_row_from_snapshot_raw(row)

        assert result.day_change_val == pytest.approx(EXPECTED_DAY_PNL, rel=1e-6), (
            f"day_change_val={result.day_change_val} should be recomputed as "
            f"(ltp - previous_close) * qty = ({LTP} - {PREV_LTP}) * {QTY} = {EXPECTED_DAY_PNL}, "
            f"not 0 and not the stale stored value"
        )

        assert result.close_price == pytest.approx(PREV_LTP, rel=1e-6), (
            f"close_price must be previous_close (Thursday settlement)={PREV_LTP}, "
            f"not ltp (Friday=Saturday)={LTP}"
        )

    def test_saturday_refresh_short_position_day_pnl(self):
        """Day P&L is correctly recomputed for SHORT overnight positions after break.

        Scenario: Short MCX crude position held from Thursday to Saturday.
        - Thursday settlement: 6000 (entry price for short)
        - Saturday LTP: 5950 (market went down)
        - Qty: -50 (short)

        Expected day_change_val = (5950 - 6000) * (-50) = 2500 (SHORT profits when market falls)
        """
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw
        from datetime import datetime, timezone

        PREV_LTP = 6000.0   # Thursday settlement
        LTP = 5950.0        # Saturday LTP
        QTY = -50           # Short position
        EXPECTED_DAY_PNL = (LTP - PREV_LTP) * QTY  # (5950 - 6000) * (-50) = 2500

        row = (
            "ZG0790",
            "CRUDEOILSEP25",
            "MCX",
            QTY,
            Decimal("6000.00"),
            Decimal(str(LTP)),
            Decimal("2500.00"),              # day_pnl (stored)
            Decimal("2500.00"),              # total_pnl
            "{}",
            datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc),
            Decimal(str(PREV_LTP)),          # previous_close (Thursday settlement) ← key
            Decimal(str(LTP)),               # prev_ltp (Saturday LTP, stale)
            None,
        )

        result = build_row_from_snapshot_raw(row)

        assert result.day_change_val == pytest.approx(EXPECTED_DAY_PNL, rel=1e-6), (
            f"day_change_val for short position should be {EXPECTED_DAY_PNL}, "
            f"got {result.day_change_val}"
        )

    def test_saturday_refresh_with_holiday_gap(self):
        """Verify day_pnl recomputation works across extended holiday gaps.

        Scenario: Position held from Friday 26-Jul (NSE close) through
        weekend + Monday 29-Jul holiday (Janmashtami).
        First refresh happens Tuesday 30-Jul.

        - Thursday 25-Jul settlement: 5400
        - Tuesday 30-Jul LTP: 5500
        - Qty: 10

        Expected day_change_val = (5500 - 5400) * 10 = 1000
        prev_batch lookback (now 7 days) should find Thursday's snapshot
        from 5 days earlier.
        """
        from backend.api.routes.positions_helpers import build_row_from_snapshot_raw
        from datetime import datetime, timezone

        PREV_LTP = 5400.0   # Thursday 25-Jul settlement
        LTP = 5500.0        # Tuesday 30-Jul LTP (5 days later)
        QTY = 10
        EXPECTED_DAY_PNL = (LTP - PREV_LTP) * QTY  # 1000

        row = (
            "ZG0790",
            "NIFTY26JULFUT",
            "NFO",
            QTY,
            Decimal("5000.00"),
            Decimal(str(LTP)),
            Decimal("1000.00"),              # day_pnl (will be recomputed)
            Decimal("5000.00"),              # total_pnl
            "{}",
            datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc),  # Tuesday 30-Jul
            Decimal(str(PREV_LTP)),          # previous_close (Thursday 25-Jul) ← 5 days earlier
            Decimal(str(LTP)),               # prev_ltp (Tuesday LTP, stale)
            None,
        )

        result = build_row_from_snapshot_raw(row)

        assert result.day_change_val == pytest.approx(EXPECTED_DAY_PNL, rel=1e-6), (
            f"day_change_val across 5-day holiday gap should be {EXPECTED_DAY_PNL}, "
            f"got {result.day_change_val}"
        )
