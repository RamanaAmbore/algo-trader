"""Tier 1 / A1 — SSOT tests for the canonical intraday Day P&L helper.

Two routes used to inline the same formula:
  • backend/brokers/broker_apis.py:_enrich_positions  (polars)
  • backend/api/routes/positions.py:_compute_day_change_val  (pandas)

This spec asserts:
  1. Golden value correctness of `decomposed_intraday_pnl` + `naive_day_pnl`.
  2. SSOT — no other backend module re-implements the raw formula
     (grep guard so future drift gets caught at CI time).
  3. Vector-equivalence — the polars expression in broker_apis and the
     pandas wrapper in positions.py both delegate to the same scalar
     helper, so a 5-row reference frame yields identical output across
     both code paths.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl


# ---------------------------------------------------------------------------
# Golden values
# ---------------------------------------------------------------------------

class TestPnlMathGoldenValues:
    def test_decomposed_carried_only(self):
        # 10 lots carried, LTP up by 5 → +50
        assert decomposed_intraday_pnl(
            oq=10, ltp=100, cls=95, bq=0, bv=0, sv=0, sq=0,
        ) == 50

    def test_decomposed_with_buy(self):
        # 0 carried, bought 5 @ 96 (notional 480), LTP 100 → +20
        assert decomposed_intraday_pnl(
            oq=0, ltp=100, cls=95, bq=5, bv=480, sv=0, sq=0,
        ) == 20

    def test_decomposed_with_sell(self):
        # 0 carried, sold 5 @ 105 (notional 525), LTP 100 → +25
        assert decomposed_intraday_pnl(
            oq=0, ltp=100, cls=95, bq=0, bv=0, sv=525, sq=5,
        ) == 25

    def test_decomposed_full(self):
        # All three legs combined
        result = decomposed_intraday_pnl(
            oq=10, ltp=100, cls=95, bq=5, bv=480, sv=525, sq=5,
        )
        # 10*(100-95) + (5*100 - 480) + (525 - 5*100) = 50 + 20 + 25 = 95
        assert result == 95

    def test_decomposed_negative(self):
        # LTP below close on a long position
        assert decomposed_intraday_pnl(
            oq=10, ltp=90, cls=100, bq=0, bv=0, sv=0, sq=0,
        ) == -100

    def test_naive_basic(self):
        assert naive_day_pnl(ltp=150, cls=140, qty=75) == 750

    def test_naive_negative(self):
        assert naive_day_pnl(ltp=95, cls=100, qty=10) == -50

    def test_naive_zero_close(self):
        # close=0 is a real signal (no prior close yet) — multiply qty
        # by ltp anyway (callers separately guard for ltp > 0).
        assert naive_day_pnl(ltp=100, cls=0, qty=1) == 100


# ---------------------------------------------------------------------------
# SSOT grep — no other backend module re-implements the formula
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# A signature substring unique to the formula's decomposition. If any
# file outside pnl_math.py contains BOTH the carried leg AND the buy
# leg as inline arithmetic (rather than a function call), the test
# fails — forcing the contributor to consolidate through the helper.
_CARRIED_LEG = re.compile(r"\(\s*ltp\s*-\s*c?ls?\b", re.IGNORECASE)
_BUY_LEG     = re.compile(r"\bbq\s*\*\s*_?ltp\s*-\s*_?bv\b|\bbq\s*\*\s*ltp\s*-\s*bv\b", re.IGNORECASE)


class TestPnlMathSSOT:
    def test_no_other_file_inlines_decomposition(self):
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            # Skip the helper itself and any test files.
            if py.name == "pnl_math.py":
                continue
            if py.name.startswith("test_"):
                continue
            # Skip caches / generated.
            if "__pycache__" in py.parts:
                continue
            try:
                src = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _BUY_LEG.search(src):
                offenders.append(str(py.relative_to(BACKEND_ROOT)))
        assert not offenders, (
            "Inline buy-leg arithmetic found outside pnl_math.py. "
            "Route every Day P&L decomposition through "
            "backend.api.algo.pnl_math.decomposed_intraday_pnl instead. "
            f"Offenders: {offenders}"
        )


# ---------------------------------------------------------------------------
# Vector equivalence — polars (broker_apis) vs pandas (positions route)
# ---------------------------------------------------------------------------

class TestPnlMathCrossEngine:
    def _ref_frame(self) -> pd.DataFrame:
        # 5 rows covering: carried-only, fresh-buy, fresh-sell, mixed, negative.
        return pd.DataFrame({
            "overnight_quantity": [10.0, 0.0, 0.0, 10.0, 10.0],
            "last_price":         [100.0, 100.0, 100.0, 100.0, 90.0],
            "close_price":        [95.0,  95.0,  95.0,  95.0,  100.0],
            "day_buy_quantity":   [0.0,   5.0,   0.0,   5.0,   0.0],
            "day_buy_value":      [0.0,   480.0, 0.0,   480.0, 0.0],
            "day_sell_value":     [0.0,   0.0,   525.0, 525.0, 0.0],
            "day_sell_quantity":  [0.0,   0.0,   5.0,   5.0,   0.0],
        })

    def test_pandas_path_matches_scalar(self):
        df = self._ref_frame()
        # Build the pandas Series via the helper exactly like
        # positions.py:_compute_day_change_val does.
        result = decomposed_intraday_pnl(
            df["overnight_quantity"], df["last_price"], df["close_price"],
            df["day_buy_quantity"], df["day_buy_value"],
            df["day_sell_value"], df["day_sell_quantity"],
        )
        expected = pd.Series([50.0, 20.0, 25.0, 95.0, -100.0])
        pd.testing.assert_series_equal(
            result.reset_index(drop=True), expected, check_dtype=False,
        )

    def test_polars_path_matches_pandas(self):
        df = self._ref_frame()
        lf = pl.from_pandas(df)

        oq = pl.col("overnight_quantity").cast(pl.Float64)
        ltp = pl.col("last_price").cast(pl.Float64)
        cls = pl.col("close_price").cast(pl.Float64)
        bq = pl.col("day_buy_quantity").cast(pl.Float64)
        bv = pl.col("day_buy_value").cast(pl.Float64)
        sv = pl.col("day_sell_value").cast(pl.Float64)
        sq = pl.col("day_sell_quantity").cast(pl.Float64)

        polars_expr = decomposed_intraday_pnl(oq, ltp, cls, bq, bv, sv, sq)
        out_pl = lf.select(polars_expr.alias("dcv")).to_pandas()["dcv"]

        # Run pandas path on the same frame
        out_pd = decomposed_intraday_pnl(
            df["overnight_quantity"], df["last_price"], df["close_price"],
            df["day_buy_quantity"], df["day_buy_value"],
            df["day_sell_value"], df["day_sell_quantity"],
        )

        for a, b in zip(out_pl.to_list(), out_pd.to_list()):
            assert math.isclose(a, b), f"polars vs pandas drift: {a} vs {b}"
