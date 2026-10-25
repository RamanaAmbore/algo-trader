"""Action resolver — `deactivate_agent`: pause the agent that fired —
useful for one-shot safety rules."""

from __future__ import annotations

from backend.api.algo.grammar_tokens._action_common import _log_invoke


async def deactivate_agent(ctx, params: dict) -> dict:
    """No-op observability stub — logs the invocation and returns a
    status dict. Real pause-the-agent side effects are not wired here;
    this is a genuinely dispatched handler (see `_NOOP_ACTION_HANDLERS`
    in `actions.py`), not a dead resolver. `params_schema` is `{}` —
    this action takes no parameters."""
    return _log_invoke("deactivate_agent", params)
