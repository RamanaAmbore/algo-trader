"""
Tests for api/background.py — PnL override and rebuild helpers.

SSOT: _rebuild_holdings_summary and _rebuild_positions_summary aggregate
      per-account + TOTAL rows from patched DataFrames.
      _perf_fetch_all_broker_data calls override functions after threadpool fetch,
      then rebuilds summaries before returning (so NavStrip gets patched data).

Perf: rebuild helpers are sync O(n) aggregations; called after async overrides return.

Stale: _override_stale_close_for_holdings + _override_stale_close_from_snapshot
       must be awaited in _perf_fetch_all_broker_data's async context.

Reuse: empty DataFrames handled gracefully; columns preserved; all dtypes stable.

UX: Integrates with background perf-poll → NavStrip day P&L shows current day
    (not stale broker close_price), MarketPulse updates = live.
"""

import os
import pytest
import pytest_asyncio
import pandas as pd
from unittest.mock import AsyncMock, patch, MagicMock, call
from datetime import datetime, timezone, timedelta

os.environ['PYTEST_RUNNING'] = '1'


# Test (a) — rebuild_holdings_summary with multi-account data
def test_rebuild_holdings_summary_multi_account():
    """_rebuild_holdings_summary must aggregate per-account + TOTAL rows.

    Verifies:
    - Per-account rows are grouped and summed
    - TOTAL row sums all accounts
    - day_change_val and pnl are correctly aggregated
    """
    from backend.api.background import _rebuild_holdings_summary

    raw = pd.DataFrame([
        {'account': 'ACC1', 'inv_val': 1000.0, 'cur_val': 1100.0, 'pnl': 100.0, 'day_change_val': 50.0},
        {'account': 'ACC1', 'inv_val': 2000.0, 'cur_val': 2200.0, 'pnl': 200.0, 'day_change_val': 80.0},
        {'account': 'ACC2', 'inv_val': 3000.0, 'cur_val': 3300.0, 'pnl': 300.0, 'day_change_val': -20.0},
    ])

    result = _rebuild_holdings_summary(raw)

    # Check TOTAL row exists and has correct sums
    total_row = result[result['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must exist"
    assert total_row['day_change_val'].iloc[0] == 110.0, \
        f"TOTAL day_change_val should be 110.0 (50 + 80 - 20), got {total_row['day_change_val'].iloc[0]}"
    assert total_row['pnl'].iloc[0] == 600.0, \
        f"TOTAL pnl should be 600.0 (100 + 200 + 300), got {total_row['pnl'].iloc[0]}"

    # Check ACC1 summary row
    acc1_row = result[result['account'] == 'ACC1']
    assert not acc1_row.empty, "ACC1 summary row must exist"
    assert acc1_row['day_change_val'].iloc[0] == 130.0, \
        f"ACC1 day_change_val should be 130.0 (50 + 80), got {acc1_row['day_change_val'].iloc[0]}"
    assert acc1_row['pnl'].iloc[0] == 300.0, \
        f"ACC1 pnl should be 300.0 (100 + 200), got {acc1_row['pnl'].iloc[0]}"

    # Check ACC2 summary row
    acc2_row = result[result['account'] == 'ACC2']
    assert not acc2_row.empty, "ACC2 summary row must exist"
    assert acc2_row['day_change_val'].iloc[0] == -20.0, \
        f"ACC2 day_change_val should be -20.0, got {acc2_row['day_change_val'].iloc[0]}"
    assert acc2_row['pnl'].iloc[0] == 300.0, \
        f"ACC2 pnl should be 300.0, got {acc2_row['pnl'].iloc[0]}"


# Test (b) — rebuild_positions_summary with multi-account data
def test_rebuild_positions_summary_multi_account():
    """_rebuild_positions_summary must aggregate per-account + TOTAL rows.

    Verifies:
    - Per-account rows are grouped and summed
    - TOTAL row sums all accounts
    - pnl and day_change_val are correctly aggregated
    """
    from backend.api.background import _rebuild_positions_summary

    raw = pd.DataFrame([
        {'account': 'ACC1', 'pnl': 500.0, 'day_change_val': 100.0},
        {'account': 'ACC2', 'pnl': -200.0, 'day_change_val': -50.0},
    ])

    result = _rebuild_positions_summary(raw)

    # Check TOTAL row exists and has correct sums
    total_row = result[result['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must exist"
    assert total_row['day_change_val'].iloc[0] == 50.0, \
        f"TOTAL day_change_val should be 50.0 (100 - 50), got {total_row['day_change_val'].iloc[0]}"
    assert total_row['pnl'].iloc[0] == 300.0, \
        f"TOTAL pnl should be 300.0 (500 - 200), got {total_row['pnl'].iloc[0]}"


# Test (c) — rebuild_holdings_summary handles empty DataFrame
def test_rebuild_holdings_summary_empty_dataframe():
    """_rebuild_holdings_summary must handle empty DataFrames gracefully.

    Verifies:
    - Empty DataFrame returns DataFrame with correct columns
    - No exception raised
    - Result is usable (not None)
    """
    from backend.api.background import _rebuild_holdings_summary

    empty_df = pd.DataFrame()
    result = _rebuild_holdings_summary(empty_df)

    assert isinstance(result, pd.DataFrame), \
        f"_rebuild_holdings_summary must return DataFrame, got {type(result)}"
    assert 'account' in result.columns, \
        "Result must have 'account' column even when empty"


# Test (d) — rebuild_positions_summary handles empty DataFrame
def test_rebuild_positions_summary_empty_dataframe():
    """_rebuild_positions_summary must handle empty DataFrames gracefully.

    Verifies:
    - Empty DataFrame returns DataFrame with correct columns
    - No exception raised
    - Result is usable (not None)
    """
    from backend.api.background import _rebuild_positions_summary

    empty_df = pd.DataFrame()
    result = _rebuild_positions_summary(empty_df)

    assert isinstance(result, pd.DataFrame), \
        f"_rebuild_positions_summary must return DataFrame, got {type(result)}"
    assert 'account' in result.columns, \
        "Result must have 'account' column even when empty"


# Test (e) — perf_fetch_all_broker_data calls holdings override
@pytest.mark.asyncio
async def test_perf_fetch_calls_holdings_override():
    """_perf_fetch_all_broker_data must call _override_stale_close_for_holdings.

    Verifies:
    - _override_stale_close_for_holdings is awaited after threadpool fetch
    - Patched DataFrame is passed to rebuild_holdings_summary
    - sum_holdings reflects the patched data
    """
    from backend.api.background import _perf_fetch_all_broker_data

    # Raw holdings DataFrame before override
    raw_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 0.0, 'tradingsymbol': 'RELIANCE'},
    ])

    # Fake holdings summary (what _fetch_holdings_direct returns)
    summary_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 0.0},
        {'account': 'TOTAL', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 0.0},
    ])

    # Raw positions and margins (empty for simplicity)
    raw_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 10.0},
    ])
    summary_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 10.0},
        {'account': 'TOTAL', 'pnl': 50.0, 'day_change_val': 10.0},
    ])
    raw_m = pd.DataFrame()

    # Mock override function that patches day_change_val
    async def mock_override_holdings(df):
        """Simulates the override by patching day_change_val."""
        df['day_change_val'] = 999.0

    async def mock_override_positions(df):
        """Simulates the override (no-op for positions in this test)."""
        pass

    # Mock _run to return fetched data
    async def mock_run(fn):
        """Simulates threadpool fetch results."""
        if 'holdings' in fn.__name__:
            return raw_h, summary_h
        elif 'positions' in fn.__name__:
            return raw_p, summary_p
        elif 'margins' in fn.__name__:
            return raw_m
        return pd.DataFrame()

    with patch('backend.api.background._run', side_effect=mock_run), \
         patch('backend.api.routes.holdings._override_stale_close_for_holdings',
               side_effect=mock_override_holdings) as mock_h_override, \
         patch('backend.api.routes.positions._override_stale_close_from_snapshot',
               side_effect=mock_override_positions):

        df_h, sum_h, df_p, sum_p, df_m = await _perf_fetch_all_broker_data()

    # Verify the override was called
    mock_h_override.assert_awaited_once()

    # Verify sum_holdings was rebuilt from patched data
    # The patched value (999.0) should appear in the summary if rebuild is working
    total_row = sum_h[sum_h['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must exist in returned sum_h"
    # After override patches day_change_val to 999.0, rebuild should preserve it
    assert total_row['day_change_val'].iloc[0] == 999.0, \
        f"sum_h TOTAL day_change_val should reflect override (999.0), " \
        f"got {total_row['day_change_val'].iloc[0]}"


# Test (f) — perf_fetch_all_broker_data calls positions override
@pytest.mark.asyncio
async def test_perf_fetch_calls_positions_override():
    """_perf_fetch_all_broker_data must call _override_stale_close_from_snapshot.

    Verifies:
    - _override_stale_close_from_snapshot is awaited after threadpool fetch
    - Patched DataFrame is passed to rebuild_positions_summary
    - sum_positions reflects the patched data
    """
    from backend.api.background import _perf_fetch_all_broker_data

    # Raw positions DataFrame before override
    raw_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 0.0,
         'tradingsymbol': 'RELIANCE', 'close_price': 2500.0},
    ])

    # Fake positions summary (what _fetch_positions_direct returns)
    summary_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 0.0},
        {'account': 'TOTAL', 'pnl': 50.0, 'day_change_val': 0.0},
    ])

    # Raw holdings and margins (empty for simplicity)
    raw_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
    ])
    summary_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
        {'account': 'TOTAL', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
    ])
    raw_m = pd.DataFrame()

    # Mock override functions
    async def mock_override_holdings(df):
        """No-op for holdings in this test."""
        pass

    async def mock_override_positions(df):
        """Simulates the override by patching day_change_val."""
        df['day_change_val'] = 25.0

    # Mock _run to return fetched data
    async def mock_run(fn):
        """Simulates threadpool fetch results."""
        if 'holdings' in fn.__name__:
            return raw_h, summary_h
        elif 'positions' in fn.__name__:
            return raw_p, summary_p
        elif 'margins' in fn.__name__:
            return raw_m
        return pd.DataFrame()

    with patch('backend.api.background._run', side_effect=mock_run), \
         patch('backend.api.routes.holdings._override_stale_close_for_holdings',
               side_effect=mock_override_holdings), \
         patch('backend.api.routes.positions._override_stale_close_from_snapshot',
               side_effect=mock_override_positions) as mock_p_override:

        df_h, sum_h, df_p, sum_p, df_m = await _perf_fetch_all_broker_data()

    # Verify the override was called
    mock_p_override.assert_awaited_once()

    # Verify sum_positions was rebuilt from patched data
    total_row = sum_p[sum_p['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must exist in returned sum_p"
    # After override patches day_change_val to 25.0, rebuild should preserve it
    assert total_row['day_change_val'].iloc[0] == 25.0, \
        f"sum_p TOTAL day_change_val should reflect override (25.0), " \
        f"got {total_row['day_change_val'].iloc[0]}"


# Test (g-extra) — perf_fetch gracefully falls back when override raises
@pytest.mark.asyncio
async def test_perf_fetch_holdings_override_failure_fallback():
    """When _override_stale_close_for_holdings raises, sum_holdings must stay
    as returned by _fetch_holdings_direct (not crash and not return empty).

    Verifies:
    - Exception inside override inner try/except does NOT propagate outward
    - sum_holdings retains the pre-override summary (original threadpool result)
    - The function still returns a 5-tuple with valid DataFrames
    """
    from backend.api.background import _perf_fetch_all_broker_data

    raw_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 42.0},
    ])
    summary_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 42.0},
        {'account': 'TOTAL', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 42.0},
    ])
    raw_p = pd.DataFrame([{'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 5.0}])
    summary_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 50.0, 'day_change_val': 5.0},
        {'account': 'TOTAL', 'pnl': 50.0, 'day_change_val': 5.0},
    ])
    raw_m = pd.DataFrame()

    async def failing_override_holdings(df):
        raise RuntimeError("DB connection failed")

    async def noop_override_positions(df):
        pass

    async def mock_run(fn):
        if 'holdings' in fn.__name__:
            return raw_h, summary_h
        elif 'positions' in fn.__name__:
            return raw_p, summary_p
        elif 'margins' in fn.__name__:
            return raw_m
        return pd.DataFrame()

    with patch('backend.api.background._run', side_effect=mock_run), \
         patch('backend.api.routes.holdings._override_stale_close_for_holdings',
               side_effect=failing_override_holdings), \
         patch('backend.api.routes.positions._override_stale_close_from_snapshot',
               side_effect=noop_override_positions):

        result = await _perf_fetch_all_broker_data()

    assert len(result) == 5, "Must return 5-tuple even when override fails"
    df_h, sum_h, df_p, sum_p, df_m = result

    # sum_holdings retains the pre-override value from _fetch_holdings_direct
    total_row = sum_h[sum_h['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must survive override failure"
    assert total_row['day_change_val'].iloc[0] == 42.0, \
        f"sum_h TOTAL day_change_val should be unchanged (42.0) on override failure, " \
        f"got {total_row['day_change_val'].iloc[0]}"


@pytest.mark.asyncio
async def test_perf_fetch_positions_override_failure_fallback():
    """When _override_stale_close_from_snapshot raises, sum_positions must stay
    as returned by _fetch_positions_direct (not crash and not return empty).

    Verifies:
    - Exception inside override inner try/except does NOT propagate outward
    - sum_positions retains the pre-override summary (original threadpool result)
    """
    from backend.api.background import _perf_fetch_all_broker_data

    raw_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
    ])
    summary_h = pd.DataFrame([
        {'account': 'ZG0790', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
        {'account': 'TOTAL', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
    ])
    raw_p = pd.DataFrame([{'account': 'ZG0790', 'pnl': 77.0, 'day_change_val': 33.0}])
    summary_p = pd.DataFrame([
        {'account': 'ZG0790', 'pnl': 77.0, 'day_change_val': 33.0},
        {'account': 'TOTAL', 'pnl': 77.0, 'day_change_val': 33.0},
    ])
    raw_m = pd.DataFrame()

    async def noop_override_holdings(df):
        pass

    async def failing_override_positions(df):
        raise RuntimeError("Query timeout")

    async def mock_run(fn):
        if 'holdings' in fn.__name__:
            return raw_h, summary_h
        elif 'positions' in fn.__name__:
            return raw_p, summary_p
        elif 'margins' in fn.__name__:
            return raw_m
        return pd.DataFrame()

    with patch('backend.api.background._run', side_effect=mock_run), \
         patch('backend.api.routes.holdings._override_stale_close_for_holdings',
               side_effect=noop_override_holdings), \
         patch('backend.api.routes.positions._override_stale_close_from_snapshot',
               side_effect=failing_override_positions):

        result = await _perf_fetch_all_broker_data()

    assert len(result) == 5, "Must return 5-tuple even when override fails"
    df_h, sum_h, df_p, sum_p, df_m = result

    # sum_positions retains the pre-override value from _fetch_positions_direct
    total_row = sum_p[sum_p['account'] == 'TOTAL']
    assert not total_row.empty, "TOTAL row must survive override failure"
    assert total_row['day_change_val'].iloc[0] == 33.0, \
        f"sum_p TOTAL day_change_val should be unchanged (33.0) on override failure, " \
        f"got {total_row['day_change_val'].iloc[0]}"


# Test (g) — rebuild_holdings_summary preserves column types
def test_rebuild_holdings_summary_column_types():
    """_rebuild_holdings_summary must preserve numeric column types.

    Verifies:
    - Numeric columns stay numeric (not converted to object)
    - 'account' column is string
    """
    from backend.api.background import _rebuild_holdings_summary

    raw = pd.DataFrame([
        {'account': 'ACC1', 'inv_val': 1000.0, 'cur_val': 1100.0,
         'pnl': 100.0, 'day_change_val': 50.0},
    ])

    result = _rebuild_holdings_summary(raw)

    # Verify account is object/string
    assert result['account'].dtype == 'object', \
        f"'account' column should be object, got {result['account'].dtype}"

    # Verify numeric columns are numeric
    for col in ['inv_val', 'cur_val', 'pnl', 'day_change_val']:
        if col in result.columns:
            assert pd.api.types.is_numeric_dtype(result[col]), \
                f"Column '{col}' should be numeric, got {result[col].dtype}"


# Test (h) — rebuild_positions_summary preserves column types
def test_rebuild_positions_summary_column_types():
    """_rebuild_positions_summary must preserve numeric column types.

    Verifies:
    - Numeric columns stay numeric (not converted to object)
    - 'account' column is string
    """
    from backend.api.background import _rebuild_positions_summary

    raw = pd.DataFrame([
        {'account': 'ACC1', 'pnl': 500.0, 'day_change_val': 100.0},
    ])

    result = _rebuild_positions_summary(raw)

    # Verify account is object/string
    assert result['account'].dtype == 'object', \
        f"'account' column should be object, got {result['account'].dtype}"

    # Verify numeric columns are numeric
    for col in ['pnl', 'day_change_val']:
        if col in result.columns:
            assert pd.api.types.is_numeric_dtype(result[col]), \
                f"Column '{col}' should be numeric, got {result[col].dtype}"
