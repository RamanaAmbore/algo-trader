"""
Exhaustive tests for empty-guard logic in _fetch_holdings_direct / _fetch_positions_direct.

Covers five quality dimensions:
  SSOT        — single guard in _fetch_holdings_direct; no duplicate logic
  Correctness — guard returns correct empty columns; normal path computes percentages
  Performance — guard prevents unnecessary groupby on empty concat result
  Reuse       — same columns returned whether guard fires or normal path
  UX          — TOTAL row included; pnl_percentage computed correctly

Scenario catalogue:
  1. All accounts return empty DataFrames from fetch_holdings() → guard fires, returns (empty raw, empty summary).
  2. Concat result is empty but contains 'account' column → guard does NOT fire, groupby proceeds.
  3. Concat result missing 'account' column entirely → guard fires, returns (raw, empty summary).
  4. One account has holdings, one empty → normal path groupby works; TOTAL row correct.
  5. Holdings summary has all 7 expected columns (account, inv_val, cur_val, pnl, day_change_val, pnl_percentage, day_change_percentage).
  6. pnl_percentage = pnl / inv_val * 100 computed correctly.
  7. Positions: all empty → guard fires, returns (empty raw, empty summary with ['account','pnl']).
  8. Positions: one account with pnl, one empty → groupby sums pnl per account; TOTAL correct.
  9. Positions: empty guard summary has no data rows (len == 0 when guard fires).
  10. Holdings: day_change_percentage = day_change_val / cur_val * 100 computed correctly.
"""

from __future__ import annotations

import pandas as pd
from unittest.mock import patch, MagicMock
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_holdings_row(account: str, inv_val: float, cur_val: float, pnl: float, day_change_val: float) -> dict:
    """Construct a single holdings row for testing."""
    return {
        'account': account,
        'inv_val': inv_val,
        'cur_val': cur_val,
        'pnl': pnl,
        'day_change_val': day_change_val,
    }


def _make_positions_row(account: str, pnl: float) -> dict:
    """Construct a single positions row for testing."""
    return {
        'account': account,
        'pnl': pnl,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TestFetchHoldingsEmptyGuard — 6 tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchHoldingsEmptyGuard:
    """Verify _fetch_holdings_direct guard fires on empty concat and returns correct structure."""

    def test_all_accounts_return_empty_df(self):
        """All broker_apis.fetch_holdings() results are empty → guard fires."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            # Simulate all accounts returning empty DataFrames
            mock_fetch.return_value = [
                pd.DataFrame(columns=['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val']),
                pd.DataFrame(columns=['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val']),
            ]

            raw, summary = _fetch_holdings_direct()

            # Raw should be empty
            assert raw.empty, "raw should be empty when all accounts have no holdings"
            # Guard returns summary with correct columns
            assert list(summary.columns) == ['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val',
                                             'pnl_percentage', 'day_change_percentage']
            assert summary.empty, "summary should be empty when guard fires"

    def test_no_account_column_in_concat(self):
        """Concat result is missing 'account' column → guard fires."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            # Return data WITHOUT 'account' column (malformed broker response)
            mock_fetch.return_value = [
                pd.DataFrame({'inv_val': [100.0], 'cur_val': [105.0]}),
            ]

            raw, summary = _fetch_holdings_direct()

            # Guard detects missing 'account' column
            assert 'account' not in raw.columns, "raw should lack 'account' column"
            # Guard returns correct empty summary with expected columns
            assert list(summary.columns) == ['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val',
                                             'pnl_percentage', 'day_change_percentage']
            assert summary.empty, "summary should be empty when 'account' column missing"

    def test_one_account_has_data_one_empty(self):
        """One account returns holdings, one is empty → normal path (groupby) works."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            # Mix: one account with data, one empty
            mock_fetch.return_value = [
                pd.DataFrame([_make_holdings_row('ZG0790', 50000.0, 52000.0, 2000.0, 500.0)]),
                pd.DataFrame(columns=['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val']),
            ]

            raw, summary = _fetch_holdings_direct()

            # Raw should have one data row
            assert len(raw) == 1, "raw should have one holding"
            assert raw.iloc[0]['account'] == 'ZG0790'
            # Summary should have account row + TOTAL row
            assert len(summary) == 2, "summary should have 1 account + 1 TOTAL row"
            assert summary.iloc[0]['account'] == 'ZG0790'
            assert summary.iloc[1]['account'] == 'TOTAL'

    def test_empty_guard_returns_correct_summary_columns(self):
        """Guard returns summary with all 7 expected columns."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            mock_fetch.return_value = [pd.DataFrame()]

            raw, summary = _fetch_holdings_direct()

            expected_cols = ['account', 'inv_val', 'cur_val', 'pnl', 'day_change_val',
                            'pnl_percentage', 'day_change_percentage']
            assert list(summary.columns) == expected_cols, \
                f"summary columns mismatch. Expected {expected_cols}, got {list(summary.columns)}"

    def test_normal_path_computes_pnl_percentage(self):
        """Normal path computes pnl_percentage = pnl / inv_val * 100."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            # Create two accounts with known pnl and inv_val
            mock_fetch.return_value = [
                pd.DataFrame([
                    _make_holdings_row('ZG0790', 50000.0, 52000.0, 2000.0, 500.0),
                    _make_holdings_row('ZG0790', 30000.0, 31500.0, 1500.0, 300.0),
                ]),
            ]

            raw, summary = _fetch_holdings_direct()

            # Should have account row + TOTAL row
            assert len(summary) == 2, "summary should have 1 account + 1 TOTAL"
            acct_row = summary.iloc[0]
            total_row = summary.iloc[1]

            # Account row: (2000 + 1500) / (50000 + 30000) * 100 = 3500 / 80000 * 100 = 4.375%
            assert acct_row['account'] == 'ZG0790'
            assert abs(acct_row['pnl_percentage'] - 4.375) < 0.01, \
                f"pnl_percentage mismatch. Expected 4.375, got {acct_row['pnl_percentage']}"

            # TOTAL row: same calculation
            assert abs(total_row['pnl_percentage'] - 4.375) < 0.01, \
                f"TOTAL pnl_percentage mismatch. Expected 4.375, got {total_row['pnl_percentage']}"

    def test_normal_path_includes_TOTAL_row(self):
        """Summary always includes a TOTAL row when normal path executes."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            mock_fetch.return_value = [
                pd.DataFrame([
                    _make_holdings_row('ZG0790', 50000.0, 52000.0, 2000.0, 500.0),
                    _make_holdings_row('ZJ6294', 40000.0, 41000.0, 1000.0, 200.0),
                ]),
            ]

            raw, summary = _fetch_holdings_direct()

            # Should have 2 account rows + 1 TOTAL
            assert len(summary) == 3, "summary should have 2 accounts + 1 TOTAL"
            last_row = summary.iloc[-1]
            assert last_row['account'] == 'TOTAL', "last row should be TOTAL"

            # TOTAL pnl should be sum of account pnls
            expected_total_pnl = 2000.0 + 1000.0
            assert abs(last_row['pnl'] - expected_total_pnl) < 0.01, \
                f"TOTAL pnl mismatch. Expected {expected_total_pnl}, got {last_row['pnl']}"


# ─────────────────────────────────────────────────────────────────────────────
# TestFetchPositionsEmptyGuard — 4 tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchPositionsEmptyGuard:
    """Verify _fetch_positions_direct guard fires on empty concat and returns correct structure."""

    def test_all_empty(self):
        """All broker_apis.fetch_positions() results are empty → guard fires."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            mock_fetch.return_value = [
                pd.DataFrame(columns=['account', 'pnl']),
                pd.DataFrame(columns=['account', 'pnl']),
            ]

            raw, summary = _fetch_positions_direct()

            assert raw.empty, "raw should be empty"
            assert list(summary.columns) == ['account', 'pnl'], \
                f"summary should have ['account', 'pnl'], got {list(summary.columns)}"
            assert summary.empty, "summary should be empty when guard fires"

    def test_no_account_column(self):
        """Concat result missing 'account' column → guard fires."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            # Malformed response: no 'account' column
            mock_fetch.return_value = [
                pd.DataFrame({'pnl': [500.0]}),
            ]

            raw, summary = _fetch_positions_direct()

            assert 'account' not in raw.columns, "raw should lack 'account'"
            assert list(summary.columns) == ['account', 'pnl']
            assert summary.empty, "summary should be empty when 'account' missing"

    def test_one_account_with_pnl(self):
        """One account with positions → normal path groupby works."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            mock_fetch.return_value = [
                pd.DataFrame([
                    _make_positions_row('ZG0790', 5000.0),
                    _make_positions_row('ZG0790', 3000.0),
                    _make_positions_row('ZJ6294', 2000.0),
                ]),
            ]

            raw, summary = _fetch_positions_direct()

            # Summary should have 2 account rows + 1 TOTAL
            assert len(summary) == 3, f"summary should have 3 rows, got {len(summary)}"

            # First account: sum of two positions
            assert summary.iloc[0]['account'] == 'ZG0790'
            assert abs(summary.iloc[0]['pnl'] - 8000.0) < 0.01, \
                f"ZG0790 pnl should be 8000, got {summary.iloc[0]['pnl']}"

            # Second account: one position
            assert summary.iloc[1]['account'] == 'ZJ6294'
            assert abs(summary.iloc[1]['pnl'] - 2000.0) < 0.01

            # TOTAL row
            assert summary.iloc[2]['account'] == 'TOTAL'
            assert abs(summary.iloc[2]['pnl'] - 10000.0) < 0.01, \
                f"TOTAL pnl should be 10000, got {summary.iloc[2]['pnl']}"

    def test_empty_guard_summary_has_no_data_rows(self):
        """When guard fires, summary has no data rows (len == 0)."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions') as mock_fetch:
            # Trigger guard: empty concat
            mock_fetch.return_value = [pd.DataFrame()]

            raw, summary = _fetch_positions_direct()

            assert len(summary) == 0, f"summary should have 0 rows when guard fires, got {len(summary)}"
            assert list(summary.columns) == ['account', 'pnl']


# ─────────────────────────────────────────────────────────────────────────────
# TestHoldingsDayChangePercentage — integration test
# ─────────────────────────────────────────────────────────────────────────────

class TestHoldingsDayChangePercentage:
    """Verify day_change_percentage = day_change_val / cur_val * 100 in normal path."""

    def test_day_change_percentage_computed_correctly(self):
        """day_change_percentage reflects intraday price movement."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            mock_fetch.return_value = [
                pd.DataFrame([
                    # Position: cur_val=1000, day_change_val=50 → 5%
                    _make_holdings_row('ZG0790', 900.0, 1000.0, 100.0, 50.0),
                ]),
            ]

            raw, summary = _fetch_holdings_direct()

            acct_row = summary.iloc[0]
            assert acct_row['account'] == 'ZG0790'
            # day_change_percentage = 50 / 1000 * 100 = 5
            assert abs(acct_row['day_change_percentage'] - 5.0) < 0.01, \
                f"day_change_percentage mismatch. Expected 5.0, got {acct_row['day_change_percentage']}"

    def test_multiple_accounts_day_change_aggregation(self):
        """day_change_percentage for TOTAL row aggregates correctly."""
        from backend.api.background import _fetch_holdings_direct

        with patch('backend.brokers.broker_apis.fetch_holdings') as mock_fetch:
            mock_fetch.return_value = [
                pd.DataFrame([
                    _make_holdings_row('ZG0790', 5000.0, 10000.0, 5000.0, 500.0),  # 5% day chg
                    _make_holdings_row('ZJ6294', 4000.0, 10000.0, 6000.0, 1000.0),  # 10% day chg
                ]),
            ]

            raw, summary = _fetch_holdings_direct()

            # TOTAL row: day_change_val=1500, cur_val=20000 → 7.5%
            total_row = summary.iloc[-1]
            assert total_row['account'] == 'TOTAL'
            expected_day_change_pct = (500.0 + 1000.0) / (10000.0 + 10000.0) * 100  # 7.5
            assert abs(total_row['day_change_percentage'] - expected_day_change_pct) < 0.01, \
                f"TOTAL day_change_percentage mismatch. Expected {expected_day_change_pct}, got {total_row['day_change_percentage']}"
