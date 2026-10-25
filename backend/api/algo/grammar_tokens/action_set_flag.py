"""Action resolver — `set_flag`: set a named runtime flag that other
agents can read via the condition grammar."""

from __future__ import annotations

from backend.api.algo.grammar_tokens._action_common import _log_invoke


async def set_flag(ctx, params: dict) -> dict:
    """No-op observability stub — logs the invocation and returns a
    status dict. The real named-flag-storage side effect is not wired
    here; this is a genuinely dispatched handler (see
    `_NOOP_ACTION_HANDLERS` in `actions.py`), not a dead resolver."""
    return _log_invoke("set_flag", params)
