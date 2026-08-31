"""Tests for pledged-holdings collateral_quantity merge (Aug 2026).

Kite returns quantity=0, collateral_quantity=N for shares pledged as margin
collateral. Before the fix, _enrich_holdings used only quantity=0, zeroing
inv_val/cur_val and hiding pledged holdings in Pulse and PositionStrip.

The fix merges collateral_quantity + t1_quantity into effective quantity before
any enrichment computation runs.

Five quality dimensions:
  * SSOT — fix lives in _enrich_holdings; all downstream (inv_val, cur_val, pnl)
    pick up the corrected quantity automatically.
  * Perf — single vectorised pandas pass; no extra I/O.
  * Stale-code grep — no silent behaviour changes; only the merge block is new.
  * Reuse — standard pd.to_numeric().fillna() pattern consistent with rest of
    _enrich_holdings.
  * UX — pledged holdings now show correct inv_val/cur_val in Pulse and
    PositionStrip instead of "—" / zero.
"""

from __future__ import annotations

import pytest
import pandas as pd

from backend.brokers.broker_apis import _enrich_holdings


class TestCollateralQuantityMerge:
    """_enrich_holdings must fold collateral_quantity + t1_quantity into
    effective quantity before computing inv_val / cur_val / pnl.
    """

    def test_pledged_holding_qty_equals_collateral(self):
        """A fully pledged holding (quantity=0, collateral_quantity=N, t1_quantity=0)
        must produce effective quantity = N after merge, and non-zero inv_val/cur_val.

        Scenario: 50 shares pledged as margin, avg=200, ltp=220.
          effective_qty = 0 + 50 + 0 = 50
          inv_val = 200 * 50 = 10000
          cur_val = 220 * 50 = 11000
          pnl     = 11000 - 10000 = 1000
        """
        df = pd.DataFrame({
            "quantity":             [0],
            "collateral_quantity":  [50],
            "t1_quantity":          [0],
            "average_price":        [200.0],
            "last_price":           [220.0],
            "close_price":          [198.0],
        })

        result = _enrich_holdings(df)

        # Effective quantity written back into df["quantity"].
        assert int(result.iloc[0]["quantity"]) == 50, (
            f"expected quantity=50 after collateral merge, got {result.iloc[0]['quantity']}"
        )
        assert result.iloc[0]["inv_val"] == pytest.approx(10000.0), (
            f"expected inv_val=10000, got {result.iloc[0]['inv_val']}"
        )
        assert result.iloc[0]["cur_val"] == pytest.approx(11000.0), (
            f"expected cur_val=11000, got {result.iloc[0]['cur_val']}"
        )
        assert result.iloc[0]["pnl"] == pytest.approx(1000.0), (
            f"expected pnl=1000, got {result.iloc[0]['pnl']}"
        )

    def test_normal_holding_unchanged(self):
        """A normal holding (quantity=M, collateral_quantity=0) must produce
        effective quantity = M — no regression for unpledged holdings.

        Scenario: 100 shares, avg=100, ltp=150.
          effective_qty = 100 + 0 + 0 = 100
          inv_val = 10000, cur_val = 15000, pnl = 5000
        """
        df = pd.DataFrame({
            "quantity":             [100],
            "collateral_quantity":  [0],
            "t1_quantity":          [0],
            "average_price":        [100.0],
            "last_price":           [150.0],
            "close_price":          [98.0],
        })

        result = _enrich_holdings(df)

        assert int(result.iloc[0]["quantity"]) == 100, (
            f"expected quantity=100 (unchanged), got {result.iloc[0]['quantity']}"
        )
        assert result.iloc[0]["inv_val"] == pytest.approx(10000.0), (
            f"expected inv_val=10000, got {result.iloc[0]['inv_val']}"
        )
        assert result.iloc[0]["cur_val"] == pytest.approx(15000.0), (
            f"expected cur_val=15000, got {result.iloc[0]['cur_val']}"
        )

    def test_holding_with_both_collateral_and_t1(self):
        """A holding with quantity=M, collateral_quantity=C, t1_quantity=T
        must produce effective quantity = M + C + T.

        Scenario: 10 settled + 30 pledged + 20 T+1 unsettled = 60 effective.
          avg=500, ltp=510.
          inv_val = 500 * 60 = 30000
          cur_val = 510 * 60 = 30600
          pnl     = 30600 - 30000 = 600
        """
        df = pd.DataFrame({
            "quantity":             [10],
            "collateral_quantity":  [30],
            "t1_quantity":          [20],
            "average_price":        [500.0],
            "last_price":           [510.0],
            "close_price":          [498.0],
        })

        result = _enrich_holdings(df)

        assert int(result.iloc[0]["quantity"]) == 60, (
            f"expected quantity=60 (10+30+20), got {result.iloc[0]['quantity']}"
        )
        assert result.iloc[0]["inv_val"] == pytest.approx(30000.0), (
            f"expected inv_val=30000, got {result.iloc[0]['inv_val']}"
        )
        assert result.iloc[0]["cur_val"] == pytest.approx(30600.0), (
            f"expected cur_val=30600, got {result.iloc[0]['cur_val']}"
        )
        assert result.iloc[0]["pnl"] == pytest.approx(600.0), (
            f"expected pnl=600, got {result.iloc[0]['pnl']}"
        )

    def test_missing_collateral_columns_no_crash(self):
        """When collateral_quantity and t1_quantity columns are absent (e.g.
        Dhan/Groww adapters that don't ship these fields), _enrich_holdings must
        not crash and must use quantity as-is.
        """
        df = pd.DataFrame({
            "quantity":        [75],
            "average_price":   [300.0],
            "last_price":      [310.0],
            "close_price":     [298.0],
        })

        result = _enrich_holdings(df)

        assert int(result.iloc[0]["quantity"]) == 75, (
            f"expected quantity=75 (no collateral cols), got {result.iloc[0]['quantity']}"
        )
        assert result.iloc[0]["inv_val"] == pytest.approx(22500.0)
        assert result.iloc[0]["cur_val"] == pytest.approx(23250.0)

    def test_multiple_rows_mixed_pledged_and_normal(self):
        """Multi-row frame: one pledged row and one normal row must each
        resolve independently.

        Row 0: pledged -- qty=0, collateral=40, t1=0  -> effective=40
        Row 1: normal  -- qty=20, collateral=0,  t1=5 -> effective=25
        """
        df = pd.DataFrame({
            "quantity":             [0,    20],
            "collateral_quantity":  [40,    0],
            "t1_quantity":          [0,     5],
            "average_price":        [100.0, 200.0],
            "last_price":           [110.0, 190.0],
            "close_price":          [98.0,  198.0],
        })

        result = _enrich_holdings(df)

        assert int(result.iloc[0]["quantity"]) == 40, (
            f"row0: expected 40, got {result.iloc[0]['quantity']}"
        )
        assert int(result.iloc[1]["quantity"]) == 25, (
            f"row1: expected 25, got {result.iloc[1]['quantity']}"
        )
        # Row 0: inv_val = 100*40=4000, cur_val = 110*40=4400
        assert result.iloc[0]["inv_val"] == pytest.approx(4000.0)
        assert result.iloc[0]["cur_val"] == pytest.approx(4400.0)
        # Row 1: inv_val = 200*25=5000, cur_val = 190*25=4750
        assert result.iloc[1]["inv_val"] == pytest.approx(5000.0)
        assert result.iloc[1]["cur_val"] == pytest.approx(4750.0)
