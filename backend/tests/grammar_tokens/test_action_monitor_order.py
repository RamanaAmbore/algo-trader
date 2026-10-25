"""Tests for grammar_tokens/action_monitor_order.py — `monitor_order`
action resolver.

Fresh coverage, written as part of Wave 1 (DSL token-unification plan,
Part A — actions). No dedicated unit test of the resolver's OWN return
value existed before this migration — `test_actions_coverage.py`'s
`test_al_run_noop_handler_monitor_order` only exercises the DISPATCH
mechanism (patches `monitor_order` as a mock and checks it was called),
which stays in place unchanged.
"""

import pytest

from backend.api.algo.grammar_tokens.action_monitor_order import monitor_order


@pytest.mark.asyncio
async def test_returns_logged_status_dict_with_params():
    params = {"account": "Z1234", "broker_order_id": "999", "timeout_minutes": 5}
    result = await monitor_order({}, params)
    assert result == {"action": "monitor_order", "status": "logged", "params": params}


@pytest.mark.asyncio
async def test_logs_the_invocation():
    """`monitor_order` is the no-op `_log_invoke("monitor_order", ...)`
    wrapper — confirmed via the real (non-skip-listed) logger name this
    shares with `actions.py`'s own module logger."""
    from backend.api.algo.grammar_tokens._action_common import logger

    calls = []
    old_info = logger.info
    logger.info = lambda msg: calls.append(msg)
    try:
        await monitor_order({}, {"account": "Z1234"})
    finally:
        logger.info = old_info
    assert any("monitor_order" in line for line in calls)


def test_logger_is_the_same_singleton_as_actions_module():
    """`get_logger("backend.api.algo.actions")` is a process-wide
    singleton keyed by name — confirms this resolver's log lines carry
    the SAME logger identity `actions.py`'s own module logger does, so
    `log_store.py:tags_for()`'s logger-name tag fallback is unchanged
    by this file's relocation out of `actions.py`."""
    from backend.api.algo import actions
    from backend.api.algo.grammar_tokens._action_common import logger

    assert logger is actions.logger
