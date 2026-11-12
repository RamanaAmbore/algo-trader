"""Tests for grammar_tokens/scope_positions_expiring_today_nfo.py —
`positions.expiring_today.nfo` scope.

Moved out of test_expiry_resolvers.py (Wave 1, scope batch A of the
DSL token-unification plan) — pure resolver-level unit test, no
behavior change.

2026-11 classifier re-base (operator decision): the scope now also
excludes flat (qty==0) rows — matching `ExpiryEngine._fetch_option_
positions()`'s own skip, so a squared-off ITM leg (or closed-out
future) no longer fires `expiry-nfo-risk-alert`. See this module's
own docstring for the full rationale.
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


def test_nfo_scope_excludes_flat_qty_zero_row():
    """A squared-off (qty==0) NFO leg must never surface — the OLD
    scope had no quantity filter at all, so a closed-out ITM leg or
    future could still fire `expiry-nfo-risk-alert` via the generic
    `is_itm`/`is_future` metrics. Matches `ExpiryEngine._fetch_
    option_positions()`'s own `qty == 0` skip."""
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 0},
        {"tradingsymbol": "NIFTY25APR23000CE", "exchange": "NFO", "quantity": 50},
    ])
    rows = _scope_positions_expiring_today_nfo(ctx)
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY25APR23000CE"]
