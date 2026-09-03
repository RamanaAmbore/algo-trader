"""
test_risk_free_ssot.py — verifies that DEFAULT_RISK_FREE from
backend.api.algo.derivatives is the sole source of the risk-free rate in:

  1. _enrich_position_greeks (backend/api/routes/positions.py)
  2. ExpiryEngine._compute_theta (backend/api/algo/expiry.py)

SSOT dimension: if DEFAULT_RISK_FREE changes, both callers pick up the new
value automatically — the magic number 0.07 must NOT appear in either call
site.

All five quality dimensions (feedback_test_dimensions.md):
  1. SSOT — both functions receive DEFAULT_RISK_FREE, not a hardcoded 0.07
  2. Performance — no heavy broker calls; greeks/implied_vol are mocked
  3. Stale code — grep confirms 0.07 literal absent from positions.py and
     expiry.py call sites
  4. Reuse — DEFAULT_RISK_FREE imported from the same canonical module
  5. UX — no visible change; numerical outputs verified consistent with constant
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.api.algo.derivatives import DEFAULT_RISK_FREE


# ---------------------------------------------------------------------------
# 1. SSOT — constant value sanity
# ---------------------------------------------------------------------------

def test_default_risk_free_value():
    """DEFAULT_RISK_FREE must equal 0.07 (7% Indian T-bill convention)."""
    assert DEFAULT_RISK_FREE == pytest.approx(0.07), (
        f"DEFAULT_RISK_FREE={DEFAULT_RISK_FREE!r}; expected 0.07"
    )


# ---------------------------------------------------------------------------
# 2. Stale-code grep: no bare 0.07 literal at call sites
# ---------------------------------------------------------------------------

_POSITIONS_SRC = Path("backend/api/routes/positions.py").read_text()
_EXPIRY_SRC    = Path("backend/api/algo/expiry.py").read_text()


def test_positions_no_hardcoded_r_rate():
    """_enrich_position_greeks must not contain `r_rate = 0.07` magic literal."""
    assert "r_rate = 0.07" not in _POSITIONS_SRC, (
        "Found hardcoded r_rate = 0.07 in positions.py; "
        "use DEFAULT_RISK_FREE from backend.api.algo.derivatives instead."
    )


def test_positions_imports_default_risk_free():
    """positions.py must import DEFAULT_RISK_FREE from backend.api.algo.derivatives."""
    assert "DEFAULT_RISK_FREE" in _POSITIONS_SRC, (
        "DEFAULT_RISK_FREE not found in positions.py; "
        "the local import inside _enrich_position_greeks must include it."
    )


def test_expiry_no_hardcoded_r():
    """_compute_theta must not contain `r = 0.07` magic literal."""
    assert "r = 0.07" not in _EXPIRY_SRC, (
        "Found hardcoded r = 0.07 in expiry.py; "
        "use DEFAULT_RISK_FREE from backend.api.algo.derivatives instead."
    )


def test_expiry_imports_default_risk_free():
    """expiry.py must import DEFAULT_RISK_FREE from backend.api.algo.derivatives."""
    assert "DEFAULT_RISK_FREE" in _EXPIRY_SRC, (
        "DEFAULT_RISK_FREE not found in expiry.py; "
        "_compute_theta must import and use it."
    )


# ---------------------------------------------------------------------------
# 3. Runtime: _enrich_position_greeks passes DEFAULT_RISK_FREE to implied_vol
#    and greeks (not 0.07 as a literal).
#
#    _enrich_position_greeks does a *local* import:
#        from backend.api.algo.derivatives import implied_vol, greeks, ...
#    so we patch the names at the module level inside backend.api.algo.derivatives
#    before the local import executes. Since the local import re-binds the
#    name from the module each time the function is called, patching the
#    source module is the correct interception point.
# ---------------------------------------------------------------------------

def test_enrich_position_greeks_uses_default_risk_free():
    """Mock implied_vol + greeks, call _enrich_position_greeks, assert
    the `r` argument received equals DEFAULT_RISK_FREE exactly.

    This test will FAIL if someone restores r_rate = 0.07 at the call site
    and DEFAULT_RISK_FREE is later changed to a different value.
    """
    from backend.api.routes.positions import _enrich_position_greeks

    # PositionRow is a msgspec.Struct and cannot be instantiated with __new__.
    # Build a plain MagicMock with the attribute shape the function reads.
    row = MagicMock()
    row.tradingsymbol  = "NIFTY25SEP24000CE"
    row.exchange       = "NFO"
    row.quantity       = 50
    row.last_price     = 120.0
    row.underlying_ltp = 0.0
    row.delta_pos      = 0.0
    row.theta_pos      = 0.0

    captured_r: list[float] = []

    def fake_implied_vol(price, S, K, T, r, opt_type):
        captured_r.append(r)
        return 0.16

    def fake_greeks(S, K, T, r, sigma, opt_type):
        captured_r.append(r)
        return {"delta": 0.5, "theta": -10.0}

    def fake_batch_fetch_spots(keys):
        return {k: 24000.0 for k in keys}

    # Patch at the derivatives module level so the local import inside the
    # function picks up the mocked versions.
    with (
        patch("backend.api.routes.positions._batch_fetch_spots",
              side_effect=fake_batch_fetch_spots),
        patch("backend.api.algo.derivatives.implied_vol",
              side_effect=fake_implied_vol),
        patch("backend.api.algo.derivatives.greeks",
              side_effect=fake_greeks),
    ):
        _enrich_position_greeks([row])

    assert len(captured_r) >= 1, (
        "_enrich_position_greeks never called implied_vol or greeks — test fixture broken"
    )
    for r_val in captured_r:
        assert r_val == pytest.approx(DEFAULT_RISK_FREE), (
            f"_enrich_position_greeks passed r={r_val!r} to implied_vol/greeks; "
            f"expected DEFAULT_RISK_FREE={DEFAULT_RISK_FREE!r}"
        )


# ---------------------------------------------------------------------------
# 4. Runtime: ExpiryEngine._compute_theta passes DEFAULT_RISK_FREE to greeks
# ---------------------------------------------------------------------------

def test_compute_theta_uses_default_risk_free():
    """Mock greeks + days_to_expiry, call _compute_theta, assert
    the `r` argument received equals DEFAULT_RISK_FREE exactly."""
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition

    # OptionPosition is a dataclass; supply all required fields.
    pos = OptionPosition(
        account="ACC1",
        tradingsymbol="CRUDEOIL26AUGCE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=6000.0,
        expiry=date(2026, 8, 19),
        quantity=1,
        product="NRML",
        underlying_ltp=6000.0,
    )

    captured_r: list[float] = []

    def fake_greeks(S, K, T, r, sigma, opt_type):
        captured_r.append(r)
        return {"theta": -5.0}

    def fake_days_to_expiry(expiry, close_time=None):
        return 7.0

    # ExpiryEngine has heavy __init__ (async callbacks etc.); bypass it.
    engine = ExpiryEngine.__new__(ExpiryEngine)

    with (
        patch("backend.api.algo.derivatives.greeks",
              side_effect=fake_greeks),
        patch("backend.api.algo.derivatives.days_to_expiry",
              side_effect=fake_days_to_expiry),
    ):
        theta = engine._compute_theta(pos)

    assert len(captured_r) >= 1, (
        "_compute_theta never called greeks — test fixture broken"
    )
    for r_val in captured_r:
        assert r_val == pytest.approx(DEFAULT_RISK_FREE), (
            f"_compute_theta passed r={r_val!r} to greeks; "
            f"expected DEFAULT_RISK_FREE={DEFAULT_RISK_FREE!r}"
        )

    assert theta == pytest.approx(-5.0), (
        f"_compute_theta returned {theta!r}; expected -5.0 from mocked greeks"
    )
