"""Tests for _apply_flat_row_hygiene in backend.api.routes.positions.

Verifies that the narrowed flat-mask (qty==0 AND oq==0) correctly:
  - Preserves day_change_val for closed overnight futures (qty=0, oq>0)
  - Zeroes day_change_val for pure break-even intraday round-trips (qty=0, oq=0, pnl=0)
  - Preserves day_change_val for closed intraday with realised P&L (Case 3: qty=0, oq=0, pnl!=0)
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

    def test_pure_intraday_roundtrip_with_pnl_preserves_day_change_val(self) -> None:
        """Case 3: qty=0, oq=0, pnl!=0 (closed intraday with gain/loss): day_change_val MUST be preserved.

        When an intraday position closes with a realised gain/loss (abs(pnl) >= 0.005),
        the apply_day_change_backstop has restored day_change_val = pnl.
        _apply_flat_row_hygiene must NOT zero it — NavStrip P slot needs the realised gain/loss.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 500.0,
            'day_change_val': 500.0,
            'day_change': 1.5,
            'day_change_percentage': 0.8,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 500.0, (
            "Case 3: closed intraday with pnl must preserve day_change_val"
        )
        assert raw.loc[0, 'day_change'] == 0.0
        assert raw.loc[0, 'day_change_percentage'] == 0.0

    def test_pure_intraday_breakeven_roundtrip_zeroes_day_change_val(self) -> None:
        """qty=0, oq=0, pnl=0 (break-even intraday round-trip): day_change_val must be zeroed.

        When an intraday position closes break-even (abs(pnl) < 0.005), there is no
        realised gain/loss, so day_change_val should be zero.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
            'day_change_percentage': 0.0,
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
        """Only break-even intraday round-trips are zeroed; overnight-closed and open rows untouched."""
        raw = _make_df([
            # Row 0: closed overnight (qty=0, oq>0) — NOT in _flat_mask, retain as-is
            {
                'quantity': 0, 'overnight_quantity': 3,
                'day_change_val': 900.0, 'day_change': 3.0, 'day_change_percentage': 1.0,
                'pnl': 900.0,
            },
            # Row 1: intraday break-even round-trip (qty=0, oq=0, pnl=0) — zero
            {
                'quantity': 0, 'overnight_quantity': 0,
                'day_change_val': 120.0, 'day_change': 0.5, 'day_change_percentage': 0.3,
                'pnl': 0.0,
            },
            # Row 2: open position (qty>0) — untouched
            {
                'quantity': 10, 'overnight_quantity': 10,
                'day_change_val': 5000.0, 'day_change': 12.0, 'day_change_percentage': 3.0,
                'pnl': 5000.0,
            },
        ])
        _apply_flat_row_hygiene(raw)

        # Row 0: closed overnight — NOT in _flat_mask (oq>0), all values untouched
        assert raw.loc[0, 'day_change_val'] == 900.0
        assert raw.loc[0, 'day_change'] == 3.0   # oq>0 excludes from _flat_mask
        assert raw.loc[0, 'day_change_percentage'] == 1.0

        # Row 1: intraday break-even — zeroed
        assert raw.loc[1, 'day_change_val'] == 0.0
        assert raw.loc[1, 'day_change'] == 0.0
        assert raw.loc[1, 'day_change_percentage'] == 0.0

        # Row 2: open position — untouched
        assert raw.loc[2, 'day_change_val'] == 5000.0
        assert raw.loc[2, 'day_change'] == 12.0
        assert raw.loc[2, 'day_change_percentage'] == 3.0

    def test_absent_overnight_quantity_column_treats_as_zero(self) -> None:
        """When overnight_quantity column is absent, rows behave as oq=0.

        A qty=0 row without oq column is treated as a pure intraday round-trip.
        When pnl is also absent (treated as 0), day_change_val is zeroed.
        """
        raw = _make_df([{
            'quantity': 0,
            'day_change_val': 400.0,
            'day_change': 2.0,
            'day_change_percentage': 0.6,
            # overnight_quantity and pnl deliberately absent
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
        """When day_change or day_change_percentage is absent, no KeyError raised.

        Row has qty=0, oq=0, and pnl absent (treated as 0) — day_change_val is zeroed.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'day_change_val': 300.0,
            # day_change, day_change_percentage, and pnl absent intentionally
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

    def test_case3_row_retains_day_change_val_after_hygiene(self) -> None:
        """Case 3 row: qty=0, oq=0, pnl=500.0 -> day_change_val preserved (not zeroed).

        This is the core Case 3 test: a fully-closed intraday trade with realised P&L
        must retain its day_change_val so the NavStrip P slot shows the realised gain/loss.
        """
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 500.0,
            'day_change_val': 500.0,
            'day_change': 1.5,
            'day_change_percentage': 0.8,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 500.0, (
            "Case 3: closed intraday with pnl=500.0 must preserve day_change_val"
        )

    def test_breakeven_roundtrip_row_day_change_val_stays_zero(self) -> None:
        """Break-even round-trip: qty=0, oq=0, pnl=0.0 -> day_change_val stays 0.0."""
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.0,
            'day_change_val': 0.0,
            'day_change': 0.0,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 0.0, (
            "Break-even round-trip (pnl=0.0) must have day_change_val=0.0"
        )

    def test_multiple_sameday_entries_exits_aggregate_pnl_case(self) -> None:
        """Multiple same-day entries/exits: qty=0, oq=0, pnl=1250.75 -> day_change_val preserved."""
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 1250.75,
            'day_change_val': 1250.75,
            'day_change': 5.2,
            'day_change_percentage': 2.1,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == 1250.75, (
            "Multiple same-day entries/exits with aggregate pnl=1250.75 must preserve day_change_val"
        )

    def test_near_zero_pnl_half_paisa_threshold(self) -> None:
        """Near-zero pnl threshold: 0.004 (below) and 0.006 (above) half-paisa threshold."""
        # Test 1: pnl=0.004 (below 0.005 threshold) — should be zeroed
        raw_below = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.004,
            'day_change_val': 0.004,
            'day_change': 0.0001,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw_below)
        assert raw_below.loc[0, 'day_change_val'] == 0.0, (
            "pnl=0.004 (below 0.005 threshold) should have day_change_val zeroed"
        )

        # Test 2: pnl=0.006 (above 0.005 threshold) — should be preserved
        raw_above = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': 0.006,
            'day_change_val': 0.006,
            'day_change': 0.0002,
            'day_change_percentage': 0.0,
        }])
        _apply_flat_row_hygiene(raw_above)
        assert raw_above.loc[0, 'day_change_val'] == 0.006, (
            "pnl=0.006 (above 0.005 threshold) should have day_change_val preserved"
        )

    def test_negative_pnl_closed_intraday_preserved(self) -> None:
        """Negative pnl on closed intraday: qty=0, oq=0, pnl=-750.25 -> day_change_val preserved."""
        raw = _make_df([{
            'quantity': 0,
            'overnight_quantity': 0,
            'pnl': -750.25,
            'day_change_val': -750.25,
            'day_change': -3.5,
            'day_change_percentage': -1.8,
        }])
        _apply_flat_row_hygiene(raw)
        assert raw.loc[0, 'day_change_val'] == -750.25, (
            "Negative pnl on closed intraday must preserve day_change_val"
        )

    def test_case3_mixed_with_overnight_closed_and_open_rows(self) -> None:
        """Mixed scenarios: Case 3 (pnl), overnight closed (oq>0), and open (qty>0) rows."""
        raw = _make_df([
            # Row 0: Case 3 — closed intraday with pnl — preserve day_change_val, zero day_change/pct
            {
                'quantity': 0,
                'overnight_quantity': 0,
                'pnl': 650.0,
                'day_change_val': 650.0,
                'day_change': 2.0,
                'day_change_percentage': 1.0,
            },
            # Row 1: overnight closed (oq>0) — NOT in _flat_mask, all values untouched as-is
            {
                'quantity': 0,
                'overnight_quantity': 2,
                'pnl': 1200.0,
                'day_change_val': 800.0,
                'day_change': 3.0,
                'day_change_percentage': 1.5,
            },
            # Row 2: open position (qty>0) — should be untouched
            {
                'quantity': 5,
                'overnight_quantity': 5,
                'pnl': 2500.0,
                'day_change_val': 2000.0,
                'day_change': 5.0,
                'day_change_percentage': 2.0,
            },
        ])
        _apply_flat_row_hygiene(raw)

        # Row 0: Case 3 — day_change_val preserved, day_change/pct zeroed (in _flat_mask)
        assert raw.loc[0, 'day_change_val'] == 650.0
        assert raw.loc[0, 'day_change'] == 0.0
        assert raw.loc[0, 'day_change_percentage'] == 0.0

        # Row 1: overnight closed (oq>0) — NOT in _flat_mask, all values untouched as-is
        assert raw.loc[1, 'day_change_val'] == 800.0
        assert raw.loc[1, 'day_change'] == 3.0  # oq>0 excludes from _flat_mask

        # Row 2: open untouched
        assert raw.loc[2, 'day_change_val'] == 2000.0
        assert raw.loc[2, 'day_change'] == 5.0
        assert raw.loc[2, 'day_change_percentage'] == 2.0
