"""Action resolver — `help`: look up a token/function/operator's
catalog description, parameter signature, and docstring — logged at
DEBUG, never alert-eligible.

File named after the DSL token (`help`), not the Python function
(`help_lookup`) — `help` collides with the Python builtin, so the
resolver function is named `help_lookup` (same precedent as
`action_log.py`'s `emit_log`, in the other direction).
"""

from __future__ import annotations

import inspect
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

# Pinned to the original "backend.api.algo.actions" name (NOT this
# module's own __name__) — same singleton-by-name reasoning as
# `_action_common.py` / `action_log.py`.
logger = get_logger("backend.api.algo.actions")


def _help_resolve_callable(token_kind: str, token: str):
    """Resolve the bound callable/value for a catalog row via the
    `GrammarRegistry` accessor keyed by its `token_kind`. Returns `None`
    on any lookup failure or for a `token_kind` with no accessor (e.g. a
    row this function doesn't know how to reflect) — never raises,
    matching this registry's existing "unknown token -> None" contract.
    """
    from backend.api.algo.grammar_registry import REGISTRY
    try:
        if token_kind == "metric":
            return REGISTRY.metric(token)
        if token_kind == "scope":
            return REGISTRY.scope(token)
        if token_kind == "operator":
            return REGISTRY.op(token)
        if token_kind == "channel":
            return REGISTRY.channel(token)
        if token_kind == "format":
            return REGISTRY.fmt(token)
        if token_kind == "template":
            return REGISTRY.template(token)
        if token_kind == "action_type":
            entry = REGISTRY.action(token)
            return (entry or {}).get("fn")
        if token_kind == "log_filter":
            # 2026-10-24 (grammar-unification Wave 2) — the kind='event'
            # agent CONDITION token (`log(tag=..., min_level=..., where=
            # {...})`). No `GrammarRegistry` loader exists for this
            # `token_kind` (`_eval_leaf` dispatches `"log" in leaf`
            # straight to `agent_evaluator._eval_log`, never through the
            # registry), so this is resolved by direct import rather than
            # a REGISTRY accessor — the one `token_kind` branch here that
            # doesn't go through `REGISTRY`.
            from backend.api.algo.agent_evaluator import _eval_log
            return _eval_log
    except Exception as e:
        logger.debug(f"help_lookup: resolving '{token}' ({token_kind}) raised: {e}")
        return None
    return None


def _help_reflect(fn: Any) -> tuple[str, str]:
    """Return (signature_text, docstring_text) for a resolved value,
    never hiding a missing piece — explicit "(... unavailable)" text
    instead of silent omission, per explicit operator instruction.
    `fn` can be `None` (dead/unwired resolver), a non-callable value
    (e.g. a notify `template` token's template-body string), or a real
    callable (metric/scope/operator/action/channel/format resolver)."""
    if fn is None:
        return "(signature unavailable)", "(no docstring)"
    if not callable(fn):
        return (
            f"(not a function — value: {fn!r})"[:200],
            "(no docstring — not a callable)",
        )
    try:
        signature_text = str(inspect.signature(fn))
    except (TypeError, ValueError):
        signature_text = "(signature unavailable)"
    doc = inspect.getdoc(fn)
    docstring_text = doc if doc else "(no docstring)"
    return signature_text, docstring_text


async def help_lookup(ctx, params: dict) -> dict:
    """Resolver for the DSL `help` action — read-only catalog
    introspection, logged at DEBUG only (never alert-eligible: no
    `extra={"tags": [...], "alert_event": ...}` at all, belt-and-
    suspenders on top of `event_agents.py:_dispatch_agent`'s structural
    DEBUG exemption, which is the real protection).

    Reverse-resolves `params["name"]` to every matching `GrammarToken`
    catalog row (a name can be reused across `token_kind`s — e.g. a
    notify `channel` token and an `action_type` token can share the
    same string — every match is returned, never guessed at), then for
    each renders: the catalog `description`, the resolved callable's
    parameter signature, and its docstring. Any missing piece renders
    explicit "(... unavailable)" text rather than being silently
    omitted, per explicit operator instruction.
    """
    from backend.api.algo.grammar_registry import REGISTRY

    name = str(params.get("name") or "").strip()
    if not name:
        logger.debug("Agent help(): no name provided")
        return {"action": "help", "status": "logged", "name": name, "matches": []}

    rows = REGISTRY.find_tokens(name)
    if not rows:
        logger.debug(f"Agent help({name}): no matching token found in the catalog")
        return {"action": "help", "status": "logged", "name": name, "matches": []}

    matches: list[dict] = []
    lines = [f"Agent help({name}): {len(rows)} match(es)"]
    for row in rows:
        fn = _help_resolve_callable(row.token_kind, row.token)
        description = row.description or "(no catalog description)"
        signature_text, docstring_text = _help_reflect(fn)
        matches.append({
            "grammar_kind": row.grammar_kind, "token_kind": row.token_kind,
            "token": row.token, "description": description,
            "signature": signature_text, "docstring": docstring_text,
        })
        lines.append(
            f"  [{row.grammar_kind}/{row.token_kind}] {row.token} — "
            f"{description} | signature: {signature_text} | doc: {docstring_text}"
        )
    logger.debug("\n".join(lines))
    return {"action": "help", "status": "logged", "name": name, "matches": matches}
