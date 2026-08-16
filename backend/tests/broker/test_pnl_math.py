"""Tests for apply_day_change_backstop() in backend/api/algo/pnl_math.py.

Covers the three recovery cases:
  Case 1 — new position (oq==0, dcv==0, pnl!=0)
  Case 2 — overnight position, LTP gate zeroed dcv but pnl is valid
  Case 3 — fully closed intraday (qty==0, dcv==0, pnl!=0)
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.pnl_math import apply_day_change_backstop


def _make_df(**kwargs) -> pd.DataFrame:
    """Build a single-row DataFrame from keyword column values."""
    return pd.DataFrame([kwargs])


class TestCase2OvernightPositionBackstop:
    """Case 2: overnight position where LTP gate zeroed dcv, pnl is valid."""

    def test_case2_overnight_position_backstop(self):
        """Core spec: day_change_val = pnl − (close − avg) × oq."""
        df = _make_df(
            quantity=10,
            overnight_quantity=10,
            day_change_val=0.0,
            pnl=500.0,
            close_price=100.0,
            average_price=95.0,
        )
        result = apply_day_change_backstop(df)
        # pnl - (close - avg) * oq = 500 - (100 - 95) * 10 = 450.0
        assert result.loc[0, "day_change_val"] == pytest.approx(450.0)

    def test_case2_does_not_fire_when_dcv_already_set(self):
        """Case 2 must not overwrite a valid day_change_val."""
        df = _make_df(
            quantity=10,
            overnight_quantity=10,
            day_change_val=300.0,   # already valid
            pnl=500.0,
            close_price=100.0,
            average_price=95.0,
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(300.0)

    def test_case2_does_not_fire_when_close_zero(self):
        """Case 2 must not fire when close_price == 0 (stale guard)."""
        df = _make_df(
            quantity=10,
            overnight_quantity=10,
            day_change_val=0.0,
            pnl=500.0,
            close_price=0.0,        # guard: close must be > 0
            average_price=95.0,
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)

    def test_case2_does_not_fire_when_avg_zero(self):
        """Case 2 must not fire when average_price == 0."""
        df = _make_df(
            quantity=10,
            overnight_quantity=10,
            day_change_val=0.0,
            pnl=500.0,
            close_price=100.0,
            average_price=0.0,      # guard: avg must be > 0
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)

    def test_case2_negative_pnl(self):
        """Case 2 works for loss positions."""
        df = _make_df(
            quantity=5,
            overnight_quantity=5,
            day_change_val=0.0,
            pnl=-200.0,
            close_price=110.0,
            average_price=100.0,
        )
        result = apply_day_change_backstop(df)
        # pnl - (close - avg) * oq = -200 - (110 - 100) * 5 = -250.0
        assert result.loc[0, "day_change_val"] == pytest.approx(-250.0)


class TestCase1NewPositionBackstop:
    """Case 1: new position (overnight_quantity == 0)."""

    def test_case1_new_position_uses_pnl(self):
        df = _make_df(
            quantity=10,
            overnight_quantity=0,
            day_change_val=0.0,
            pnl=150.0,
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(150.0)

    def test_case1_no_columns_for_case2_still_works(self):
        """Case 1 must work even without close_price / average_price columns."""
        df = _make_df(
            quantity=10,
            overnight_quantity=0,
            day_change_val=0.0,
            pnl=200.0,
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(200.0)


class TestCase3FlatIntradayBackstop:
    """Case 3: fully closed intraday (quantity == 0)."""

    def test_case3_flat_intraday_uses_pnl(self):
        df = _make_df(
            quantity=0,
            overnight_quantity=0,
            day_change_val=0.0,
            pnl=75.0,
        )
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(75.0)


class TestBackstopEdgeCases:
    """Guards and edge cases for apply_day_change_backstop."""

    def test_empty_dataframe_returned_unchanged(self):
        df = pd.DataFrame()
        result = apply_day_change_backstop(df)
        assert result.empty

    def test_none_returned_unchanged(self):
        result = apply_day_change_backstop(None)  # type: ignore[arg-type]
        assert result is None

    def test_no_day_change_val_column_no_error(self):
        """If day_change_val column is absent the function must not crash."""
        df = _make_df(quantity=10, overnight_quantity=0, pnl=100.0)
        result = apply_day_change_backstop(df)
        assert "day_change_val" not in result.columns

    def test_original_df_not_mutated(self):
        """Function must return a copy, not mutate the input."""
        df = _make_df(
            quantity=10,
            overnight_quantity=0,
            day_change_val=0.0,
            pnl=100.0,
        )
        original_val = df.loc[0, "day_change_val"]
        apply_day_change_backstop(df)
        assert df.loc[0, "day_change_val"] == original_val

    def test_case1_and_case2_same_row_case1_wins(self):
        """When oq==0, Case 1 fires; Case 2 guard (oq > 0) does not."""
        df = _make_df(
            quantity=0,
            overnight_quantity=0,
            day_change_val=0.0,
            pnl=300.0,
            close_price=100.0,
            average_price=95.0,
        )
        result = apply_day_change_backstop(df)
        # Case 1 mask fires: dcv = pnl = 300.0
        assert result.loc[0, "day_change_val"] == pytest.approx(300.0)

    def test_multi_row_mixed_cases(self):
        """Multi-row DataFrame exercises all three cases in one call."""
        df = pd.DataFrame([
            # Case 1: new position
            dict(quantity=5,  overnight_quantity=0,  day_change_val=0.0,
                 pnl=100.0, close_price=50.0,  average_price=45.0),
            # Case 2: overnight, dcv zeroed
            dict(quantity=10, overnight_quantity=10, day_change_val=0.0,
                 pnl=500.0, close_price=100.0, average_price=95.0),
            # Case 3: flat intraday
            dict(quantity=0,  overnight_quantity=0,  day_change_val=0.0,
                 pnl=75.0,  close_price=80.0,  average_price=70.0),
            # No case: dcv already valid
            dict(quantity=8,  overnight_quantity=8,  day_change_val=200.0,
                 pnl=300.0, close_price=100.0, average_price=90.0),
        ])
        result = apply_day_change_backstop(df)
        assert result.loc[0, "day_change_val"] == pytest.approx(100.0)   # Case 1
        assert result.loc[1, "day_change_val"] == pytest.approx(450.0)   # Case 2
        assert result.loc[2, "day_change_val"] == pytest.approx(75.0)    # Case 3
        assert result.loc[3, "day_change_val"] == pytest.approx(200.0)   # untouched
