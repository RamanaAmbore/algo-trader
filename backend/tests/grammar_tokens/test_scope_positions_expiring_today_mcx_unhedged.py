"""Tests for grammar_tokens/scope_positions_expiring_today_mcx_unhedged.py
— `positions.expiring_today.mcx_unhedged` scope.

Moved out of test_expiry_resolvers.py (Wave 1, scope batch A of the
DSL token-unification plan) — pure resolver-level unit tests, no
behavior change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.scope_positions_expiring_today_mcx_unhedged import (
    _scope_positions_expiring_today_mcx_unhedged,
)


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def _gold_mcx_book(expiry_yymon: str, *, with_hedge: bool):
    """Build a GOLD MCX option book.

    `expiry_yymon` is the 2-char yy + 3-char MON token Kite uses
    (e.g. '25MAY' for the contract expiring on the last Thursday of
    May 2025). Tomorrow's expiry for the user — GOLDM May 2025 —
    parses with the last-Thursday convention.

    `with_hedge=True` adds a CE/PE pair on the same strike whose
    quantities net to zero (the broker settles these against each
    other). The MCX-unhedged scope SHOULD skip them.
    """
    book = [
        # The unhedged ITM call we expect the scope to surface
        {"tradingsymbol": f"GOLDM{expiry_yymon}75000CE", "exchange": "MCX",
         "quantity": 1, "last_price": 500.0},
    ]
    if with_hedge:
        # Long CE + short PE on the SAME strike → net qty 0 → fully hedged
        book.append(
            {"tradingsymbol": f"GOLD{expiry_yymon}75000CE", "exchange": "MCX",
             "quantity": 1, "last_price": 800.0})
        book.append(
            {"tradingsymbol": f"GOLD{expiry_yymon}75000PE", "exchange": "MCX",
             "quantity": -1, "last_price": 100.0})
    return book


def test_mcx_unhedged_skips_perfectly_hedged_pair():
    # CRUDE call + put on same underlying+expiry, qty net = 0.
    # Should be skipped because broker nets them.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX",
         "quantity": 1, "last_price": 100},
        {"tradingsymbol": "CRUDEOILM25MAY5500PE", "exchange": "MCX",
         "quantity": -1, "last_price": 200},
    ])
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert rows == [], "perfectly hedged CE/PE pair should not surface"


def test_mcx_unhedged_surfaces_lone_leg():
    # Single unhedged GOLD call expiring this expiry → MUST surface.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)  # ~03:30 IST tomorrow (close to 23:00 IST tonight)
    ctx = _ctx(now=today_dt,
               position_rows=_gold_mcx_book("25MAY", with_hedge=False))
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    assert len(rows) == 1
    assert rows[0]["tradingsymbol"] == "GOLDM25MAY75000CE"


def test_mcx_unhedged_surfaces_unhedged_amid_hedged():
    # GOLDM has a lone leg; GOLD has a hedged pair — only GOLDM
    # should surface because GOLD is fully hedged.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt,
               position_rows=_gold_mcx_book("25MAY", with_hedge=True))
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    syms = {r["tradingsymbol"] for r in rows}
    assert syms == {"GOLDM25MAY75000CE"}, (
        f"only the GOLDM leg should surface (hedged GOLD pair must be skipped), got {syms}")


def test_mcx_unhedged_groups_by_underlying_plus_expiry():
    # Same underlying but DIFFERENT expiry months should be grouped
    # separately — a hedge across different expirations isn't a hedge.
    today = date(2025, 5, 29)
    today_dt = datetime(today.year, today.month, today.day, 22, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        # GOLD May 2025 → expiry tomorrow
        {"tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX",
         "quantity": 1, "last_price": 500},
        # GOLD Jun 2025 → next month — NOT a hedge against May
        {"tradingsymbol": "GOLD25JUN75000PE", "exchange": "MCX",
         "quantity": -1, "last_price": 200},
    ])
    rows = _scope_positions_expiring_today_mcx_unhedged(ctx)
    # Only the May leg surfaces (Jun is far from expiry, so the
    # expiring_today filter drops it before the unhedged check).
    assert [r["tradingsymbol"] for r in rows] == ["GOLD25MAY75000CE"]


def test_mcx_unhedged_skips_nfo_rows():
    # Even an unhedged NFO row should NOT appear in the MCX scope.
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0,
                        tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
    ])
    assert _scope_positions_expiring_today_mcx_unhedged(ctx) == []
