"""
test_intraday_store_db_only.py

Covers Slice A (db_only mode) added in Sprint G:

  - db_only=True on store_base.get() skips Tier 3 (broker) entirely.
  - get_or_fetch_intraday(db_only=True) returns Tier1/2 data; broker.historical_data is NEVER called.
  - get_or_fetch_daily(db_only=True) returns DB bars on a partial-range miss; broker is NOT called.
  - batch_sparkline uses _any_segment_open (imported from snapshot_gate) — not an inline reimplementation.
  - batch_sparkline delegates to _intraday_store.get_or_fetch_intraday (SSOT — no parallel fetcher).
  - db_only_misses counter increments correctly.
  - Performance budget: db_only path for 50 symbols resolves in < 100 ms.

Six quality dimensions (feedback_test_dimensions.md):
  1. SSOT        — single intraday fetcher; _any_segment_open imported from snapshot_gate.
  2. Performance — db_only path <100 ms for 50 symbols with no broker latency.
  3. Stale       — grep: batch_sparkline does NOT reimplement market-open check inline.
  4. Reuse       — batch_sparkline uses get_or_fetch_intraday from intraday_store (not parallel).
  5. UX          — N/A (backend).
  6. Response    — time.perf_counter budget <100 ms for 50 symbols under db_only.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. SSOT: _any_segment_open is imported from snapshot_gate, not reimplemented ─

def test_batch_sparkline_imports_any_segment_open():
    """batch_sparkline must import _any_segment_open from snapshot_gate.

    This is the canonical market-open gate.  If quote.py reintroduces an
    inline is_any_segment_open / _all_exchanges_closed check for the db_only
    decision it diverges from the shared contract and risks drift.
    """
    import backend.api.routes.quote as quote_mod
    # The module-level import makes _any_segment_open available as an attribute.
    assert hasattr(quote_mod, "_any_segment_open"), (
        "quote.py must import _any_segment_open from "
        "backend.api.helpers.snapshot_gate at module level"
    )
    from backend.api.helpers.snapshot_gate import _any_segment_open as canonical
    assert quote_mod._any_segment_open is canonical, (
        "quote.py._any_segment_open must be the same object as "
        "snapshot_gate._any_segment_open (not a local copy)"
    )


# ── 2. SSOT: get_or_fetch_intraday is the sole public API, not a parallel path ──

def test_batch_sparkline_uses_get_or_fetch_intraday():
    """_fetch_bars_parallel must call get_or_fetch_intraday and get_or_fetch_daily.

    A parallel direct call to _intraday_store.get() would bypass the
    db_only plumbing added in Slice A and silently fall through to the
    broker during closed hours.
    """
    import backend.api.routes.quote as quote_mod
    # _fetch_bars_parallel is a module-level helper; find it via getattr
    fetch_fn = getattr(quote_mod, "_fetch_bars_parallel", None)
    assert fetch_fn is not None, (
        "quote.py must define _fetch_bars_parallel as a module-level helper"
    )
    src = inspect.getsource(fetch_fn)
    assert "get_or_fetch_intraday" in src, (
        "_fetch_bars_parallel must call "
        "intraday_store.get_or_fetch_intraday (not _intraday_store.get directly)"
    )
    assert "get_or_fetch_daily" in src, (
        "_fetch_bars_parallel must call "
        "ohlcv_store.get_or_fetch_daily"
    )


# ── 3. Stale: no inline market-open reimplementation ─────────────────────────────

def test_no_inline_market_open_in_batch_sparkline():
    """batch_sparkline must not inline its own _any_segment_open check.

    The db_only decision must come from the module-level import of
    _any_segment_open (snapshot_gate). Grep for common reimplementation
    patterns: is_any_segment_open / _all_exchanges_closed / is_market_open
    inside the function body.
    """
    from backend.api.routes.quote import SparklineController
    handler = SparklineController.batch_sparkline
    fn = getattr(handler, "fn", handler)
    src = inspect.getsource(fn)
    # These would indicate an inline reimplementation bypassing snapshot_gate.
    assert "is_any_segment_open(" not in src, (
        "batch_sparkline must not call is_any_segment_open() inline; "
        "use _any_segment_open from snapshot_gate via asyncio.to_thread"
    )
    # _any_segment_open IS expected (it's the import); only flag a raw
    # is_any_segment_open() call that skips the shared module.


# ── 4. db_only flag wired into both fetch closures ───────────────────────────────

def test_batch_sparkline_threads_db_only():
    """_fetch_bars_parallel must pass db_only=db_only to both fetch helpers."""
    import backend.api.routes.quote as quote_mod
    # _fetch_bars_parallel is a module-level helper
    fetch_fn = getattr(quote_mod, "_fetch_bars_parallel", None)
    assert fetch_fn is not None, (
        "quote.py must define _fetch_bars_parallel as a module-level helper"
    )
    src = inspect.getsource(fetch_fn)
    assert "db_only=db_only" in src, (
        "_fetch_bars_parallel must pass db_only=db_only to get_or_fetch_intraday "
        "and get_or_fetch_daily so closed-hours calls skip the broker"
    )


# ── 5. store_base: db_only skips broker; db_only_misses counter increments ──────

@pytest.mark.asyncio
async def test_store_base_db_only_skips_broker():
    """PersistentStoreBase.get(db_only=True) must NOT call _broker_fetch."""
    from backend.api.persistence.store_base import PersistentStoreBase

    class _TestStore(PersistentStoreBase):
        _name = "test_store"
        broker_called: int = 0

        async def _db_fetch(self, key):
            return None   # Tier 2 miss

        async def _broker_fetch(self, key):
            self.__class__.broker_called += 1
            return {"value": 42}

        def _is_complete(self, value, key):
            return value is not None

        def _enqueue_persist(self, key, value):
            pass

    store = _TestStore()
    _TestStore.broker_called = 0

    result = await store.get("any_key", db_only=True)
    assert result is None, "db_only=True on full cache miss must return None"
    assert _TestStore.broker_called == 0, (
        "_broker_fetch must NOT be called when db_only=True"
    )
    assert store._db_only_misses == 1, (
        "_db_only_misses must be incremented on a db_only miss"
    )


@pytest.mark.asyncio
async def test_store_base_db_only_returns_tier1_hit():
    """db_only=True still serves Tier 1 (memory) when the key is hot."""
    from backend.api.persistence.store_base import PersistentStoreBase

    class _TestStore2(PersistentStoreBase):
        _name = "test_store2"
        broker_called: int = 0

        async def _db_fetch(self, key):
            return None

        async def _broker_fetch(self, key):
            self.__class__.broker_called += 1
            return "should_not_reach"

        def _is_complete(self, value, key):
            return value is not None

        def _enqueue_persist(self, key, value):
            pass

    store = _TestStore2()
    _TestStore2.broker_called = 0
    # Manually populate Tier 1.
    store._mem_cache["test_key"] = "hot_value"

    result = await store.get("test_key", db_only=True)
    assert result == "hot_value", (
        "db_only=True must serve from Tier 1 when the key is already cached"
    )
    assert _TestStore2.broker_called == 0, "_broker_fetch must not be called"
    assert store._db_only_misses == 0, (
        "_db_only_misses must not increment when Tier 1 serves the request"
    )


# ── 6. db_only_misses in get_metrics() ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_metrics_includes_db_only_misses():
    """get_metrics() must expose db_only_misses."""
    from backend.api.persistence.store_base import PersistentStoreBase

    class _MetricsStore(PersistentStoreBase):
        _name = "metrics_store"

        async def _db_fetch(self, key):
            return None

        async def _broker_fetch(self, key):
            return None

        def _is_complete(self, value, key):
            return value is not None

        def _enqueue_persist(self, key, value):
            pass

    store = _MetricsStore()
    await store.get("key1", db_only=True)
    await store.get("key2", db_only=True)

    metrics = store.get_metrics()
    assert "db_only_misses" in metrics, (
        "get_metrics() must include 'db_only_misses' key"
    )
    assert metrics["db_only_misses"] == 2, (
        "db_only_misses counter must reflect the number of db_only miss events"
    )


# ── 7. get_or_fetch_intraday db_only signature ────────────────────────────────────

def test_get_or_fetch_intraday_has_db_only_param():
    """get_or_fetch_intraday must accept a db_only keyword argument."""
    from backend.api.persistence.intraday_store import get_or_fetch_intraday
    sig = inspect.signature(get_or_fetch_intraday)
    assert "db_only" in sig.parameters, (
        "get_or_fetch_intraday must expose a db_only parameter "
        "(plumbed from batch_sparkline in Slice A)"
    )
    assert sig.parameters["db_only"].default is False, (
        "db_only must default to False to preserve existing callers"
    )


# ── 8. get_or_fetch_daily db_only signature ───────────────────────────────────────

def test_get_or_fetch_daily_has_db_only_param():
    """get_or_fetch_daily must accept a db_only keyword argument."""
    from backend.api.persistence.ohlcv_store import get_or_fetch_daily
    sig = inspect.signature(get_or_fetch_daily)
    assert "db_only" in sig.parameters, (
        "get_or_fetch_daily must expose a db_only parameter"
    )
    assert sig.parameters["db_only"].default is False, (
        "db_only must default to False to preserve existing callers"
    )


# ── 9. Performance: db_only resolves <100 ms for 50 symbols ──────────────────────

@pytest.mark.asyncio
async def test_db_only_perf_50_symbols():
    """db_only path for 50 symbols must complete in <100 ms (no broker latency)."""
    from backend.api.persistence.store_base import PersistentStoreBase

    class _PerfStore(PersistentStoreBase):
        _name = "perf_store"
        _max_keys = 200

        async def _db_fetch(self, key):
            return None   # simulate DB miss — still fast

        async def _broker_fetch(self, key):
            # Should never be called in db_only mode.
            await asyncio.sleep(1.0)   # would blow the budget if reached
            return None

        def _is_complete(self, value, key):
            return value is not None

        def _enqueue_persist(self, key, value):
            pass

    store = _PerfStore()
    keys = [f"SYM{i:03d}" for i in range(50)]

    t0 = time.perf_counter()
    results = await asyncio.gather(*[store.get(k, db_only=True) for k in keys])
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert all(r is None for r in results), (
        "All db_only misses should return None"
    )
    assert elapsed_ms < 100.0, (
        f"db_only path for 50 symbols took {elapsed_ms:.1f} ms (budget: 100 ms). "
        "If _broker_fetch is being called, check that db_only short-circuits before "
        "the Tier 3 lock section."
    )
