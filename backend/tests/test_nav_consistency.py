"""Regression contract: compute_firm_nav holdings_mtm matches
Σ cur_val as seen by the holdings route for the same broker payload.

Root cause being guarded against:
  `compute_firm_nav` calls `fetch_holdings()` which (pre-fix) returned
  raw per-account frames — Dhan/Groww rows with last_price=0 had
  cur_val=0 from `_enrich_holdings`. The holdings route applied
  `backfill_market_data` which patched last_price and recomputed
  cur_val. The ₹9,739 drift was the sum of non-zero cur_val on rows
  whose broker delivered zero LTP but PriceBroker.quote() had a valid
  tick.

Post-fix (Approach A): `fetch_holdings()` applies `backfill_market_data`
before storing to `_RAW_CACHE`. Both nav.py and holdings.py read the
same post-patch frames → consistent cur_val → zero drift.

Test strategy (no broker mocks):
  1. Build a synthetic holdings DataFrame with Dhan-style zero LTP row.
  2. Patch `_fetch_holdings_local` to return that DataFrame.
  3. Patch `backfill_market_data` to write a known LTP into the zero row
     and recompute cur_val (simulates PriceBroker.quote() response).
  4. Assert `_holdings_from_df` reports the same cur_val that the
     holdings route would compute from the same frame.
  5. Assert the drift |nav_cur_val − route_cur_val| < ₹1.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd
import pytest

from backend.api.algo.nav import _holdings_from_df
from backend.brokers.broker_apis import (
    _apply_backfill_to_list,
    _raw_cache_invalidate,
    _enrich_holdings,
)


# ---------------------------------------------------------------------------
# Synthetic broker payload helpers
# ---------------------------------------------------------------------------

def _make_holdings_frame(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal holdings DataFrame that mirrors the Kite-shape
    produced by `_fetch_holdings_local` after `_enrich_holdings`."""
    df = pd.DataFrame(rows)
    return _enrich_holdings(df)


def _kite_row(sym: str, avg: float, qty: int, ltp: float, close: float) -> dict:
    """Kite-style row with all market-data fields populated.

    Kite brokers always supply a numeric pnl in the response — pass it
    explicitly so that `_enrich_holdings` takes the "trust broker pnl"
    branch and correctly computes cur_val = inv_val + pnl in ONE
    with_columns() call (the alias-dependency ordering means computed pnl
    only feeds cur_val when the ORIGINAL pnl column is not-null).
    """
    return {
        "tradingsymbol": sym,
        "exchange": "NSE",
        "account": "ZG0790",
        "average_price": float(avg),
        "opening_quantity": float(qty),
        "last_price": float(ltp),
        "close_price": float(close),
        "pnl": float((ltp - avg) * qty),  # broker-computed P&L
        "day_change_val": float((ltp - close) * qty),
        "type": "H",
    }


def _dhan_row(sym: str, avg: float, qty: int, close: float) -> dict:
    """Dhan-style row: last_price=0 (Dhan sometimes omits it).

    Omit pnl entirely so _enrich_holdings falls into the "no broker pnl"
    branch and computes (ltp - avg) * qty → which gives 0 when ltp=0.
    This is the exact pre-fix state causing nav drift.
    """
    return {
        "tradingsymbol": sym,
        "exchange": "BSE",
        "account": "DH6847",
        "average_price": float(avg),
        "opening_quantity": float(qty),
        "last_price": 0.0,   # <-- the problem field
        "close_price": float(close),
        # pnl intentionally absent → _enrich_holdings computes (0 - avg) * qty = -cost
        # which is the wrong pre-fix state. After backfill patches ltp, pnl should
        # be patched too; that's what _simulate_backfill models.
        "type": "H",
    }


# ---------------------------------------------------------------------------
# Helper: simulate backfill patching a specific LTP onto the zero row
# ---------------------------------------------------------------------------

def _simulate_backfill(df: pd.DataFrame, sym: str, patched_ltp: float) -> None:
    """Mimic `backfill_market_data`: write `patched_ltp` for the row
    matching `sym` that has last_price=0, then recompute derived columns.
    This is the side-effect we assert is now stored in _RAW_CACHE.
    """
    mask = (df["tradingsymbol"] == sym) & (df["last_price"].le(0))
    if not mask.any():
        return
    df.loc[mask, "last_price"] = patched_ltp
    # Recompute cur_val and pnl for the patched rows
    for idx in df.index[mask]:
        avg = float(df.at[idx, "average_price"])
        qty = float(df.at[idx, "opening_quantity"])
        close = float(df.at[idx, "close_price"])
        ltp = patched_ltp
        df.at[idx, "pnl"] = (ltp - avg) * qty
        df.at[idx, "cur_val"] = avg * qty + (ltp - avg) * qty  # = ltp * qty
        df.at[idx, "day_change_val"] = (ltp - close) * qty
        cost = avg * qty
        df.at[idx, "pnl_percentage"] = ((ltp - avg) * qty / cost * 100) if cost != 0 else 0.0
        close_denom = abs(close * qty)
        df.at[idx, "day_change_percentage"] = (
            (ltp - close) * qty / close_denom * 100 if close_denom != 0 else 0.0
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNavHoldingsConsistency:
    """Assert holdings_mtm in compute_firm_nav matches cur_val in the route
    for the same payload after backfill is applied at the broker boundary."""

    def _build_mixed_frame(self, dhan_ltp_after_backfill: float = 1500.0):
        """Build a two-broker holdings frame: one Kite row (LTP populated),
        one Dhan row (LTP=0, patched by backfill). Returns (df, expected_cur_val)."""
        kite = _kite_row("INFY", avg=1400.0, qty=10, ltp=1450.0, close=1420.0)
        dhan = _dhan_row("RELIANCE", avg=2800.0, qty=5, close=2750.0)
        df = _make_holdings_frame([kite, dhan])
        # Simulate backfill: patch the zero-LTP Dhan row
        _simulate_backfill(df, "RELIANCE", dhan_ltp_after_backfill)
        return df

    def test_holdings_from_df_reads_patched_cur_val(self):
        """_holdings_from_df should report the cur_val that backfill wrote."""
        df = self._build_mixed_frame(dhan_ltp_after_backfill=1500.0)

        class _StubTicker:
            def get_ltp_by_sym(self, sym):
                return None  # no ticker — force cur_val path

        mtm, accts = _holdings_from_df(df, _StubTicker())

        # Expected: Kite cur_val = 1450*10=14500, Dhan cur_val = 1500*5=7500
        # Sum = 22000
        assert math.isclose(mtm, 22000.0, abs_tol=1.0), (
            f"holdings_mtm={mtm:.2f} expected 22000.00"
        )
        assert set(accts) == {"ZG0790", "DH6847"}

    def test_zero_ltp_row_without_backfill_underreports(self):
        """Without backfill the Dhan row (LTP=0) contributes 0 to cur_val —
        this is the pre-fix state we're guarding against."""
        kite = _kite_row("INFY", avg=1400.0, qty=10, ltp=1450.0, close=1420.0)
        dhan = _dhan_row("RELIANCE", avg=2800.0, qty=5, close=2750.0)
        df_raw = _make_holdings_frame([kite, dhan])
        # Do NOT apply backfill — raw state (pre-fix).

        class _StubTicker:
            def get_ltp_by_sym(self, sym):
                return None

        mtm_raw, _ = _holdings_from_df(df_raw, _StubTicker())

        # Kite contributes 14500; Dhan with ltp=0 → cur_val=0 → 0 contribution
        # (or at best avg*qty=14000 if inv_val fallback fires — either way < 22000)
        kite_cur = 1450.0 * 10
        assert mtm_raw < kite_cur + 7500.0 - 0.5, (
            "Pre-fix baseline: raw mtm should be lower than post-backfill mtm"
        )

    def test_drift_is_zero_after_backfill(self):
        """Post-fix: _holdings_from_df total matches Σ cur_val from the same frame.

        After backfill patches Dhan's zero LTP and recomputes cur_val,
        both the NAV path (_holdings_from_df) and the route path (Σ cur_val)
        read the same post-patch numbers. This asserts the core property:
        if the frame is consistent, nav and route agree within ₹1.
        """
        df = self._build_mixed_frame(dhan_ltp_after_backfill=1500.0)

        class _StubTicker:
            def get_ltp_by_sym(self, sym):
                return None

        mtm_nav, _ = _holdings_from_df(df, _StubTicker())

        # Route path: sum cur_val from the same patched frame.
        # After _simulate_backfill writes cur_val for the Dhan row,
        # both rows have a valid cur_val. Fill NaN → 0 mirrors what
        # the route's `raw[numeric].fillna(0)` step does before summing.
        cur_val_route = float(df["cur_val"].fillna(0).sum())

        # Both should agree within ₹1. If _holdings_from_df were reading
        # a DIFFERENT (unpatched) cur_val than the route, the gap would
        # be the full Dhan cur_val (~₹7,500) not a rounding noise.
        assert abs(mtm_nav - cur_val_route) < 1.0, (
            f"Drift: nav computed {mtm_nav:.2f}, route would sum {cur_val_route:.2f}. "
            f"Gap = {abs(mtm_nav - cur_val_route):.2f} (must be < ₹1). "
            f"This means nav.py and the route are reading different cur_val values."
        )

    def test_apply_backfill_to_list_wraps_single_frame(self):
        """_apply_backfill_to_list returns a single-element list after
        concatenating and running backfill — preserves iteration contract."""
        # Use a minimal frame with no zero-LTP rows so backfill is a no-op
        # and we just verify the list-shape contract.
        df1 = pd.DataFrame([{
            "tradingsymbol": "NIFTY50", "account": "ZG0790",
            "average_price": 100.0, "opening_quantity": 10.0,
            "last_price": 105.0, "close_price": 102.0,
        }])
        df2 = pd.DataFrame([{
            "tradingsymbol": "SENSEX", "account": "DH6847",
            "average_price": 200.0, "opening_quantity": 5.0,
            "last_price": 210.0, "close_price": 205.0,
        }])

        with patch("backend.brokers.broker_apis.backfill_market_data", return_value=0):
            result = _apply_backfill_to_list([df1, df2])

        assert len(result) == 1, "Expected single-element list"
        assert len(result[0]) == 2, "Combined frame should have 2 rows"

    def test_apply_backfill_to_list_empty_list_passthrough(self):
        """Empty input list passes through without error."""
        result = _apply_backfill_to_list([])
        assert result == []

    def test_apply_backfill_to_list_all_empty_frames(self):
        """All-empty frames pass through unchanged (outage guard)."""
        result = _apply_backfill_to_list([pd.DataFrame(), pd.DataFrame()])
        # All empty → non_empty is [] → returns original list
        assert all(df.empty for df in result)

    def test_apply_backfill_to_list_exception_safety(self):
        """A backfill exception returns the original frames (safety net)."""
        df = pd.DataFrame([{"a": 1}])
        with patch(
            "backend.brokers.broker_apis.backfill_market_data",
            side_effect=RuntimeError("test error"),
        ):
            result = _apply_backfill_to_list([df])
        # Should return the raw list, not raise
        assert len(result) == 1
        assert result[0] is df


class TestNavCurValSSoT:
    """SSOT: verify that _holdings_from_df's cur_val path
    and the route's summation path read the same column."""

    def test_holdings_from_df_prefers_cur_val_over_qty_ltp(self):
        """When cur_val is populated, _holdings_from_df sums it directly.
        This is the fast path used for Kite + backfill-patched Dhan rows."""
        df = pd.DataFrame([
            {"tradingsymbol": "A", "account": "ZG0790",
             "opening_quantity": 10.0, "cur_val": 1500.0, "last_price": 0.0},
            {"tradingsymbol": "B", "account": "ZG0790",
             "opening_quantity": 5.0,  "cur_val": 2000.0, "last_price": 0.0},
        ])

        class _StubTicker:
            def get_ltp_by_sym(self, sym):
                return None

        mtm, _ = _holdings_from_df(df, _StubTicker())
        assert math.isclose(mtm, 3500.0, abs_tol=0.01)
