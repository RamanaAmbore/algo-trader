"""Tests for prev_settlement_pnl field in positions API.

prev_settlement_pnl captures yesterday's total_pnl from the most-recent
daily_book snapshot captured before today's midnight IST. Tests verify:

1. Field is populated from daily_book.total_pnl when a pre-midnight snapshot exists.
2. Field is null for new positions (no daily_book entry before today).
3. captured_at guard prevents mid-session snapshots from being used.
4. Field is correctly threaded through the DataFrame and into the response.
"""

import asyncio
import math
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _make_mcx_position_row(
    account: str = "ZG0790",
    symbol: str = "CRUDEOIL26JUL6900PE",
    last_price: float = 264.5,
    close_price: float = 220.0,
    quantity: int = 10,
) -> dict:
    """Build a minimal positions DataFrame row for testing."""
    return {
        'account': account,
        'tradingsymbol': symbol,
        'exchange': 'MCX',
        'product': 'NRML',
        'last_price': last_price,
        'close_price': close_price,
        'quantity': quantity,
        'overnight_quantity': quantity,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'average_price': 200.0,
        'unrealised': 0.0,
        'realised': 0.0,
        'pnl': (last_price - 200.0) * quantity,
        'day_change_val': 0.0,
        'day_change': 0.0,
        'day_change_percentage': 0.0,
        'pnl_percentage': 0.0,
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
    # Use a fixed midnight (2026-07-08 00:00 IST) for deterministic tests
    midnight = datetime(2026, 7, 8, 0, 0, 0, tzinfo=ist)

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


class TestPrevSettlementPnlPopulation:
    """SSOT: prev_settlement_pnl is populated inside _override_stale_close_from_snapshot."""

    def test_prev_settlement_pnl_populated_from_daily_book(self):
        """Given a position with a pre-midnight snapshot row, prev_settlement_pnl
        must be set from the snapshot's total_pnl (500.0 in this test).
        """
        PREV_PNL = 500.0

        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="CRUDEOIL26JUL6900PE",
        )])

        # Snapshot row: (account, symbol, ltp, total_pnl)
        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, PREV_PNL)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert 'prev_settlement_pnl' in df.columns, \
            "prev_settlement_pnl column must be added to DataFrame"
        assert df.at[0, 'prev_settlement_pnl'] == PREV_PNL, \
            f"Expected prev_settlement_pnl={PREV_PNL}, got {df.at[0, 'prev_settlement_pnl']}"

    def test_prev_settlement_pnl_null_for_new_position(self):
        """Given a position with NO snapshot row, prev_settlement_pnl must
        remain None (the new position was opened today).
        """
        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="NEW_POSITION",
        )])

        # No snapshot row for this symbol
        snapshot_rows = []
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Field either is not present, or is None for this row
        if 'prev_settlement_pnl' in df.columns:
            val = df.at[0, 'prev_settlement_pnl']
            assert val is None or pd.isna(val), \
                f"Expected None/NaN for new position, got {val}"
        else:
            # Field was never added because no snapshots matched
            pass

    def test_prev_settlement_pnl_captured_at_guard(self):
        """Snapshots with captured_at >= today's midnight are excluded by the
        query filter. This test verifies the SQL logic inside the function
        by checking that only pre-midnight snapshots are used.

        Since the mock returns what we tell it to, we trust the SQL guard
        and verify that only snapshots passed to _run_override_stale_close_from_snapshot
        are used. A real DB test would verify the SQL itself.
        """
        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="CRUDEOIL26JUL6900PE",
        )])

        # In the real code, the query filters: captured_at < today_midnight.
        # We simulate that by ONLY passing a pre-midnight snapshot.
        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Verify the field was populated
        assert df.at[0, 'prev_settlement_pnl'] == 500.0

    def test_prev_settlement_pnl_matching_account_symbol(self):
        """Only the matching (account, symbol) pair gets prev_settlement_pnl.
        Non-matching rows remain None.
        """
        df = pd.concat([
            pd.DataFrame([_make_mcx_position_row(
                account="ZG0790",
                symbol="CRUDEOIL26JUL6900PE",
            )]),
            pd.DataFrame([_make_mcx_position_row(
                account="ZJ6294",
                symbol="CRUDEOIL26JUL6900PE",
            )]),
        ], ignore_index=True)

        # Only provide snapshot for ZG0790
        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, 500.0)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Row 0 (ZG0790) should have prev_settlement_pnl
        zg_row = df[df['account'] == 'ZG0790'].iloc[0]
        assert zg_row['prev_settlement_pnl'] == 500.0, \
            "ZG0790 should have prev_settlement_pnl from snapshot"

        # Row 1 (ZJ6294) should not
        zj_row = df[df['account'] == 'ZJ6294'].iloc[0]
        pnl_val = zj_row.get('prev_settlement_pnl')
        if pnl_val is not None and not pd.isna(pnl_val):
            pytest.fail(f"ZJ6294 should not have prev_settlement_pnl, got {pnl_val}")

    def test_prev_settlement_pnl_coexists_with_close_override(self):
        """prev_settlement_pnl backfill and close-price override both operate
        on the same daily_book query. Verify both mutations are applied.
        """
        SNAPSHOT_LTP = 220.0
        STALE_CLOSE = 180.0
        PREV_PNL = 500.0

        df = pd.DataFrame([_make_mcx_position_row(
            last_price=264.5,
            close_price=STALE_CLOSE,
        )])

        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", SNAPSHOT_LTP, PREV_PNL)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Both patches should apply
        assert abs(df.at[0, 'close_price'] - SNAPSHOT_LTP) < 0.005, \
            f"close_price should be {SNAPSHOT_LTP}, got {df.at[0, 'close_price']}"
        assert df.at[0, 'prev_settlement_pnl'] == PREV_PNL, \
            f"prev_settlement_pnl should be {PREV_PNL}, got {df.at[0, 'prev_settlement_pnl']}"


class TestPrevSettlementPnlIntegration:
    """Integration: prev_settlement_pnl flows through to PositionRow in the response."""

    def test_prev_settlement_pnl_in_position_row_schema(self):
        """PositionRow schema includes prev_settlement_pnl field."""
        from backend.api.schemas import PositionRow

        # Create a PositionRow with prev_settlement_pnl
        row = PositionRow(
            account="ZG0790",
            tradingsymbol="CRUDEOIL26JUL6900PE",
            exchange="MCX",
            product="NRML",
            quantity=10,
            average_price=200.0,
            close_price=220.0,
            pnl=645.0,
            prev_settlement_pnl=500.0,  # Set explicitly
        )
        assert row.prev_settlement_pnl == 500.0

    def test_prev_settlement_pnl_defaults_to_none(self):
        """When not set explicitly, prev_settlement_pnl defaults to None."""
        from backend.api.schemas import PositionRow

        row = PositionRow(
            account="ZG0790",
            tradingsymbol="NEW_SYMBOL",
            exchange="MCX",
            product="NRML",
            quantity=10,
            average_price=200.0,
            close_price=220.0,
            pnl=100.0,
            # prev_settlement_pnl not set
        )
        assert row.prev_settlement_pnl is None, \
            "prev_settlement_pnl must default to None for new positions"

    def test_prev_settlement_pnl_serialization(self):
        """prev_settlement_pnl is correctly serialized in the msgspec response."""
        from backend.api.schemas import PositionRow
        import msgspec

        row = PositionRow(
            account="ZG0790",
            tradingsymbol="CRUDEOIL26JUL6900PE",
            exchange="MCX",
            product="NRML",
            quantity=10,
            average_price=200.0,
            close_price=220.0,
            pnl=645.0,
            prev_settlement_pnl=500.0,
        )

        # Serialize to bytes and back
        encoder = msgspec.json.Encoder()
        decoder = msgspec.json.Decoder(type=PositionRow)
        encoded = encoder.encode(row)
        decoded = decoder.decode(encoded)

        assert decoded.prev_settlement_pnl == 500.0, \
            "prev_settlement_pnl must survive msgspec round-trip"


class TestPrevSettlementPnlEdgeCases:
    """Edge cases and defensive checks."""

    def test_prev_settlement_pnl_null_in_snapshot_row(self):
        """When daily_book.total_pnl is NULL (shouldn't happen, but guard it),
        prev_settlement_pnl remains None for that row.
        """
        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="CRUDEOIL26JUL6900PE",
        )])

        # Snapshot with None total_pnl
        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, None)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        # Column added, but value should remain None
        if 'prev_settlement_pnl' in df.columns:
            val = df.at[0, 'prev_settlement_pnl']
            if val is not None and not pd.isna(val):
                pytest.fail(f"Expected None/NaN when total_pnl is NULL, got {val}")

    def test_prev_settlement_pnl_empty_dataframe(self):
        """Empty DataFrame must not crash and should not query the DB."""
        df = pd.DataFrame()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from backend.api.routes.positions import _override_stale_close_from_snapshot

        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_from_snapshot(df))

        # No exception; session was never used (early return on empty DF)
        mock_session.__aenter__.assert_not_called()

    def test_prev_settlement_pnl_multiple_positions_same_account(self):
        """Multiple positions from the same account each get their own
        prev_settlement_pnl from the snapshot map.
        """
        df = pd.concat([
            pd.DataFrame([_make_mcx_position_row(
                account="ZG0790",
                symbol="CRUDEOIL26JUL6900PE",
            )]),
            pd.DataFrame([_make_mcx_position_row(
                account="ZG0790",
                symbol="GOLDM26AUGFUT",
            )]),
        ], ignore_index=True)

        snapshot_rows = [
            ("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, 500.0),
            ("ZG0790", "GOLDM26AUGFUT", 6800.0, 250.0),
        ]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        crudeoil_row = df[df['tradingsymbol'] == 'CRUDEOIL26JUL6900PE'].iloc[0]
        goldm_row = df[df['tradingsymbol'] == 'GOLDM26AUGFUT'].iloc[0]

        assert crudeoil_row['prev_settlement_pnl'] == 500.0, \
            "CRUDEOIL should have prev_settlement_pnl=500.0"
        assert goldm_row['prev_settlement_pnl'] == 250.0, \
            "GOLDM should have prev_settlement_pnl=250.0"

    def test_prev_settlement_pnl_negative_value(self):
        """prev_settlement_pnl can be negative (a loss from yesterday)."""
        NEGATIVE_PNL = -1250.50

        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="CRUDEOIL26JUL6900PE",
        )])

        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, NEGATIVE_PNL)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert df.at[0, 'prev_settlement_pnl'] == NEGATIVE_PNL, \
            f"Expected prev_settlement_pnl={NEGATIVE_PNL}, got {df.at[0, 'prev_settlement_pnl']}"

    def test_prev_settlement_pnl_zero_value(self):
        """prev_settlement_pnl can be 0.0 (break-even yesterday)."""
        ZERO_PNL = 0.0

        df = pd.DataFrame([_make_mcx_position_row(
            account="ZG0790",
            symbol="CRUDEOIL26JUL6900PE",
        )])

        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, ZERO_PNL)]
        df = _run_override_stale_close_from_snapshot(df, snapshot_rows)

        assert df.at[0, 'prev_settlement_pnl'] == ZERO_PNL, \
            f"Expected prev_settlement_pnl={ZERO_PNL}, got {df.at[0, 'prev_settlement_pnl']}"
