"""Action resolver — `monitor_order`: poll an order until it fills or
times out, then trigger on_fill / on_timeout actions."""

from __future__ import annotations

from backend.api.algo.grammar_tokens._action_common import _log_invoke


async def monitor_order(ctx, params: dict) -> dict:
    """No-op observability stub — logs the invocation and returns a
    status dict. The real poll-until-fill/timeout behaviour this token
    describes is not wired to a broker call; this is a genuinely
    dispatched handler (see `_NOOP_ACTION_HANDLERS` in `actions.py`),
    not a dead resolver."""
    return _log_invoke("monitor_order", params)
