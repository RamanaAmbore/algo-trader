"""Tests for positions backstop Case 2 non-corruption on flat stocks.

Key invariant: For a position whose price is unchanged today (ltp = close_price),
the day_change_val must remain 0 after apply_day_change_backstop Case 2, NOT
be set to the position's total P&L.

This test ensures:
  * Case 2 (overnight, dcv=0, pnl≠0, close>0, avg>0) correctly applies the formula
    dcv = pnl - (close - avg) × oq
  * For a flat stock: ltp = close → pnl = (ltp - avg) × oq = (close - avg) × oq
    → backstop gives pnl - pnl = 0 (correct)
  * Case 2 does not corrupt the per-share P&L computation

Reference: backend/api/algo/pnl_math.py:apply_day_change_backstop()
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.pnl_math import apply_day_change_backstop


def _make_df(**kwargs) -> pd.DataFrame:
    """Build a single-row DataFrame from keyword column values."""
    return pd.DataFrame([kwargs])


class TestCase2FlatStockNonCorruption:
    """Case 2: overnight position with unchanged price must show dcv=0."""

    def test_flat_stock_ltp_equals_close_stays_zero(self):
        """Concrete example: overnight position, ltp=100, close=100, avg=80, oq=50, qty=50, pnl=1000.

        Case 2 applies: dcv = pnl − (close − avg) × oq
        = 1000 − (100 − 80) × 50
        = 1000 − 1000 = 0

        So day_change_val stays 0 — correct!
        """
        df = _make_df(
            quantity=50,
            overnight_quantity=50,
            last_price=100.0,
            close_price=100.0,      # ltp = close → no day move
            average_price=80.0,
            pnl=1000.0,             # (100-80)*50 = 1000
            day_change_val=0.0,     # correct: no move today
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = 1000 - (100-80)*50 = 1000 - 1000 = 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0), (
            "flat stock (ltp=close) must have day_change_val=0 after Case 2 backstop"
        )

    def test_flat_stock_small_spreads(self):
        """Flat stock with realistic bid/ask spreads: ltp=99.95, close=100.0 (epsilon noise).

        Case 2 should handle this cleanly.
        """
        df = _make_df(
            quantity=100,
            overnight_quantity=100,
            last_price=99.95,
            close_price=100.0,      # close ≈ ltp (within tick noise)
            average_price=90.0,
            pnl=999.5,              # (99.95-90)*100 = 999.5
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = 999.5 - (100-90)*100 = 999.5 - 1000 = -0.5
        # (Correct: small loss due to bid/ask spread move)
        assert result.loc[0, "day_change_val"] == pytest.approx(-0.5, abs=0.1), (
            "flat stock with epsilon spread must reflect tick move in dcv"
        )

    def test_flat_short_position_stays_zero(self):
        """Short position (oq < 0) with unchanged price stays flat.

        oq=-50, qty=-50, ltp=100, close=100, avg=120, pnl=(100-120)*50=1000 (gain on short).
        Case 2: dcv = 1000 - (100-120)*(-50) = 1000 - 1000 = 0
        """
        df = _make_df(
            quantity=-50,
            overnight_quantity=-50,
            last_price=100.0,
            close_price=100.0,      # unchanged
            average_price=120.0,
            pnl=1000.0,             # (100-120)*(-50) = 1000 (gain)
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = 1000 - (100-120)*(-50) = 1000 - (-1000) = 1000 - 1000 = 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0), (
            "short position (oq<0) with unchanged price must have dcv=0"
        )

    def test_flat_stock_loss_position(self):
        """Flat stock in loss: ltp=80, close=80, avg=100, oq=25, qty=25, pnl=-500.

        Case 2: dcv = -500 - (80-100)*25 = -500 - (-500) = 0
        """
        df = _make_df(
            quantity=25,
            overnight_quantity=25,
            last_price=80.0,
            close_price=80.0,       # unchanged
            average_price=100.0,
            pnl=-500.0,             # (80-100)*25 = -500 (loss)
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = -500 - (80-100)*25 = -500 - (-500) = 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0), (
            "flat stock with loss must have dcv=0"
        )

    @pytest.mark.parametrize(
        "ltp,close,avg,oq,qty,expected_dcv",
        [
            (100.0, 100.0, 80.0, 50, 50, 0.0),           # flat stock
            (105.0, 100.0, 80.0, 50, 50, 250.0),         # up 5 from close
            (95.0, 100.0, 80.0, 50, 50, -250.0),         # down 5 from close
            (120.0, 120.0, 100.0, 10, 10, 0.0),          # flat, larger qty
            (150.0, 150.0, 100.0, 100, 100, 0.0),        # flat, 100 qty
        ],
    )
    def test_case2_formula_correctness_generic(self, ltp, close, avg, oq, qty, expected_dcv):
        """Generic Case 2 formula test: dcv = pnl - (close - avg) × oq."""
        pnl = (ltp - avg) * oq
        df = _make_df(
            quantity=qty,
            overnight_quantity=oq,
            last_price=ltp,
            close_price=close,
            average_price=avg,
            pnl=pnl,
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = pnl - (close - avg) * oq
        actual_dcv = result.loc[0, "day_change_val"]
        assert actual_dcv == pytest.approx(expected_dcv), (
            f"ltp={ltp}, close={close}, avg={avg}, oq={oq}: "
            f"expected dcv={expected_dcv}, got {actual_dcv}"
        )


class TestCase2NonCorruptionMultiRow:
    """Multi-row test: Case 2 applies correctly to multiple overnight positions."""

    def test_batch_of_flat_stocks_all_stay_zero(self):
        """Apply Case 2 to a DataFrame with multiple flat positions."""
        df = pd.DataFrame([
            # Row 0: flat, oq=50
            dict(quantity=50, overnight_quantity=50, last_price=100.0, close_price=100.0,
                 average_price=80.0, pnl=1000.0, day_change_val=0.0),
            # Row 1: flat, oq=30
            dict(quantity=30, overnight_quantity=30, last_price=200.0, close_price=200.0,
                 average_price=150.0, pnl=1500.0, day_change_val=0.0),
            # Row 2: moved up 5 from close, oq=20
            dict(quantity=20, overnight_quantity=20, last_price=105.0, close_price=100.0,
                 average_price=80.0, pnl=500.0, day_change_val=0.0),
        ])
        result = apply_day_change_backstop(df)

        # Row 0: flat → Case 2 gives 1000 - (100-80)*50 = 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)
        # Row 1: flat → Case 2 gives 1500 - (200-150)*30 = 0
        assert result.loc[1, "day_change_val"] == pytest.approx(0.0)
        # Row 2: up 5 → Case 2 gives 500 - (100-80)*20 = 500 - 400 = 100
        assert result.loc[2, "day_change_val"] == pytest.approx(100.0)


class TestCase2EdgeCasesWithFlatStock:
    """Edge cases for Case 2 when ltp ≈ close."""

    def test_flat_stock_with_zero_close_does_not_fire_case2(self):
        """Case 2 has guard close_price > 0; if close=0, Case 2 must not fire."""
        df = _make_df(
            quantity=50,
            overnight_quantity=50,
            last_price=100.0,
            close_price=0.0,        # stale guard
            average_price=80.0,
            pnl=1000.0,
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2 guard (close > 0) not met → dcv stays 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)

    def test_flat_stock_with_zero_avg_does_not_fire_case2(self):
        """Case 2 has guard average_price > 0; if avg=0, Case 2 must not fire."""
        df = _make_df(
            quantity=50,
            overnight_quantity=50,
            last_price=100.0,
            close_price=100.0,
            average_price=0.0,      # stale guard
            pnl=1000.0,
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2 guard (avg > 0) not met → dcv stays 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0)

    def test_flat_stock_partial_qty_change(self):
        """Overnight position partially sold: oq=50, qty=30 (sold 20).

        Even though qty != oq, Case 2 uses oq for backstop.
        """
        df = _make_df(
            quantity=30,
            overnight_quantity=50,
            last_price=100.0,
            close_price=100.0,      # flat
            average_price=80.0,
            pnl=1000.0,             # (100-80)*50 = 1000 (lifetime on 50 shares)
            day_change_val=0.0,
        )
        result = apply_day_change_backstop(df)
        # Case 2: dcv = 1000 - (100-80)*50 = 0
        assert result.loc[0, "day_change_val"] == pytest.approx(0.0), (
            "partial sale (qty < oq) still uses oq for Case 2 formula"
        )


# ---------------------------------------------------------------------------
# New tests: _enrich_holdings pnl_per_share + _build_holding_row_from_snapshot
# ---------------------------------------------------------------------------

import polars as pl


def _make_holdings_df(**kwargs) -> pd.DataFrame:
    """Build a minimal holdings DataFrame suitable for _enrich_holdings."""
    defaults = dict(
        quantity=10,
        average_price=100.0,
        last_price=120.0,
        close_price=110.0,
        pnl=200.0,
        inv_val=1000.0,
        day_change_val=0.0,
    )
    defaults.update(kwargs)
    return pd.DataFrame([defaults])


class TestEnrichHoldingsPnlPerShare:
    """_enrich_holdings computes pnl_per_share correctly via Polars pass 2."""

    def test_basic_pnl_per_share(self):
        """pnl=200, qty=10 → pnl_per_share=20.0."""
        from backend.brokers.broker_apis import _enrich_holdings

        df = _make_holdings_df(quantity=10, pnl=200.0)
        result = _enrich_holdings(df)
        assert "pnl_per_share" in result.columns, "pnl_per_share column must be present"
        assert result.loc[0, "pnl_per_share"] == pytest.approx(20.0), (
            "pnl_per_share must equal pnl / quantity"
        )

    def test_zero_quantity_yields_zero(self):
        """quantity=0 must produce pnl_per_share=0.0 — no ZeroDivisionError."""
        from backend.brokers.broker_apis import _enrich_holdings

        df = _make_holdings_df(quantity=0, pnl=500.0)
        result = _enrich_holdings(df)
        assert "pnl_per_share" in result.columns, "pnl_per_share column must be present"
        assert result.loc[0, "pnl_per_share"] == pytest.approx(0.0), (
            "pnl_per_share must be 0 when quantity=0 (no division error)"
        )

    def test_negative_pnl_per_share(self):
        """Loss position: pnl=-300, qty=15 → pnl_per_share=-20.0."""
        from backend.brokers.broker_apis import _enrich_holdings

        df = _make_holdings_df(quantity=15, pnl=-300.0)
        result = _enrich_holdings(df)
        assert result.loc[0, "pnl_per_share"] == pytest.approx(-20.0), (
            "negative pnl must produce negative pnl_per_share"
        )

    def test_multi_row_each_row_computed_independently(self):
        """Multi-row frame: pnl_per_share is computed per row, not aggregate."""
        from backend.brokers.broker_apis import _enrich_holdings

        df = pd.DataFrame([
            dict(quantity=10, average_price=100.0, last_price=110.0, close_price=100.0,
                 pnl=100.0, inv_val=1000.0, day_change_val=0.0),
            dict(quantity=5,  average_price=200.0, last_price=220.0, close_price=200.0,
                 pnl=100.0, inv_val=1000.0, day_change_val=0.0),
            dict(quantity=0,  average_price=150.0, last_price=160.0, close_price=150.0,
                 pnl=50.0,  inv_val=0.0,    day_change_val=0.0),
        ])
        result = _enrich_holdings(df)
        assert result.loc[0, "pnl_per_share"] == pytest.approx(10.0)   # 100/10
        assert result.loc[1, "pnl_per_share"] == pytest.approx(20.0)   # 100/5
        assert result.loc[2, "pnl_per_share"] == pytest.approx(0.0)    # qty=0 → zero


class TestBuildHoldingRowFromSnapshot:
    """_build_holding_row_from_snapshot populates previous_close and pnl_per_share."""

    def _make_raw_row(
        self,
        account="ZG0790",
        symbol="INFY",
        exchange="NSE",
        qty=10,
        avg_cost=1500.0,
        ltp=1600.0,
        previous_close=1550.0,
        day_pnl=500.0,
        total_pnl=1000.0,
        captured_at=None,
        prev_ltp=1545.0,
        previous_close_backup=None,
    ):
        """Build a raw_row namedtuple-compatible tuple matching the SQL query output.

        Now 12 columns: ..., prev_ltp, previous_close_backup.
        """
        from datetime import datetime
        _cap = captured_at or datetime(2026, 8, 24, 9, 30, 0)
        return (account, symbol, exchange, qty, avg_cost, ltp, previous_close,
                day_pnl, total_pnl, _cap, prev_ltp, previous_close_backup)

    def test_previous_close_populated(self):
        """previous_close from daily_book must be forwarded to HoldingRow.previous_close."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(previous_close=1550.0, ltp=1600.0)
        row, _inv, _cur, _pnl, _dcv = _build_holding_row_from_snapshot(raw_row)
        assert row.previous_close == pytest.approx(1550.0), (
            "HoldingRow.previous_close must be set from daily_book previous_close "
            "(no corruption since |1550 - 1600| = 50 >> 0.01 threshold)"
        )

    def test_previous_close_falls_back_to_prev_ltp_when_none(self):
        """When previous_close is None but prev_ltp is available, fall back to prev_ltp.

        The safety net fills previous_close_f from prev_ltp when previous_close is
        missing (≤ 0) and no backup exists — avoids a silent 0.0 that would make
        day P&L = ltp × qty instead of (ltp - prior_close) × qty.
        """
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(previous_close=None, ltp=1600.0, prev_ltp=1545.0,
                                     previous_close_backup=None)
        row, _inv, _cur, _pnl, _dcv = _build_holding_row_from_snapshot(raw_row)
        assert row.previous_close == pytest.approx(1545.0), (
            "When previous_close is None and prev_ltp is available, "
            "safety net must fill in prev_ltp (1545.0)"
        )

    def test_previous_close_zero_when_all_missing(self):
        """When previous_close, backup, and prev_ltp are all None/zero, previous_close = 0.0."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(previous_close=None, ltp=1600.0, prev_ltp=None,
                                     previous_close_backup=None)
        row, _inv, _cur, _pnl, _dcv = _build_holding_row_from_snapshot(raw_row)
        assert row.previous_close == pytest.approx(0.0), (
            "When previous_close, backup, and prev_ltp are all missing, "
            "HoldingRow.previous_close must be 0.0"
        )

    def test_pnl_per_share_populated(self):
        """pnl_per_share = total_pnl / qty when qty != 0."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(qty=10, total_pnl=1000.0)
        row, _inv, _cur, _pnl, _dcv = _build_holding_row_from_snapshot(raw_row)
        assert row.pnl_per_share == pytest.approx(100.0), (
            "pnl_per_share must equal total_pnl / qty"
        )

    def test_pnl_per_share_zero_when_qty_zero(self):
        """pnl_per_share must be 0.0 when qty=0 (fully-sold holding in snapshot)."""
        from backend.api.routes.holdings import _build_holding_row_from_snapshot

        raw_row = self._make_raw_row(qty=0, total_pnl=500.0)
        row, _inv, _cur, _pnl, _dcv = _build_holding_row_from_snapshot(raw_row)
        assert row.pnl_per_share == pytest.approx(0.0), (
            "pnl_per_share must be 0.0 when qty=0"
        )
