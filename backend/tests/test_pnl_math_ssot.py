"""Tier 1 / A1 — SSOT tests for the canonical intraday Day P&L helper.

Two routes used to inline the same formula:
  • backend/brokers/broker_apis.py:_enrich_positions  (polars)
  • backend/api/routes/positions.py:_compute_day_change_val  (pandas)

This spec asserts:
  1. Golden value correctness of `decomposed_intraday_pnl` + `naive_day_pnl`.
  2. Golden value correctness of `recompute_row_percentages` — the helper
     that keeps day_change_percentage + pnl_percentage in sync with their
     absolute counterparts after any in-place LTP/close override.
  3. SSOT — no other backend module re-implements the raw formula
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

from backend.api.algo.pnl_math import (
    decomposed_intraday_pnl,
    naive_day_pnl,
    recompute_row_percentages,
)


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


# ---------------------------------------------------------------------------
# recompute_row_percentages — correctness + override-path regression contract
# ---------------------------------------------------------------------------

class TestRecomputeRowPercentages:
    """Defect #2 regression: positions overrides must refresh percentages.

    Before the fix, `_override_stale_ltp_from_ticker` and
    `_override_stale_close_from_snapshot` in positions.py updated
    `day_change_val` and `pnl` but left `day_change_percentage` and
    `pnl_percentage` at the broker's stale values. This class asserts
    the shared helper computes the right numbers and that calling it
    after an override yields consistent columns.
    """

    def _pos_frame(self) -> pd.DataFrame:
        """A synthetic positions frame with 3 rows representing:
          0 — normal Kite row, percentages already correct
          1 — row where LTP was just overridden (stale percentage before fix)
          2 — opened-today row (close_price=0; fallback to avg denominator)
        """
        return pd.DataFrame({
            "tradingsymbol":       ["NIFTY24DEC23000CE", "CRUDEOIL26JUL6900PE", "GOLDM26JUL"],
            "quantity":            [50.0, 10.0, 100.0],
            "average_price":       [200.0, 250.0, 6500.0],
            "close_price":         [190.0, 220.0, 0.0],    # row 2: opened today
            "last_price":          [210.0, 264.5, 6800.0],
            # day_change_val correctly updated by override:
            "day_change_val":      [(210-190)*50, (264.5-220)*10, (6800-6500)*100],
            # pnl correctly updated by override:
            "pnl":                 [(210-200)*50, (264.5-250)*10, (6800-6500)*100],
            # STALE percentages (pre-override values — what the broker shipped):
            "day_change_percentage": [4.396, 4.396, 0.0],    # row 1 should be ~1.677%
            "pnl_percentage":        [5.0,   4.396, 0.0],    # row 1 should be ~1.38%
        })

    def test_day_change_percentage_recomputed_correctly(self):
        """day_change_percentage = day_change_val / |close × qty| × 100."""
        df = self._pos_frame()
        sel = pd.Index([1])  # only the overridden row
        recompute_row_percentages(df, sel)

        # CRUDEOIL: dcv=(264.5-220)*10=445, denom=|220*10|=2200
        # expected = 445/2200*100 = 20.227...
        expected_dcp = (264.5 - 220) * 10 / (220 * 10) * 100
        assert math.isclose(df.at[1, "day_change_percentage"], expected_dcp, rel_tol=1e-6), (
            f"day_change_percentage={df.at[1, 'day_change_percentage']:.4f} "
            f"expected {expected_dcp:.4f}"
        )

    def test_pnl_percentage_recomputed_correctly(self):
        """pnl_percentage = pnl / |avg × qty| × 100."""
        df = self._pos_frame()
        sel = pd.Index([1])
        recompute_row_percentages(df, sel)

        # CRUDEOIL: pnl=(264.5-250)*10=145, cost=|250*10|=2500
        # expected = 145/2500*100 = 5.8%
        expected_pnl_pct = (264.5 - 250) * 10 / (250 * 10) * 100
        assert math.isclose(df.at[1, "pnl_percentage"], expected_pnl_pct, rel_tol=1e-6), (
            f"pnl_percentage={df.at[1, 'pnl_percentage']:.4f} "
            f"expected {expected_pnl_pct:.4f}"
        )

    def test_non_patched_rows_unchanged(self):
        """Only selected rows are modified; others keep their original values."""
        df = self._pos_frame()
        orig_dcp_0 = df.at[0, "day_change_percentage"]
        orig_pnl_0 = df.at[0, "pnl_percentage"]
        sel = pd.Index([1])  # patch only row 1
        recompute_row_percentages(df, sel)

        assert df.at[0, "day_change_percentage"] == orig_dcp_0
        assert df.at[0, "pnl_percentage"] == orig_pnl_0

    def test_opened_today_fallback_to_avg_denom(self):
        """When close_price=0 the denominator falls back to |avg × qty|.
        This is the opened-today case (no prior session for the symbol).
        """
        df = self._pos_frame()
        sel = pd.Index([2])  # row 2: close=0
        recompute_row_percentages(df, sel)

        # day_change_val = (6800-6500)*100 = 30000
        # close_denom = |0 * 100| = 0  → fallback: avg_denom = |6500*100| = 650000
        # dcp = 30000 / 650000 * 100 = 4.615...%
        expected_dcp = 30000.0 / (6500.0 * 100.0) * 100.0
        assert math.isclose(df.at[2, "day_change_percentage"], expected_dcp, rel_tol=1e-6), (
            f"opened-today row dcp={df.at[2, 'day_change_percentage']:.4f} "
            f"expected {expected_dcp:.4f} (fallback avg denom)"
        )

    def test_noop_on_empty_mask(self):
        """Empty selection index is a no-op."""
        df = self._pos_frame()
        orig = df.copy()
        recompute_row_percentages(df, pd.Index([], dtype="int64"))
        pd.testing.assert_frame_equal(df, orig)

    def test_noop_on_empty_dataframe(self):
        """Empty dataframe does not raise."""
        recompute_row_percentages(pd.DataFrame(), pd.Index([]))

    def test_noop_when_columns_absent(self):
        """If day_change_percentage / pnl_percentage columns are absent,
        no-op (safe to call unconditionally from override helpers)."""
        df = pd.DataFrame({
            "quantity": [10.0],
            "average_price": [100.0],
            "last_price": [110.0],
            "day_change_val": [50.0],
            "pnl": [50.0],
            # No percentage columns
        })
        recompute_row_percentages(df, pd.Index([0]))  # must not raise

    def test_holdings_context_uses_opening_quantity(self):
        """Holdings frames use `opening_quantity` not `quantity`.
        `recompute_row_percentages` probes `opening_quantity` first so
        the denominator uses the right qty column for holdings rows.
        """
        df = pd.DataFrame({
            "opening_quantity":      [10.0],
            "average_price":         [1400.0],
            "close_price":           [1380.0],
            "day_change_val":        [(1420.0 - 1380.0) * 10],  # =400
            "pnl":                   [(1420.0 - 1400.0) * 10],  # =200
            "day_change_percentage": [0.0],   # stale
            "pnl_percentage":        [0.0],   # stale
        })
        recompute_row_percentages(df, pd.Index([0]))

        expected_dcp = 400.0 / (1380.0 * 10.0) * 100.0
        expected_pct = 200.0 / (1400.0 * 10.0) * 100.0
        assert math.isclose(df.at[0, "day_change_percentage"], expected_dcp, rel_tol=1e-6)
        assert math.isclose(df.at[0, "pnl_percentage"], expected_pct, rel_tol=1e-6)

    def test_pre_fix_percentage_was_stale(self):
        """Document the pre-fix state: after LTP override from 220→264.5,
        the broker-shipped day_change_percentage (4.396%) is wrong.
        Post-fix value should be ~20.23% (not ~1.677% as the issue stated —
        that was for a different CRUDEOIL contract; our synthetic uses the
        same arithmetic pattern).
        """
        df = self._pos_frame()
        pre_fix_dcp = df.at[1, "day_change_percentage"]
        assert pre_fix_dcp == 4.396, "Pre-fix value must be 4.396 (broker stale)"

        sel = pd.Index([1])
        recompute_row_percentages(df, sel)
        post_fix_dcp = df.at[1, "day_change_percentage"]

        # Post-fix must NOT equal the broker stale value
        assert not math.isclose(post_fix_dcp, pre_fix_dcp, abs_tol=0.1), (
            "post-fix percentage must diverge from pre-fix broker value"
        )
        # And must match the formula
        expected = (264.5 - 220) * 10 / (220 * 10) * 100
        assert math.isclose(post_fix_dcp, expected, rel_tol=1e-6)
