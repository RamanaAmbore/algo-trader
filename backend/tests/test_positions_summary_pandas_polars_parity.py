"""Parity test: `background.py:_rebuild_positions_summary` (pandas path)
vs `positions.py:_build_polars_summary` (polars route-summary path) must
agree on TOTAL `day_change_val` for the SAME underlying row — specifically
a Groww-style row where `realised`/`unrealised` are both exactly 0 (not
natively populated) but `pnl` is nonzero.

Before the 2026-09 Day P&L audit round 3 fix (item #4), `_rebuild_positions_
summary` used the plain (no-pnl-fallback) `baseline_diff_day_pnl_series`,
while `_build_polars_summary` already used the fallback-aware
`baseline_diff_day_pnl_expr_with_fallback` — so these two SSE/polling vs.
live-route summary paths disagreed on any row where both legs were exactly
0 but `pnl` was nonzero. This test proves they now agree.
"""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest


def _groww_style_row() -> dict:
    """realised=0, unrealised=0 (not natively populated), pnl=1200
    (combined broker field), prev_settlement_pnl=200 (yesterday's base)."""
    return {
        "account": "GROWWACC",
        "tradingsymbol": "TCS",
        "quantity": 10,
        "realised": 0.0,
        "unrealised": 0.0,
        "pnl": 1200.0,
        "prev_close": 100.0,
        "close_price": 100.0,
        "prev_settlement_pnl": 200.0,
    }


class TestPandasPolarsPositionsSummaryParity:
    def test_total_day_change_val_agrees_for_pnl_fallback_row(self):
        from backend.api.background import _rebuild_positions_summary
        from backend.api.routes.positions import _build_polars_summary

        row = _groww_style_row()
        pandas_df = pd.DataFrame([row])
        polars_df = pl.DataFrame([row])

        pandas_summary = _rebuild_positions_summary(pandas_df)
        polars_summary = _build_polars_summary(polars_df)

        pandas_total = pandas_summary[pandas_summary["account"] == "TOTAL"]
        polars_total = polars_summary.filter(pl.col("account") == "TOTAL")

        assert not pandas_total.empty, "pandas rebuild must produce a TOTAL row"
        assert polars_total.height == 1, "polars summary must produce a TOTAL row"

        pandas_dcv = float(pandas_total["day_change_val"].iloc[0])
        polars_dcv = float(polars_total["day_change_val"][0])

        # Expected: pnl-fallback triggers (realised=unrealised=0) →
        # current_total_profit = pnl (1200) − base_pnl (200) = 1000.
        assert pandas_dcv == pytest.approx(1000.0), (
            f"pandas path (_rebuild_positions_summary) must apply the "
            f"pnl-fallback baseline-diff formula — expected 1000.0, got "
            f"{pandas_dcv}"
        )
        assert polars_dcv == pytest.approx(1000.0), (
            f"polars path (_build_polars_summary) must apply the "
            f"pnl-fallback baseline-diff formula — expected 1000.0, got "
            f"{polars_dcv}"
        )
        assert pandas_dcv == pytest.approx(polars_dcv), (
            f"pandas rebuild ({pandas_dcv}) and polars route-summary "
            f"({polars_dcv}) must agree on the SAME underlying row — "
            f"any drift here means the SSE/polling path and the live "
            f"/api/positions route can disagree on Day P&L for Groww "
            f"(or any broker missing native realised/unrealised split)"
        )

    def test_total_day_change_val_agrees_for_natively_split_row(self):
        """Sanity check: a Kite-style row (realised/unrealised genuinely
        populated, one legitimately 0) must NOT trigger the pnl fallback,
        and both paths must still agree."""
        from backend.api.background import _rebuild_positions_summary
        from backend.api.routes.positions import _build_polars_summary

        row = {
            "account": "KITEACC",
            "tradingsymbol": "INFY",
            "quantity": 5,
            "realised": 0.0,
            "unrealised": 500.0,
            "pnl": 500.0,
            "prev_close": 200.0,
            "close_price": 200.0,
            "prev_settlement_pnl": 50.0,
        }
        pandas_df = pd.DataFrame([row])
        polars_df = pl.DataFrame([row])

        pandas_summary = _rebuild_positions_summary(pandas_df)
        polars_summary = _build_polars_summary(polars_df)

        pandas_dcv = float(pandas_summary[pandas_summary["account"] == "TOTAL"]["day_change_val"].iloc[0])
        polars_dcv = float(polars_summary.filter(pl.col("account") == "TOTAL")["day_change_val"][0])

        # current_total_profit = 0 + 500 = 500; day_pnl = 500 - 50 = 450.
        assert pandas_dcv == pytest.approx(450.0)
        assert polars_dcv == pytest.approx(450.0)
        assert pandas_dcv == pytest.approx(polars_dcv)
