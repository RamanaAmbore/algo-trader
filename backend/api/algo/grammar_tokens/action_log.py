"""Action resolver — `log`: write a message to the app log, with
optional alert-routing metadata for any downstream event agent that
matches it.

File named after the DSL token (`log`), not the Python function
(`emit_log`) — the dotted resolver path does not have to match the DSL
token string (same precedent as `action_help.py`'s `help_lookup`,
below). The function keeps its original name `emit_log` (renamed from
`emit_log` -> `log` 2026-10-23 at the DSL-dispatch-key level only — see
the docstring body for the full history).
"""

from __future__ import annotations

from backend.shared.helpers.ramboq_logger import get_logger

# Pinned to the original "backend.api.algo.actions" name (NOT this
# module's own __name__) — same singleton-by-name reasoning as
# `_action_common.py`. Preserves every existing test's
# `monkeypatch.setattr(actions.logger, ...)` and keeps
# `log_store.py:tags_for()`'s logger-name tag fallback unchanged.
logger = get_logger("backend.api.algo.actions")


async def emit_log(ctx, params: dict) -> dict:
    """Resolver for the DSL `log` action (dispatch key renamed from
    `emit_log` -> `log` 2026-10-23 — operator wants simple/small DSL
    words; this Python function keeps its original name, since a
    resolver's dotted path does not have to match its DSL token name,
    same precedent as `help`/`help_lookup`).

    `level` stays mostly informational; the real downstream-routing
    signal is the new `priority` param — reused by `event_agents.py`'s
    `_rec_tracked_ntfy_priority()` as an explicit override of a matching
    event agent's own tier-derived ntfy priority default (same "producer
    decides what happened, the agent decides who gets told and how"
    split documented in ALERT_AGENTS_DESIGN.md, and the same `extra`-dict
    threading mechanism `agent_engine.py:_v2_send_rich_alert()` already
    uses for `tier` — see CLAUDE.md's "Rich-alert ntfy priority" note).
    `debug(x)`'s compiled sugar never sets `priority` — it stays purely
    diagnostic.

    `name`/`tags` (2026-10 routing-tree plan, additive — same convention
    as `priority` above) let an operator-authored `log(...)` call emit a
    fully channel-agnostic signal the same way a seeded event agent's
    own slug does. Stored in `extra` as `route_name`/`route_tags` — NOT
    `name`/`tags` — for two confirmed, distinct reasons: (1) `name` is a
    reserved `logging.LogRecord` attribute; `Logger.makeRecord` raises
    `KeyError: "Attempt to overwrite 'name' in LogRecord"` the instant
    `extra` carries that key, so every `log(name=...)` call would
    silently vanish (swallowed by the noop-action-handler's own
    exception guard — no crash visible anywhere, just no log line at
    all). (2) `tags` is this codebase's REAL structural condition-match
    field (`log_store.py:tags_for()` reads `record.tags`; every seeded
    agent's `{"log": {"tag": ...}}` leaf matches against it) — writing a
    second meaning onto the same key would silently change which
    agents' CONDITIONS match this record, not just how it's routed.
    `route_name`/`route_tags` collide with neither. A FUTURE
    `alert_routing.py` matcher keyed on a record's own routing-specific
    name/tags (rather than the dispatching agent's slug) has real data
    to read from `extra["route_name"]`/`extra["route_tags"]`. No current
    routing rule consults either — `alert_routing.resolve_channels()`
    resolves once per AGENT today (keyed on `agent.slug`/`agent.priority`),
    not per record — so this is forward-looking plumbing, not a behavior
    change for any existing caller (neither param set -> `extra` omits
    both keys, byte-identical to before this change).
    """
    level    = (params.get("level") or "info").lower()
    message  = params.get("message", "")
    priority = params.get("priority") or None
    route_name = params.get("name") or None
    route_tags = params.get("tags") or None
    # Sprint G, Task 3 — carry the firing agent's kind (injected into
    # `ctx` by `execute()` above) on the log line AND in the returned
    # dict, so a future LogPanel kind-filter has something real to key
    # off. `ctx` may be `{}`/None in direct-call tests — `.get` degrades
    # to None rather than raising, same missing-vs-zero convention used
    # everywhere else in this module.
    agent_kind = (ctx or {}).get("agent_kind")
    logline = f"Agent emit_log: {message}"
    if agent_kind:
        logline = f"{logline} [agent_kind={agent_kind}]"
    log_fn = getattr(logger, level, logger.info)
    # `extra=` is only passed when there's something real to attach —
    # every pre-existing caller (no `priority`/`name`/`tags` param) gets
    # the exact same single-positional-arg call as before, so this stays
    # a true no-op/zero-regression change for every currently-saved
    # agent.
    extra = {}
    if priority:
        extra["priority"] = priority
    if route_name:
        extra["route_name"] = route_name
    if route_tags:
        extra["route_tags"] = list(route_tags) if isinstance(route_tags, (list, tuple)) else [route_tags]
    if extra:
        log_fn(logline, extra=extra)
    else:
        log_fn(logline)
    return {
        "action": "emit_log", "status": "logged", "level": level,
        "message": message, "agent_kind": agent_kind, "priority": priority,
        "name": route_name, "tags": extra.get("route_tags"),
    }
