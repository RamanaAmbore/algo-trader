"""Tests for `_enrich_positions`'s per-broker `pnl` sourcing in broker_apis.py.

History: `_enrich_positions` used to compute `pnl = broker_pnl + realised`
unconditionally for every broker. That double-counted for Kite, whose
native `pnl` field is confirmed (Zerodha's own forum) to already equal
`realised + unrealised` — adding `realised` again inflated every Kite
row's total P&L by the realised amount.

Fix: `pnl` (== `current_total_profit = realised + unrealised`) is now
sourced per-broker via `_positions_total_pnl_expr`, selected through the
new `broker_kind` param on `_enrich_positions` ("kite" / "dhan" / "groww",
default "kite" — covers the legacy `kite=` call path where `broker is
None`):

  • Kite:  native `pnl` directly when non-null (never `+ realised`).
  • Groww: native `pnl` when non-null; else `realised + unrealised`.
  • Dhan:  no trustworthy native combined field — always
           `realised (native) + (ltp − avg) × qty` (locally derived,
           gated to 0 when ltp/avg are not valid, e.g. pre-open).

Six test classes:
  1. TestEnrichPositionsKiteNativePnl      — Kite: native pnl used as-is, no double-count
  2. TestEnrichPositionsGrowwNativePresent — Groww: native pnl used as-is, no double-count
  3. TestEnrichPositionsGrowwNativeAbsent  — Groww: pnl null → realised + unrealised fallback
  4. TestEnrichPositionsDhanDerived        — Dhan: realised + (ltp-avg)*qty, native pnl ignored
  5. TestEnrichPositionsDhanPreOpenGuard   — Dhan: ltp=0 → derived term gated to 0
  6. TestEnrichPositionsBackwardCompat     — no realised/unrealised cols, default broker_kind
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
        "prev_close": 195.0,
        "quantity": 10,
    }
    base.update(kwargs)
    return pd.DataFrame([base])


# ---------------------------------------------------------------------------
# 1. Kite — native pnl trusted directly, realised NOT added on top
# ---------------------------------------------------------------------------

class TestEnrichPositionsKiteNativePnl:
    """Kite's native `pnl` already equals realised + unrealised. Numeric
    proof: pnl=7000 (already combined), realised=2000 (must be ignored).
    Old buggy formula would give 9000 — assert we do NOT get that."""

    def test_native_pnl_used_as_is(self):
        df = _pos_row(pnl=7000.0, realised=2000.0)
        result = broker_apis._enrich_positions(df, broker_kind="kite")
        assert result["pnl"].iloc[0] == pytest.approx(7000.0)
        assert result["pnl"].iloc[0] != pytest.approx(9000.0), (
            "Double-count regression: realised must not be added on top "
            "of Kite's already-combined native pnl"
        )

    def test_default_broker_kind_is_kite(self):
        """`_enrich_positions(df)` with no broker_kind arg behaves like Kite
        (covers the legacy `kite=` call path where `broker is None`)."""
        df = _pos_row(pnl=7000.0, realised=2000.0)
        result = broker_apis._enrich_positions(df)
        assert result["pnl"].iloc[0] == pytest.approx(7000.0)

    def test_negative_realised_does_not_affect_kite_pnl(self):
        df = _pos_row(pnl=3500.0, realised=-1500.0)
        result = broker_apis._enrich_positions(df, broker_kind="kite")
        assert result["pnl"].iloc[0] == pytest.approx(3500.0)

    def test_null_pnl_falls_back_to_local_formula(self):
        """When Kite's native pnl is null, fall back to (ltp-avg)*qty."""
        df = _pos_row(pnl=float("nan"), realised=float("nan"),
                       last_price=200.0, average_price=190.0, quantity=10)
        result = broker_apis._enrich_positions(df, broker_kind="kite")
        # (200-190)*10 = 100
        assert result["pnl"].iloc[0] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 2. Groww — native pnl present → trusted directly (mirrors Kite)
# ---------------------------------------------------------------------------

class TestEnrichPositionsGrowwNativePresent:
    """Numeric proof: raw pnl=5500 (native combined), realised=1500,
    unrealised=4000 both present but must be IGNORED since native pnl
    is non-null. Naive realised+unrealised sum would give 5500 too by
    coincidence here — use mismatched numbers to prove native wins."""

    def test_native_pnl_used_when_present(self):
        df = _pos_row(pnl=5500.0, realised=1500.0, unrealised=4000.0)
        result = broker_apis._enrich_positions(df, broker_kind="groww")
        assert result["pnl"].iloc[0] == pytest.approx(5500.0)

    def test_native_pnl_wins_over_mismatched_split_fields(self):
        """realised+unrealised sums to something OTHER than native pnl —
        proves native is used, not the fallback sum."""
        df = _pos_row(pnl=5500.0, realised=1500.0, unrealised=9999.0)
        result = broker_apis._enrich_positions(df, broker_kind="groww")
        assert result["pnl"].iloc[0] == pytest.approx(5500.0)
        assert result["pnl"].iloc[0] != pytest.approx(1500.0 + 9999.0)


# ---------------------------------------------------------------------------
# 3. Groww — native pnl absent (null) → realised + unrealised fallback
# ---------------------------------------------------------------------------

class TestEnrichPositionsGrowwNativeAbsent:
    """Numeric proof: pnl=None (absent), realised=1500, unrealised=4000
    → fallback sum = 5500."""

    def test_null_pnl_falls_back_to_realised_plus_unrealised(self):
        df = _pos_row(pnl=None, realised=1500.0, unrealised=4000.0)
        result = broker_apis._enrich_positions(df, broker_kind="groww")
        assert result["pnl"].iloc[0] == pytest.approx(5500.0)

    def test_nan_pnl_treated_same_as_none(self):
        df = _pos_row(pnl=float("nan"), realised=1500.0, unrealised=4000.0)
        result = broker_apis._enrich_positions(df, broker_kind="groww")
        assert result["pnl"].iloc[0] == pytest.approx(5500.0)

    def test_missing_unrealised_col_treated_as_zero(self):
        df = _pos_row(pnl=None, realised=1500.0)
        assert "unrealised" not in df.columns
        result = broker_apis._enrich_positions(df, broker_kind="groww")
        assert result["pnl"].iloc[0] == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# 4. Dhan — realised (native) + (ltp-avg)*qty (locally derived)
# ---------------------------------------------------------------------------

class TestEnrichPositionsDhanDerived:
    """Dhan has no trustworthy native combined field — native `pnl` is
    ALWAYS ignored, even when it looks plausible. Numeric proof: native
    pnl=999999 (garbage), ltp=105, avg=100, qty=100 → derived
    unrealised=(105-100)*100=500, realised=3000 → total=3500."""

    def test_derived_formula_ignores_native_pnl(self):
        df = _pos_row(
            last_price=105.0, average_price=100.0, prev_close=102.0,
            quantity=100, pnl=999999.0, realised=3000.0,
        )
        result = broker_apis._enrich_positions(df, broker_kind="dhan")
        assert result["pnl"].iloc[0] == pytest.approx(3500.0)
        assert result["pnl"].iloc[0] != pytest.approx(999999.0 + 3000.0)

    def test_fully_closed_dhan_row(self):
        """qty=0 → derived term is 0 regardless of ltp/avg → pnl == realised."""
        df = _pos_row(
            last_price=120.0, average_price=100.0, prev_close=100.0,
            quantity=0, pnl=0.0, realised=-800.0,
        )
        result = broker_apis._enrich_positions(df, broker_kind="dhan")
        assert result["pnl"].iloc[0] == pytest.approx(-800.0)

    def test_no_realised_column_treated_as_zero(self):
        df = _pos_row(
            last_price=105.0, average_price=100.0, quantity=100, pnl=42.0,
        )
        assert "realised" not in df.columns
        result = broker_apis._enrich_positions(df, broker_kind="dhan")
        # derived = (105-100)*100 = 500, realised absent → 0 → total 500
        assert result["pnl"].iloc[0] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 5. Dhan — pre-open guard: ltp<=0 must not produce a phantom loss
# ---------------------------------------------------------------------------

class TestEnrichPositionsDhanPreOpenGuard:
    """Mirrors the Dhan adapter's own `_normalise_position_prices_and_pnl`
    guard (`pnl_calc = 0.0` when ltp<=0 or avg<=0). Without this guard,
    (0 - avg) * qty produces a large phantom loss during the pre-open
    window when ltp has not ticked yet."""

    def test_zero_ltp_gates_derived_term_to_zero(self):
        df = _pos_row(
            last_price=0.0, average_price=100.0, prev_close=102.0,
            quantity=100, pnl=999999.0, realised=3000.0,
        )
        result = broker_apis._enrich_positions(df, broker_kind="dhan")
        # Without the guard: (0-100)*100 + 3000 = -7000 (wrong).
        assert result["pnl"].iloc[0] == pytest.approx(3000.0)
        assert result["pnl"].iloc[0] != pytest.approx(-7000.0)

    def test_zero_avg_gates_derived_term_to_zero(self):
        df = _pos_row(
            last_price=105.0, average_price=0.0, prev_close=102.0,
            quantity=100, pnl=999999.0, realised=-500.0,
        )
        result = broker_apis._enrich_positions(df, broker_kind="dhan")
        assert result["pnl"].iloc[0] == pytest.approx(-500.0)


# ---------------------------------------------------------------------------
# 6. Backward-compat — missing columns / default broker_kind don't crash
# ---------------------------------------------------------------------------

class TestEnrichPositionsBackwardCompat:
    def test_no_pnl_col_no_crash_minimal_df(self):
        """Minimal DataFrame (no pnl, no realised) does not raise."""
        df = _pos_row()  # no pnl, no realised
        assert "realised" not in df.columns
        assert "pnl" not in df.columns
        result = broker_apis._enrich_positions(df)
        assert "pnl" in result.columns
        # fallback formula: (200-190)*10 = 100
        assert result["pnl"].iloc[0] == pytest.approx(100.0)

    def test_unknown_broker_kind_falls_back_to_kite_semantics(self):
        """An unrecognised broker_kind string defaults to Kite behaviour
        (native pnl trusted, no realised addition) rather than raising."""
        df = _pos_row(pnl=7000.0, realised=2000.0)
        result = broker_apis._enrich_positions(df, broker_kind="unknown_vendor")
        assert result["pnl"].iloc[0] == pytest.approx(7000.0)


# ---------------------------------------------------------------------------
# 7. _broker_kind — must work identically in-process AND via RemoteBroker
#    (conn-service mode). type(broker).__name__ always reads "RemoteBroker"
#    there, so the resolver must read `broker_id`, not the class name.
# ---------------------------------------------------------------------------

class _FakeBroker:
    """Minimal stand-in exposing only `broker_id`, mirroring the real
    `Broker` interface contract (and what `RemoteBroker` forwards from
    conn_service, as opposed to its own class name)."""

    def __init__(self, broker_id: str):
        self.broker_id = broker_id


class TestBrokerKindResolution:
    def test_none_broker_defaults_to_kite(self):
        """Legacy `kite=` call path — broker is None."""
        assert broker_apis._broker_kind(None) == "kite"

    def test_kite_broker_id(self):
        assert broker_apis._broker_kind(_FakeBroker("zerodha_kite")) == "kite"

    def test_dhan_broker_id(self):
        assert broker_apis._broker_kind(_FakeBroker("dhan")) == "dhan"

    def test_groww_broker_id(self):
        assert broker_apis._broker_kind(_FakeBroker("groww")) == "groww"

    def test_remote_broker_class_name_does_not_fool_resolution(self):
        """A RemoteBroker-shaped stub (class name 'RemoteBroker', NOT
        'DhanBroker') must still resolve via broker_id, not type name —
        this is the conn-service (RAMBOQ_USE_CONN_SERVICE=1) code path."""
        class RemoteBroker:
            def __init__(self, broker_id):
                self.broker_id = broker_id

        stub = RemoteBroker("dhan")
        assert type(stub).__name__ == "RemoteBroker"
        assert broker_apis._broker_kind(stub) == "dhan"

    def test_broker_missing_broker_id_attr_defaults_to_kite(self):
        class Weird:
            pass
        assert broker_apis._broker_kind(Weird()) == "kite"
