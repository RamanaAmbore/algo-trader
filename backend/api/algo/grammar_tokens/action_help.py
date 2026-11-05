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


# 2026-11-05 fix (operator: "for help info, show help even info dsl
# built tokens like when, do, etc.") — `help(name)` used to consult ONLY
# the `GrammarRegistry` catalog (metrics/scopes/operators/channels/
# formats/templates/action_types/log_filter), so a lookup for one of the
# DSL's own STRUCTURAL syntax keywords — `when`/`do` and the condition-
# expression operators `and`/`or`/`not`/`in`, plus `always`/`nop`/`true`/
# `false` — always rendered "no matching token found", even though these
# are exactly the words an operator typing a WHEN...DO statement in the
# Console would most want a quick reminder about. These are NOT catalog
# rows (the frontend grammar's `_RESERVED` set, agents.js, is the one
# place they're defined — there is no backend `GrammarToken` row for any
# of them, by design: they're parsed structurally, not resolved), so
# this is a small static dict, checked alongside (not instead of) the
# real catalog lookup. `given` is included too, despite no longer being
# valid syntax — it's exactly the word an operator migrating an old
# GIVEN-style statement would type into `help()` first.
_DSL_SYNTAX_KEYWORDS: dict[str, tuple[str, str]] = {
    "when": (
        "Starts the optional condition clause of a WHEN ... DO ... "
        "statement. Everything before WHEN is the preamble (runs "
        "unconditionally, every cycle for a saved agent); WHEN's own "
        "condition gates the paired DO block. WHEN without a paired DO, "
        "or DO without WHEN, is a compile error.",
        "WHEN <condition> DO <action1>, <action2>, ...",
    ),
    "do": (
        "Starts the action clause paired with a preceding WHEN. Every "
        "action listed after DO fires only when WHEN's own condition "
        "matched this cycle. 'DO nop' alone (no real action) is rejected.",
        "WHEN <condition> DO <action1>, <action2>, ...",
    ),
    "nop": (
        "A no-op action placeholder — does nothing on its own. 'DO nop' "
        "alone is rejected; it only has meaning alongside a real action "
        "in the same DO clause.",
        "WHEN <condition> DO nop, <real action>",
    ),
    "always": (
        "A condition that is unconditionally true every cycle. Used for "
        "a schedule-only agent (e.g. one gated purely by fire_at_time) "
        "that needs a trivially-satisfied WHEN condition.",
        "WHEN always DO <action>",
    ),
    "in": (
        "Membership operator inside a condition expression — true when "
        "the left value equals one of the listed right-hand values.",
        "WHEN <metric> in [<value1>, <value2>, ...] DO <action>",
    ),
    "not": (
        "Logical negation inside a condition expression.",
        "WHEN not <condition> DO <action>",
    ),
    "and": (
        "Logical AND inside a condition expression — both sides must be "
        "true for the overall condition to match.",
        "WHEN <condition1> and <condition2> DO <action>",
    ),
    "or": (
        "Logical OR inside a condition expression — either side being "
        "true is enough for the overall condition to match.",
        "WHEN <condition1> or <condition2> DO <action>",
    ),
    "true": (
        "Boolean literal — always true. Used on either side of a "
        "condition comparison.",
        "WHEN <metric> == true DO <action>",
    ),
    "false": (
        "Boolean literal — always false. Used on either side of a "
        "condition comparison.",
        "WHEN <metric> == false DO <action>",
    ),
    "given": (
        "REMOVED from the DSL (operator decision, 2026-10) — a leading "
        "GIVEN clause is now a compile error. Write the preamble "
        "directly before WHEN instead: label assignments and ordinary "
        "calls (order(...), alert(...), log(...), debug(...)) all work "
        "there without a GIVEN wrapper.",
        "(no longer valid syntax — write the preamble directly, no wrapper)",
    ),
}


def _help_lookup_dsl_keyword(name: str) -> "dict | None":
    """Return a synthetic catalog-row-shaped match for a DSL structural
    keyword (see `_DSL_SYNTAX_KEYWORDS` above), or `None` when *name*
    isn't one. Shaped identically to a real per-row match dict so the
    caller can render/merge it with zero special-casing."""
    entry = _DSL_SYNTAX_KEYWORDS.get(name.lower())
    if entry is None:
        return None
    description, usage = entry
    return {
        "grammar_kind": "syntax", "token_kind": "keyword", "token": name.lower(),
        "description": description, "signature": usage,
        "docstring": "(DSL syntax keyword — not a resolver function; see 'signature' for usage)",
    }


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

    ALSO checks `_DSL_SYNTAX_KEYWORDS` for a DSL structural keyword
    (`when`/`do`/`and`/`or`/`not`/`in`/`always`/`nop`/`true`/`false`/
    `given`) — 2026-11-05 fix (operator: "for help info, show help even
    info dsl built tokens like when, do, etc."). These were previously
    invisible to `help()` entirely, because they're parsed structurally
    by the frontend grammar and have no `GrammarToken` catalog row at
    all. Checked first and merged with any real catalog matches (a name
    collision between a keyword and a catalog token is not possible
    today — the lexer reserves these words so they can never be used as
    a label/action/metric name — but the merge is unconditional rather
    than either/or, so that would still render both if it ever happened).
    """
    from backend.api.algo.grammar_registry import REGISTRY

    name = str(params.get("name") or "").strip()
    if not name:
        logger.debug("Agent help(): no name provided")
        return {"action": "help", "status": "logged", "name": name, "matches": []}

    matches: list[dict] = []
    keyword_match = _help_lookup_dsl_keyword(name)
    if keyword_match is not None:
        matches.append(keyword_match)

    rows = REGISTRY.find_tokens(name)
    for row in rows:
        fn = _help_resolve_callable(row.token_kind, row.token)
        description = row.description or "(no catalog description)"
        signature_text, docstring_text = _help_reflect(fn)
        matches.append({
            "grammar_kind": row.grammar_kind, "token_kind": row.token_kind,
            "token": row.token, "description": description,
            "signature": signature_text, "docstring": docstring_text,
        })

    if not matches:
        logger.debug(f"Agent help({name}): no matching token found in the catalog")
        return {"action": "help", "status": "logged", "name": name, "matches": []}

    lines = [f"Agent help({name}): {len(matches)} match(es)"]
    for m in matches:
        lines.append(
            f"  [{m['grammar_kind']}/{m['token_kind']}] {m['token']} — "
            f"{m['description']} | signature: {m['signature']} | doc: {m['docstring']}"
        )
    logger.debug("\n".join(lines))
    return {"action": "help", "status": "logged", "name": name, "matches": matches}
