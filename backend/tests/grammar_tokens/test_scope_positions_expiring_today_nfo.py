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
from unittest.mock import patch

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


# ═══════════════════════════════════════════════════════════════════════
# Audit finding #14 (2026-11-24) — the scope now owns ITM/future
# membership itself, and fails loud on an unresolved spot price.
# ═══════════════════════════════════════════════════════════════════════

def test_itm_and_future_included_otm_excluded_when_spot_known():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, spot_prices={"NIFTY": 23000.0}, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},  # ITM (spot>strike)
        {"tradingsymbol": "NIFTY25APR24000CE", "exchange": "NFO", "quantity": 50},  # OTM
        {"tradingsymbol": "NIFTY25APRFUT",     "exchange": "NFO", "quantity": 50},  # future, always in
    ])
    rows = _scope_positions_expiring_today_nfo(ctx)
    assert {r["tradingsymbol"] for r in rows} == {"NIFTY25APR22000CE", "NIFTY25APRFUT"}


def test_one_unresolved_spot_returns_every_nfo_row_fail_loud():
    """Audit finding #14: an unresolvable spot for ONE option must not
    silently drop that row — it must fail loud and return every qty!=0
    NFO row (including a genuinely OTM one) as unconfirmed-risk."""
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, spot_prices={"NIFTY": 23000.0}, position_rows=[
        {"tradingsymbol": "NIFTY25APR24000CE", "exchange": "NFO", "quantity": 50},   # OTM, known spot
        {"tradingsymbol": "BANKNIFTY25APR50000CE", "exchange": "NFO", "quantity": 25},  # no spot at all
    ])
    with patch("backend.api.algo.grammar_tokens.scope_positions_expiring_today_nfo.logger") as mock_logger:
        rows = _scope_positions_expiring_today_nfo(ctx)
    assert {r["tradingsymbol"] for r in rows} == {
        "NIFTY25APR24000CE", "BANKNIFTY25APR50000CE",
    }
    assert mock_logger.critical.called


def test_zero_or_negative_spot_is_also_unresolved():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, spot_prices={"NIFTY": 0.0}, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
    ])
    rows = _scope_positions_expiring_today_nfo(ctx)
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY25APR22000CE"]


def test_classification_exception_fails_loud_returns_all_rows():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, spot_prices={"NIFTY": 23000.0}, position_rows=[
        {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50},
        {"tradingsymbol": "NIFTY25APR24000CE", "exchange": "NFO", "quantity": 50},
    ])
    with patch(
        "backend.api.algo.grammar_tokens.scope_positions_expiring_today_nfo._nfo_classify_row",
        side_effect=RuntimeError("boom"),
    ), patch(
        "backend.api.algo.grammar_tokens.scope_positions_expiring_today_nfo.logger",
    ) as mock_logger:
        rows = _scope_positions_expiring_today_nfo(ctx)
    assert {r["tradingsymbol"] for r in rows} == {"NIFTY25APR22000CE", "NIFTY25APR24000CE"}
    assert mock_logger.critical.called


def test_no_nfo_rows_at_all_returns_empty_without_classifying():
    today = date(2025, 4, 24)
    today_dt = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    ctx = _ctx(now=today_dt, position_rows=[
        {"tradingsymbol": "GOLD25APR75000CE", "exchange": "MCX", "quantity": 1},
    ])
    rows = _scope_positions_expiring_today_nfo(ctx)
    assert rows == []
