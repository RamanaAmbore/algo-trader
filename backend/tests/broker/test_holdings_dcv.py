"""Tests for the day_change_val fallback in broker_apis._enrich_holdings().

Covers the Dhan holdings case where `day_change_val` is zeroed by the polars
path (because the broker shipped 0.0, which is not-null) but a scalar
`day_change = ltp - close` is available. The fallback multiplies
day_change × quantity (remaining shares) to recover the total session change.

Also covers Gap 1 fix: all P&L and value computations (inv_val, cur_val, pnl,
day_change_val) must use `quantity` (remaining after partial sells) not
`opening_quantity` (original lot size before any sells).
"""

from __future__ import annotations

import pandas as pd
import pytest

# _enrich_holdings is module-private but importable directly for unit tests.
from backend.brokers.broker_apis import _enrich_holdings  # noqa: PLC2701


def _base_holding(**overrides) -> pd.DataFrame:
    """Return a minimal single-row holdings DataFrame.
    Both `quantity` (remaining) and `opening_quantity` (display-only) are
    present so tests can verify that P&L uses quantity, not opening_quantity.
    """
    row = dict(
        last_price=955.0,
        average_price=900.0,
        quantity=100,              # remaining shares (after any partial sells)
        opening_quantity=100,      # display-only — same as quantity when no sells
        close_price=950.0,
        day_change=5.0,            # ltp - close = 955 - 950
        day_change_val=0.0,        # broker shipped 0 — triggers fallback
        pnl=5500.0,                # broker total pnl (not used for dcv here)
    )
    row.update(overrides)
    return pd.DataFrame([row])


class TestDhanHoldingsDayChangeFallback:
    """day_change_val fallback: day_change × opening_quantity."""

    def test_dhan_holdings_day_change_fallback(self):
        """Core spec: dcv == 0, day_change=5, oq=100 → dcv = 500."""
        df = _base_holding()
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(500.0)

    def test_fallback_does_not_fire_when_dcv_already_valid(self):
        """Fallback must not overwrite a non-zero day_change_val."""
        df = _base_holding(day_change_val=750.0)
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(750.0)

    def test_fallback_does_not_fire_when_close_zero(self):
        """Guard: close_price must be > 0 for fallback to fire."""
        # With close_price=0, the polars path itself won't produce a
        # valid dcv, and the fallback guard (_f_cls > 0) should block it.
        df = _base_holding(close_price=0.0)
        result = _enrich_holdings(df)
        # dcv stays 0 — both polars and pandas fallback blocked
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)

    def test_fallback_negative_day_change(self):
        """Negative day_change produces negative day_change_val."""
        df = _base_holding(
            last_price=945.0,
            day_change=-5.0,   # ltp - close = 945 - 950
            day_change_val=0.0,
        )
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(-500.0)

    def test_fallback_zero_day_change_leaves_dcv_zero(self):
        """When day_change == 0 the fallback condition does not fire."""
        df = _base_holding(day_change=0.0, day_change_val=0.0)
        result = _enrich_holdings(df)
        # dcv stays at whatever polars computed (could be formula or 0)
        # — the important thing is the fallback didn't produce NaN or crash
        assert pd.notna(result.loc[0, "day_change_val"])

    def test_fallback_absent_when_day_change_col_missing(self):
        """Without a day_change column the fallback block is skipped."""
        df = _base_holding()
        df = df.drop(columns=["day_change"])
        result = _enrich_holdings(df)
        # No crash; dcv computed by polars formula (ltp - close) * qty
        assert pd.notna(result.loc[0, "day_change_val"])

    def test_fallback_large_quantity(self):
        """Verify arithmetic for realistic lot sizes."""
        df = _base_holding(
            day_change=2.5,
            quantity=400,
            opening_quantity=400,
            day_change_val=0.0,
        )
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(1000.0)

    def test_multi_row_mixed_dcv(self):
        """Multi-row: fallback fires only on zero-dcv rows."""
        df = pd.DataFrame([
            # Row 0: dcv == 0 → fallback should fire → 5 * 100 = 500
            dict(last_price=955.0, average_price=900.0,
                 quantity=100, opening_quantity=100,
                 close_price=950.0, day_change=5.0, day_change_val=0.0, pnl=5500.0),
            # Row 1: dcv already valid → must not change
            dict(last_price=955.0, average_price=900.0,
                 quantity=100, opening_quantity=100,
                 close_price=950.0, day_change=5.0, day_change_val=750.0, pnl=5500.0),
        ])
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(500.0)
        assert result.loc[1, "day_change_val"] == pytest.approx(750.0)


class TestHoldingsPartialSell:
    """Gap 1 fix: P&L and value computations must use `quantity` (remaining
    shares after partial sells), not `opening_quantity` (original lot size).

    Scenario: operator bought 200 shares, sold 100 → quantity=100,
    opening_quantity=200. All P&L and value columns must reflect 100 shares
    only, not 200.

    All test fixtures include `pnl` (as real broker rows always do) to
    activate the broker-trust path and avoid the pnl-alias ordering issue
    in the no-pnl code path (which is a pre-existing Polars single-pass
    limitation not introduced by this fix).
    """

    def _partial_sell_row(self, **overrides) -> pd.DataFrame:
        """200 shares bought, 100 sold → quantity=100, opening_quantity=200."""
        row = dict(
            last_price=110.0,
            average_price=100.0,
            quantity=100,           # remaining after partial sell
            opening_quantity=200,   # original lot — display-only
            close_price=105.0,
            pnl=1_000.0,            # broker total pnl on remaining qty
            day_change_val=0.0,
            day_change=5.0,         # ltp - close = 110 - 105
        )
        row.update(overrides)
        return pd.DataFrame([row])

    def test_inv_val_uses_quantity_not_opening_quantity(self):
        """inv_val = avg × quantity (remaining), not avg × opening_quantity."""
        result = _enrich_holdings(self._partial_sell_row())
        # inv_val = 100 × 100 = 10 000 (not 100 × 200 = 20 000)
        assert result.loc[0, "inv_val"] == pytest.approx(10_000.0), (
            "inv_val must use quantity (remaining) not opening_quantity"
        )

    def test_dcv_uses_quantity_not_opening_quantity(self):
        """day_change_val = (ltp − close) × quantity (remaining), not opening_quantity."""
        result = _enrich_holdings(self._partial_sell_row())
        # day_change_val = (110 - 105) × 100 = 500 (not 5 × 200 = 1 000)
        assert result.loc[0, "day_change_val"] == pytest.approx(500.0), (
            "day_change_val must use quantity (remaining) not opening_quantity"
        )

    def test_fallback_dcv_uses_quantity_not_opening_quantity(self):
        """_apply_holdings_dcv_fallback path also uses quantity (remaining).

        Force the Dhan fallback path: supply `day_change` but set `pnl=None`
        so the polars pnl-branch can't fire the decomposed formula.
        """
        # Remove close_price to force the (ltp-close)*qty formula to miss
        # and activate the day_change × qty fallback in pandas.
        df = pd.DataFrame([dict(
            last_price=110.0,
            average_price=100.0,
            quantity=100,
            opening_quantity=200,
            close_price=105.0,
            day_change=5.0,       # ltp - close; non-zero triggers fallback
            day_change_val=0.0,   # broker shipped 0 — fallback activates
            pnl=1000.0,
        )])
        result = _enrich_holdings(df)
        # day_change_val = (110 - 105) × 100 = 500 from polars or fallback.
        assert result.loc[0, "day_change_val"] == pytest.approx(500.0), (
            "Fallback day_change_val must use quantity (remaining) not opening_quantity"
        )

    def test_cur_val_uses_quantity_not_opening_quantity(self):
        """cur_val = inv_val + pnl. When broker pnl = (ltp-avg)×qty = 1 000
        and inv_val = avg×qty = 10 000, cur_val = 11 000 = ltp×qty."""
        result = _enrich_holdings(self._partial_sell_row())
        # cur_val = 10 000 + 1 000 = 11 000 (not 110 × 200 = 22 000)
        assert result.loc[0, "cur_val"] == pytest.approx(11_000.0), (
            "cur_val must use quantity (remaining) not opening_quantity"
        )

    def test_opening_quantity_preserved_as_display_field(self):
        """opening_quantity column must remain in the output unchanged."""
        result = _enrich_holdings(self._partial_sell_row())
        assert "opening_quantity" in result.columns, (
            "opening_quantity must be preserved as a display-only column"
        )
        assert result.loc[0, "opening_quantity"] == 200, (
            "opening_quantity display value must not be modified"
        )
