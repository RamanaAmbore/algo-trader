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


def test_mcx_uses_dst_aware_close_time_not_a_flat_23_30():
    # Live-reported concern (2026-11-26): this resolver used to hardcode
    # MCX close as a flat 23:30, the same bug class fixed elsewhere in
    # this codebase for the DST-aware winter close (23:55). 2026-11-27 is
    # in the winter window, so the real close is 23:55, not 23:30.
    #
    # At 2026-11-26 11:30 IST (= 06:00 UTC), the REAL days-to-expiry
    # against the correct 23:55 close is ~1.517 days -- just OUTSIDE the
    # 1.5-day pre-warning window, so this position should NOT appear.
    # The old flat-23:30 bug would have computed ~1.500 days exactly,
    # which (by this scope's own `d <= 1.5` rule) WOULD have wrongly
    # included it a little early. This test fails against the pre-fix
    # hardcoded (23, 30) tuple and passes against the DST-aware fix.
    now_dt = datetime(2026, 11, 26, 6, 0, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=now_dt, position_rows=[
        {"tradingsymbol": "GOLD26NOV123000PE", "exchange": "MCX", "quantity": -100},
    ])
    rows = _scope_positions_expiring_today(ctx)
    assert rows == [], (
        "MCX close time must resolve via mcx_non_agri_close_time() "
        "(23:55 in the winter window), not a flat 23:30 -- using the "
        "flat value wrongly includes this row a bit early."
    )


def test_mcx_dst_aware_close_time_still_includes_once_genuinely_within_window():
    # Same contract, same real close time (23:55), but "now" is a few
    # minutes later -- genuinely inside the 1.5-day window even with the
    # correct close time. Confirms the fix didn't just make everything
    # fail to match.
    now_dt = datetime(2026, 11, 26, 6, 30, 0, tzinfo=timezone.utc)  # 12:00 IST
    ctx = _ctx(now=now_dt, position_rows=[
        {"tradingsymbol": "GOLD26NOV123000PE", "exchange": "MCX", "quantity": -100},
    ])
    rows = _scope_positions_expiring_today(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "GOLD26NOV123000PE"
