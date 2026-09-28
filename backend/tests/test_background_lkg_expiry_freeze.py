"""Tests for `_preload_db_lkg_cache`'s expiry-freeze hardening
(background.py, 2026-09-29 GOLDM incident sibling fix).

Bug: the confirmed production defect was `positions.py:_fetch()`'s LIVE
broker-fetch path having no expiry filter at all (see
test_positions_route.py's "_filter_expired_live_rows / _fetch()" section
and test_expiry_freeze.py's `is_live_row_past_freeze_window` tests for the
primary fix). `_preload_db_lkg_cache` (background.py) shares the identical
unfiltered-assumption gap — it loads each account's latest `daily_book`
batch with no expiry awareness at all — so it gets the same defensive
guard, even though there is no live evidence this path is currently firing
the bug (a future failed/skipped fetch interval could otherwise silently
substitute an expired-past-freeze-window row into the LKG fallback).

Five quality dimensions:
  SSOT        — reuses expiry_freeze.is_live_row_past_freeze_window, the
                same predicate driving positions.py's live-fetch filter —
                no parallel expiry-classification logic here.
  Correctness — an expired-past-freeze-window 'positions' row is excluded
                from the DataFrame handed to set_db_lkg_frame; a
                currently-valid row and 'holdings' rows are unaffected.
  Performance — the predicate is checked once per UNIQUE (symbol,
                exchange) pair among 'positions' rows only (verified via
                call-count assertion), not once per raw row.
  Reuse       — no duplicate holiday/expiry logic in background.py.
  UX          — a cold-restart LKG substitution for Dhan/Groww never
                resurrects an already-obsolete expired contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _row(account, kind, symbol, exchange, qty=1, avg_cost=100.0, ltp=100.0,
         day_pnl=0.0, total_pnl=0.0, captured_at=None):
    return SimpleNamespace(
        account=account, kind=kind, symbol=symbol, exchange=exchange,
        qty=qty, avg_cost=avg_cost, ltp=ltp, day_pnl=day_pnl,
        total_pnl=total_pnl, captured_at=captured_at,
    )


def _mock_session_with_rows(rows):
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    result = AsyncMock()
    result.all = lambda: rows
    mock_session.execute = AsyncMock(return_value=result)
    return mock_session


@pytest.mark.asyncio
async def test_preload_lkg_excludes_expired_past_freeze_positions_row():
    """A 'positions' row for a past-freeze-window symbol must never reach
    set_db_lkg_frame; a currently-valid 'positions' row must."""
    from backend.api import background as background_module

    rows = [
        _row("ZJ6294", "positions", "INFY", "NSE", qty=50, avg_cost=2400.0, ltp=2500.0),
        _row("ZJ6294", "positions", "GOLDM26SEP148000PE", "MCX", qty=1,
             avg_cost=850.0, ltp=5.0, total_pnl=-69860.0),
    ]
    mock_session = _mock_session_with_rows(rows)

    captured = {}

    def _fake_set_db_lkg_frame(kind, account, df):
        captured[(kind, account)] = df

    async def _fake_past_freeze(symbol, exchange, now_ist):
        return symbol == "GOLDM26SEP148000PE"

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.expiry_freeze.is_live_row_past_freeze_window",
               side_effect=_fake_past_freeze), \
         patch("backend.brokers.broker_apis.set_db_lkg_frame",
               side_effect=_fake_set_db_lkg_frame):
        await background_module._preload_db_lkg_cache()

    df = captured[("positions", "ZJ6294")]
    syms = set(df["tradingsymbol"])
    assert "GOLDM26SEP148000PE" not in syms, (
        "expired-past-freeze-window row must be excluded from the DB-LKG "
        "preload cache"
    )
    assert "INFY" in syms, "currently-valid row must still be preloaded"


@pytest.mark.asyncio
async def test_preload_lkg_holdings_rows_unaffected():
    """'holdings' rows are never expiry-checked (kind != 'positions') —
    equity holdings symbols would resolve False anyway via the predicate's
    own fast path, but the preload loop explicitly scopes the filter to
    kind == 'positions' only."""
    from backend.api import background as background_module

    rows = [
        _row("ZJ6294", "holdings", "TCS", "NSE", qty=10, avg_cost=3000.0, ltp=3100.0),
    ]
    mock_session = _mock_session_with_rows(rows)

    captured = {}

    def _fake_set_db_lkg_frame(kind, account, df):
        captured[(kind, account)] = df

    call_count = {"n": 0}

    async def _fake_past_freeze(symbol, exchange, now_ist):
        call_count["n"] += 1
        return False

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.expiry_freeze.is_live_row_past_freeze_window",
               side_effect=_fake_past_freeze), \
         patch("backend.brokers.broker_apis.set_db_lkg_frame",
               side_effect=_fake_set_db_lkg_frame):
        await background_module._preload_db_lkg_cache()

    assert call_count["n"] == 0, (
        "holdings rows must never be routed through the expiry-freeze "
        "predicate — only 'positions' rows are"
    )
    assert "TCS" in set(captured[("holdings", "ZJ6294")]["tradingsymbol"])


@pytest.mark.asyncio
async def test_preload_lkg_dedupes_expiry_check_per_unique_pair():
    """Two accounts holding the same expired symbol trigger exactly one
    predicate check for that (symbol, exchange) pair, not once per row."""
    from backend.api import background as background_module

    rows = [
        _row("ZJ6294", "positions", "GOLDM26SEP148000PE", "MCX", qty=1, total_pnl=-69860.0),
        _row("ZG0790", "positions", "GOLDM26SEP148000PE", "MCX", qty=2, total_pnl=-1200.0),
    ]
    mock_session = _mock_session_with_rows(rows)

    call_count = {"n": 0}

    async def _fake_past_freeze(symbol, exchange, now_ist):
        call_count["n"] += 1
        return True

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.expiry_freeze.is_live_row_past_freeze_window",
               side_effect=_fake_past_freeze), \
         patch("backend.brokers.broker_apis.set_db_lkg_frame") as mock_set_frame:
        await background_module._preload_db_lkg_cache()

    assert call_count["n"] == 1, (
        f"expected exactly 1 predicate check for the single unique "
        f"(symbol, exchange) pair shared across 2 accounts, got {call_count['n']}"
    )
    # Both accounts' positions frames end up empty (their only row was
    # filtered) — set_db_lkg_frame must not be called for either since it
    # no-ops on empty frames (see its own docstring).
    called_keys = {(c.args[0], c.args[1]) for c in mock_set_frame.call_args_list}
    assert ("positions", "ZJ6294") not in called_keys
    assert ("positions", "ZG0790") not in called_keys


@pytest.mark.asyncio
async def test_preload_lkg_expiry_check_failure_fails_open():
    """If the expiry-freeze check itself fails (e.g. holiday-calendar DB
    hiccup), the preload must fail OPEN — proceed with the unfiltered
    rows rather than losing the entire LKG preload."""
    from backend.api import background as background_module

    rows = [
        _row("ZJ6294", "positions", "INFY", "NSE", qty=50, avg_cost=2400.0, ltp=2500.0),
    ]
    mock_session = _mock_session_with_rows(rows)

    captured = {}

    def _fake_set_db_lkg_frame(kind, account, df):
        captured[(kind, account)] = df

    async def _raising_past_freeze(symbol, exchange, now_ist):
        raise RuntimeError("holiday calendar DB hiccup")

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.algo.expiry_freeze.is_live_row_past_freeze_window",
               side_effect=_raising_past_freeze), \
         patch("backend.brokers.broker_apis.set_db_lkg_frame",
               side_effect=_fake_set_db_lkg_frame):
        await background_module._preload_db_lkg_cache()

    assert "INFY" in set(captured[("positions", "ZJ6294")]["tradingsymbol"]), (
        "a failed expiry-freeze check must fail open — the preload must "
        "still proceed with the unfiltered rows"
    )
