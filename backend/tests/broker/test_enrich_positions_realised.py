"""Tests for _enrich_positions realised-P&L inclusion fix in broker_apis.py.

The `_enrich_positions` function previously used only `pnl` (unrealised) from
the broker adapter, silently dropping `realised` for partially-closed and
fully-closed intraday positions. After the fix, the enriched `pnl` column equals
`broker_pnl + realised` so that Kite, Dhan, and Groww rows all surface total P&L.

Five test classes — one per requirement:
  1. TestEnrichPositionsAddsRealised           — pnl=5000, realised=2000 → 7000
  2. TestEnrichPositionsNoRealisedColumn       — no realised col → no crash, pnl unchanged
  3. TestEnrichPositionsRealisedNull           — realised=NaN → fill_null(0) → pnl=5000
  4. TestEnrichPositionsDhanRow                — pnl=500, realised=3000 → 3500
  5. TestEnrichPositionsGrowwRow               — pnl=4000, realised=1500 → 5500
"""

from __future__ import annotations

import math
import pandas as pd
import pytest

from backend.brokers import broker_apis


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _pos_row(**kwargs) -> pd.DataFrame:
    """Return a single-row positions DataFrame with required base columns plus overrides."""
    base = {
        "last_price": 200.0,
        "average_price": 190.0,
        "close_price": 195.0,
        "quantity": 10,
    }
    base.update(kwargs)
    return pd.DataFrame([base])


# ---------------------------------------------------------------------------
# 1. Realised added to broker pnl
# ---------------------------------------------------------------------------

class TestEnrichPositionsAddsRealised:
    """pnl=5000, realised=2000 → enriched pnl == 7000."""

    def test_pnl_plus_realised_equals_total(self):
        df = _pos_row(pnl=5000.0, realised=2000.0)
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(7000.0), (
            f"Expected 7000 but got {result['pnl'].iloc[0]}"
        )

    def test_zero_realised_leaves_pnl_unchanged(self):
        """realised=0 is a no-op — pnl stays at broker value."""
        df = _pos_row(pnl=5000.0, realised=0.0)
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(5000.0)

    def test_negative_realised_reduces_pnl(self):
        """Loss on the closed portion reduces total pnl correctly."""
        df = _pos_row(pnl=5000.0, realised=-1500.0)
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(3500.0)


# ---------------------------------------------------------------------------
# 2. No realised column — backward-compat, no crash
# ---------------------------------------------------------------------------

class TestEnrichPositionsNoRealisedColumn:
    """When adapter does not emit a `realised` column, enrichment must not crash
    and pnl must equal the broker-supplied pnl unchanged."""

    def test_no_realised_col_uses_broker_pnl(self):
        df = _pos_row(pnl=8000.0)
        assert "realised" not in df.columns
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(8000.0)

    def test_no_realised_col_no_crash_minimal_df(self):
        """Minimal DataFrame (no pnl, no realised) does not raise."""
        df = _pos_row()  # no pnl, no realised
        assert "realised" not in df.columns
        assert "pnl" not in df.columns
        result = broker_apis._enrich_positions(df)
        # pnl should still be computed via fallback formula
        assert "pnl" in result.columns


# ---------------------------------------------------------------------------
# 3. realised=NaN — fill_null(0) so pnl stays at broker_pnl
# ---------------------------------------------------------------------------

class TestEnrichPositionsRealisedNull:
    """realised=NaN must be treated as 0 so pnl == broker_pnl."""

    def test_nan_realised_treated_as_zero(self):
        df = _pos_row(pnl=5000.0, realised=float("nan"))
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(5000.0), (
            f"NaN realised should be fill_null(0); got {result['pnl'].iloc[0]}"
        )

    def test_nan_realised_with_null_pnl_falls_back_to_formula(self):
        """When broker_pnl is null AND realised is NaN, fallback formula runs."""
        df = _pos_row(pnl=float("nan"), realised=float("nan"))
        result = broker_apis._enrich_positions(df)
        # fallback: (ltp - avg) * qty = (200 - 190) * 10 = 100
        assert result["pnl"].iloc[0] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 4. Dhan-style row: unrealised pnl + realisedProfit
# ---------------------------------------------------------------------------

class TestEnrichPositionsDhanRow:
    """Dhan adapter normalises realisedProfit → realised column.

    pnl=500 (unrealised on open qty) + realised=3000 → total pnl = 3500.
    """

    def test_dhan_row_total_pnl(self):
        df = _pos_row(
            last_price=105.0,
            average_price=100.0,
            close_price=102.0,
            quantity=100,
            pnl=500.0,
            realised=3000.0,
        )
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(3500.0)

    def test_dhan_fully_closed_row(self):
        """Fully closed Dhan position: qty=0, pnl=0, realised=−800 → total −800."""
        df = _pos_row(
            last_price=0.0,
            average_price=100.0,
            close_price=102.0,
            quantity=0,
            pnl=0.0,
            realised=-800.0,
        )
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(-800.0)


# ---------------------------------------------------------------------------
# 5. Groww-style row: unrealised_pnl + realised_pnl
# ---------------------------------------------------------------------------

class TestEnrichPositionsGrowwRow:
    """Groww adapter normalises realised_pnl → realised column.

    pnl=4000, realised=1500 → total pnl = 5500.
    """

    def test_groww_row_total_pnl(self):
        df = _pos_row(
            last_price=110.0,
            average_price=100.0,
            close_price=105.0,
            quantity=200,
            pnl=4000.0,
            realised=1500.0,
        )
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(5500.0)

    def test_groww_partial_close_row(self):
        """Partial close: pnl from remaining qty + realised from closed portion."""
        df = _pos_row(
            last_price=108.0,
            average_price=100.0,
            close_price=105.0,
            quantity=50,
            pnl=400.0,
            realised=600.0,
        )
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(1000.0)
