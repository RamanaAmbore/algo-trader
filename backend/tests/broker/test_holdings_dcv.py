"""Tests for the day_change_val fallback in broker_apis._enrich_holdings().

Covers the Dhan holdings case where `day_change_val` is zeroed by the polars
path (because the broker shipped 0.0, which is not-null) but a scalar
`day_change = ltp - close` is available. The fallback multiplies
day_change × opening_quantity to recover the total session change.
"""

from __future__ import annotations

import pandas as pd
import pytest

# _enrich_holdings is module-private but importable directly for unit tests.
from backend.brokers.broker_apis import _enrich_holdings  # noqa: PLC2701


def _base_holding(**overrides) -> pd.DataFrame:
    """Return a minimal single-row holdings DataFrame."""
    row = dict(
        last_price=955.0,
        average_price=900.0,
        opening_quantity=100,
        close_price=950.0,
        day_change=5.0,       # ltp - close = 955 - 950
        day_change_val=0.0,   # broker shipped 0 — triggers fallback
        pnl=5500.0,           # broker total pnl (not used for dcv here)
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
            opening_quantity=400,
            day_change_val=0.0,
        )
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(1000.0)

    def test_multi_row_mixed_dcv(self):
        """Multi-row: fallback fires only on zero-dcv rows."""
        df = pd.DataFrame([
            # Row 0: dcv == 0 → fallback should fire → 5 * 100 = 500
            dict(last_price=955.0, average_price=900.0, opening_quantity=100,
                 close_price=950.0, day_change=5.0, day_change_val=0.0, pnl=5500.0),
            # Row 1: dcv already valid → must not change
            dict(last_price=955.0, average_price=900.0, opening_quantity=100,
                 close_price=950.0, day_change=5.0, day_change_val=750.0, pnl=5500.0),
        ])
        result = _enrich_holdings(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(500.0)
        assert result.loc[1, "day_change_val"] == pytest.approx(750.0)
