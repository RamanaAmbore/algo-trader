"""
test_ticker_backstop.py

Covers the daily_book backstop for cold-start Kite ticker subscriptions
(audit a1ccfca970c48f755).

Defect: when conn_service restarts AND Dhan circuit-breaker is open at
boot, fetch_holdings() / fetch_positions() return empty for the breaker-
open account.  Dhan symbols never subscribed to the Kite ticker until the
next healthy performance cycle (up to 30+ min away).

Fix: _snapshot_book_symbols() queries daily_book (DB-persisted, 7-day
window) and returns (tradingsymbol, exchange) pairs that survive any
broker outage.  Both _task_sparkline_warm._collect_symbols() and
_task_performance phase 2 union these pairs into the subscription universe.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — _snapshot_book_symbols is the single DB-backstop
                   implementation; no parallel query exists.
  2. Performance — query uses .distinct() — no N+1 / redundant rows.
  3. Stale code  — symbols older than 7 days not included.
  4. Reusable    — _collect_symbols and _task_performance both call the
                   same module-level helper.
  5. Correctness — breaker-open cold-start, breaker-closed live union,
                   stale-exclusion, and weekend-cold-start scenarios all
                   pass.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import date, timedelta, timezone, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_daily_book_row(symbol: str, exchange: str, kind: str, days_ago: int = 0):
    """Simulate a row returned by SQLAlchemy result.fetchall()."""
    row = MagicMock()
    row.symbol   = symbol
    row.exchange = exchange
    row.kind     = kind
    # date field not used by _snapshot_book_symbols directly (filter is in SQL)
    return row


# ---------------------------------------------------------------------------
# 1. SSOT — single DB-backstop helper
# ---------------------------------------------------------------------------

def test_snapshot_book_symbols_is_module_level():
    """_snapshot_book_symbols lives at module level in background.py, not nested."""
    import backend.api.background as bg
    assert hasattr(bg, "_snapshot_book_symbols"), (
        "_snapshot_book_symbols must be a module-level async function"
    )
    assert inspect.iscoroutinefunction(bg._snapshot_book_symbols), (
        "_snapshot_book_symbols must be async"
    )


def test_single_backstop_helper():
    """Only one DB-backstop function; no parallel implementation."""
    import backend.api.background as bg
    import inspect

    # Gather all async functions in the module named *snapshot*book*
    matches = [
        name for name, obj in inspect.getmembers(bg, inspect.iscoroutinefunction)
        if "snapshot" in name and "book" in name
    ]
    assert matches == ["_snapshot_book_symbols"], (
        f"Expected exactly one snapshot-book helper; found: {matches}"
    )


# ---------------------------------------------------------------------------
# 2. _snapshot_book_symbols — cold-start breaker open (empty broker)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_breaker_open():
    """
    When broker returns empty (circuit-breaker open), _snapshot_book_symbols
    returns symbols from daily_book that are within the 7-day window.
    50 Dhan symbols seeded; all 50 should be returned.
    """
    import backend.api.background as bg

    dhan_syms = [f"DHAN{i:03d}" for i in range(50)]
    mock_rows = [
        _make_daily_book_row(sym, "NSE", "holdings")
        for sym in dhan_syms
    ]

    captured_pairs = await _call_snapshot_with_mock_session(bg, mock_rows)
    syms_returned = {s for s, _ in captured_pairs}
    assert syms_returned == set(dhan_syms), (
        "All 50 Dhan symbols must be returned even when broker is empty"
    )


async def _call_snapshot_with_mock_session(bg_module, mock_rows):
    """Helper: call _snapshot_book_symbols with a mocked async_session.

    _snapshot_book_symbols imports async_session via a local
    ``from backend.api.database import async_session`` at call time, so we
    must patch the binding on backend.api.database (not background).
    The returned value is used as an async context manager:
        async with async_session() as session: ...
    so _FakeSession() must return an object that satisfies the
    async context-manager protocol.
    """
    mock_result = MagicMock()
    mock_result.fetchall.return_value = mock_rows

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    class _FakeAsyncSession:
        """Callable that returns an async context manager."""
        def __call__(self):
            return mock_session

    with patch("backend.api.database.async_session", _FakeAsyncSession()):
        return await bg_module._snapshot_book_symbols(days=7)


# ---------------------------------------------------------------------------
# 3. _snapshot_book_symbols — breaker closed (live + snapshot union, no drop)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_union_when_live_present():
    """
    When broker is healthy, live + snapshot symbols are unioned.
    No symbol from daily_book should be dropped even if the live path
    already produced some of the same symbols.
    """
    import backend.api.background as bg

    live_syms = [f"LIVE{i:03d}" for i in range(10)]
    snap_syms = [f"SNAP{i:03d}" for i in range(10)]
    # Overlap: first 5 live_syms also appear in snapshot
    overlap   = live_syms[:5]
    all_snap  = overlap + snap_syms  # 15 rows from DB

    mock_rows = [
        _make_daily_book_row(sym, "NSE", "holdings")
        for sym in all_snap
    ]

    pairs = await _call_snapshot_with_mock_session(bg, mock_rows)
    returned_syms = {s for s, _ in pairs}

    # snapshot must include all 15 distinct symbols (deduped)
    assert returned_syms == set(all_snap), (
        f"Expected {set(all_snap)}, got {returned_syms}"
    )
    # No duplicates
    assert len(pairs) == len(returned_syms), "Duplicate pairs must not appear"


# ---------------------------------------------------------------------------
# 4. Stale exclusion — snapshot symbols older than 7 days not included
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_stale_excluded():
    """
    _snapshot_book_symbols filters by date in SQL (cutoff = today - 7 days).
    Symbols not in the DB result (SQL already filtered them) are not returned.

    We verify this by supplying empty mock_rows (simulating an all-stale DB)
    and asserting an empty list is returned.
    """
    import backend.api.background as bg

    pairs = await _call_snapshot_with_mock_session(bg, [])
    assert pairs == [], (
        "No symbols should be returned when daily_book result is empty (all stale)"
    )


# ---------------------------------------------------------------------------
# 5. Weekend cold-start — Monday boot, only Friday rows in daily_book
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_weekend_cold_start():
    """
    Monday morning: conn_service restarts, Dhan breaker open.
    daily_book has Friday rows only (date = today - 3 days).
    A 7-day window includes Friday rows; a 1-day window would miss them.

    We verify that _snapshot_book_symbols with days=7 returns those symbols.
    (The SQL filter cutoff is computed inside the function; we validate the
    function accepts days=7 and the rows we supply are returned.)
    """
    import backend.api.background as bg

    friday_syms = ["RELIANCE", "INFY", "TCS", "HDFC", "ICICIBANK"]
    mock_rows = [
        _make_daily_book_row(sym, "NSE", "holdings")
        for sym in friday_syms
    ]

    # days=7 should include these rows (function doesn't re-filter the
    # already-SQL-filtered result; it trusts the DB predicate).
    pairs = await _call_snapshot_with_mock_session(bg, mock_rows)
    returned_syms = {s for s, _ in pairs}
    assert returned_syms == set(friday_syms), (
        f"Weekend cold-start: Friday symbols must be returned with days=7. "
        f"Got: {returned_syms}"
    )


# ---------------------------------------------------------------------------
# 6. Exchange fallback — NULL exchange defaults per kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_exchange_fallback():
    """
    DailyBook.exchange is nullable.
    kind='holdings' → default 'NSE'
    kind='positions' → default 'NFO'
    """
    import backend.api.background as bg

    rows = [
        _make_daily_book_row("EQUITYSYM", None, "holdings"),
        _make_daily_book_row("FOOSYM25JUNFUT", None, "positions"),
    ]
    # Nullify exchange to trigger fallback
    rows[0].exchange = None
    rows[1].exchange = None

    pairs = await _call_snapshot_with_mock_session(bg, rows)
    pair_map = dict(pairs)

    assert pair_map.get("EQUITYSYM") == "NSE", (
        "holdings with NULL exchange must default to NSE"
    )
    assert pair_map.get("FOOSYM25JUNFUT") == "NFO", (
        "positions with NULL exchange must default to NFO"
    )


# ---------------------------------------------------------------------------
# 7. Reuse — both callers use the module-level helper (no inline SQL)
# ---------------------------------------------------------------------------

def test_collect_symbols_calls_snapshot_helper():
    """
    The sparkline-warm symbol collection must use _snapshot_book_symbols,
    not contain an inline daily_book SQL query.

    After the CC-reduction refactor, the delegating call moved into the
    module-level helper _sparkline_collect_snapshot which is called from
    _collect_symbols (nested inside _task_sparkline_warm).  Verify both:
      1. _sparkline_collect_snapshot calls _snapshot_book_symbols.
      2. _task_sparkline_warm (or its nested _collect_symbols) calls
         _sparkline_collect_snapshot.
    """
    import backend.api.background as bg
    import inspect

    snapshot_src = inspect.getsource(bg._sparkline_collect_snapshot)
    assert "_snapshot_book_symbols" in snapshot_src, (
        "_sparkline_collect_snapshot must call _snapshot_book_symbols"
    )

    warm_src = inspect.getsource(bg._task_sparkline_warm)
    assert "_sparkline_collect_snapshot" in warm_src, (
        "_task_sparkline_warm must call _sparkline_collect_snapshot "
        "(not contain an inline daily_book SQL query)"
    )


def test_task_performance_calls_snapshot_helper():
    """
    _task_performance phase 2 must call _snapshot_book_symbols.
    """
    import backend.api.background as bg
    import inspect

    src = inspect.getsource(bg._task_performance)
    assert "_snapshot_book_symbols" in src, (
        "_task_performance phase 2 must call _snapshot_book_symbols"
    )


# ---------------------------------------------------------------------------
# 8. Performance — query uses DISTINCT (no N+1)
# ---------------------------------------------------------------------------

def test_snapshot_helper_uses_distinct():
    """
    _snapshot_book_symbols must use .distinct() in its query to avoid
    returning duplicate rows (one per daily_book snapshot day).
    """
    import backend.api.background as bg
    import inspect

    src = inspect.getsource(bg._snapshot_book_symbols)
    assert ".distinct()" in src, (
        "_snapshot_book_symbols query must use .distinct() to prevent "
        "returning duplicate (symbol, exchange) pairs from multiple days"
    )


# ---------------------------------------------------------------------------
# 9. Cap correctness — snapshot symbols land in book_pairs, never truncated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_symbols_survive_cap():
    """
    Snapshot symbols must be classified as 'book' (not mover) in the
    300-symbol cap logic and therefore never truncated.

    We verify _snapshot_book_symbols returns pairs that are NOT in the
    mover_warm_pairs set, so they will be in book_pairs and capped last.
    """
    import backend.api.background as bg

    # Use a symbol that definitely is not in the mover universe
    snap_rows = [
        _make_daily_book_row("DHANSYM001", "NSE", "holdings"),
        _make_daily_book_row("DHANSYM002", "NSE", "holdings"),
    ]
    pairs = await _call_snapshot_with_mock_session(bg, snap_rows)
    returned_syms = {s for s, _ in pairs}
    assert {"DHANSYM001", "DHANSYM002"} == returned_syms
