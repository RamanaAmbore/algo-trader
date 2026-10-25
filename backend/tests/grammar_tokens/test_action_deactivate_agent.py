"""Tests for grammar_tokens/action_deactivate_agent.py —
`deactivate_agent` action resolver.

Fresh coverage, written as part of Wave 1 (DSL token-unification plan,
Part A — actions). No dedicated unit test of the resolver's OWN return
value existed before this migration — `test_actions_coverage.py`'s
`test_al_run_noop_handler_deactivate_agent` only exercises the DISPATCH
mechanism (patches `deactivate_agent` as a mock), which stays unchanged.
"""

import pytest

from backend.api.algo.grammar_tokens.action_deactivate_agent import deactivate_agent


@pytest.mark.asyncio
async def test_returns_logged_status_dict_with_empty_params():
    """params_schema is `{}` — this action takes no real parameters."""
    result = await deactivate_agent({}, {})
    assert result == {"action": "deactivate_agent", "status": "logged", "params": {}}


@pytest.mark.asyncio
async def test_tolerates_unexpected_params_without_raising():
    result = await deactivate_agent({}, {"unexpected": True})
    assert result["action"] == "deactivate_agent"
    assert result["params"] == {"unexpected": True}
