"""Tests for grammar_tokens/metric_is_ntm.py — `is_ntm` metric.

Moved verbatim from test_expiry_resolvers.py (Wave 1, batch 3 of the
DSL token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_is_ntm import _metric_is_ntm


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_is_ntm_within_band():
    # Spot 22000, strike 22050 → 0.227% away → NTM
    row = {"tradingsymbol": "NIFTY25APR22050CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert _metric_is_ntm(ctx, row) == 1.0


def test_is_ntm_outside_band():
    # Spot 22000, strike 22500 → 2.27% away → outside ±1.5%
    row = {"tradingsymbol": "NIFTY25APR22500CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert _metric_is_ntm(ctx, row) == 0.0


def test_is_ntm_none_without_spot():
    row = {"tradingsymbol": "NIFTY25APR22000CE"}
    assert _metric_is_ntm(_ctx(), row) is None


def test_is_ntm_none_for_future():
    row = {"tradingsymbol": "NIFTY25APRFUT"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert _metric_is_ntm(ctx, row) is None
