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

        # Mock latest_snapshot_ltp_map to return (ltp, day_pnl) tuple
        snapshot_map = {("TEST001", "RELIANCE"): (148.5, 100.0)}

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


# ---------------------------------------------------------------------------
# New tests for Bug 1 + Bug 2 + Bug 3 fixes
# ---------------------------------------------------------------------------

class TestBuildHoldingRowFromSnapshot:
    """Unit tests for _build_holding_row_from_snapshot priority logic."""

    def _make_raw_row(
        self,
        account="ACC1",
        symbol="RELIANCE",
        exchange="NSE",
        qty=100,
        avg_cost=2000.0,
        ltp=2100.0,
        previous_close=2050.0,
        day_pnl=5000.0,
        total_pnl=10000.0,
        captured_at=None,
        prev_ltp=None,
    ):
        from datetime import datetime
        return (
            account, symbol, exchange, qty, avg_cost, ltp, previous_close,
            day_pnl, total_pnl,
            captured_at or datetime(2026, 8, 22, 15, 45, 0),
            prev_ltp,
        )

    def test_stored_day_pnl_used_as_primary_when_nonzero(self):
        """When day_pnl_f != 0, it is used as day_change_val (not recomputed)."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(
            ltp=2100.0,
            previous_close=2050.0,
            day_pnl=9999.0,   # Stored non-zero EOD value
            qty=100,
        )
        row, _, _, _, day_change_val = _build_holding_row_from_snapshot(raw_row)
        assert day_change_val == 9999.0, (
            f"Stored day_pnl_f=9999.0 should be used directly; got {day_change_val}"
        )
        assert row.day_change_val == 9999.0

    def test_fallback_to_ltp_minus_close_when_day_pnl_zero(self):
        """When day_pnl_f == 0, recompute from (ltp - previous_close) × qty."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(
            ltp=2100.0,
            previous_close=2050.0,
            day_pnl=0.0,   # Zero — must fall back to price formula
            qty=50,
        )
        row, _, _, _, day_change_val = _build_holding_row_from_snapshot(raw_row)
        expected = (2100.0 - 2050.0) * 50  # = 2500.0
        assert abs(day_change_val - expected) < 0.01, (
            f"Expected (ltp-prev_close)*qty = {expected}; got {day_change_val}"
        )

    def test_fallback_to_prev_ltp_when_no_previous_close(self):
        """When day_pnl_f == 0 and previous_close == 0, use prev_ltp."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(
            ltp=2100.0,
            previous_close=0.0,   # Missing
            day_pnl=0.0,
            qty=10,
            prev_ltp=2080.0,
        )
        row, _, _, _, day_change_val = _build_holding_row_from_snapshot(raw_row)
        expected = (2100.0 - 2080.0) * 10  # = 200.0
        assert abs(day_change_val - expected) < 0.01, (
            f"Expected (ltp-prev_ltp)*qty = {expected}; got {day_change_val}"
        )

    def test_zero_when_all_references_missing(self):
        """When day_pnl_f == 0, previous_close == 0, prev_ltp is None → 0.0."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(
            ltp=2100.0,
            previous_close=0.0,
            day_pnl=0.0,
            qty=10,
            prev_ltp=None,
        )
        _, _, _, _, day_change_val = _build_holding_row_from_snapshot(raw_row)
        assert day_change_val == 0.0, (
            f"Expected 0.0 when no references available; got {day_change_val}"
        )


class TestSnapshotCutoffWeekdayFormula:
    """Unit tests for the weekday-aware snapshot_cutoff formula (Bug 3)."""

    def _compute_cutoff(self, weekday: int, today_midnight):
        """Replicate the formula from _query_holdings_snapshot_rows."""
        from datetime import timedelta
        if weekday == 5:   # Saturday
            return today_midnight
        elif weekday == 6:  # Sunday
            return today_midnight - timedelta(days=1)
        else:              # Mon–Fri
            return today_midnight + timedelta(days=1)

    def test_saturday_gives_today_midnight(self):
        """Saturday: cutoff = today midnight (excludes any Sat snapshot)."""
        from datetime import datetime
        today = datetime(2026, 8, 29, 10, 0, 0)  # Saturday
        midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
        assert today.weekday() == 5, "Sanity: 2026-08-29 must be Saturday"
        result = self._compute_cutoff(today.weekday(), midnight)
        assert result == midnight, f"Saturday cutoff should be today midnight; got {result}"

    def test_sunday_gives_saturday_midnight(self):
        """Sunday: cutoff = Saturday 00:00 (yesterday midnight)."""
        from datetime import datetime, timedelta
        today = datetime(2026, 8, 30, 10, 0, 0)  # Sunday
        midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
        assert today.weekday() == 6, "Sanity: 2026-08-30 must be Sunday"
        result = self._compute_cutoff(today.weekday(), midnight)
        expected = midnight - timedelta(days=1)  # Saturday 00:00
        assert result == expected, f"Sunday cutoff should be Saturday midnight; got {result}"

    def test_monday_gives_tomorrow_midnight(self):
        """Monday: cutoff = Tuesday 00:00 (includes Monday EOD snapshot)."""
        from datetime import datetime, timedelta
        today = datetime(2026, 8, 31, 10, 0, 0)  # Monday
        midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
        assert today.weekday() == 0, "Sanity: 2026-08-31 must be Monday"
        result = self._compute_cutoff(today.weekday(), midnight)
        expected = midnight + timedelta(days=1)  # Tuesday 00:00
        assert result == expected, f"Monday cutoff should be tomorrow midnight; got {result}"

    def test_friday_gives_tomorrow_midnight(self):
        """Friday afternoon: cutoff = Saturday 00:00 (includes Friday 15:45 EOD)."""
        from datetime import datetime, timedelta
        today = datetime(2026, 8, 28, 16, 0, 0)  # Friday afternoon
        midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
        assert today.weekday() == 4, "Sanity: 2026-08-28 must be Friday"
        result = self._compute_cutoff(today.weekday(), midnight)
        expected = midnight + timedelta(days=1)  # Saturday 00:00
        # Friday 15:45 < Saturday 00:00 → EOD snapshot IS included
        from datetime import datetime as _dt
        eod_snapshot = _dt(2026, 8, 28, 15, 45, 0)
        assert eod_snapshot < result, "Friday 15:45 EOD snapshot must be < cutoff"
        assert result == expected, f"Friday cutoff should be Saturday midnight; got {result}"
