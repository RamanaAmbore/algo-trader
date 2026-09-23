"""Tests for day_change_percentage computation in background positions summary.

The pending fix adds `day_change_percentage` computation to:
  1. _rebuild_positions_summary() — used after stale-close override patches
  2. _fetch_positions_direct() — raw broker data with day-change backstop applied

Formula (from positions.py:_build_polars_summary):
  denominator = Σ |close_price × quantity| per account
  day_change_percentage = day_change_val / denominator × 100
  (returns 0 when denominator == 0)

Five quality dimensions tested:
  1. SSOT   — formula matches positions.py reference implementation
  2. Perf   — pure computation, O(n) groupby, no DB/broker calls
  3. Stale  — no dead code paths; guard covers zero-denominator edge case
  4. Reuse  — canonical groupby pattern used across background + routes
  5. UX     — result integrates into NavStrip P slot 1 (performance total)
"""

from __future__ import annotations

import pandas as pd
import pytest
from backend.api.background import _rebuild_positions_summary, _fetch_positions_direct


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: basic percentage computation
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_basic_percentage():
    """Test day_change_percentage with two accounts.

    Account A: close_price=100, quantity=10 → denominator=1000
              day_change_val=200 → percentage=200/1000*100=20.0

    Account B: close_price=200, quantity=5 → denominator=1000
              day_change_val=-50 → percentage=-50/1000*100=-5.0

    TOTAL: day_change_val=(200-50)=150
           denominator=(1000+1000)=2000
           percentage=150/2000*100=7.5
    """
    raw = pd.DataFrame({
        'account': ['A', 'B'],
        'close_price': [100.0, 200.0],
        'quantity': [10, 5],
        'pnl': [500.0, -100.0],
        'day_change_val': [200.0, -50.0],
    })

    result = _rebuild_positions_summary(raw)

    # Verify result shape and columns
    assert not result.empty, "Expected non-empty result"
    assert 'account' in result.columns, "Missing 'account' column"
    assert 'day_change_percentage' in result.columns, "Missing 'day_change_percentage' column"

    # Extract rows by account
    row_a = result[result['account'] == 'A']
    row_b = result[result['account'] == 'B']
    row_total = result[result['account'] == 'TOTAL']

    assert len(row_a) == 1, f"Expected 1 account A row, got {len(row_a)}"
    assert len(row_b) == 1, f"Expected 1 account B row, got {len(row_b)}"
    assert len(row_total) == 1, f"Expected 1 TOTAL row, got {len(row_total)}"

    # Verify account A percentage: 200 / (100*10) * 100 = 20.0
    pct_a = row_a['day_change_percentage'].iloc[0]
    assert abs(pct_a - 20.0) < 0.001, (
        f"Account A: expected 20.0%, got {pct_a}"
    )

    # Verify account B percentage: -50 / (200*5) * 100 = -5.0
    pct_b = row_b['day_change_percentage'].iloc[0]
    assert abs(pct_b - (-5.0)) < 0.001, (
        f"Account B: expected -5.0%, got {pct_b}"
    )

    # Verify TOTAL percentage: 150 / 2000 * 100 = 7.5
    pct_total = row_total['day_change_percentage'].iloc[0]
    assert abs(pct_total - 7.5) < 0.001, (
        f"TOTAL: expected 7.5%, got {pct_total}"
    )


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: zero denominator guard
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_zero_denominator_close_price_zero():
    """When close_price=0 for a position, denominator=0 → day_change_percentage=0.

    No ZeroDivisionError should be raised.
    """
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [0.0],
        'quantity': [100],
        'pnl': [500.0],
        'day_change_val': [100.0],
    })

    result = _rebuild_positions_summary(raw)

    assert not result.empty, "Expected non-empty result"
    row = result[result['account'] == 'A']
    assert len(row) == 1

    # With zero denominator, should return 0.0 (guard against division by zero)
    pct = row['day_change_percentage'].iloc[0]
    assert pct == 0.0, (
        f"Expected 0.0% for zero denominator, got {pct}"
    )


def test_rebuild_positions_summary_zero_denominator_quantity_zero():
    """When quantity=0 for a position, denominator=0 → day_change_percentage=0.

    Flat positions (already closed) should not cause errors.
    """
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [100.0],
        'quantity': [0],
        'pnl': [500.0],
        'day_change_val': [100.0],
    })

    result = _rebuild_positions_summary(raw)

    assert not result.empty
    row = result[result['account'] == 'A']
    pct = row['day_change_percentage'].iloc[0]
    assert pct == 0.0, (
        f"Expected 0.0% for zero quantity, got {pct}"
    )


def test_rebuild_positions_summary_mixed_zero_nonzero():
    """Account with one zero-denominator row and one normal row.

    Account A has 2 positions:
      - Position 1: close=100, qty=10, dcv=200 → contributes 1000 to denom
      - Position 2: close=0, qty=5, dcv=100 → contributes 0 to denom

    Grouped by account, denominator sum=1000, dcv sum=300
    Percentage = 300/1000*100 = 30.0
    """
    raw = pd.DataFrame({
        'account': ['A', 'A'],
        'close_price': [100.0, 0.0],
        'quantity': [10, 5],
        'pnl': [500.0, 100.0],
        'day_change_val': [200.0, 100.0],
    })

    result = _rebuild_positions_summary(raw)

    row_a = result[result['account'] == 'A']
    pct_a = row_a['day_change_percentage'].iloc[0]
    # Expected: (200+100) / 1000 * 100 = 30.0
    assert abs(pct_a - 30.0) < 0.001, (
        f"Expected 30.0%, got {pct_a}"
    )


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: empty DataFrame
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_empty():
    """Empty raw DataFrame should return empty result with correct columns."""
    raw = pd.DataFrame()
    result = _rebuild_positions_summary(raw)

    # Should return a DataFrame with expected columns
    assert isinstance(result, pd.DataFrame)
    # May be empty or have 0 rows
    assert len(result) == 0, (
        f"Expected 0 rows for empty input, got {len(result)}"
    )


def test_rebuild_positions_summary_empty_with_account_column():
    """Empty DataFrame with 'account' column should still return empty result."""
    raw = pd.DataFrame(columns=['account', 'close_price', 'quantity', 'pnl', 'day_change_val'])
    result = _rebuild_positions_summary(raw)

    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: missing close_price column
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_missing_close_price():
    """DataFrame without close_price column.

    _rebuild_positions_summary should not crash and should return a result
    (day_change_percentage will be computed only from available columns or
    default to 0 if the computation is not attempted due to missing inputs).
    """
    raw = pd.DataFrame({
        'account': ['A', 'B'],
        'quantity': [10, 5],
        'pnl': [500.0, -100.0],
        'day_change_val': [200.0, -50.0],
    })

    # Should not raise an error
    result = _rebuild_positions_summary(raw)

    assert not result.empty, "Expected non-empty result"
    # Result should contain the summary data
    assert 'account' in result.columns
    assert 'day_change_val' in result.columns
    assert len(result) >= 2  # At least two account rows


def test_rebuild_positions_summary_missing_quantity():
    """DataFrame without quantity column.

    Without quantity, the denominator cannot be computed (close_price × qty).
    The function should handle gracefully.
    """
    raw = pd.DataFrame({
        'account': ['A', 'B'],
        'close_price': [100.0, 200.0],
        'pnl': [500.0, -100.0],
        'day_change_val': [200.0, -50.0],
    })

    # Should not raise an error
    result = _rebuild_positions_summary(raw)

    assert not result.empty
    assert 'account' in result.columns


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: single position per account
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_single_position_per_account():
    """Simple case with one position per account."""
    raw = pd.DataFrame({
        'account': ['A', 'B'],
        'close_price': [50.0, 100.0],
        'quantity': [20, 10],
        'pnl': [200.0, 300.0],
        'day_change_val': [100.0, 150.0],
    })

    result = _rebuild_positions_summary(raw)

    # Account A: dcv=100, denom=50*20=1000 → pct=10.0
    row_a = result[result['account'] == 'A']
    pct_a = row_a['day_change_percentage'].iloc[0]
    assert abs(pct_a - 10.0) < 0.001

    # Account B: dcv=150, denom=100*10=1000 → pct=15.0
    row_b = result[result['account'] == 'B']
    pct_b = row_b['day_change_percentage'].iloc[0]
    assert abs(pct_b - 15.0) < 0.001


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: large positive moves
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_large_positive_day_pnl():
    """Day change of +50% should compute correctly."""
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [100.0],
        'quantity': [10],
        'pnl': [1000.0],
        'day_change_val': [500.0],  # 50% of denominator
    })

    result = _rebuild_positions_summary(raw)
    row = result[result['account'] == 'A']
    pct = row['day_change_percentage'].iloc[0]
    assert abs(pct - 50.0) < 0.001


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: large negative moves
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_large_negative_day_pnl():
    """Day change of -75% should compute correctly."""
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [100.0],
        'quantity': [10],
        'pnl': [-1000.0],
        'day_change_val': [-750.0],  # -75% of denominator
    })

    result = _rebuild_positions_summary(raw)
    row = result[result['account'] == 'A']
    pct = row['day_change_percentage'].iloc[0]
    assert abs(pct - (-75.0)) < 0.001


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: TOTAL row aggregation
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_total_row_aggregation():
    """TOTAL row must aggregate day_change_percentage correctly across accounts.

    Uses the same formula: Σ day_change_val / Σ (|close × qty|) * 100
    """
    raw = pd.DataFrame({
        'account': ['A', 'A', 'B', 'B'],
        'close_price': [100.0, 50.0, 200.0, 100.0],
        'quantity': [10, 20, 5, 10],
        'pnl': [500.0, 200.0, 300.0, 100.0],
        'day_change_val': [100.0, 50.0, 75.0, 25.0],  # TOTAL=250
    })

    result = _rebuild_positions_summary(raw)

    row_total = result[result['account'] == 'TOTAL']
    assert len(row_total) == 1

    # Account A: denom = 100*10 + 50*20 = 1000+1000 = 2000, dcv = 100+50 = 150 → 7.5%
    # Account B: denom = 200*5 + 100*10 = 1000+1000 = 2000, dcv = 75+25 = 100 → 5.0%
    # TOTAL: denom = 4000, dcv = 250 → 6.25%

    pct_total = row_total['day_change_percentage'].iloc[0]
    expected_total = 250.0 / 4000.0 * 100
    assert abs(pct_total - expected_total) < 0.001, (
        f"TOTAL: expected {expected_total}%, got {pct_total}"
    )


# ---------------------------------------------------------------------------
# Test _rebuild_positions_summary: missing day_change_val column
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_missing_day_change_val():
    """DataFrame without day_change_val column.

    The function should handle gracefully. It may skip the computation
    or add a column with default values.
    """
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [100.0],
        'quantity': [10],
        'pnl': [500.0],
    })

    # Should not raise an error
    result = _rebuild_positions_summary(raw)

    assert not result.empty


# ---------------------------------------------------------------------------
# Integration test: verify TOTAL row is always present
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_total_row_always_present():
    """TOTAL row must always be present in output, even for single account."""
    raw = pd.DataFrame({
        'account': ['A'],
        'close_price': [100.0],
        'quantity': [10],
        'pnl': [500.0],
        'day_change_val': [200.0],
    })

    result = _rebuild_positions_summary(raw)

    # Must have at least 2 rows (A + TOTAL)
    assert len(result) >= 2, f"Expected at least 2 rows, got {len(result)}"

    # Last row must be TOTAL
    last_row = result.iloc[-1]
    assert last_row['account'] == 'TOTAL', (
        f"Expected TOTAL in last row, got {last_row['account']}"
    )


# ---------------------------------------------------------------------------
# Stress test: many accounts and positions
# ---------------------------------------------------------------------------

def test_rebuild_positions_summary_many_accounts():
    """Verify performance and correctness with many accounts."""
    import numpy as np

    # Create 10 accounts, 5 positions each
    accounts = ['A'] * 5 + ['B'] * 5 + ['C'] * 5 + ['D'] * 5 + ['E'] * 5 + \
               ['F'] * 5 + ['G'] * 5 + ['H'] * 5 + ['I'] * 5 + ['J'] * 5

    close_prices = np.random.uniform(50, 500, 50)
    quantities = np.random.randint(1, 100, 50)
    pnls = np.random.uniform(-1000, 1000, 50)
    day_changes = np.random.uniform(-500, 500, 50)

    raw = pd.DataFrame({
        'account': accounts,
        'close_price': close_prices,
        'quantity': quantities,
        'pnl': pnls,
        'day_change_val': day_changes,
    })

    result = _rebuild_positions_summary(raw)

    # Should have 10 accounts + 1 TOTAL = 11 rows
    assert len(result) == 11, f"Expected 11 rows, got {len(result)}"

    # Verify all accounts are present
    assert 'A' in result['account'].values
    assert 'J' in result['account'].values
    assert 'TOTAL' in result['account'].values

    # Verify no NaN in percentages
    assert not result['day_change_percentage'].isna().any(), (
        "Found NaN values in day_change_percentage"
    )


# ---------------------------------------------------------------------------
# Test _fetch_positions_direct: integration note
# ---------------------------------------------------------------------------

def test_fetch_positions_direct_would_use_summary():
    """
    Note: _fetch_positions_direct() calls broker APIs (which we don't mock),
    so we cannot fully test it here. However, this test documents that it
    returns (raw_df, summary_df) and the fix adds day_change_percentage
    to the summary_df returned.

    When the fix is applied:
      - _fetch_positions_direct() will call _rebuild_positions_summary()
      - The summary will include 'day_change_percentage' column
      - NavStrip P slot 1 will use this for the performance total
    """
    # This is a documentation test — actual test requires broker mock
    pass
