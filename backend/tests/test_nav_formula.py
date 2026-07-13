"""Unit tests for NAV v4 formula: firm_nav = cash_sod + option_premium + Σ unrealised + Σ cur_val.

This module tests the core composition of the v4 NAV formula as implemented in
backend/api/algo/nav.py. Each test builds minimal in-memory DataFrames with
known values and asserts the formula components combine correctly.

Tests cover:
  1. Four-component composition (cash + premium + positions + holdings)
  2. Edge cases (empty DataFrames, missing columns, zero values)
  3. option_premium vs used_margin distinction
  4. Holdings zero-LTP backfill paths
  5. Type handling (floats, Polars scalars)
  6. Negative unrealised (short positions losing money)
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pandas as pd
import polars as pl
import pytest

from backend.api.algo.nav import (
    _funds_from_df,
    _positions_from_df,
    _holdings_from_df,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal ticker stub for LTP lookups
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_ticker():
    """Minimal ticker stub that returns None for all LTP lookups."""
    ticker = MagicMock()
    ticker.get_ltp_by_sym = MagicMock(return_value=None)
    return ticker


@pytest.fixture
def ticker_with_prices():
    """Ticker stub that returns known prices for specific symbols."""
    ticker = MagicMock()
    prices = {"INFY": 1450.0, "RELIANCE": 1500.0, "HDFCBANK": 1600.0}
    ticker.get_ltp_by_sym = MagicMock(
        side_effect=lambda sym: prices.get(sym)
    )
    return ticker


# ---------------------------------------------------------------------------
# Tests: Four-component composition
# ---------------------------------------------------------------------------

class TestNavFourComponentComposition:
    """Assert _funds_from_df + _positions_from_df + _holdings_from_df
    sum to the complete NAV = cash_sod + option_premium + unrealised + cur_val."""

    def test_firm_nav_four_components_exact_sum(self, stub_ticker):
        """Build minimal DataFrames with known values and assert the sum."""
        # Cash: avail opening_balance=10000, option_premium=500
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 10000.0,
            "util option_premium": 500.0,
        }])

        # Positions: quantity=10, unrealised=1000 (long position with gain)
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": 10.0,
            "unrealised": 1000.0,
        }])

        # Holdings: opening_quantity=100, cur_val=15000 (stock value)
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 100.0,
            "cur_val": 15000.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        # Expected: 10000 + 500 + 1000 + 15000 = 26500
        nav = cash_total + positions_mtm + holdings_mtm
        assert math.isclose(nav, 26500.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 26500.00 "
            f"(cash={cash_total:.2f} + pos={positions_mtm:.2f} + hold={holdings_mtm:.2f})"
        )

    def test_firm_nav_no_positions(self, stub_ticker):
        """Empty positions DataFrame → NAV = cash_sod + holdings only."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 5000.0,
            "util option_premium": 0.0,
        }])
        positions_df = pd.DataFrame()  # empty
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 50.0,
            "cur_val": 7500.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        nav = cash_total + positions_mtm + holdings_mtm
        assert math.isclose(nav, 12500.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 12500.00 (cash=5000, pos=0, hold=7500)"
        )

    def test_firm_nav_no_holdings(self, stub_ticker):
        """Empty holdings DataFrame → NAV = cash_sod + positions only."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 10000.0,
            "util option_premium": 500.0,
        }])
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": 10.0,
            "unrealised": 2000.0,
        }])
        holdings_df = pd.DataFrame()  # empty

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        nav = cash_total + positions_mtm + holdings_mtm
        assert math.isclose(nav, 12500.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 12500.00 (cash=10500, pos=2000, hold=0)"
        )

    def test_firm_nav_both_empty(self, stub_ticker):
        """Empty positions and holdings → NAV = cash_sod only."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 25000.0,
            "util option_premium": 0.0,
        }])
        positions_df = pd.DataFrame()
        holdings_df = pd.DataFrame()

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        nav = cash_total + positions_mtm + holdings_mtm
        assert math.isclose(nav, 25000.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 25000.00 (cash only, no pos/hold)"
        )


# ---------------------------------------------------------------------------
# Tests: Missing column defaults
# ---------------------------------------------------------------------------

class TestNavMissingColumnDefaults:
    """Assert graceful handling when expected columns are absent."""

    def test_option_premium_defaults_to_zero_when_absent(self, stub_ticker):
        """Funds DataFrame without util option_premium → default to 0."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 5000.0,
            # util option_premium intentionally absent
        }])

        cash_total, _ = _funds_from_df(funds_df)
        # Should be just the opening balance, no error
        assert math.isclose(cash_total, 5000.0, abs_tol=0.01), (
            f"cash_total={cash_total:.2f}, expected 5000.00 "
            "(option_premium column absent, should default to 0)"
        )

    def test_unrealised_defaults_to_zero_when_absent(self, stub_ticker):
        """Positions DataFrame without unrealised column → sum to 0."""
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": 10.0,
            # unrealised intentionally absent
        }])

        positions_mtm, _ = _positions_from_df(positions_df)
        assert math.isclose(positions_mtm, 0.0, abs_tol=0.01), (
            f"positions_mtm={positions_mtm:.2f}, expected 0.00 "
            "(unrealised column absent, should default to 0)"
        )

    def test_cur_val_defaults_to_zero_when_absent(self, stub_ticker):
        """Holdings DataFrame without cur_val column → sum to 0."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 100.0,
            # cur_val intentionally absent
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)
        assert math.isclose(holdings_mtm, 0.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 0.00 "
            "(cur_val column absent, should default to 0)"
        )


# ---------------------------------------------------------------------------
# Tests: option_premium vs used_margin distinction
# ---------------------------------------------------------------------------

class TestOptionPremiumVsUsedMargin:
    """Assert the formula uses option_premium (v4), not used_margin (v3)."""

    def test_funds_extracts_option_premium_not_used_margin(self):
        """_funds_from_df reads util option_premium column specifically."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 10000.0,
            "util option_premium": 750.0,
            "used_margin": 2000.0,  # present but must be ignored
        }])

        cash_total, _ = _funds_from_df(funds_df)

        # v4 formula: cash_sod + option_premium = 10000 + 750 = 10750
        # NOT: cash_sod + used_margin = 10000 + 2000 = 12000
        assert math.isclose(cash_total, 10750.0, abs_tol=0.01), (
            f"cash_total={cash_total:.2f}, expected 10750.00 (option_premium only, "
            "used_margin must be ignored per v4)"
        )

    def test_funds_extracts_option_premium_fallback_column(self):
        """When util option_premium absent, fall back to option_premium."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 10000.0,
            "option_premium": 500.0,  # fallback column
            # util option_premium intentionally absent
        }])

        cash_total, _ = _funds_from_df(funds_df)
        assert math.isclose(cash_total, 10500.0, abs_tol=0.01), (
            f"cash_total={cash_total:.2f}, expected 10500.00 (fallback to option_premium)"
        )


# ---------------------------------------------------------------------------
# Tests: Holdings zero-LTP backfill paths
# ---------------------------------------------------------------------------

class TestHoldingsZeroLtpBackfill:
    """Assert holdings rows with zero LTP are backfilled via ticker or last_price."""

    def test_holdings_zero_ltp_uses_ticker_fallback(self, ticker_with_prices):
        """Holdings row with cur_val=0 falls back to qty × ticker.get_ltp_by_sym()."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 10.0,
            "cur_val": 0.0,  # zero, will need backfill
            "last_price": 0.0,  # also zero
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, ticker_with_prices)

        # Ticker returns 1450.0 for INFY → 10 * 1450 = 14500
        assert math.isclose(holdings_mtm, 14500.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 14500.00 "
            "(qty=10 * ticker_ltp=1450)"
        )

    def test_holdings_zero_ltp_uses_last_price_fallback(self, stub_ticker):
        """When ticker has no LTP, fall back to qty × last_price."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 10.0,
            "cur_val": 0.0,
            "last_price": 1500.0,  # fallback source
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        # Ticker returns None, fall back to last_price → 10 * 1500 = 15000
        assert math.isclose(holdings_mtm, 15000.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 15000.00 "
            "(qty=10 * last_price=1500)"
        )

    def test_holdings_zero_ltp_zero_qty_contributes_nothing(self, stub_ticker):
        """Holdings row with qty=0 contributes 0 regardless of LTP."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 0.0,  # zero qty
            "cur_val": 0.0,
            "last_price": 0.0,
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)
        assert math.isclose(holdings_mtm, 0.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 0.00 (qty=0 → no contribution)"
        )

    def test_holdings_cur_val_preferred_over_ltp_path(self, ticker_with_prices):
        """When cur_val is populated, LTP fallback path is skipped (performance)."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 10.0,
            "cur_val": 12000.0,  # pre-computed, should be used
            "last_price": 1500.0,  # ticker has 1450.0, but cur_val preferred
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, ticker_with_prices)

        # Should sum cur_val (12000), NOT qty * ticker_ltp (10 * 1450 = 14500)
        assert math.isclose(holdings_mtm, 12000.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 12000.00 (cur_val preferred)"
        )


# ---------------------------------------------------------------------------
# Tests: Type handling
# ---------------------------------------------------------------------------

class TestNavTypeHandling:
    """Assert return types and Polars scalar conversions."""

    def test_funds_returns_float(self):
        """_funds_from_df returns Python float, not Polars scalar."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 5000.0,
            "util option_premium": 100.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        assert isinstance(cash_total, float), (
            f"cash_total type={type(cash_total)}, expected float"
        )
        assert not isinstance(cash_total, (pl.Float32, pl.Float64)), (
            "cash_total must be Python float, not Polars scalar"
        )

    def test_positions_returns_float(self):
        """_positions_from_df returns Python float, not Polars scalar."""
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": 10.0,
            "unrealised": 1000.0,
        }])

        positions_mtm, _ = _positions_from_df(positions_df)
        assert isinstance(positions_mtm, float), (
            f"positions_mtm type={type(positions_mtm)}, expected float"
        )

    def test_holdings_returns_float(self, stub_ticker):
        """_holdings_from_df returns Python float, not Polars scalar."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 100.0,
            "cur_val": 15000.0,
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)
        assert isinstance(holdings_mtm, float), (
            f"holdings_mtm type={type(holdings_mtm)}, expected float"
        )

    def test_nav_not_none(self, stub_ticker):
        """NAV components never return None; default to 0.0."""
        # Empty DataFrames should return 0.0, not None
        cash_total, _ = _funds_from_df(pd.DataFrame())
        positions_mtm, _ = _positions_from_df(pd.DataFrame())
        holdings_mtm, _ = _holdings_from_df(pd.DataFrame(), stub_ticker)

        assert cash_total == 0.0 and cash_total is not None
        assert positions_mtm == 0.0 and positions_mtm is not None
        assert holdings_mtm == 0.0 and holdings_mtm is not None


# ---------------------------------------------------------------------------
# Tests: Negative unrealised (short positions losing money)
# ---------------------------------------------------------------------------

class TestNavNegativeUnrealised:
    """Assert NAV correctly reduces when positions have negative unrealised (losses)."""

    def test_short_position_losing_reduces_nav(self, stub_ticker):
        """Short position with negative unrealised reduces NAV correctly."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 50000.0,
            "util option_premium": 0.0,
        }])
        # Short position down 500 (negative unrealised)
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": -10.0,  # short
            "unrealised": -500.0,  # losing
        }])
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 100.0,
            "cur_val": 15000.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        nav = cash_total + positions_mtm + holdings_mtm
        # Expected: 50000 + (-500) + 15000 = 64500
        assert math.isclose(nav, 64500.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 64500.00 "
            "(short loss correctly reduces nav: 50000 - 500 + 15000)"
        )

    def test_multiple_positions_mixed_gains_losses(self, stub_ticker):
        """Mix of long gains and short losses net correctly."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": 100000.0,
            "util option_premium": 1000.0,
        }])
        positions_df = pd.DataFrame([
            {
                "account": "ZG0790",
                "symbol": "NIFTY50",
                "quantity": 10.0,
                "unrealised": 2000.0,  # long gain
            },
            {
                "account": "ZG0790",
                "symbol": "BANKNIFTY",
                "quantity": -5.0,
                "unrealised": -1000.0,  # short loss
            },
        ])
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 100.0,
            "cur_val": 15000.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        positions_mtm, _ = _positions_from_df(positions_df)
        holdings_mtm, _ = _holdings_from_df(holdings_df, stub_ticker)

        nav = cash_total + positions_mtm + holdings_mtm
        # Expected: (100000 + 1000) + (2000 - 1000) + 15000 = 117000
        assert math.isclose(nav, 117000.0, abs_tol=0.01), (
            f"NAV={nav:.2f}, expected 117000.00 "
            "(mixed gains/losses: 101000 + 1000 + 15000)"
        )


# ---------------------------------------------------------------------------
# Tests: Multi-account scenarios
# ---------------------------------------------------------------------------

class TestNavMultiAccount:
    """Assert NAV correctly aggregates across multiple broker accounts."""

    def test_nav_sums_multiple_accounts_funds(self):
        """Multiple fund rows sum correctly."""
        funds_df = pd.DataFrame([
            {
                "account": "ZG0790",
                "avail opening_balance": 10000.0,
                "util option_premium": 500.0,
            },
            {
                "account": "DH6847",
                "avail opening_balance": 20000.0,
                "util option_premium": 0.0,
            },
        ])

        cash_total, accts = _funds_from_df(funds_df)
        assert math.isclose(cash_total, 30500.0, abs_tol=0.01), (
            f"cash_total={cash_total:.2f}, expected 30500.00 "
            "(sum of all account funds)"
        )
        assert set(accts) == {"ZG0790", "DH6847"}, (
            f"accounts={accts}, expected ZG0790 and DH6847"
        )

    def test_nav_sums_multiple_accounts_positions(self):
        """Multiple account positions sum correctly."""
        positions_df = pd.DataFrame([
            {
                "account": "ZG0790",
                "symbol": "NIFTY50",
                "quantity": 10.0,
                "unrealised": 1000.0,
            },
            {
                "account": "DH6847",
                "symbol": "BANKNIFTY",
                "quantity": 5.0,
                "unrealised": 500.0,
            },
        ])

        positions_mtm, accts = _positions_from_df(positions_df)
        assert math.isclose(positions_mtm, 1500.0, abs_tol=0.01), (
            f"positions_mtm={positions_mtm:.2f}, expected 1500.00 "
            "(sum of all account positions)"
        )
        assert set(accts) == {"ZG0790", "DH6847"}

    def test_nav_sums_multiple_accounts_holdings(self, stub_ticker):
        """Multiple account holdings sum correctly."""
        holdings_df = pd.DataFrame([
            {
                "account": "ZG0790",
                "tradingsymbol": "INFY",
                "opening_quantity": 100.0,
                "cur_val": 15000.0,
            },
            {
                "account": "DH6847",
                "tradingsymbol": "RELIANCE",
                "opening_quantity": 50.0,
                "cur_val": 7500.0,
            },
        ])

        holdings_mtm, accts = _holdings_from_df(holdings_df, stub_ticker)
        assert math.isclose(holdings_mtm, 22500.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 22500.00 "
            "(sum of all account holdings)"
        )
        assert set(accts) == {"ZG0790", "DH6847"}


# ---------------------------------------------------------------------------
# Tests: Null/NaN handling
# ---------------------------------------------------------------------------

class TestNavNullNanHandling:
    """Assert safe handling of NaN and None values in DataFrames."""

    def test_funds_null_cash_treated_as_zero(self):
        """NaN in avail opening_balance is converted to 0.0."""
        funds_df = pd.DataFrame([{
            "account": "ZG0790",
            "avail opening_balance": float("nan"),
            "util option_premium": 500.0,
        }])

        cash_total, _ = _funds_from_df(funds_df)
        assert math.isclose(cash_total, 500.0, abs_tol=0.01), (
            f"cash_total={cash_total:.2f}, expected 500.00 (null cash → 0)"
        )

    def test_positions_null_unrealised_treated_as_zero(self):
        """NaN in unrealised is converted to 0.0."""
        positions_df = pd.DataFrame([{
            "account": "ZG0790",
            "symbol": "NIFTY50",
            "quantity": 10.0,
            "unrealised": float("nan"),
        }])

        positions_mtm, _ = _positions_from_df(positions_df)
        assert math.isclose(positions_mtm, 0.0, abs_tol=0.01), (
            f"positions_mtm={positions_mtm:.2f}, expected 0.00 (null unrealised → 0)"
        )

    def test_holdings_null_cur_val_uses_ltp_fallback(self, ticker_with_prices):
        """NaN in cur_val triggers LTP fallback path."""
        holdings_df = pd.DataFrame([{
            "account": "ZG0790",
            "tradingsymbol": "INFY",
            "opening_quantity": 10.0,
            "cur_val": float("nan"),
            "last_price": 0.0,
        }])

        holdings_mtm, _ = _holdings_from_df(holdings_df, ticker_with_prices)
        # Ticker returns 1450.0 for INFY → 10 * 1450 = 14500
        assert math.isclose(holdings_mtm, 14500.0, abs_tol=0.01), (
            f"holdings_mtm={holdings_mtm:.2f}, expected 14500.00 (null cur_val → ticker path)"
        )


# ---------------------------------------------------------------------------
# Tests: Zero-quantity position guard
# ---------------------------------------------------------------------------

class TestNavZeroQuantityGuard:
    """Assert positions with qty=0 don't contribute to NAV (guard against notional bugs)."""

    def test_positions_with_zero_qty_excluded(self):
        """Position row with quantity=0 doesn't contribute unrealised."""
        positions_df = pd.DataFrame([
            {
                "account": "ZG0790",
                "symbol": "NIFTY50",
                "quantity": 10.0,
                "unrealised": 1000.0,  # real position
            },
            {
                "account": "ZG0790",
                "symbol": "BANKNIFTY",
                "quantity": 0.0,  # closed position
                "unrealised": 5000.0,  # notional value (should be ignored)
            },
        ])

        positions_mtm, _ = _positions_from_df(positions_df)
        # Only the quantity=10 position contributes → 1000
        # The quantity=0 position is skipped despite having unrealised=5000
        assert math.isclose(positions_mtm, 1000.0, abs_tol=0.01), (
            f"positions_mtm={positions_mtm:.2f}, expected 1000.00 "
            "(qty=0 position excluded, protects against F&O notional bugs)"
        )
