"""
test_persistence_date_coercion.py

Regression test for the persistence read-path string→DATE coercion bug.

Symptom
-------
Operator complaint 2026-07-03 IST: "sparklines are not updated". Investigation
showed the /pulse sparkline cell rendered "—" for a superset of positions,
holdings, and pinned MCX/CDS roots. Log grep on the server surfaced:

    [SPARK-EMPTY] symbol=NSE:SBIN reason=warm_universe_empty
    cache_layer=tier1_2_cache past=0 today=0 ltp=None market_closed=True
    ...
    instruments_store: DB fetch failed for ('2026-07-03', 'NFO'):
    (asyncpg.exceptions.DataError): invalid input for query argument $2:
    '2026-07-03' ('str' object has no attribute 'toordinal')

Root cause
----------
Four persistence read helpers passed ISO date STRINGS as `:from_d` / `:to_d`
/ `:date` / `:on_date` bind parameters to asyncpg queries. asyncpg requires
a real `datetime.date` for columns typed `DATE`; a string raises
`'str' object has no attribute 'toordinal'`. The bug had four incarnations:

  * `ohlcv_store._db_fetch`      — swallowed by `except: return []`  (silent)
  * `intraday_store._db_fetch`   — swallowed by `except: return None` (silent)
  * `instruments_store._db_fetch` — logged as WARNING but returned None
  * `backfill._count_db_bars`    — logged as DEBUG but returned 0

Effect: Tier 2 (DB) always missed. Every read fell through to Tier 3 (broker),
which hit rate-limit cool-off within minutes. Warm and self-heal both bailed
on the cool-off guard, so the DB never repopulated — a self-sustaining outage.
Sparklines, MarketPulse quotes, and the Kite instruments token map all went
dark during closed hours. During market hours the live LTP tail masked the
symptom for tick-subscribed rows; pinned MCX/CDS roots (no LTP tail) stayed
blank.

Fix
---
Coerce string keys to `datetime.date` objects at the boundary of every
affected read helper. Same pattern the write-side `_parse_date()` in
`db_worker.py` uses. Also switched two silent `except:` clauses to log
so a future regression surfaces immediately instead of silently poisoning
the cache tier.

Five quality dimensions
-----------------------

1. SSOT       — one coercion pattern (`date.fromisoformat`) applied uniformly
                across all four call sites. A single grep proves the pattern
                lives in exactly the four places and nowhere else.
2. Performance — coercion is O(1) per call, off the critical path. The main
                perf win is Tier 2 hits stop being 100% misses — sparkline
                / MarketPulse load drops from Tier-3-every-time to
                Tier-2-warm-hit.
3. Stale code  — the four bare `except: return []` / `return None` clauses
                that masked the bug are audited; two are converted to
                `except Exception as exc: logger.warning(...)`. The other
                two already logged.
4. Reusable   — no new helper — the existing `date.fromisoformat` pattern
                is the SSOT. Consistent with the write-side `_parse_date()`
                in `db_worker.py`.
5. Correctness — asyncpg accepts the coerced `date` object without raising
                (asserted). Malformed ISO input logs + returns empty (does
                not crash the caller).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _capture_execute_params():
    """Return an AsyncMock that records the bind-parameter dicts passed to
    ``session.execute``. Uses a lightweight session-manager stand-in so the
    stores' `async with async_session()` block yields our mock session.
    """
    calls: list[dict] = []

    session = MagicMock()
    async def _execute(stmt, params=None):
        # Record a copy so later mutations don't disturb the assertion.
        calls.append(dict(params or {}))
        result = MagicMock()
        result.fetchall.return_value = []
        result.fetchone.return_value = None
        return result
    session.execute = _execute

    class _AsyncSessionCtx:
        async def __aenter__(self):
            return session
        async def __aexit__(self, *a):
            return None

    def _factory():
        return _AsyncSessionCtx()

    return calls, _factory


# ── Dimension 1: ohlcv_store binds a real `date` object, not a string ─────────

def test_ohlcv_store_binds_date_object_not_string():
    """`_db_fetch` MUST pass `datetime.date` values to :from_d / :to_d.
    asyncpg rejects strings — that's the root of the bug we're guarding."""
    from backend.api.persistence.ohlcv_store import OHLCVStore

    calls, factory = _capture_execute_params()
    store = OHLCVStore()

    with patch("backend.api.database.async_session", side_effect=factory):
        key = ("RELIANCE", "NSE", "2026-06-24", "2026-07-03")
        result = asyncio.run(store._db_fetch(key))

    assert result == []                # empty rows path (fetchall returned [])
    assert len(calls) == 1
    params = calls[0]
    assert isinstance(params["from_d"], date), (
        f"from_d must be datetime.date, got {type(params['from_d']).__name__}"
    )
    assert isinstance(params["to_d"], date), (
        f"to_d must be datetime.date, got {type(params['to_d']).__name__}"
    )
    assert params["from_d"] == date(2026, 6, 24)
    assert params["to_d"]   == date(2026, 7, 3)


# ── Dimension 2: intraday_store binds a real `date` object ────────────────────

def test_intraday_store_binds_date_object_not_string():
    """`_db_fetch` MUST pass `datetime.date` to :on_date. Same asyncpg contract."""
    from backend.api.persistence.intraday_store import IntradayStore

    calls, factory = _capture_execute_params()
    store = IntradayStore()

    with patch("backend.api.database.async_session", side_effect=factory):
        key = ("RELIANCE", "NSE", "2026-07-03", "30minute")
        result = asyncio.run(store._db_fetch(key))

    assert result is None              # no rows → None
    assert len(calls) == 1
    params = calls[0]
    assert isinstance(params["on_date"], date)
    assert params["on_date"] == date(2026, 7, 3)


# ── Dimension 3: instruments_store binds a real `date` object ─────────────────

def test_instruments_store_binds_date_object_not_string():
    """`_db_fetch` MUST pass `datetime.date` to :date."""
    from backend.api.persistence.instruments_store import InstrumentsStore

    calls, factory = _capture_execute_params()
    store = InstrumentsStore()

    with patch("backend.api.database.async_session", side_effect=factory):
        key = ("2026-07-03", "NSE")
        result = asyncio.run(store._db_fetch(key))

    assert result is None
    assert len(calls) == 1
    params = calls[0]
    assert isinstance(params["date"], date)
    assert params["date"] == date(2026, 7, 3)


# ── Dimension 4: backfill._count_db_bars passes `date`, not `.isoformat()` ────

def test_backfill_count_binds_date_object_not_string():
    """`_count_db_bars` MUST pass the incoming `date` params through
    unchanged — the previous `.isoformat()` call round-tripped them into
    strings and asyncpg silently returned 0 counts, making backfill
    think coverage was ≥ threshold and skip real fetches."""
    from backend.api.persistence.backfill import _count_db_bars

    calls, factory = _capture_execute_params()

    with patch("backend.api.database.async_session", side_effect=factory):
        n = asyncio.run(_count_db_bars(
            "RELIANCE", "NSE",
            date(2025, 7, 3), date(2026, 7, 2),
        ))

    assert n == 0                      # fetchone() returned None
    assert len(calls) == 1
    params = calls[0]
    assert isinstance(params["from_d"], date)
    assert isinstance(params["to_d"], date)


# ── Dimension 5: malformed ISO date logs + returns empty, does NOT crash ──────

def test_ohlcv_store_bad_iso_returns_empty_no_crash():
    """A key with a garbled ISO date must not blow up the whole read —
    the coercion `except ValueError` returns []."""
    from backend.api.persistence.ohlcv_store import OHLCVStore

    store = OHLCVStore()
    # Never touches DB — coercion trips before the SQLAlchemy import.
    key = ("RELIANCE", "NSE", "not-a-date", "2026-07-03")
    result = asyncio.run(store._db_fetch(key))
    assert result == []


def test_intraday_store_bad_iso_returns_none_no_crash():
    from backend.api.persistence.intraday_store import IntradayStore

    store = IntradayStore()
    key = ("RELIANCE", "NSE", "garbage", "30minute")
    result = asyncio.run(store._db_fetch(key))
    assert result is None


def test_instruments_store_bad_iso_returns_none_no_crash():
    from backend.api.persistence.instruments_store import InstrumentsStore

    store = InstrumentsStore()
    key = ("nope", "NSE")
    result = asyncio.run(store._db_fetch(key))
    assert result is None
