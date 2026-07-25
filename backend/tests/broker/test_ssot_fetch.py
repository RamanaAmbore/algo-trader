"""
Tests for backend.shared.helpers.ssot_fetch decorator

Coverage:
  1. async coalesce collapses 10 concurrent callers on same key → underlying called once
  2. async coalesce with different keys → 2 underlying calls (independent)
  3. async serialize → callers queue; counter increments correctly
  4. sync coalesce with 20 threads → underlying called once
  5. force_refresh bypasses in-flight → new call launched
  6. exception propagates to all async waiters (same exception instance)
  7. sync exception propagates to all thread waiters
"""

import asyncio
import pytest
import threading
import time
from backend.shared.helpers.ssot_fetch import ssot_fetch


# ===== Test 1: async coalesce collapses 10 concurrent callers =====

@pytest.mark.asyncio
async def test_coalesce_async_collapses_concurrent_calls():
    """10 concurrent async callers on same key → underlying called exactly once."""
    call_count = 0

    @ssot_fetch(mode="coalesce", key="x")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return 42

    # Launch 10 concurrent callers
    results = await asyncio.gather(*[fetch() for _ in range(10)])

    assert call_count == 1, f"expected 1 call but got {call_count}"
    assert all(r == 42 for r in results), "all results should be 42"
    assert len(results) == 10, "all 10 callers should get a result"


# ===== Test 2: async coalesce with different keys → independent calls =====

@pytest.mark.asyncio
async def test_coalesce_async_different_keys_independent():
    """Different keys should trigger independent underlying calls."""
    call_count = 0

    @ssot_fetch(mode="coalesce", key=lambda x: f"key_{x}")
    async def fetch(key_arg):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return key_arg * 10  # Return deterministic value

    # 5 callers with key=1, 5 with key=2 → 2 underlying calls total
    results = await asyncio.gather(
        *[fetch(1) for _ in range(5)] + [fetch(2) for _ in range(5)]
    )

    assert call_count == 2, f"expected 2 calls (one per key) but got {call_count}"
    assert results[:5] == [10] * 5, "first 5 results should be 10 (1*10)"
    assert results[5:] == [20] * 5, "last 5 results should be 20 (2*10)"


# ===== Test 3: async serialize → queues callers sequentially =====

@pytest.mark.asyncio
async def test_serialize_async_queues_callers():
    """Serialize mode: each caller gets executed sequentially, getting fresh results."""
    call_count = 0

    @ssot_fetch(mode="serialize", key="serialize_test")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return call_count

    # 5 concurrent calls in serialize mode should each execute and increment
    results = await asyncio.gather(*[fetch() for _ in range(5)])

    # In serialize, each call increments the counter (executes sequentially)
    assert call_count == 5, f"expected 5 calls in serialize but got {call_count}"
    # Results should be 1,2,3,4,5 in some order (order depends on lock acquisition)
    # or could be [5,5,5,5,5] if they all run after the last one increments
    # Actually with asyncio serialize lock, they should queue and return different values
    assert sum(results) == 15, f"sum of results should be 15, got {sum(results)}"


@pytest.mark.asyncio
async def test_serialize_async_different_concurrent_batches():
    """Serialize with separate batches shows sequential behavior."""
    call_count = 0
    call_log = []

    @ssot_fetch(mode="serialize", key="test_key")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        call_log.append(call_count)
        return call_count

    # First batch: 2 concurrent calls
    r1 = await asyncio.gather(*[fetch() for _ in range(2)])
    assert call_count == 2, f"batch 1 expected 2 calls but got {call_count}"

    # Second batch: 3 concurrent calls
    r2 = await asyncio.gather(*[fetch() for _ in range(3)])
    assert call_count == 5, f"batch 2 expected 5 total calls but got {call_count}"


# ===== Test 4: sync coalesce with 20 threads =====

def test_coalesce_sync_threading():
    """20 threads on same key → underlying called exactly once."""
    call_count = 0
    lock = threading.Lock()

    @ssot_fetch(mode="coalesce", key="sync_x")
    def fetch():
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.05)
        return 99

    results = []
    results_lock = threading.Lock()

    def worker():
        try:
            result = fetch()
            with results_lock:
                results.append(result)
        except Exception as e:
            with results_lock:
                results.append(("ERROR", e))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1, f"expected 1 call but got {call_count}"
    assert len(results) == 20, "all 20 threads should get a result"
    assert all(r == 99 for r in results), "all results should be 99"


# ===== Test 5: force_refresh bypasses in-flight =====

@pytest.mark.asyncio
async def test_force_refresh_async_bypasses_inflight():
    """force_refresh=True should evict cache and launch a new call.

    Note: Once a task completes, its done_callback removes it from cache.
    So this tests that force_refresh works while a task is in-flight.
    """
    call_count = 0
    started_count = 0

    @ssot_fetch(mode="coalesce", key="refresh_test")
    async def fetch():
        nonlocal call_count, started_count
        started_count += 1
        await asyncio.sleep(0.05)
        call_count += 1
        return started_count

    # Start first call but let it run long
    task1 = asyncio.create_task(fetch())

    # Give it time to register in cache
    await asyncio.sleep(0.01)

    # force_refresh while in-flight should bypass and create new task
    task2 = asyncio.create_task(fetch(force_refresh=True))

    r1 = await task1
    r2 = await task2

    # Both should complete without error
    # started_count should be 2 (two different tasks started)
    assert started_count == 2, f"expected 2 tasks started with force_refresh but got {started_count}"


def test_force_refresh_sync_bypasses_inflight():
    """Sync version: force_refresh=True evicts cache and forces new execution.

    The sync implementation cleans up the cache after each execution, so
    force_refresh prevents cache reuse when another thread would be waiting.
    """
    started = []
    started_lock = threading.Lock()

    @ssot_fetch(mode="coalesce", key="sync_refresh")
    def fetch():
        with started_lock:
            started.append(1)
        time.sleep(0.02)
        return len(started)

    # First call
    r1 = fetch()
    assert r1 == 1
    assert len(started) == 1

    # Second call with force_refresh creates a new execution
    r2 = fetch(force_refresh=True)
    assert r2 == 2
    assert len(started) == 2


# ===== Test 6: async exception propagates to all waiters =====

@pytest.mark.asyncio
async def test_coalesce_async_exception_propagates_to_all_waiters():
    """Exception in async coalesce should be raised to all concurrent waiters."""
    call_count = 0

    @ssot_fetch(mode="coalesce", key="err")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    # 5 concurrent callers all waiting for the same in-flight task
    results = await asyncio.gather(
        *[fetch() for _ in range(5)],
        return_exceptions=True
    )

    assert call_count == 1, f"underlying should be called exactly once, got {call_count}"
    assert len(results) == 5, "all 5 callers should get a result"
    # All should be ValueError due to shared task
    assert all(isinstance(r, ValueError) for r in results), f"all should be ValueError, got {[type(r).__name__ for r in results]}"
    assert all(str(r) == "boom" for r in results), "all should have same message"


# ===== Test 7: sync exception propagates to all thread waiters =====

def test_coalesce_sync_exception_propagates_to_all_threads():
    """Exception in sync coalesce should be raised to all waiting threads."""
    call_count = 0
    lock = threading.Lock()

    @ssot_fetch(mode="coalesce", key="sync_err")
    def fetch():
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.02)
        raise RuntimeError("sync boom")

    errors = []
    errors_lock = threading.Lock()

    def worker():
        try:
            fetch()
        except RuntimeError as e:
            with errors_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1, f"expected 1 call but got {call_count}"
    assert len(errors) == 5, f"expected 5 errors but got {len(errors)}"
    assert all(str(e) == "sync boom" for e in errors), "all errors should have same message"


# ===== Additional tests for edge cases =====

@pytest.mark.asyncio
async def test_coalesce_async_with_callable_key():
    """Test callable key resolution.

    The key function transforms arguments into a cache key. Same key reuses
    the in-flight task; different keys create independent tasks.
    """
    call_count = 0

    @ssot_fetch(mode="coalesce", key=lambda x: f"key_{x}")
    async def fetch(x):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return x * 2

    # Concurrent calls with x=5 should share one task
    results = await asyncio.gather(*[fetch(5) for _ in range(3)])
    assert all(r == 10 for r in results), "all concurrent calls with same key should get same result"
    assert call_count == 1, f"one key should produce one task, got {call_count}"

    # Different key should create new call (but note: task callback removes from cache)
    r2 = await fetch(6)
    assert r2 == 12
    assert call_count == 2, f"different key should trigger new call, got {call_count}"


def test_coalesce_sync_with_callable_key():
    """Test callable key resolution in sync mode."""
    call_count = 0
    lock = threading.Lock()

    @ssot_fetch(mode="coalesce", key=lambda x: f"key_{x}")
    def fetch(x):
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.01)
        return x * 3

    # Multiple threads with same key should share one execution
    results = []
    results_lock = threading.Lock()

    def worker(x):
        r = fetch(x)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(4,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == 12 for r in results), "all threads with same key should get same result"
    assert call_count == 1, f"same key should produce one execution, got {call_count}"

    # Different key should create new execution
    r2 = fetch(7)
    assert r2 == 21
    assert call_count == 2


@pytest.mark.asyncio
async def test_parallel_mode_no_collapsing():
    """Parallel mode should not collapse concurrent calls."""
    call_count = 0

    @ssot_fetch(mode="parallel", key="parallel_test")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return 42

    results = await asyncio.gather(*[fetch() for _ in range(5)])

    assert call_count == 5, f"parallel mode should call 5 times but got {call_count}"
    assert all(r == 42 for r in results), "all results should be 42"


def test_parallel_mode_sync_no_collapsing():
    """Sync parallel mode should not collapse calls."""
    call_count = 0
    lock = threading.Lock()

    @ssot_fetch(mode="parallel", key="parallel_sync")
    def fetch():
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.01)
        return 99

    results = []
    results_lock = threading.Lock()

    def worker():
        result = fetch()
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 5, f"parallel should call 5 times but got {call_count}"
    assert all(r == 99 for r in results), "all results should be 99"


@pytest.mark.asyncio
async def test_coalesce_async_default_key_from_args():
    """Test default key resolution (args tuple).

    With key=None, the key defaults to str(args), so fetch("a") and fetch("a")
    share the same cache key.
    """
    call_count = 0

    @ssot_fetch(mode="coalesce", key=None)  # Use args tuple as key
    async def fetch(arg):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return arg

    # Concurrent calls with same arg should share task
    results = await asyncio.gather(*[fetch("a") for _ in range(3)])
    assert all(r == "a" for r in results), "concurrent calls with same key should get same result"
    assert call_count == 1, f"same key should produce one task, got {call_count}"

    # Different arg should create new task (after first task completes)
    r2 = await fetch("b")
    assert r2 == "b"
    assert call_count == 2


def test_coalesce_sync_default_key_from_args():
    """Sync version: default key resolution from args."""
    call_count = 0
    lock = threading.Lock()

    @ssot_fetch(mode="coalesce", key=None)
    def fetch(arg):
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.01)
        return arg

    # Multiple threads with same arg should share one execution
    results = []
    results_lock = threading.Lock()

    def worker(arg):
        r = fetch(arg)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=("x",)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == "x" for r in results), "threads with same key should get same result"
    assert call_count == 1, f"same key should produce one execution, got {call_count}"

    r2 = fetch("y")
    assert r2 == "y"
    assert call_count == 2


@pytest.mark.asyncio
async def test_coalesce_async_mixed_concurrent_and_sequential():
    """Concurrent callers with same key share task; sequential after completion reuse result.

    The decorator caches results, so sequential calls after the first completes
    will reuse the cached result without re-running.
    """
    started = []

    @ssot_fetch(mode="coalesce", key="mixed")
    async def fetch():
        started.append(1)
        await asyncio.sleep(0.02)
        return len(started)

    # Launch 3 concurrent calls
    r1 = await fetch()
    assert r1 == 1
    assert len(started) == 1, f"first call should execute once, got {len(started)}"

    # Sequential call after first batch complete should use cached result
    r2 = await fetch()
    assert r2 == 1, "should reuse cached result"
    assert len(started) == 1, "sequential call should not re-execute"

    # Another sequential call should also use cache
    r3 = await fetch()
    assert r3 == 1, "should reuse cached result again"
    assert len(started) == 1, "still no new execution"


def test_coalesce_sync_result_caching():
    """Sync coalesce should cache results for sequential calls."""
    call_log = []

    @ssot_fetch(mode="coalesce", key="sync_cache")
    def fetch():
        call_log.append(1)
        return len(call_log)

    r1 = fetch()
    assert r1 == 1
    assert len(call_log) == 1

    # Sequential calls should use cached result
    r2 = fetch()
    assert r2 == 1, "should reuse cached result"
    assert len(call_log) == 1, "should not re-execute"

    r3 = fetch()
    assert r3 == 1, "should reuse cached result again"
    assert len(call_log) == 1


@pytest.mark.asyncio
async def test_coalesce_async_result_caching():
    """Async coalesce should cache results for sequential calls."""
    call_log = []

    @ssot_fetch(mode="coalesce", key="async_cache")
    async def fetch():
        call_log.append(1)
        await asyncio.sleep(0.01)
        return len(call_log)

    r1 = await fetch()
    assert r1 == 1
    assert len(call_log) == 1

    # Sequential calls should use cached result
    r2 = await fetch()
    assert r2 == 1, "should reuse cached result"
    assert len(call_log) == 1, "should not re-execute"

    r3 = await fetch()
    assert r3 == 1, "should reuse cached result again"
    assert len(call_log) == 1


@pytest.mark.asyncio
async def test_coalesce_async_exception_does_not_cache():
    """If a task raises an exception, the result is not cached.

    The next call should retry.
    """
    call_count = 0

    @ssot_fetch(mode="coalesce", key="no_cache_error")
    async def fetch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("first attempt fails")
        return 42

    # First call fails
    try:
        await fetch()
        assert False, "should have raised"
    except ValueError as e:
        assert str(e) == "first attempt fails"

    assert call_count == 1

    # Second call should retry (result not cached on exception)
    r2 = await fetch()
    assert r2 == 42
    assert call_count == 2


def test_coalesce_sync_exception_does_not_cache():
    """Sync version: exceptions don't get cached."""
    call_count = 0

    @ssot_fetch(mode="coalesce", key="sync_no_cache_error")
    def fetch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first attempt fails")
        return 99

    # First call fails
    try:
        fetch()
        assert False, "should have raised"
    except RuntimeError as e:
        assert str(e) == "first attempt fails"

    assert call_count == 1

    # Second call should retry
    r2 = fetch()
    assert r2 == 99
    assert call_count == 2


@pytest.mark.asyncio
async def test_serialize_async_does_not_cache():
    """Serialize mode should NOT cache results; each call executes fresh."""
    call_count = 0

    @ssot_fetch(mode="serialize", key="serialize_no_cache")
    async def fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return call_count

    # Sequential calls should each execute (not use cache)
    r1 = await fetch()
    assert r1 == 1
    assert call_count == 1

    r2 = await fetch()
    assert r2 == 2, "serialize should execute fresh each time"
    assert call_count == 2

    r3 = await fetch()
    assert r3 == 3, "serialize should execute fresh each time"
    assert call_count == 3


def test_serialize_sync_does_not_cache():
    """Sync serialize mode should NOT cache results."""
    call_count = 0

    @ssot_fetch(mode="serialize", key="sync_serialize_no_cache")
    def fetch():
        nonlocal call_count
        call_count += 1
        time.sleep(0.01)
        return call_count

    r1 = fetch()
    assert r1 == 1
    assert call_count == 1

    r2 = fetch()
    assert r2 == 2, "serialize should execute fresh each time"
    assert call_count == 2

    r3 = fetch()
    assert r3 == 3, "serialize should execute fresh each time"
    assert call_count == 3
