"""Tests for grammar_tokens/scope_positions_expiring_today.py —
`positions.expiring_today` scope.

Moved out of test_expiry_resolvers.py (Wave 1, scope batch A of the
DSL token-unification plan) — pure resolver-level unit tests, no
behavior change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_scope_expiring_today_filters_to_today():
    # Build a fake position book: one expiring today, one next month,
    # one cash equity. Only the first should appear.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "NIFTY25MAY22500CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "RELIANCE",          "exchange": "NSE", "quantity": 10},
    ])
    rows = _scope_positions_expiring_today(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "NIFTY25APR22000CE"


def test_scope_expiring_today_empty_when_no_position_rows():
    ctx = _ctx(position_rows=[])
    assert _scope_positions_expiring_today(ctx) == []
