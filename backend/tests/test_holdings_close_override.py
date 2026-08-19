"""Tests for holdings route close_price override — Gap 2 fix.

Invariant: close_price = prior session settlement LTP, frozen off-market.
Kite REST API's close_price field drifts post-settlement (becomes equal to LTP),
making (ltp - close) × qty = 0 and zeroing out holdings day P&L off-market.

This test suite verifies _override_stale_close_for_holdings (Gap 2) which:
  1. Queries daily_book for pre-08:00 IST snapshots (kind='holdings')
  2. Replaces stale close_price with the snapshot LTP per (account, tradingsymbol)
  3. Skips rows where snapshot == close (within epsilon 0.005) — already correct
  4. Leaves the DataFrame unchanged when no snapshot exists or DB fails
  5. Recomputes day_change_val on patched rows — _enrich_holdings ran inside
     broker_apis.fetch_holdings (before this override fires), so day_change_val
     was computed against the stale close_price and must be corrected here.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


def _make_holdings_df(
    account: str = "TEST001",
    tradingsymbol: str = "RELIANCE",
    quantity: int = 50,
    last_price: float = 150.0,
    close_price: float = 150.0,
    average_price: float = 145.0,
) -> pd.DataFrame:
    """Build a minimal holdings DataFrame for close-override tests."""
    return pd.DataFrame([{
        'account': account,
        'tradingsymbol': tradingsymbol,
        'exchange': 'NSE',
        'quantity': quantity,
        'opening_quantity': quantity,
        'average_price': average_price,
        'last_price': last_price,
        'close_price': close_price,
        'pnl': (last_price - average_price) * quantity,
        'day_change': last_price - close_price,
        'day_change_val': 0.0,
        'day_change_percentage': 0.0,
        'pnl_percentage': 0.0,
    }])


def _run_close_override_for_holdings(
    df: pd.DataFrame,
    snapshot_rows: list,
    mock_time: datetime | None = None,
) -> pd.DataFrame:
    """Invoke _override_stale_close_for_holdings with mocked DB.

    Args:
        df: Holdings DataFrame to patch
        snapshot_rows: List of (account, symbol, ltp) tuples returned by the
            DB query. The function's WHERE clause filters captured_at < 08:00
            IST — in tests we model that filter by controlling what rows
            the mock returns (pre-08:00 snapshots are in the list; post-08:00
            snapshots are excluded from the list, simulating SQL filtering).
        mock_time: Time to mock for 'current time' used to compute the 08:00
            cutoff. Defaults to 2026-07-08 08:30 IST.
    """
    from backend.api.routes.holdings import _override_stale_close_for_holdings

    IST = ZoneInfo("Asia/Kolkata")
    if mock_time is None:
        mock_time = datetime(2026, 7, 8, 8, 30, 0, tzinfo=IST)

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
            return_value=mock_time,
        ),
    ):
        asyncio.run(_override_stale_close_for_holdings(df))

    return df


class TestHoldingsCloseOverrideForHoldings:
    """Unit tests for _override_stale_close_for_holdings (Gap 2)."""

    def test_stale_close_replaced_by_snapshot_ltp(self):
        """Stale Kite close_price (150) replaced by pre-08:00 snapshot (145).

        Setup:
          broker close_price = 150 (drifted to settlement)
          snapshot ltp = 145 (captured before 08:00 IST — modelled by mock row)
          last_price = 150, quantity = 50, average_price = 140

        Expected: close_price patched to 145.
        """
        df = _make_holdings_df(
            account="TEST001",
            tradingsymbol="RELIANCE",
            quantity=50,
            last_price=150.0,
            close_price=150.0,
            average_price=140.0,
        )
        # Snapshot row: (account, symbol, ltp) — 3 columns, matches SELECT
        snapshot_rows = [("TEST001", "RELIANCE", 145.0)]

        df = _run_close_override_for_holdings(df, snapshot_rows)

        assert abs(df.iloc[0]['close_price'] - 145.0) < 0.01, (
            f"close_price should be 145.0 (snapshot), got {df.iloc[0]['close_price']}"
        )

        # day_change_val must also be recomputed: (ltp - snapped_close) × qty
        # = (150 - 145) × 50 = 250. The stale value was (150 - 150) × 50 = 0.
        expected_dcv = (150.0 - 145.0) * 50
        assert abs(df.iloc[0]['day_change_val'] - expected_dcv) < 0.01, (
            f"day_change_val should be {expected_dcv} after close override, "
            f"got {df.iloc[0]['day_change_val']}"
        )

    def test_skips_when_no_snapshot(self):
        """No snapshot for symbol → close_price unchanged, no crash."""
        df = _make_holdings_df(
            last_price=150.0,
            close_price=150.0,
        )
        original_close = float(df.iloc[0]['close_price'])

        df = _run_close_override_for_holdings(df, snapshot_rows=[])

        assert abs(df.iloc[0]['close_price'] - original_close) < 0.01, (
            f"close_price must remain {original_close} when no snapshot, "
            f"got {df.iloc[0]['close_price']}"
        )

    def test_skips_zero_snapshot_ltp(self):
        """Snapshot ltp=0 → skip override (SQL WHERE ltp > 0 filters these out)."""
        # The DB WHERE clause has `ltp > 0`, so zero-LTP rows are excluded.
        # Simulated here by returning no rows from mock.
        df = _make_holdings_df(close_price=150.0)
        original_close = float(df.iloc[0]['close_price'])

        # Zero-ltp row filtered by SQL; mock returns empty
        df = _run_close_override_for_holdings(df, snapshot_rows=[])

        assert abs(df.iloc[0]['close_price'] - original_close) < 0.01, (
            "close_price must not change when no valid snapshot LTP exists"
        )

    def test_skips_post_08am_snapshot(self):
        """Post-08:00 IST snapshots are excluded via SQL WHERE captured_at < cutoff.

        Simulated by mock returning no rows (the SQL would have filtered them).
        Expected: close_price unchanged.
        """
        df = _make_holdings_df(close_price=140.0, last_price=150.0)
        original_close = float(df.iloc[0]['close_price'])

        # Post-gate snapshots are excluded from DB result → empty mock
        df = _run_close_override_for_holdings(df, snapshot_rows=[])

        assert abs(df.iloc[0]['close_price'] - original_close) < 0.01, (
            "close_price must not be patched when no pre-08:00 snapshot exists"
        )

    def test_epsilon_guard_skips_near_identical_values(self):
        """Snapshot ltp within epsilon of current close → no patch.

        Protects against spurious rewrites when values agree within 0.005.
        """
        CLOSE = 150.0
        SNAP = 150.002  # within epsilon (< 0.005)
        df = _make_holdings_df(close_price=CLOSE, last_price=160.0)

        df = _run_close_override_for_holdings(df, [("TEST001", "RELIANCE", SNAP)])

        assert abs(df.iloc[0]['close_price'] - CLOSE) < 0.01, (
            "epsilon guard must skip patching for near-identical values"
        )

    def test_only_matching_account_symbol_patched(self):
        """Snapshot keyed by (account, symbol) — only the matching row is patched."""
        df = pd.concat([
            _make_holdings_df("TEST001", "RELIANCE", close_price=150.0, last_price=160.0),
            _make_holdings_df("TEST002", "RELIANCE", close_price=150.0, last_price=160.0),
            _make_holdings_df("TEST001", "INFY", close_price=1500.0, last_price=1550.0),
        ], ignore_index=True)

        # Snapshot only for TEST001/RELIANCE
        df = _run_close_override_for_holdings(df, [("TEST001", "RELIANCE", 145.0)])

        t1_rel = df[(df['account'] == 'TEST001') & (df['tradingsymbol'] == 'RELIANCE')].iloc[0]
        assert abs(t1_rel['close_price'] - 145.0) < 0.01, "TEST001/RELIANCE should be patched"

        t2_rel = df[(df['account'] == 'TEST002') & (df['tradingsymbol'] == 'RELIANCE')].iloc[0]
        assert abs(t2_rel['close_price'] - 150.0) < 0.01, "TEST002/RELIANCE must not be patched"

        t1_infy = df[(df['account'] == 'TEST001') & (df['tradingsymbol'] == 'INFY')].iloc[0]
        assert abs(t1_infy['close_price'] - 1500.0) < 0.01, "TEST001/INFY must not be patched"

    def test_close_price_patch_enables_correct_dcv_after_enrich(self):
        """Complementary path: when day_change_val column is absent, override
        cannot recompute it (guard skips), so _enrich_holdings must compute it
        correctly against the already-patched close_price.

        In the real fetch pipeline day_change_val IS present (set by
        _enrich_holdings before the override). This test covers the edge case
        where a broker adapter omits the column entirely.

        When day_change_val is absent the enrichment uses the formula
        (ltp - close) × qty. With the corrected close (130 not 140),
        the expected DCV = (150 - 130) × 100 = 2 000, not (150 - 140) × 100 = 1 000.
        """
        from backend.brokers.broker_apis import _enrich_holdings

        # Build a raw frame without day_change_val so the formula path fires
        # (not the broker-trust path which would return 0 for the broker's 0).
        df = pd.DataFrame([{
            'account': 'TEST001',
            'tradingsymbol': 'RELIANCE',
            'exchange': 'NSE',
            'quantity': 100,
            'opening_quantity': 100,
            'average_price': 120.0,
            'last_price': 150.0,
            'close_price': 140.0,  # stale — will be patched to 130
            'pnl': (150.0 - 120.0) * 100,  # 3000
        }])

        # Apply close override
        df = _run_close_override_for_holdings(df, [("TEST001", "RELIANCE", 130.0)])
        assert abs(df.iloc[0]['close_price'] - 130.0) < 0.01, "close_price must be patched"

        # Enrich — no day_change_val column → formula path: (ltp - close) × qty
        df = _enrich_holdings(df)

        # day_change_val = (150 - 130) × 100 = 2 000
        expected_dcv = (150.0 - 130.0) * 100
        assert abs(df.iloc[0]['day_change_val'] - expected_dcv) < 0.5, (
            f"day_change_val must reflect corrected close (130, not 140). "
            f"Expected {expected_dcv}, got {df.iloc[0]['day_change_val']}"
        )

    def test_empty_dataframe_no_db_call(self):
        """Empty DataFrame must not trigger DB query."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        df = pd.DataFrame()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_for_holdings(df))

        # DB session must never be entered for an empty frame
        mock_session.__aenter__.assert_not_called()

    def test_db_failure_leaves_dataframe_unchanged(self):
        """DB query failure → DataFrame unchanged, no exception propagated."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        IST = ZoneInfo("Asia/Kolkata")
        df = _make_holdings_df(close_price=150.0)
        original_close = float(df.iloc[0]['close_price'])

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("DB error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_time = datetime(2026, 7, 8, 8, 30, 0, tzinfo=IST)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_time,
            ),
        ):
            asyncio.run(_override_stale_close_for_holdings(df))

        assert abs(df.iloc[0]['close_price'] - original_close) < 0.01, (
            "close_price must be unchanged when DB query fails"
        )

    def test_inv_val_and_pnl_unchanged_by_close_override(self):
        """Override close_price only. inv_val and pnl depend on avg+qty+ltp, not close."""
        from backend.brokers.broker_apis import _enrich_holdings

        df = _make_holdings_df(
            quantity=50,
            average_price=140.0,
            last_price=160.0,
            close_price=150.0,
        )
        df['pnl'] = (160.0 - 140.0) * 50  # 1000

        # Apply close override: new close = 130 (stale was 150)
        df = _run_close_override_for_holdings(df, [("TEST001", "RELIANCE", 130.0)])

        # Enrich to compute derived cols
        df = _enrich_holdings(df)

        # inv_val = avg × qty = 140 × 50 = 7000 (unchanged — no close dependency)
        assert abs(df.iloc[0]['inv_val'] - 7000.0) < 0.1, (
            f"inv_val should be 7000 (avg×qty), got {df.iloc[0]['inv_val']}"
        )
        # cur_val = inv_val + pnl = 7000 + 1000 = 8000 = ltp×qty
        assert abs(df.iloc[0]['cur_val'] - 8000.0) < 0.1, (
            f"cur_val should be 8000 (ltp×qty), got {df.iloc[0]['cur_val']}"
        )
        # pnl = (ltp - avg) × qty = 1000 (unchanged — no close dependency)
        assert abs(df.iloc[0]['pnl'] - 1000.0) < 0.1, (
            f"pnl should be 1000, got {df.iloc[0]['pnl']}"
        )

    def test_missing_required_columns_no_crash(self):
        """DataFrame missing account or tradingsymbol → early return, no crash."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        # Missing 'account' column
        df = pd.DataFrame([{'tradingsymbol': 'RELIANCE', 'close_price': 100.0}])

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)

        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_for_holdings(df))

        # No DB call because 'account' column is missing
        mock_session.__aenter__.assert_not_called()

    def test_uses_holdings_kind_not_positions(self):
        """DB query must use kind='holdings', not kind='positions'."""
        from backend.api.routes.holdings import _override_stale_close_for_holdings

        IST = ZoneInfo("Asia/Kolkata")
        df = _make_holdings_df(close_price=150.0)

        captured_sql: list[str] = []

        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        async def _capture_execute(sql, *args, **kwargs):
            captured_sql.append(str(sql))
            return mock_result
        mock_session.execute = _capture_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_time = datetime(2026, 7, 8, 8, 30, 0, tzinfo=IST)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_time,
            ),
        ):
            asyncio.run(_override_stale_close_for_holdings(df))

        assert len(captured_sql) == 1, "Exactly one DB query should be issued"
        assert "holdings" in captured_sql[0].lower(), (
            f"Query must use kind='holdings', got: {captured_sql[0]}"
        )
        assert "positions" not in captured_sql[0].lower(), (
            "Query must NOT reference 'positions' kind"
        )

    def test_recompute_row_percentages_skipped_when_no_matches(self):
        """recompute_row_percentages must not be called when patched_indices is empty.

        Before the fix, raw.index.isin([]) returned a boolean Series whose len()
        equals total row count (never zero), so the early-exit guard inside
        recompute_row_percentages never triggered on a no-match call.

        After the fix, pd.Index([]) has len() == 0, so the guard correctly
        short-circuits and recompute_row_percentages is not invoked at all.
        """
        from unittest.mock import patch as _patch

        df = _make_holdings_df(close_price=150.0, last_price=160.0)

        # No snapshot rows → patched_indices will be [] → pd.Index([]) len==0
        with _patch(
            "backend.api.routes.holdings.recompute_row_percentages"
        ) as mock_recompute:
            _run_close_override_for_holdings(df, snapshot_rows=[])

        mock_recompute.assert_not_called(), (
            "recompute_row_percentages must not be called when no rows were patched"
        )
