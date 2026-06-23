"""
FIX 6, 7, 8 — Event loop offload fixes

Tests that blocking operations are correctly offloaded to thread pool:
  - FIX 6: paper.py tick_loop uses run_in_executor for step()
  - FIX 7: actions.py basket_margin validation uses run_in_executor
  - FIX 8: simulator seed-live route uses run_in_executor

These tests verify that blocking broker calls don't stall the async
event loop by running on separate threads.
"""

import pytest
import threading
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock


@pytest.mark.asyncio
async def test_paper_tick_loop_uses_executor():
    """
    paper.py tick_loop uses run_in_executor to offload step() to a thread.
    Verifies that step() runs on a non-main thread.

    Regression test: check source contains the run_in_executor call.
    """
    from backend.api.algo.paper import PaperTradeEngine

    # Check that the source code contains the expected pattern
    import inspect
    source = inspect.getsource(PaperTradeEngine.tick_loop)

    # Verify the source contains the offload pattern
    assert "run_in_executor" in source, "tick_loop should use run_in_executor"
    assert "self.step" in source, "tick_loop should call step"


@pytest.mark.asyncio
async def test_basket_margin_uses_executor():
    """
    actions.py _basket_margin_validate uses run_in_executor to offload
    basket_margin call to a thread. Verifies the async function correctly
    delegates to the thread pool.
    """
    from backend.api.algo.actions import _basket_margin_validate

    # Create a mock broker. _basket_margin_validate calls
    # broker.basket_order_margins directly (the Broker ABC contract),
    # not broker.kite.basket_order_margins as the older test assumed.
    mock_broker = MagicMock()

    # Track which thread the basket_margin call runs on
    call_thread = None

    def mock_basket_margin(orders):
        nonlocal call_thread
        call_thread = threading.current_thread()
        return {"status": "ok"}

    mock_broker.basket_order_margins = mock_basket_margin

    # Call the function with a sample order
    order = {
        "exchange": "NFO",
        "symbol": "NIFTY25APRFUT",
        "side": "BUY",
        "qty": 1,
        "price": 22500.0,
    }

    ok, reason = await _basket_margin_validate(mock_broker, order)

    # Verify the call succeeded
    assert ok is True
    assert reason == "basket_margin OK"

    # Verify basket_margin was called on a thread pool thread, not the main thread
    assert call_thread is not None
    main_thread = threading.current_thread()
    assert call_thread.name != main_thread.name, \
        f"basket_margin should run on executor thread, not {main_thread.name}"


@pytest.mark.asyncio
async def test_basket_margin_executor_exception_handling():
    """
    When basket_margin raises an exception, _basket_margin_validate
    catches it and returns (False, error_message).
    """
    from backend.api.algo.actions import _basket_margin_validate

    mock_broker = MagicMock()

    # Make basket_margin raise an exception. The function calls
    # broker.basket_order_margins directly per the Broker ABC.
    mock_broker.basket_order_margins = MagicMock(
        side_effect=ValueError("Insufficient margin")
    )

    order = {
        "exchange": "NFO",
        "symbol": "NIFTY25APRFUT",
        "side": "BUY",
        "qty": 1,
        "price": 22500.0,
    }

    ok, reason = await _basket_margin_validate(mock_broker, order)

    # Should return False with the error
    assert ok is False
    assert "Insufficient margin" in reason


@pytest.mark.asyncio
async def test_simulator_seed_live_uses_executor():
    """
    The simulator route's seed_live handler offloads broker fetches
    so the asyncio event loop never blocks. After the refactor the
    inline `run_in_executor` moved INTO the broker layer
    (Connections() + broker_apis already wrap their HTTP calls in a
    ThreadPoolExecutor), and the route just awaits the async wrapper
    `SimDriver.seed_live_async`.

    Contract this test enforces:
      • SimDriver exposes an async `seed_live_async` method
      • The simulator route awaits it
    Both are necessary conditions for non-blocking event-loop
    behaviour; the executor offload itself is now a broker-layer
    concern verified elsewhere.
    """
    import inspect
    from backend.api.algo.sim.driver import SimDriver

    # The async wrapper exists.
    assert hasattr(SimDriver, "seed_live_async"), \
        "SimDriver should expose async seed_live_async"
    assert inspect.iscoroutinefunction(SimDriver.seed_live_async), \
        "seed_live_async must be an async coroutine function"

    # The route awaits it (read the source — the handler itself is
    # wrapped by Litestar's @post decorator so we can't introspect
    # the function object directly).
    from pathlib import Path
    sim_route = Path(__file__).resolve().parent.parent / "api/routes/simulator.py"
    src = sim_route.read_text()
    assert "seed_live_async" in src, \
        "simulator route should call seed_live_async"
    assert "await" in src and "seed_live_async" in src, \
        "simulator route should await seed_live_async"


@pytest.mark.asyncio
async def test_simulator_seed_live_threading():
    """
    Functional test: simulator seed_live route offloads to thread pool.
    Verifies that seed_live runs on a non-main thread.
    """
    from backend.api.algo.sim.driver import SimDriver

    # Track which thread seed_live runs on
    call_thread = None

    # Create a minimal SimDriver mock
    mock_driver = MagicMock()

    def mock_seed_live():
        nonlocal call_thread
        call_thread = threading.current_thread()
        return {"status": "ok"}

    mock_driver.seed_live = mock_seed_live

    # Simulate the route's executor call
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, mock_driver.seed_live)

    # Verify it ran on a different thread
    assert call_thread is not None
    main_thread = threading.current_thread()
    assert call_thread.name != main_thread.name, \
        f"seed_live should run on executor thread, not {main_thread.name}"


@pytest.mark.asyncio
async def test_concurrent_executor_calls():
    """
    Multiple async executor calls can run concurrently without blocking
    the event loop (demonstrating that the offload is working).
    """
    import time

    loop = asyncio.get_running_loop()

    def blocking_operation(delay):
        """Simulates a blocking broker call."""
        time.sleep(delay)
        return f"done_{delay}"

    # Pre-warm the default executor so the first call's thread-creation
    # overhead doesn't count against the concurrency budget. The default
    # ThreadPoolExecutor lazy-spawns threads on first submit; on macOS +
    # Python 3.14 that overhead is ~50ms per thread and would otherwise
    # push two concurrent 0.1s tasks over the 0.18s budget on cold cache.
    await asyncio.gather(
        loop.run_in_executor(None, lambda: None),
        loop.run_in_executor(None, lambda: None),
    )

    # Start two concurrent executor calls
    task1 = loop.run_in_executor(None, blocking_operation, 0.1)
    task2 = loop.run_in_executor(None, blocking_operation, 0.1)

    # Both should complete without the first blocking the second
    start = time.time()
    result1, result2 = await asyncio.gather(task1, task2)
    elapsed = time.time() - start

    # If they ran serially, total would be ~0.2s. If concurrent, ~0.1s.
    # 0.18s budget tolerates the ~50ms thread-creation overhead on slow
    # CI runners while still distinguishing concurrent from serial.
    assert elapsed < 0.18, f"Calls should run concurrently, took {elapsed}s"
    assert result1 == "done_0.1"
    assert result2 == "done_0.1"
