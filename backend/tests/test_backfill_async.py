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
    _backfill_collect_holdings (extracted helper), not called directly on the
    event loop thread.

    A direct call blocks the event loop for the full duration of all
    serial broker HTTP round-trips (~1-5 s per account × N accounts)
    during the 60 s startup warm window.
    """
    from backend.api.background import _backfill_collect_holdings

    src = inspect.getsource(_backfill_collect_holdings)

    assert "asyncio.to_thread(broker_apis.fetch_holdings)" in src, (
        "_backfill_collect_holdings must call `await asyncio.to_thread(broker_apis.fetch_holdings)` "
        "instead of `broker_apis.fetch_holdings()` directly. "
        "The direct call blocks the event loop for the entire duration of all "
        "broker HTTP round-trips during the 60 s startup warm window."
    )


def test_fetch_positions_wrapped_in_to_thread():
    """fetch_positions must be called via asyncio.to_thread in
    _backfill_collect_positions (extracted helper), not called directly on the
    event loop thread.

    Same reasoning as fetch_holdings: it is a synchronous @for_all_accounts
    function with real broker HTTP calls; direct invocation blocks the loop.
    """
    from backend.api.background import _backfill_collect_positions

    src = inspect.getsource(_backfill_collect_positions)

    assert "asyncio.to_thread(broker_apis.fetch_positions)" in src, (
        "_backfill_collect_positions must call `await asyncio.to_thread(broker_apis.fetch_positions)` "
        "instead of `broker_apis.fetch_positions()` directly. "
        "The direct call blocks the event loop for the entire duration of all "
        "broker HTTP round-trips during the 60 s startup warm window."
    )


def test_no_direct_blocking_broker_calls_in_warm_backfill():
    """Neither fetch_holdings() nor fetch_positions() should appear as a direct
    call (i.e. with parentheses immediately following the function name) in
    _task_warm_backfill or its extracted helpers. Both must be mediated by
    asyncio.to_thread.
    """
    from backend.api.background import (
        _task_warm_backfill,
        _backfill_collect_holdings,
        _backfill_collect_positions,
    )
    import re

    # Check in the parent task (calls should be delegated to helpers)
    src_parent = inspect.getsource(_task_warm_backfill)
    assert re.search(r"broker_apis\.fetch_holdings\(\)", src_parent) is None, (
        "Found `broker_apis.fetch_holdings()` called directly in "
        "_task_warm_backfill. Use `await asyncio.to_thread(broker_apis.fetch_holdings)` "
        "to avoid blocking the event loop."
    )
    assert re.search(r"broker_apis\.fetch_positions\(\)", src_parent) is None, (
        "Found `broker_apis.fetch_positions()` called directly in "
        "_task_warm_backfill. Use `await asyncio.to_thread(broker_apis.fetch_positions)` "
        "to avoid blocking the event loop."
    )

    # Check that the helpers themselves use asyncio.to_thread (not direct calls)
    for fn, name in [
        (_backfill_collect_holdings, "fetch_holdings"),
        (_backfill_collect_positions, "fetch_positions"),
    ]:
        src = inspect.getsource(fn)
        assert re.search(rf"broker_apis\.{name}\(\)", src) is None, (
            f"Found `broker_apis.{name}()` called directly in {fn.__name__}. "
            f"Use `await asyncio.to_thread(broker_apis.{name})` "
            f"to avoid blocking the event loop."
        )
