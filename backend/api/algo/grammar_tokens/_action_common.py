"""Shared private helper for the trivial noop action resolvers
(`monitor_order` / `deactivate_agent` / `set_flag`) — NOT itself a
catalog token, so it carries no `grammar_tokens` test file of its own
(mirrors `grammar.py`'s own precedent of keeping small cross-token
helpers like `_num_or_none`/`_parsed_or_none` in one place that the
real token files import from).

Deliberately has ZERO imports from `backend.api.algo.actions` — these
three resolver files import this module at their own top level, and
`actions.py` re-exports all three functions back into its own
namespace (for `_HandlerRef`/`patch()` backward compatibility); a
reverse import here would reproduce the exact circular-import deadlock
that pattern is designed to avoid.
"""

from __future__ import annotations

from backend.shared.helpers.ramboq_logger import get_logger

# Pinned to the original "backend.api.algo.actions" name (NOT this
# module's own __name__) — `logging.getLogger(name)` is a process-wide
# singleton keyed by name, so this is the IDENTICAL Logger object
# `actions.py`'s own `logger` already is. Preserves every existing
# test's `monkeypatch.setattr(actions.logger, ...)` /
# `patch("backend.api.algo.actions.logger")` call, and keeps
# `log_store.py:tags_for()`'s logger-name tag fallback unchanged for
# these three resolvers' log lines.
logger = get_logger("backend.api.algo.actions")


def _log_invoke(action: str, params: dict) -> dict:
    """Generic no-op action body: log the invocation, return a status
    dict. Shared by `monitor_order`, `deactivate_agent`, and `set_flag`
    — none of these three action types do anything beyond being
    observable in the log/agent_events pipeline."""
    logger.info(f"Agent action invoked: {action} params={params}")
    return {"action": action, "status": "logged", "params": params}
