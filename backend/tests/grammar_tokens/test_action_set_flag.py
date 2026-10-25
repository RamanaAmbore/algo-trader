"""Tests for grammar_tokens/action_set_flag.py — `set_flag` action
resolver.

Fresh coverage, written as part of Wave 1 (DSL token-unification plan,
Part A — actions). No dedicated unit test of the resolver's OWN return
value existed before this migration — `test_actions_coverage.py`'s
`test_al_run_noop_handler_set_flag` only exercises the DISPATCH
mechanism (patches `set_flag` as a mock), which stays unchanged.
"""

import pytest

from backend.api.algo.grammar_tokens.action_set_flag import set_flag


@pytest.mark.asyncio
async def test_returns_logged_status_dict_with_name_and_value():
    params = {"name": "my_flag", "value": True}
    result = await set_flag({}, params)
    assert result == {"action": "set_flag", "status": "logged", "params": params}
