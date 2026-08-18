"""Tests for _apply_flat_row_hygiene in backend.api.routes.positions.

Verifies that the narrowed flat-mask (qty==0 AND oq==0) correctly:
  - Preserves day_change_val for closed overnight futures (qty=0, oq>0)
  - Zeroes day_change_val for pure intraday round-trips (qty=0, oq=0)
  - Leaves open positions (qty>0) untouched
"""
import pandas as pd
import pytest

from backend.api.routes.positions import _apply_flat_row_hygiene


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal positions DataFrame from a list of row dicts."""
    return pd.DataFrame(rows)


class TestApplyFlatRowHygiene:
    """Unit tests for _apply_flat_row_hygiene."""

    def test_closed_overnight_futures_retains_day_change_val(self) -> None:
        """qty=0, oq>0 (closed overnight position): day_change_val must NOT be zeroed.

        This is the core regression guard for the bug where apply_day_change_backstop
        correctly set day_change_val for closed overnight rows but _apply_flat_row_hygiene
        subsequently overwrote it with 0.0.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 1,
            'day_change_val': 1500.0,
            'day_change': 0.0,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 1500.0, (
            "Closed overnight position (qty=0, oq>0) must retain backstop day_change_val"
        )

    def test_closed_overnight_futures_negative_pnl_retains(self) -> None:
        """Negative day_change_val for a losing closed overnight position is also retained."""
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 2,
            'day_change_val': -800.0,
            'day_change': -5.0,
            'day_change_percentage': -1.2,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == -800.0

    def test_pure_intraday_roundtrip_zeroes_day_change_val(self) -> None:
        """qty=0, oq=0 (pure intraday round-trip): day_change_val MUST be zeroed.

        LTP is meaningless for a fully-closed intraday trade; leaving it
        non-zero would cause phantom P&L in the NavStrip performance slot.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'day_change_val': 250.0,
            'day_change': 1.5,
            'day_change_percentage': 0.8,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 0.0
        assert raw.loc[0, 'day_change'] == 0.0
        assert raw.loc[0, 'day_change_percentage'] == 0.0

    def test_open_position_untouched(self) -> None:
        """qty>0 (open position): all fields must be untouched."""
        raw = _make_df([{
            'quantity': 5,
            'overnight_quantity': 5,
            'day_change_val': 3200.0,
            'day_change': 8.0,
            'day_change_percentage': 2.5,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 3200.0
        assert raw.loc[0, 'day_change'] == 8.0
        assert raw.loc[0, 'day_change_percentage'] == 2.5

    def test_mixed_rows_only_intraday_roundtrip_zeroed(self) -> None:
        """When all three row types are present, only the intraday round-trip is zeroed."""
        raw = _make_df([
            # Row 0: closed overnight (qty=0, oq>0) — retain
            {
                'quantity': 0, 'overnight_quantity': 3,
                'day_change_val': 900.0, 'day_change': 3.0, 'day_change_percentage': 1.0,
            },
            # Row 1: intraday round-trip (qty=0, oq=0) — zero
            {
                'quantity': 0, 'overnight_quantity': 0,
                'day_change_val': 120.0, 'day_change': 0.5, 'day_change_percentage': 0.3,
            },
            # Row 2: open position (qty>0) — untouched
            {
                'quantity': 10, 'overnight_quantity': 10,
                'day_change_val': 5000.0, 'day_change': 12.0, 'day_change_percentage': 3.0,
            },
        ])
        _apply_flat_row_hygiene(raw)

        # Row 0: closed overnight — retained
        assert raw.loc[0, 'day_change_val'] == 900.0
        assert raw.loc[0, 'day_change'] == 3.0
        assert raw.loc[0, 'day_change_percentage'] == 1.0

        # Row 1: intraday round-trip — zeroed
        assert raw.loc[1, 'day_change_val'] == 0.0
        assert raw.loc[1, 'day_change'] == 0.0
        assert raw.loc[1, 'day_change_percentage'] == 0.0

        # Row 2: open position — untouched
        assert raw.loc[2, 'day_change_val'] == 5000.0
        assert raw.loc[2, 'day_change'] == 12.0
        assert raw.loc[2, 'day_change_percentage'] == 3.0

    def test_absent_overnight_quantity_column_treats_as_zero(self) -> None:
        """When overnight_quantity column is absent, rows behave as oq=0.

        This covers adapters that don't populate overnight_quantity.
        A qty=0 row without oq column is treated as a pure intraday round-trip
        and gets zeroed, not retained.
        """
        raw = _make_df([{
            'quantity': 0,
            'day_change_val': 400.0,
            'day_change': 2.0,
            'day_change_percentage': 0.6,
            # overnight_quantity deliberately absent
        }])
        assert 'overnight_quantity' not in raw.columns
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 0.0

    def test_empty_dataframe_is_noop(self) -> None:
        """Empty DataFrame must not raise."""
        raw = pd.DataFrame()
        _apply_flat_row_hygiene(raw)  # must not raise

    def test_missing_quantity_column_is_noop(self) -> None:
        """DataFrame without quantity column must not raise."""
        raw = _make_df([{'overnight_quantity': 1, 'day_change_val': 100.0}])
        _apply_flat_row_hygiene(raw)  # must not raise
        assert raw.loc[0, 'day_change_val'] == 100.0

    def test_partial_columns_only_present_ones_zeroed(self) -> None:
        """When day_change or day_change_percentage is absent, no KeyError raised."""
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'day_change_val': 300.0,
            # day_change and day_change_percentage absent intentionally
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 0.0

    def test_no_flat_rows_returns_early(self) -> None:
        """When no qty==0 rows exist, the function is a no-op (early return path)."""
        raw = _make_df([{
            'quantity': 1,
            'overnight_quantity': 1,
            'day_change_val': 700.0,
            'day_change': 5.0,
            'day_change_percentage': 1.5,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 700.0
