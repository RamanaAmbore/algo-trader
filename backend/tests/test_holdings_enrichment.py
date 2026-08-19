"""Tests for holdings enrichment — verify quantity vs opening_quantity usage.

Invariant: when a holding is partially or fully sold, the sold qty moves to a
CNC positions row. Holdings day_change_val, inv_val, cur_val must use the REMAINING
`quantity`, not `opening_quantity`.

Example: opened 100 shares, sold 50 → holdings should show:
  - quantity=50 (remaining)
  - opening_quantity=100 (for reference/audit only)
  - inv_val = avg_price × 50 (remaining only)
  - cur_val = ltp × 50 (remaining only)
  - day_change_val = (ltp - close) × 50 (remaining only)

The sold 50 shares appear as a separate CNC positions row with their own P&L.
"""

import pandas as pd
import polars as pl
import pytest


def _make_holdings_row(include_pnl: bool = True, include_dcv: bool = False, **kwargs) -> dict:
    """Build a minimal holdings row dict with sensible defaults.

    Args:
        include_pnl: whether to include the pnl field (default True for most tests)
        include_dcv: whether to include day_change_val (default False so enrichment recalculates)
        **kwargs: overrides for the defaults
    """
    defaults = {
        'account': 'TEST001',
        'tradingsymbol': 'RELIANCE',
        'exchange': 'NSE',
        'quantity': 100,
        'opening_quantity': 100,
        'average_price': 150.0,
        'last_price': 200.0,
        'close_price': 150.0,
        'day_change': 0.0,
    }
    if include_pnl:
        # Compute pnl based on current values in kwargs or defaults
        qty = kwargs.get('quantity', defaults['quantity'])
        avg = kwargs.get('average_price', defaults['average_price'])
        ltp = kwargs.get('last_price', defaults['last_price'])
        defaults['pnl'] = (ltp - avg) * qty

    if include_dcv:
        defaults['day_change_val'] = 0.0

    merged = {**defaults, **kwargs}
    return merged


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    """Build DataFrame from rows and ensure numeric dtypes."""
    df = pd.DataFrame(rows)
    # Ensure all numeric columns are properly typed (polars pass needs this)
    numeric_cols = [
        'quantity', 'opening_quantity', 'average_price', 'last_price',
        'close_price', 'pnl', 'day_change', 'day_change_val'
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def _enrich_holdings_minimal(df: pd.DataFrame) -> pd.DataFrame:
    """Minimal version of _enrich_holdings for testing — just the relevant parts."""
    from backend.brokers.broker_apis import _enrich_holdings
    return _enrich_holdings(df)


class TestHoldingsQuantityVsOpeningQuantity:
    """Unit tests for holdings enrichment quantity semantics."""

    def test_holdings_dcv_uses_quantity_not_opening_quantity(self):
        """Partially sold holding: day_change_val uses REMAINING quantity, not opening.

        Setup: opened 100 shares, sold 50 → quantity=50 (remaining)
        Expected: day_change_val = (200-150) × 50 = 2500
        NOT: (200-150) × 100 = 5000
        """
        df = _prepare_df([_make_holdings_row(
            quantity=50,
            opening_quantity=100,
            last_price=200.0,
            close_price=150.0,
        )])

        df = _enrich_holdings_minimal(df)

        # The enriched frame MUST use quantity, not opening_quantity
        expected_dcv = (200.0 - 150.0) * 50  # 2500
        actual_dcv = float(df.iloc[0]['day_change_val'])

        assert abs(actual_dcv - expected_dcv) < 0.1, (
            f"day_change_val should use quantity (50) not opening_quantity (100). "
            f"Expected {expected_dcv}, got {actual_dcv}"
        )

    def test_holdings_inv_val_uses_quantity(self):
        """Partially sold holding: inv_val uses REMAINING quantity, not opening.

        Setup: opened 100 @ 150, sold 50 → quantity=50 (remaining)
        Expected: inv_val = 150 × 50 = 7500
        NOT: 150 × 100 = 15000
        """
        df = _prepare_df([_make_holdings_row(
            quantity=50,
            opening_quantity=100,
            average_price=150.0,
        )])

        df = _enrich_holdings_minimal(df)

        expected_inv_val = 150.0 * 50  # 7500
        actual_inv_val = float(df.iloc[0]['inv_val'])

        assert abs(actual_inv_val - expected_inv_val) < 0.1, (
            f"inv_val should use quantity (50) not opening_quantity (100). "
            f"Expected {expected_inv_val}, got {actual_inv_val}"
        )

    def test_holdings_cur_val_uses_quantity(self):
        """Partially sold holding: cur_val uses REMAINING quantity, not opening.

        Setup: opened 100, sold 50 → quantity=50 (remaining), ltp=200
        Expected: cur_val = 200 × 50 = 10000
        NOT: 200 × 100 = 20000
        """
        df = _prepare_df([_make_holdings_row(
            quantity=50,
            opening_quantity=100,
            average_price=150.0,
            last_price=200.0,
        )])

        df = _enrich_holdings_minimal(df)

        expected_cur_val = 200.0 * 50  # 10000
        actual_cur_val = float(df.iloc[0]['cur_val'])

        assert abs(actual_cur_val - expected_cur_val) < 0.1, (
            f"cur_val should use quantity (50) not opening_quantity (100). "
            f"Expected {expected_cur_val}, got {actual_cur_val}"
        )

    def test_holdings_pnl_uses_quantity_not_opening(self):
        """Partially sold holding: pnl from broker but if recalculated, uses quantity.

        When pnl is computed (not trusting broker), use quantity.
        Setup: opened 100 @ 150, sold 50, quantity=50, ltp=200
        Expected: pnl = (200-150) × 50 = 2500
        """
        df = _prepare_df([_make_holdings_row(
            include_pnl=True,  # Ensure pnl is computed with quantity, not opening
            quantity=50,
            opening_quantity=100,
            average_price=150.0,
            last_price=200.0,
        )])

        df = _enrich_holdings_minimal(df)

        expected_pnl = (200.0 - 150.0) * 50  # 2500
        actual_pnl = float(df.iloc[0]['pnl'])

        assert abs(actual_pnl - expected_pnl) < 0.1, (
            f"pnl should use quantity (50) not opening_quantity (100). "
            f"Expected {expected_pnl}, got {actual_pnl}"
        )

    def test_holdings_fully_sold_quantity_zero(self):
        """Fully sold holding: quantity=0, opening_quantity=100.

        When all shares are sold, the holding should not appear (quantity=0).
        This row would have been moved to positions as a CNC entry.
        Test that if it somehow still appears, day_change_val=0.
        """
        df = _prepare_df([_make_holdings_row(
            quantity=0,
            opening_quantity=100,
            last_price=200.0,
            close_price=150.0,
        )])

        df = _enrich_holdings_minimal(df)

        # day_change_val should be 0 when quantity=0 (nothing left to hold)
        actual_dcv = float(df.iloc[0]['day_change_val'])
        assert abs(actual_dcv) < 0.1, (
            f"day_change_val should be ~0 for fully sold (quantity=0), "
            f"got {actual_dcv}"
        )

    def test_holdings_quantity_equals_opening_quantity_no_regression(self):
        """No-regression test: when quantity==opening_quantity, result unchanged.

        Normal case where no shares were sold.
        quantity=100, opening_quantity=100, ltp=200, close=150
        day_change_val = (200-150) × 100 = 5000
        """
        df = _prepare_df([_make_holdings_row(
            quantity=100,
            opening_quantity=100,
            last_price=200.0,
            close_price=150.0,
        )])

        df = _enrich_holdings_minimal(df)

        expected_dcv = (200.0 - 150.0) * 100  # 5000
        actual_dcv = float(df.iloc[0]['day_change_val'])

        assert abs(actual_dcv - expected_dcv) < 0.1, (
            f"day_change_val regression: expected {expected_dcv}, got {actual_dcv}"
        )

    def test_holdings_pnl_percentage_uses_remaining_quantity_basis(self):
        """pnl_percentage denominator: inv_val = avg × quantity (remaining).

        Setup: opened 100 @ 150, sold 50, ltp=200 → quantity=50
        inv_val = 150 × 50 = 7500
        pnl = (200-150) × 50 = 2500
        pnl_percentage = 2500 / 7500 × 100 = 33.33%
        """
        df = _prepare_df([_make_holdings_row(
            quantity=50,
            opening_quantity=100,
            average_price=150.0,
            last_price=200.0,
        )])

        df = _enrich_holdings_minimal(df)

        inv_val = float(df.iloc[0]['inv_val'])
        pnl = float(df.iloc[0]['pnl'])
        pnl_pct = float(df.iloc[0]['pnl_percentage'])

        expected_inv_val = 150.0 * 50      # 7500
        expected_pnl = (200.0 - 150.0) * 50  # 2500
        expected_pnl_pct = expected_pnl / expected_inv_val * 100  # 33.33%

        assert abs(inv_val - expected_inv_val) < 0.1, (
            f"inv_val basis wrong for pnl_pct: expected {expected_inv_val}, "
            f"got {inv_val}"
        )
        assert abs(pnl_pct - expected_pnl_pct) < 0.1, (
            f"pnl_percentage should be {expected_pnl_pct}%, got {pnl_pct}%"
        )

    def test_multiple_holdings_partial_sales_mixed(self):
        """Multiple holdings with mixed sales: each row uses its own quantity.

        Row 1: RELIANCE  quantity=50, opening=100
        Row 2: INFY      quantity=100, opening=100 (not sold)
        Row 3: TCS       quantity=20, opening=50 (partially sold)
        """
        df = _prepare_df([
            _make_holdings_row(
                tradingsymbol='RELIANCE',
                quantity=50,
                opening_quantity=100,
                average_price=150.0,
                last_price=200.0,
                close_price=150.0,
            ),
            _make_holdings_row(
                tradingsymbol='INFY',
                quantity=100,
                opening_quantity=100,
                average_price=1500.0,
                last_price=1600.0,
                close_price=1500.0,
            ),
            _make_holdings_row(
                tradingsymbol='TCS',
                quantity=20,
                opening_quantity=50,
                average_price=3500.0,
                last_price=3700.0,
                close_price=3500.0,
            ),
        ])

        df = _enrich_holdings_minimal(df)

        # Row 1: RELIANCE
        rel_dcv = float(df[df['tradingsymbol'] == 'RELIANCE'].iloc[0]['day_change_val'])
        assert abs(rel_dcv - (200.0 - 150.0) * 50) < 0.1, (
            "RELIANCE day_change_val wrong"
        )

        # Row 2: INFY
        infy_dcv = float(df[df['tradingsymbol'] == 'INFY'].iloc[0]['day_change_val'])
        assert abs(infy_dcv - (1600.0 - 1500.0) * 100) < 0.1, (
            "INFY day_change_val wrong"
        )

        # Row 3: TCS
        tcs_dcv = float(df[df['tradingsymbol'] == 'TCS'].iloc[0]['day_change_val'])
        assert abs(tcs_dcv - (3700.0 - 3500.0) * 20) < 0.1, (
            "TCS day_change_val wrong"
        )
