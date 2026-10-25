"""Tests for grammar_tokens/scope_positions_expiring_today_nfo.py —
`positions.expiring_today.nfo` scope.

Moved out of test_expiry_resolvers.py (Wave 1, scope batch A of the
DSL token-unification plan) — pure resolver-level unit test, no
behavior change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_nfo import (
    _scope_positions_expiring_today_nfo,
)


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_nfo_scope_returns_only_nfo_rows():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "GOLD25APR75000CE",  "exchange": "MCX", "quantity": 1},
        {"tradingsymbol": "RELIANCE",          "exchange": "NSE", "quantity": 10},
    ])
    rows = _scope_positions_expiring_today_nfo(ctx)
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY25APR22000CE"]
