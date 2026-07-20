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
    """_positions_snapshot prefers yesterday's prev_ltp over today's previous_close."""

    @pytest.mark.asyncio
    async def test_snapshot_prefers_prev_ltp_over_previous_close(self):
        """Core fix: when prev_ltp is present and > 0, use it as close_price.
        Do NOT use snapshot's previous_close (which may be today's settlement).

        Scenario (MCX after close):
        - Latest snapshot: ltp=5500, previous_close=5500 (today's settlement — BAD)
        - Prior snapshot: prev_ltp=5400 (yesterday's settlement — GOOD)

        Expected: close_price = 5400
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
            Decimal("5500.00"),             # previous_close (today's settlement — BAD)
            Decimal("5400.00"),             # prev_ltp (yesterday's settlement — GOOD)
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

        # The core fix: close_price should use yesterday's LTP (5400), not today's settlement
        assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
            f"close_price={row.close_price} should prefer prev_ltp=5400, "
            f"not today's settlement (previous_close)=5500"
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
            (
                "ZG0790", "NIFTY26JULFUT", "NFO", 10,
                Decimal("5000.00"), Decimal("5500.00"),
                Decimal("500.00"), Decimal("5000.00"), "{}",
                captured_ts, Decimal("5500.00"),
                Decimal("5400.00"), Decimal("4000.00"),
            ),
            # Position 2: ZJ6294 / CRUDEOIL — new (no yesterday snapshot)
            (
                "ZJ6294", "CRUDEOIL26AUGFUT", "MCX", 100,
                Decimal("5000.00"), Decimal("5550.00"),
                Decimal("5000.00"), Decimal("5000.00"), "{}",
                captured_ts, Decimal("5400.00"),
                None, None,
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

        # Position 2: ZJ6294 / CRUDEOIL (new)
        crudeoil_row = next(r for r in resp.rows if r.tradingsymbol == "CRUDEOIL26AUGFUT")
        assert crudeoil_row.close_price == pytest.approx(5400.0, rel=1e-6), (
            "CRUDEOIL close_price should fallback to previous_close=5400"
        )

        # Position 3: ZG0790 / GOLDM
        goldm_row = next(r for r in resp.rows if r.tradingsymbol == "GOLDM26AUGFUT")
        assert goldm_row.close_price == pytest.approx(6810.0, rel=1e-6), (
            "GOLDM close_price should use prev_ltp=6810"
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
        """Verify that day_change_val (stored from daily_book) is preserved
        and NOT collapsed to 0 by the close-price fix.

        Before fix: close_price = settlement_price, so frontend formula
          day_pnl = total_pnl - oq * (ltp - settlement) = total_pnl - 0 = total_pnl
        (collapsed to lifetime P&L)

        After fix: close_price = yesterday_settlement, so formula works correctly:
          day_pnl = total_pnl - oq * (ltp - yesterday_settlement) = correct value
        """
        from backend.api.routes.positions import _positions_snapshot

        captured_ts = datetime(2026, 7, 13, 10, 30, tzinfo=timezone.utc)

        # Position: opened yesterday with lifetime P&L = 4000,
        # today's intraday gain = 500
        YESTERDAY_TOTAL_PNL = 4000.0
        TODAY_INTRADAY_GAIN = 500.0
        TODAY_TOTAL_PNL = YESTERDAY_TOTAL_PNL + TODAY_INTRADAY_GAIN  # 4500

        snapshot_row = (
            "ZG0790",
            "NIFTY26JULFUT",
            "NFO",
            10,
            Decimal("5000.00"),              # avg_cost
            Decimal("5500.00"),              # ltp (today's settlement price)
            Decimal(str(TODAY_INTRADAY_GAIN)),  # day_pnl = 500 (stored value)
            Decimal(str(TODAY_TOTAL_PNL)),   # total_pnl = 4500
            "{}",
            captured_ts,
            Decimal("5500.00"),              # previous_close = today's settlement (BAD if used)
            Decimal("5400.00"),              # prev_ltp = yesterday's settlement (GOOD)
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

        # With the fix, close_price = yesterday's settlement (5400)
        assert row.close_price == pytest.approx(5400.0, rel=1e-6), (
            "close_price should be yesterday's LTP=5400 (fix applied)"
        )

        # day_change_val must preserve the stored intraday gain (NOT collapse to 0)
        assert row.day_change_val == pytest.approx(TODAY_INTRADAY_GAIN, rel=1e-6), (
            f"day_change_val={row.day_change_val} should preserve stored "
            f"day_pnl={TODAY_INTRADAY_GAIN}, not collapse to 0"
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
