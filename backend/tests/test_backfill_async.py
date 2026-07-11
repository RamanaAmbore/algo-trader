"""
test_backfill_async.py

Regression test: _task_warm_backfill must NOT call fetch_holdings /
fetch_positions directly on the event loop thread.  Both are synchronous
@for_all_accounts functions that make real broker HTTP round-trips; calling
them directly blocks the event loop for the entire startup warm window.

Fix: wrap in `await asyncio.to_thread(...)`.
"""

from __future__ import annotations

import inspect


def test_fetch_holdings_wrapped_in_to_thread():
    """fetch_holdings must be called via asyncio.to_thread in
    _task_warm_backfill, not called directly on the event loop thread.

    A direct call blocks the event loop for the full duration of all
    serial broker HTTP round-trips (~1-5 s per account × N accounts)
    during the 60 s startup warm window.
    """
    from backend.api.background import _task_warm_backfill

    src = inspect.getsource(_task_warm_backfill)

    assert "asyncio.to_thread(broker_apis.fetch_holdings)" in src, (
        "_task_warm_backfill must call `await asyncio.to_thread(broker_apis.fetch_holdings)` "
        "instead of `broker_apis.fetch_holdings()` directly. "
        "The direct call blocks the event loop for the entire duration of all "
        "broker HTTP round-trips during the 60 s startup warm window."
    )


def test_fetch_positions_wrapped_in_to_thread():
    """fetch_positions must be called via asyncio.to_thread in
    _task_warm_backfill, not called directly on the event loop thread.

    Same reasoning as fetch_holdings: it is a synchronous @for_all_accounts
    function with real broker HTTP calls; direct invocation blocks the loop.
    """
    from backend.api.background import _task_warm_backfill

    src = inspect.getsource(_task_warm_backfill)

    assert "asyncio.to_thread(broker_apis.fetch_positions)" in src, (
        "_task_warm_backfill must call `await asyncio.to_thread(broker_apis.fetch_positions)` "
        "instead of `broker_apis.fetch_positions()` directly. "
        "The direct call blocks the event loop for the entire duration of all "
        "broker HTTP round-trips during the 60 s startup warm window."
    )


def test_no_direct_blocking_broker_calls_in_warm_backfill():
    """Neither fetch_holdings() nor fetch_positions() should appear as a direct
    call (i.e. with parentheses immediately following the function name) in
    _task_warm_backfill.  Both must be mediated by asyncio.to_thread.
    """
    from backend.api.background import _task_warm_backfill
    import re

    src = inspect.getsource(_task_warm_backfill)

    # Match bare `broker_apis.fetch_holdings()` or `broker_apis.fetch_positions()`
    # without a preceding `to_thread(` on the same expression.
    direct_holdings  = re.search(r"broker_apis\.fetch_holdings\(\)", src)
    direct_positions = re.search(r"broker_apis\.fetch_positions\(\)", src)

    assert direct_holdings is None, (
        "Found `broker_apis.fetch_holdings()` called directly in "
        "_task_warm_backfill. Use `await asyncio.to_thread(broker_apis.fetch_holdings)` "
        "to avoid blocking the event loop."
    )
    assert direct_positions is None, (
        "Found `broker_apis.fetch_positions()` called directly in "
        "_task_warm_backfill. Use `await asyncio.to_thread(broker_apis.fetch_positions)` "
        "to avoid blocking the event loop."
    )
