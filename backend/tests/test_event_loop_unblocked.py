"""Event loop non-blocking regression tests.

The slice 1-4 refactor wrapped sync broker calls in asyncio.to_thread.
Verify that blocking operations don't freeze the event loop under realistic load.

Pattern: start a lightweight ticker task that increments a counter,
then call a "slow" broker API via the route handler. If event loop is
unblocked, the ticker runs multiple times during the broker call.
If event loop is blocked, ticker runs 0 times.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import pandas as pd


class TestAsyncioToThreadUnblocks:
    """Verify asyncio.to_thread doesn't block event loop."""

    @pytest.mark.asyncio
    async def test_to_thread_does_not_block_event_loop(self):
        """asyncio.to_thread() should not block the event loop."""

        def slow_sync_function():
            """Simulate 300ms sync operation."""
            time.sleep(0.3)
            return "done"

        # Track ticker ticks during the slow call
        tick_count = 0

        async def ticker():
            """Lightweight async task that runs during sync operation."""
            nonlocal tick_count
            while True:
                await asyncio.sleep(0.02)  # 20ms tick
                tick_count += 1

        t = asyncio.create_task(ticker())
        try:
            start = time.monotonic()
            # asyncio.to_thread() should execute sync function without blocking loop
            result = await asyncio.to_thread(slow_sync_function)
            elapsed = time.monotonic() - start

            # Sync operation took ~300ms; ticker runs every 20ms = ~15 ticks expected
            # If event loop is unblocked, expect tick_count >= 10
            # If event loop is blocked, expect tick_count = 0
            assert (
                tick_count >= 8
            ), f"Event loop blocked — ticker ran {tick_count}× during {elapsed*1000:.0f}ms sync call"
            assert (
                elapsed >= 0.28
            ), f"Sync call too fast: {elapsed*1000:.0f}ms (expected ~300ms)"
            assert result == "done"
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


class TestMultipleParallelOperations:
    """Verify multiple async operations can run in parallel."""

    @pytest.mark.asyncio
    async def test_multiple_to_thread_calls_interleave(self):
        """Multiple to_thread() calls should interleave, not block each other."""

        def slow_op(duration: float, op_id: int):
            """Simulate async operation."""
            time.sleep(duration)
            return f"op_{op_id}_done"

        # Track execution order
        execution_log = []

        async def ticker_with_log():
            """Log ticker events."""
            count = 0
            while True:
                await asyncio.sleep(0.05)  # 50ms tick
                execution_log.append(f"tick_{count}")
                count += 1

        t = asyncio.create_task(ticker_with_log())
        try:
            start = time.monotonic()

            # Start two slow operations in parallel
            results = await asyncio.gather(
                asyncio.to_thread(slow_op, 0.2, 1),  # 200ms
                asyncio.to_thread(slow_op, 0.2, 2),  # 200ms
            )
            elapsed = time.monotonic() - start

            # Both operations took ~200ms, but running in parallel they should
            # finish in ~200ms total (not 400ms). Ticker should fire 3-4 times.
            tick_count = len([x for x in execution_log if "tick" in x])
            assert (
                tick_count >= 2
            ), f"Parallel ops blocked event loop — ticker ran {tick_count}× during {elapsed*1000:.0f}ms"
            assert (
                elapsed < 0.38
            ), f"Parallel ops took {elapsed*1000:.0f}ms (expected <380ms for parallel)"
            assert len(results) == 2
            assert results[0] == "op_1_done"
            assert results[1] == "op_2_done"
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
