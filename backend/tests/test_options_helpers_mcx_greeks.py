"""
Tests for the `is_mcx` Black-76 dispatch in `options_helpers.py`'s
chain-snapshot Greeks helpers.

Context: `_chain_snapshot_iv_greeks` / `_chain_snapshot_compute_leg` /
`_chain_snapshot_compute_rows` priced every option via plain Black-Scholes
(`implied_vol`/`greeks`) even when the snapshot's underlying is an MCX
commodity whose "spot" is resolved as the matching futures contract price
(see `derivatives.py`'s cost-of-carry `b` comment block). Fixed by
threading an `is_mcx: bool = False` parameter through all three functions,
selecting `implied_vol_76`/`greeks_76` (Black-76) instead.

Five quality dimensions:
  1. SSOT       — exercises the real production helpers, not a reimplementation
  2. Correctness— MCX path numerically matches greeks_76()/implied_vol_76()
                  and differs materially from the plain-BS path
  3. Regression — spies on derivatives module functions so reverting the
                  branch to always-BS fails these tests
  4. Reusable   — follows test_derivatives_vec.py's existing dispatch-test pattern
  5. Back-compat— is_mcx defaults to False; NSE/index callers are unaffected
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.api.algo import derivatives as deriv_mod
from backend.api.algo.derivatives import DEFAULT_RISK_FREE, greeks, greeks_76


_F = 9400.0   # futures/spot price
_K = 9400.0   # ATM strike
_T = 30 / 365.0
_SIDE = "CE"


def test_chain_snapshot_iv_greeks_mcx_dispatches_black_76():
    from backend.api.routes.options_helpers import _chain_snapshot_iv_greeks

    with patch("backend.api.routes.options_helpers.greeks_76", wraps=deriv_mod.greeks_76) as spy_76, \
         patch("backend.api.routes.options_helpers.greeks", wraps=deriv_mod.greeks) as spy_bs:
        iv, g = _chain_snapshot_iv_greeks(200.0, _F, _K, _T, _SIDE, is_mcx=True)

    assert spy_76.called, "is_mcx=True must dispatch through greeks_76"
    assert not spy_bs.called, "is_mcx=True must NOT fall through to plain BS greeks()"
    assert g is not None

    # Cross-check against a direct greeks_76()/greeks() call at the same sigma —
    # must match Black-76 exactly, and differ materially from plain BS.
    g76_direct = greeks_76(_F, _K, _T, DEFAULT_RISK_FREE, iv, _SIDE)
    gbs_direct = greeks(_F, _K, _T, DEFAULT_RISK_FREE, iv, _SIDE)
    for k in g:
        assert g[k] == pytest.approx(g76_direct[k], abs=1e-9)
    assert abs(g["delta"] - gbs_direct["delta"]) > 1e-3


def test_chain_snapshot_iv_greeks_default_stays_on_plain_bs():
    from backend.api.routes.options_helpers import _chain_snapshot_iv_greeks

    with patch("backend.api.routes.options_helpers.greeks_76", wraps=deriv_mod.greeks_76) as spy_76, \
         patch("backend.api.routes.options_helpers.greeks", wraps=deriv_mod.greeks) as spy_bs:
        iv, g = _chain_snapshot_iv_greeks(50.0, 2800.0, 2800.0, _T, _SIDE)  # is_mcx defaults False

    assert spy_bs.called, "is_mcx default (False) must use plain BS greeks()"
    assert not spy_76.called, "is_mcx default (False) must NOT dispatch through greeks_76"
    assert g is not None


def test_chain_snapshot_compute_rows_threads_is_mcx_through(monkeypatch=None):
    """Guards the wiring one level up: `_chain_snapshot_compute_rows` →
    `_chain_snapshot_compute_leg` → `_chain_snapshot_iv_greeks`. A
    regression that drops `is_mcx` anywhere in this chain reverts MCX
    snapshot Greeks to plain BS while the lower-level test above keeps
    passing untouched (it never exercises the wiring)."""
    from backend.api.routes.options_helpers import _chain_snapshot_compute_rows

    class FakeLeg:
        def __init__(self, ltp, bid, ask, iv, delta, gamma, theta, vega, rho):
            self.ltp, self.bid, self.ask, self.iv = ltp, bid, ask, iv
            self.delta, self.gamma, self.theta, self.vega, self.rho = (
                delta, gamma, theta, vega, rho,
            )

    class FakeRow:
        def __init__(self, k, atm_distance, ce, pe):
            self.k, self.atm_distance, self.ce, self.pe = k, atm_distance, ce, pe

    sym_by_strike = {_K: {"CE": "CRUDEOIL26OCT9400CE", "PE": "CRUDEOIL26OCT9400PE"}}
    quote_resp = {
        "MCX:CRUDEOIL26OCT9400CE": {"last_price": 200.0, "depth": {}},
        "MCX:CRUDEOIL26OCT9400PE": {"last_price": 190.0, "depth": {}},
    }

    with patch("backend.api.routes.options_helpers.greeks_76", wraps=deriv_mod.greeks_76) as spy_76, \
         patch("backend.api.routes.options_helpers.greeks", wraps=deriv_mod.greeks) as spy_bs:
        rows = _chain_snapshot_compute_rows(
            sym_by_strike, [_K], quote_resp, _F, _T, FakeLeg, FakeRow, is_mcx=True,
        )

    assert len(rows) == 1
    assert spy_76.called, "is_mcx=True must propagate through compute_rows -> compute_leg -> iv_greeks"
    assert not spy_bs.called
