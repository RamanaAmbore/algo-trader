"""Tests for nav.py Fix A — stale-LTP split in _holdings_from_df.

Dhan/Groww holdings rows arrive with last_price=0 (stale broker cache) but
cur_val = avg×qty > 0 (cost basis, not market value). Pre-fix those rows
landed in cv_sum and inflated NAV with cost basis. Post-fix they are routed
to _ltp_fallback_sum which reads from KiteTicker for a market-value rescue.

Three canonical cases:
  1. Stale-LTP row with ticker rescue  → qty × ticker_ltp (market value)
  2. Valid-LTP row                     → cur_val directly (fast path)
  3. Mixed frame (one Kite + one Dhan) → Kite uses cur_val, Dhan uses ticker
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backend.api.algo.nav import _holdings_from_df


# ---------------------------------------------------------------------------
# Ticker stubs
# ---------------------------------------------------------------------------

class _TickerNoLtp:
    """Ticker that always returns 0 (cold / unavailable)."""
    def get_ltp_by_sym(self, sym: str) -> float:
        return 0.0


class _TickerFixed:
    """Ticker that returns a fixed LTP for one specific symbol, 0 for others."""
    def __init__(self, sym: str, ltp: float) -> None:
        self._sym = sym
        self._ltp = ltp

    def get_ltp_by_sym(self, sym: str) -> float:
        return self._ltp if sym == self._sym else 0.0


class _TickerMap:
    """Ticker that returns per-symbol LTPs from a dict."""
    def __init__(self, mapping: dict) -> None:
        self._m = mapping

    def get_ltp_by_sym(self, sym: str) -> float:
        return float(self._m.get(sym, 0))


# ---------------------------------------------------------------------------
# Test Case 1 — stale-LTP row goes to ticker rescue
# ---------------------------------------------------------------------------

class TestStaleLtpRowTickerRescue:
    """Row with last_price=0, cur_val=cost_basis — must use ticker LTP."""

    def _build_stale_row(self, sym="RELIANCE", avg=2800.0, qty=10, cur_val=28000.0):
        return pd.DataFrame([{
            "tradingsymbol": sym,
            "exchange": "BSE",
            "account": "DH6847",
            "opening_quantity": float(qty),
            "last_price": 0.0,          # stale — Dhan cold cache
            "cur_val": float(cur_val),  # cost basis, NOT market value
            "average_price": float(avg),
        }])

    def test_ticker_rescue_returns_market_value(self):
        """Ticker rescues stale row: result = qty × ticker_ltp, not cur_val."""
        df = self._build_stale_row(sym="RELIANCE", qty=10, cur_val=28000.0)
        ticker = _TickerFixed("RELIANCE", ltp=3000.0)

        mtm, accts = _holdings_from_df(df, ticker)

        # Expect 10 × 3000 = 30000, NOT 28000 (cost basis)
        assert math.isclose(mtm, 30000.0, abs_tol=1.0), (
            f"Expected 30000.0 (ticker rescue), got {mtm}"
        )

    def test_stale_ltp_no_ticker_returns_zero(self):
        """When ticker also has nothing, stale row contributes 0 — safer than cost basis."""
        df = self._build_stale_row(sym="RELIANCE", qty=10, cur_val=28000.0)
        ticker = _TickerNoLtp()

        mtm, _ = _holdings_from_df(df, ticker)

        assert math.isclose(mtm, 0.0, abs_tol=0.01), (
            f"Expected 0.0 (no rescue available), got {mtm}"
        )

    def test_stale_ltp_not_cost_basis(self):
        """Verify the result is NOT the cost basis (the pre-fix wrong value)."""
        cost_basis = 28000.0
        df = self._build_stale_row(qty=10, cur_val=cost_basis)
        ticker = _TickerNoLtp()

        mtm, _ = _holdings_from_df(df, ticker)

        # Post-fix: contributes 0, not cost_basis
        assert abs(mtm - cost_basis) > 1.0, (
            f"mtm={mtm} too close to cost_basis={cost_basis}: cost basis must not be trusted"
        )


# ---------------------------------------------------------------------------
# Test Case 2 — valid-LTP row uses cur_val directly
# ---------------------------------------------------------------------------

class TestValidLtpRowUsesCurVal:
    """Row with last_price > 0 still goes to cv_sum (fast path unchanged)."""

    def test_valid_ltp_uses_cur_val(self):
        df = pd.DataFrame([{
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "account": "ZG0790",
            "opening_quantity": 10.0,
            "last_price": 150.0,    # valid LTP
            "cur_val": 1500.0,      # correct market value (150 × 10)
            "average_price": 140.0,
        }])
        ticker = _TickerNoLtp()  # ticker not needed for this path

        mtm, accts = _holdings_from_df(df, ticker)

        assert math.isclose(mtm, 1500.0, abs_tol=0.01), (
            f"Expected 1500.0 (cur_val fast path), got {mtm}"
        )
        assert "ZG0790" in accts

    def test_valid_ltp_ticker_not_called_for_good_rows(self):
        """Good rows use cur_val even when ticker would give a different value."""
        df = pd.DataFrame([{
            "tradingsymbol": "TCS",
            "exchange": "NSE",
            "account": "ZG0790",
            "opening_quantity": 5.0,
            "last_price": 200.0,
            "cur_val": 1000.0,
            "average_price": 190.0,
        }])
        # Ticker returns a different value — should NOT be used
        ticker = _TickerFixed("TCS", ltp=999.0)

        mtm, _ = _holdings_from_df(df, ticker)

        # cur_val=1000 must be used, not qty×999=4995
        assert math.isclose(mtm, 1000.0, abs_tol=0.01), (
            f"Expected 1000.0 (cur_val), got {mtm} (must not use ticker for valid rows)"
        )


# ---------------------------------------------------------------------------
# Test Case 3 — mixed frame (one Kite + one Dhan)
# ---------------------------------------------------------------------------

class TestMixedFrame:
    """Mixed Kite (valid LTP) + Dhan (stale LTP) frame."""

    def test_mixed_frame_kite_cur_val_plus_dhan_ticker(self):
        """Total = Kite cur_val + Dhan ticker value (NOT Dhan cost basis)."""
        kite_cur_val = 14500.0   # 1450 × 10
        dhan_ticker_ltp = 3100.0
        dhan_qty = 5
        dhan_cost_basis = 14000.0  # 2800 × 5 — should NOT appear in result

        df = pd.DataFrame([
            {
                "tradingsymbol": "INFY",
                "exchange": "NSE",
                "account": "ZG0790",
                "opening_quantity": 10.0,
                "last_price": 1450.0,          # valid
                "cur_val": kite_cur_val,
                "average_price": 1400.0,
            },
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "BSE",
                "account": "DH6847",
                "opening_quantity": float(dhan_qty),
                "last_price": 0.0,             # stale
                "cur_val": dhan_cost_basis,    # cost basis — must not be used
                "average_price": 2800.0,
            },
        ])

        ticker = _TickerMap({"RELIANCE": dhan_ticker_ltp})

        mtm, accts = _holdings_from_df(df, ticker)

        expected = kite_cur_val + dhan_qty * dhan_ticker_ltp  # 14500 + 15500 = 30000
        assert math.isclose(mtm, expected, abs_tol=1.0), (
            f"Expected {expected} (Kite cv + Dhan ticker), got {mtm}"
        )
        # Verify cost basis not used
        wrong = kite_cur_val + dhan_cost_basis  # 14500 + 14000 = 28500
        assert not math.isclose(mtm, wrong, abs_tol=1.0), (
            f"Result {mtm} must not equal cost-basis sum {wrong}"
        )
        assert set(accts) == {"ZG0790", "DH6847"}

    def test_mixed_frame_dhan_stale_no_ticker(self):
        """When Dhan stale row has no ticker rescue, Kite still contributes correctly."""
        kite_cur_val = 14500.0

        df = pd.DataFrame([
            {
                "tradingsymbol": "INFY",
                "exchange": "NSE",
                "account": "ZG0790",
                "opening_quantity": 10.0,
                "last_price": 1450.0,
                "cur_val": kite_cur_val,
                "average_price": 1400.0,
            },
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "BSE",
                "account": "DH6847",
                "opening_quantity": 5.0,
                "last_price": 0.0,
                "cur_val": 14000.0,   # cost basis
                "average_price": 2800.0,
            },
        ])

        ticker = _TickerNoLtp()

        mtm, _ = _holdings_from_df(df, ticker)

        # Kite: 14500. Dhan: 0 (no rescue). Total: 14500.
        assert math.isclose(mtm, kite_cur_val, abs_tol=1.0), (
            f"Expected {kite_cur_val} (Kite only, Dhan=0), got {mtm}"
        )

    def test_no_last_price_column_at_all(self):
        """Frame without a last_price column at all — all rows treated as stale-LTP.
        Ticker rescue fires for non-zero cv rows."""
        df = pd.DataFrame([
            {
                "tradingsymbol": "GOLDBEES",
                "exchange": "NSE",
                "account": "GR5321",
                "opening_quantity": 100.0,
                "cur_val": 5000.0,    # cost basis, no LTP column
                "average_price": 50.0,
            },
        ])
        # No "last_price" column in the frame
        assert "last_price" not in df.columns

        ticker = _TickerFixed("GOLDBEES", ltp=55.0)
        mtm, _ = _holdings_from_df(df, ticker)

        # qty=100 × ticker=55 = 5500, not cost_basis=5000
        assert math.isclose(mtm, 5500.0, abs_tol=1.0), (
            f"Expected 5500.0 (ticker rescue for frame without last_price col), got {mtm}"
        )
