"""Tests for holdings snapshot overlay and close-override error handling.

Covers:
  - _overlay_snapshot_for_closed_exchanges: per-exchange overlay logic
    1. All exchanges open → _hold_tag_open_row applied to all rows
    2. All exchanges closed, snapshot available → _hold_tag_closed_row with snapshot LTP
    3. All exchanges closed, no snapshot → _hold_tag_closed_row with snap_ltp=None (fallback to broker LTP)
    4. Mixed open/closed → appropriate tagging per row's exchange
  - _override_stale_close_for_holdings DB failure path
    1. DB raises exception → function logs warning and returns early
    2. previous_close not set to 0.0 for all rows (column absent/unchanged)
    3. Empty snapshot_map path → returns early, previous_close unchanged
"""

import asyncio
from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import msgspec
import pandas as pd
import pytest


IST = ZoneInfo("Asia/Kolkata")


def _make_holding_row(
    account: str = "TEST001",
    tradingsymbol: str = "RELIANCE",
    exchange: str = "NSE",
    quantity: int = 50,
    last_price: float = 150.0,
    close_price: float = 145.0,
    average_price: float = 140.0,
) -> object:
    """Build a minimal HoldingRow-like msgspec struct for overlay tests."""
    from backend.api.schemas import HoldingRow
    return HoldingRow(
        account=account,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        quantity=quantity,
        opening_quantity=quantity,
        average_price=average_price,
        close_price=close_price,
        last_price=last_price,
        inv_val=average_price * quantity,
        cur_val=last_price * quantity,
        pnl=(last_price - average_price) * quantity,
        pnl_percentage=0.0,
        day_change_val=0.0,
        day_change=(last_price - close_price),
        day_change_percentage=0.0,
        last_price_stale=False,
        price_source="live",
        current_price=last_price,
        is_animating=False,
        previous_close=0.0,
        pnl_per_share=0.0,
    )


class TestHoldingsOverlaySnapshotForClosedExchanges:
    """Tests for _overlay_snapshot_for_closed_exchanges."""

    @pytest.mark.asyncio
    async def test_all_exchanges_open(self):
        """All exchanges open → all rows tagged with _hold_tag_open_row."""
        from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges

        rows = [
            _make_holding_row(exchange="NSE"),
            _make_holding_row(exchange="BSE", tradingsymbol="INFY"),
        ]

        with patch(
            "backend.api.routes.holdings.is_exchange_closed_now",
            return_value=False,
        ):
            result = await _overlay_snapshot_for_closed_exchanges(rows)

        assert len(result) == 2
        # Verify that _hold_tag_open_row was applied (price_source should reflect open market)
        assert all(
            r.price_source in ("live", "delayed") for r in result
        ), "All rows should be tagged for open exchanges"

    @pytest.mark.asyncio
    async def test_all_exchanges_closed_with_snapshot(self):
        """All exchanges closed, snapshot LTP available → use snapshot LTP."""
        from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges

        rows = [
            _make_holding_row(exchange="NSE", last_price=150.0),
        ]

        # Mock is_exchange_closed_now to return True for NSE
        def mock_is_closed(exch):
            return exch.upper() == "NSE"

        # Mock latest_snapshot_ltp_map to return a snapshot entry
        snapshot_map = {("TEST001", "RELIANCE"): 148.5}

        with (
            patch(
                "backend.api.routes.holdings.is_exchange_closed_now",
                side_effect=mock_is_closed,
            ),
            patch(
                "backend.api.routes.holdings.latest_snapshot_ltp_map",
                return_value=snapshot_map,
            ),
        ):
            result = await _overlay_snapshot_for_closed_exchanges(rows)

        assert len(result) == 1
        # Snapshot LTP should be applied if resolve_current_price selected it
        assert result[0].current_price is not None, "Snapshot path should set current_price"
        # price_source should indicate snapshot (closed exchange overlay)
        assert "snapshot" in result[0].price_source.lower() or result[0].price_source in (
            "snapshot_settled", "snapshot_quote"
        ), f"Expected snapshot source, got {result[0].price_source}"

    @pytest.mark.asyncio
    async def test_all_exchanges_closed_no_snapshot(self):
        """All exchanges closed, no snapshot available → use broker LTP."""
        from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges

        rows = [
            _make_holding_row(exchange="NSE", last_price=150.0),
        ]

        def mock_is_closed(exch):
            return exch.upper() == "NSE"

        # Empty snapshot map — no snapshot for this row
        snapshot_map = {}

        with (
            patch(
                "backend.api.routes.holdings.is_exchange_closed_now",
                side_effect=mock_is_closed,
            ),
            patch(
                "backend.api.routes.holdings.latest_snapshot_ltp_map",
                return_value=snapshot_map,
            ),
        ):
            result = await _overlay_snapshot_for_closed_exchanges(rows)

        assert len(result) == 1
        # Fallback to broker LTP (from row.last_price)
        assert result[0].current_price == 150.0, "Should use broker LTP when no snapshot"

    @pytest.mark.asyncio
    async def test_mixed_open_and_closed_exchanges(self):
        """Mixed open/closed → each row tagged according to its exchange state."""
        from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges

        rows = [
            _make_holding_row(exchange="NSE", last_price=150.0, tradingsymbol="RELIANCE"),
            _make_holding_row(exchange="MCX", last_price=100.0, tradingsymbol="CRUDEOIL"),
        ]

        def mock_is_closed(exch):
            # NSE closed, MCX open
            return exch.upper() == "NSE"

        snapshot_map = {("TEST001", "RELIANCE"): 148.5}  # Only NSE snapshot

        with (
            patch(
                "backend.api.routes.holdings.is_exchange_closed_now",
                side_effect=mock_is_closed,
            ),
            patch(
                "backend.api.routes.holdings.latest_snapshot_ltp_map",
                return_value=snapshot_map,
            ),
        ):
            result = await _overlay_snapshot_for_closed_exchanges(rows)

        assert len(result) == 2
        # NSE row (closed) should have snapshot overlay applied
        nse_row = [r for r in result if r.tradingsymbol == "RELIANCE"][0]
        assert "snapshot" in nse_row.price_source.lower() or nse_row.price_source in (
            "snapshot_settled", "snapshot_quote"
        ), f"NSE (closed) should have snapshot source, got {nse_row.price_source}"

        # MCX row (open) should have live/open market tagging
        mcx_row = [r for r in result if r.tradingsymbol == "CRUDEOIL"][0]
        assert mcx_row.price_source in ("live", "delayed"), (
            f"MCX (open) should have live source, got {mcx_row.price_source}"
        )

    @pytest.mark.asyncio
    async def test_empty_rows_list(self):
        """Empty rows list → returns empty list unchanged."""
        from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges

        result = await _overlay_snapshot_for_closed_exchanges([])
        assert result == []


class TestHoldingsOverrideStaleCloseDBFailurePath:
    """Tests for _override_stale_close_for_holdings DB failure and empty snapshot paths."""

    @pytest.mark.asyncio
    async def test_db_query_raises_exception(self):
        """DB query raises exception → logs warning and returns early without adding previous_close."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        # Create a DataFrame WITHOUT previous_close column
        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 50,
            'last_price': 150.0,
            'close_price': 150.0,
            'average_price': 140.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
        }])

        original_columns = set(df.columns)

        # Mock async_session to raise an exception
        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("DB connection timeout")
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            await _override_stale_close_for_holdings(df)

        # After the function returns:
        # 1. previous_close column should NOT be added (function returned early)
        # 2. The function should have logged a warning (not tested here)
        # 3. DataFrame columns unchanged (except those that existed before)
        assert 'previous_close' not in df.columns, (
            "previous_close should NOT be added when DB query fails"
        )
        assert set(df.columns) == original_columns, (
            "DataFrame columns should remain unchanged when DB query fails"
        )

    @pytest.mark.asyncio
    async def test_empty_snapshot_map_returns_early(self):
        """Empty snapshot_map from DB → function returns early before adding previous_close."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 50,
            'last_price': 150.0,
            'close_price': 150.0,
            'average_price': 140.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
        }])

        original_columns = set(df.columns)

        # Mock DB to return empty result (no snapshot rows)
        mock_result = MagicMock()
        mock_result.all.return_value = []  # Empty snapshot map

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            await _override_stale_close_for_holdings(df)

        # Function should return after the "if not snapshot_map" check (line 454-455)
        # This is BEFORE previous_close column is initialized (line 461-462)
        # So previous_close should NOT be in the DataFrame
        assert 'previous_close' not in df.columns, (
            "previous_close should NOT be added when snapshot_map is empty"
        )
        assert set(df.columns) == original_columns, (
            "DataFrame columns should remain unchanged when snapshot_map is empty"
        )

    @pytest.mark.asyncio
    async def test_previous_close_column_only_set_when_snapshot_exists(self):
        """previous_close is written ONLY for rows with snapshot data."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = pd.DataFrame([
            {
                'account': 'TEST001',
                'tradingsymbol': 'RELIANCE',
                'exchange': 'NSE',
                'quantity': 50,
                'last_price': 150.0,
                'close_price': 150.0,
                'average_price': 140.0,
                'day_change_val': 0.0,
                'day_change': 0.0,
            },
            {
                'account': 'TEST001',
                'tradingsymbol': 'INFY',
                'exchange': 'NSE',
                'quantity': 30,
                'last_price': 200.0,
                'close_price': 200.0,
                'average_price': 195.0,
                'day_change_val': 0.0,
                'day_change': 0.0,
            },
        ])

        # Mock DB to return snapshot for only RELIANCE (not INFY)
        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('TEST001', 'RELIANCE', 148.5),  # RELIANCE snapshot
            # INFY has no snapshot
        ]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            await _override_stale_close_for_holdings(df)

        # RELIANCE should have previous_close patched
        reliance_row = df[df['tradingsymbol'] == 'RELIANCE'].iloc[0]
        assert reliance_row['previous_close'] == 148.5, (
            "RELIANCE should have snapshot previous_close = 148.5"
        )

        # INFY should remain at 0.0 (no snapshot)
        infy_row = df[df['tradingsymbol'] == 'INFY'].iloc[0]
        assert infy_row['previous_close'] == 0.0, (
            "INFY should remain at 0.0 (no snapshot entry)"
        )

    @pytest.mark.asyncio
    async def test_day_change_val_recomputed_when_previous_close_gt_zero(self):
        """day_change_val is recomputed for ALL rows with previous_close > 0."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 50,
            'last_price': 150.0,
            'close_price': 150.0,  # Stale (drifted to LTP)
            'average_price': 140.0,
            'day_change_val': 0.0,  # Stale (was computed against close_price=150)
            'day_change': 0.0,
        }])

        # Snapshot: previous LTP was 148.5
        mock_result = MagicMock()
        mock_result.all.return_value = [
            ('TEST001', 'RELIANCE', 148.5),
        ]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.api.database.async_session",
            return_value=mock_session,
        ):
            await _override_stale_close_for_holdings(df)

        # day_change_val should be recomputed: (ltp - previous_close) × qty
        # = (150 - 148.5) × 50 = 75
        expected_dcv = (150.0 - 148.5) * 50
        assert abs(df.iloc[0]['day_change_val'] - expected_dcv) < 0.01, (
            f"day_change_val should be recomputed to {expected_dcv}, "
            f"got {df.iloc[0]['day_change_val']}"
        )
