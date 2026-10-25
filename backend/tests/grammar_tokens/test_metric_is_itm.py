"""Tests for grammar_tokens/metric_is_itm.py — `is_itm` metric.

Moved verbatim from test_expiry_resolvers.py (Wave 1, batch 3 of the
DSL token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_is_itm import _metric_is_itm


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_is_itm_call_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert _metric_is_itm(ctx, row) == 1.0


def test_is_itm_call_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert _metric_is_itm(ctx, row) == 0.0


def test_is_itm_put_below_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 21800.0})
    assert _metric_is_itm(ctx, row) == 1.0


def test_is_itm_put_above_strike():
    row = {"tradingsymbol": "NIFTY25APR22000PE", "exchange": "NFO"}
    ctx = _ctx(spot_prices={"NIFTY": 22150.0})
    assert _metric_is_itm(ctx, row) == 0.0


def test_is_itm_none_without_spot():
    row = {"tradingsymbol": "NIFTY25APR22000CE"}
    assert _metric_is_itm(_ctx(), row) is None  # spot_prices empty


def test_is_itm_none_for_future():
    row = {"tradingsymbol": "NIFTY25APRFUT"}
    ctx = _ctx(spot_prices={"NIFTY": 22000.0})
    assert _metric_is_itm(ctx, row) is None  # futures have no strike
