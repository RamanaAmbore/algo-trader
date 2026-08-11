"""Tests for the with_guard decorator in backend/shared/helpers/decorators.py."""
import asyncio
import pytest
from backend.shared.helpers.decorators import with_guard


@pytest.mark.asyncio
async def test_second_call_skipped_while_in_flight():
    """A concurrent second call returns None while the first is still running."""
    barrier = asyncio.Event()
    call_count = 0

    @with_guard
    async def slow_fn():
        nonlocal call_count
        call_count += 1
        await barrier.wait()
        return call_count

    task1 = asyncio.create_task(slow_fn())
    await asyncio.sleep(0)  # yield so task1 starts
    result2 = await slow_fn()  # in-flight → should be dropped

    barrier.set()
    result1 = await task1

    assert result2 is None
    assert call_count == 1
    assert result1 == 1


@pytest.mark.asyncio
async def test_guard_resets_after_completion():
    """Sequential calls both execute — guard resets between invocations."""
    results = []

    @with_guard
    async def fn():
        results.append(len(results) + 1)
        return results[-1]

    r1 = await fn()
    r2 = await fn()

    assert r1 == 1
    assert r2 == 2
    assert results == [1, 2]


@pytest.mark.asyncio
async def test_guard_resets_on_exception():
    """Guard resets to False even when the decorated function raises."""
    call_count = 0

    @with_guard
    async def fn():
        nonlocal call_count
        call_count += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await fn()

    # Guard must be reset — second call should execute, not be skipped.
    with pytest.raises(ValueError):
        await fn()

    assert call_count == 2


@pytest.mark.asyncio
async def test_independent_state_across_wrappers():
    """Two separate @with_guard-wrapped functions have independent flags."""
    barrier = asyncio.Event()
    count_a = 0
    count_b = 0

    @with_guard
    async def fn_a():
        nonlocal count_a
        count_a += 1
        await barrier.wait()

    @with_guard
    async def fn_b():
        nonlocal count_b
        count_b += 1

    task_a = asyncio.create_task(fn_a())
    await asyncio.sleep(0)

    # fn_b has its own flag — must not be blocked by fn_a's in-flight state.
    await fn_b()

    barrier.set()
    await task_a

    assert count_a == 1
    assert count_b == 1


@pytest.mark.asyncio
async def test_return_value_passes_through():
    """The decorated function's return value reaches the caller unchanged."""

    @with_guard
    async def fn():
        return {"key": "value", "num": 42}

    result = await fn()
    assert result == {"key": "value", "num": 42}


@pytest.mark.asyncio
async def test_concurrent_gather_only_one_executes():
    """asyncio.gather with two concurrent calls — only the first runs."""
    call_count = 0

    @with_guard
    async def fn():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return call_count

    results = await asyncio.gather(fn(), fn())

    # One returns the value; the other returns None (dropped).
    assert sorted([r for r in results if r is not None]) == [1]
    assert None in results
    assert call_count == 1
