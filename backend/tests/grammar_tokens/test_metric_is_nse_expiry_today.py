"""Tests for grammar_tokens/metric_is_nse_expiry_today.py —
`is_nse_expiry_today` metric (1.0 when a held NFO F&O position expires today).

Reference date 2025-04-24 is the last Thursday of April 2025 (NFO monthly expiry).
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_is_nse_expiry_today import _metric_is_nse_expiry_today

NOW = datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc)


def _ctx(position_rows=None) -> Context:
    return Context(position_rows=position_rows or [], now=NOW)


def test_returns_one_when_held_nfo_position_expires_today():
    ctx = _ctx([{"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO", "quantity": 50}])
    assert _metric_is_nse_expiry_today(ctx, {}) == 1.0


def test_returns_zero_when_no_position_rows():
    assert _metric_is_nse_expiry_today(_ctx([]), {}) == 0.0


def test_returns_zero_when_only_mcx_expiry_today():
    ctx = _ctx([{"tradingsymbol": "CRUDEOIL25APRFUT", "exchange": "MCX", "quantity": 1}])
    assert _metric_is_nse_expiry_today(ctx, {}) == 0.0, \
        "an MCX expiry must not trip the NFO token"


def test_returns_zero_when_nfo_position_expires_later():
    ctx = _ctx([{"tradingsymbol": "NIFTY25MAY22500CE", "exchange": "NFO", "quantity": 50}])
    assert _metric_is_nse_expiry_today(ctx, {}) == 0.0


def test_never_returns_none():
    for ctx in (_ctx([]), Context(now=NOW)):
        result = _metric_is_nse_expiry_today(ctx, {})
        assert result is not None, "expiry token must read 0.0, never None"
