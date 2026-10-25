"""
agent_ai — natural-language → agent draft pipeline.

Operator describes what they want (terminal `agent ai create` command);
this module:

  1. Snapshots the live grammar registry (metrics / scopes / operators /
     channels / action types) so the LLM only proposes tokens that exist.
  2. Calls Gemini with a system prompt that explains the schema, the
     available tokens, and the safety guardrails.
  3. Parses + validates the JSON response against the schema + the
     evaluator's validator.
  4. Applies safety defaults: AI agents land inactive + paper-only +
     one_shot lifespan regardless of what the LLM produced.
  5. Computes safety warnings (destructive actions, sub-percent thresholds,
     missing cooldown, etc.) so the operator sees the risk before saving.

Public:
  draft_agent_from_prompt(prompt: str) -> AgentDraft
  draft_token_from_prompt(prompt: str, ...) -> TokenDraft

Wave 3 of the "Unify DSL token registration" plan (see
`backend/api/routes/lab.py:McpController.draft_token`, the one HTTP
entry point for `draft_token_from_prompt` below) adds a second,
structurally similar pipeline: operator describes a desired grammar
token (metric / scope / notify-channel / action), the LLM drafts a
candidate `(resolver .py source, test .py source, params_schema,
description)` bundle, and this module statically validates it
(ast.parse, signature shape, a dangerous-import/call scan) WITHOUT EVER
executing a single line of the drafted code. The result is returned as
plain JSON for the operator to review — see `TokenDraft`'s own
docstring for the full safety boundary. Writing the reviewed files,
running the test, and deploying is a human/Claude-Code step, same as
Wave 1's own migration of every system resolver into
`backend/api/algo/grammar_tokens/`. Registering the deployed resolver
live is the EXISTING Wave 2 endpoint (`POST /api/admin/grammar/tokens`)
— this module never calls it and never writes to disk.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.shared.helpers.utils import secrets, ramboq_config, is_enabled
from backend.shared.helpers.settings import get_int

logger = logging.getLogger(__name__)


# Destructive action types — AI-generated agents that touch these are
# clamped to paper + warned. Live mode requires manual operator opt-in.
_DESTRUCTIVE_ACTIONS = {
    "chase_close",
    "chase_close_positions",
    "cancel_order",
    "cancel_all_orders",
    "close_position",
}

# Reasonable thresholds for automation — leaves below this read as
# "trip-wire" (fires on every tick) and trigger a warning.
_TRIPWIRE_PCT  = 0.10   # 0.1% on % metrics
_TRIPWIRE_ABS  = 100    # ₹100 on absolute metrics


@dataclass
class AgentDraft:
    draft: dict[str, Any]      # full agent JSON ready to POST/PATCH
    errors: list[str]          # validation errors (empty → ok to save)
    warnings: list[str]        # operator-visible safety notes
    why_summary: str           # one-line LLM summary of what this agent does
    prompt: str = ""           # original NL prompt — preserved for audit + UI
    # Additive side-channel from `_clamp_safety` — which fields the clamp
    # itself mutated and how (e.g. {"stripped_actions": [...],
    # "lifespan_type_defaulted": True}). The CREATE path
    # (`_cmd_ai_create`) ignores this entirely; it exists so
    # `_cmd_ai_refine` (backend/api/routes/agents.py) can tell "the LLM's
    # own draft already had this value" apart from "the safety clamp
    # changed it after the fact" — see `_clamp_safety`'s docstring.
    clamp_effects: dict[str, Any] = field(default_factory=dict)


def _summarise_token(token, kind: str) -> str:
    """Render a single registry token as a one-line summary that the LLM
    can read — surfaces value_type / units / enum_values / params_schema
    so the LLM knows what each token needs WITHOUT having to guess."""
    bits = []
    if token.value_type:
        v = token.value_type
        if token.units:
            v = f"{v} {token.units}"
        bits.append(v)
    if token.enum_values:
        bits.append("∈ {" + ", ".join(map(str, token.enum_values)) + "}")
    if kind == "action_type" and token.params_schema:
        # params_schema is a JSON Schema — surface required keys so the
        # LLM knows e.g. place_order needs {account, symbol, qty, …}.
        ps = token.params_schema or {}
        req = ps.get("required") or []
        props = ps.get("properties") or {}
        keys = [(f"{k}*" if k in req else k) for k in props.keys()]
        if keys:
            bits.append("params: {" + ", ".join(keys) + "}")
    elif token.params_schema:
        # Parameterized call-syntax tokens (metric/scope/channel/format —
        # e.g. mean_pnl's {"minutes": {"type": "number"}}) use a flat
        # {param_name: spec} shape, not action tokens' JSON-Schema
        # {required, properties} shape above — so this needs its own
        # rendering. Generated structurally from the schema rather than
        # depending solely on a token's hand-written `description` text
        # happening to mention the call syntax.
        keys = list((token.params_schema or {}).keys())
        if keys:
            bits.append(f"call syntax: {token.token}({', '.join(keys)})")
    suffix = f" — {' · '.join(bits)}" if bits else ""
    desc = (token.description or "").strip()
    if desc:
        suffix = (suffix + f" — {desc}") if suffix else f" — {desc}"
    return f"{token.token}{suffix}"


def _grammar_snapshot() -> dict[str, list[str]]:
    """Pull active tokens from the registry — drives the system prompt.

    For each token slot, returns a list of one-line summaries that
    include value_type / units / enum_values / params_schema so the LLM
    sees not just the names but what each token expects.
    """
    from backend.api.algo.grammar_registry import REGISTRY
    snap = {"metrics": [], "scopes": [], "operators": [], "channels": [], "actions": []}
    raw_tokens = {"metrics": [], "scopes": [], "operators": [], "channels": [], "actions": []}
    for token in REGISTRY.tokens.values():
        if not token.is_active:
            continue
        gk, tk = token.grammar_kind, token.token_kind
        if gk == "condition" and tk == "metric":
            snap["metrics"].append(_summarise_token(token, tk))
            raw_tokens["metrics"].append(token.token)
        elif gk == "condition" and tk == "scope":
            snap["scopes"].append(_summarise_token(token, tk))
            raw_tokens["scopes"].append(token.token)
        elif gk == "condition" and tk == "operator":
            snap["operators"].append(_summarise_token(token, tk))
            raw_tokens["operators"].append(token.token)
        elif gk == "notify" and tk == "channel":
            snap["channels"].append(_summarise_token(token, tk))
            raw_tokens["channels"].append(token.token)
        elif gk == "action" and tk == "action_type":
            snap["actions"].append(_summarise_token(token, tk))
            raw_tokens["actions"].append(token.token)
    for k in snap:
        snap[k].sort()
        raw_tokens[k].sort()
    snap["_raw"] = raw_tokens   # used by the unknown-token diagnostic
    return snap


_SYSTEM_PROMPT = """\
You are a strict JSON generator that produces RamboQuant agent definitions
from an operator's natural-language request.

An agent is a risk/automation rule with:
- conditions: a tree of AND / OR / NOT / leaf nodes
- events:     list of notify channels (telegram, email, websocket, log)
- actions:    list of action objects {type, params}
- scope:      "total" | "per_account"
- schedule:   "market_hours" | "always" (default "market_hours")
- cooldown_minutes: int (default 30; lower = re-fires more often)
- lifespan_type: "one_shot" | "n_fires" | "until_date" | "persistent"

Condition tree node forms:
  Leaf:   {"metric": "<m>", "scope": "<s>", "op": "<op>", "value": <number>}
  AND:    {"all": [<node>, ...]}
  OR:     {"any": [<node>, ...]}
  NOT:    {"not": <node>}

You MUST only use tokens from the live grammar registry (provided below).
If the operator asks for something the registry can't express, set the
"errors" field in your response and explain.

Return ONE JSON object with this exact shape (no markdown fences):
{
  "draft": {
    "name": "<short name>",
    "description": "<one-line description>",
    "conditions": <condition tree>,
    "events": ["telegram", "email"],
    "actions": [],
    "scope": "<scope>",
    "schedule": "<schedule>",
    "cooldown_minutes": <int>,
    "lifespan_type": "one_shot"
  },
  "why_summary": "<one sentence describing what fires when>",
  "errors": []
}

Rules:
- Default lifespan_type to "one_shot" — the operator can widen it.
- Default events to ["telegram", "email"] unless the operator specifies.
- Use "all" / "any" / "not" for multi-condition logic.
- Do NOT invent metrics, scopes, operators, channels, or action types.
- Do NOT propose destructive actions (cancel_order, chase_close, etc.)
  unless the operator explicitly requests them.
- Thresholds should be reasonable: avoid sub-0.1% pct values or sub-₹100
  abs values — flag those as risky in the why_summary.
"""


def _build_prompt(user_prompt: str, grammar: dict[str, Any]) -> str:
    """Compose the user message — operator request + the live grammar.

    Each token slot is rendered as a multi-line block with one summary
    per token (value_type, units, enum, params_schema) so the LLM sees
    not just names but what each token expects.
    """
    def _block(items: list[str]) -> str:
        return "\n".join(f"  - {it}" for it in items) if items else "  (none)"
    return (
        f"Operator request: {user_prompt.strip()}\n\n"
        f"Live grammar registry — use ONLY these tokens (* = required):\n"
        f"\nmetrics:\n{_block(grammar['metrics'])}\n"
        f"\nscopes:\n{_block(grammar['scopes'])}\n"
        f"\noperators:\n{_block(grammar['operators'])}\n"
        f"\nchannels:\n{_block(grammar['channels'])}\n"
        f"\nactions:\n{_block(grammar['actions'])}\n"
    )


_UNKNOWN_TOKEN_RE = re.compile(
    r"unknown\s+(metric|scope|operator)\s+token\s+'(?P<tok>[^']+)'", re.IGNORECASE
)


def _enrich_unknown_token_errors(errors: list[str], raw: dict[str, list[str]]) -> None:
    """For each 'unknown <kind> token X' error, append a hint listing the
    closest registered tokens so the operator knows whether to (a) re-prompt
    with a different name, or (b) register the missing token at /admin/tokens.

    Mutates `errors` in place."""
    if not errors:
        return
    KIND_TO_SLOT = {"metric": "metrics", "scope": "scopes", "operator": "operators"}
    enriched = []
    for e in errors:
        m = _UNKNOWN_TOKEN_RE.search(e)
        if m:
            kind = m.group(1).lower()
            slot = KIND_TO_SLOT.get(kind)
            if slot and raw.get(slot):
                avail = ", ".join(raw[slot][:8]) + ("…" if len(raw[slot]) > 8 else "")
                e = (f"{e}. Available {kind}s: {avail}. "
                     f"Add a custom {kind} at /admin/tokens if needed.")
        enriched.append(e)
    errors[:] = enriched


def _strip_fences(s: str) -> str:
    """LLMs often wrap JSON in ```json ... ``` despite instructions."""
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _clamp_safety(
    draft: dict, warnings: list[str], clamp_effects: dict[str, Any] | None = None,
) -> None:
    """In-place: force AI agents to inactive + paper + reasonable defaults.

    `clamp_effects` is an optional, additive side-channel: when passed a
    dict, this function records exactly which fields it mutated and how
    (`stripped_actions`: list of destructive action types removed from
    the LLM's own draft; `lifespan_type_defaulted`: True when the LLM's
    draft omitted `lifespan_type` and this function supplied the
    `one_shot` default). The CREATE path (`draft_agent_from_prompt`,
    below) does not pass this argument — `None` (the default) means this
    is purely a no-op write target, so CREATE-path behavior and output
    stay byte-for-byte unchanged. It exists for `_cmd_ai_refine`
    (backend/api/routes/agents.py) to distinguish "the LLM's own draft
    already proposed this value" from "the safety clamp changed it after
    the fact" when deciding what's safe to write back onto an EXISTING
    agent row.
    """
    # Always inactive on save — operator must explicitly activate.
    draft.setdefault("status", "inactive")
    # Always paper — operator flips to live via the per-row P/L chip.
    if draft.get("trade_mode") not in (None, "paper"):
        warnings.append(
            f"AI agents land in paper mode (was '{draft.get('trade_mode')}'). "
            "Flip the P/L chip on /agents to enable live."
        )
    draft["trade_mode"] = "paper"
    # Default lifespan to one_shot — least-surprise for AI-generated rules.
    if not draft.get("lifespan_type"):
        draft["lifespan_type"] = "one_shot"
        if clamp_effects is not None:
            clamp_effects["lifespan_type_defaulted"] = True
    # Strip any destructive actions the LLM may have proposed.
    actions = draft.get("actions") or []
    safe_actions = []
    for a in actions:
        if a.get("type") in _DESTRUCTIVE_ACTIONS:
            warnings.append(
                f"Removed destructive action '{a.get('type')}' — re-add manually "
                "if intended."
            )
            if clamp_effects is not None:
                clamp_effects.setdefault("stripped_actions", []).append(a.get("type"))
            continue
        safe_actions.append(a)
    draft["actions"] = safe_actions


def _walk_leaves(node: Any):
    """Yield every leaf in a condition tree."""
    if not isinstance(node, dict):
        return
    if "metric" in node:
        yield node
        return
    for k in ("all", "any"):
        if isinstance(node.get(k), list):
            for child in node[k]:
                yield from _walk_leaves(child)
    if "not" in node:
        yield from _walk_leaves(node["not"])


def _scan_thresholds(draft: dict, warnings: list[str]) -> None:
    """Flag sub-percent / sub-rupee thresholds as trip-wire risks."""
    cond = draft.get("conditions") or {}
    for leaf in _walk_leaves(cond):
        v = leaf.get("value")
        if not isinstance(v, (int, float)):
            continue
        metric = leaf.get("metric") or "?"
        if "pct" in metric or "_percentage" in metric or metric.endswith("%"):
            if abs(v) < _TRIPWIRE_PCT:
                warnings.append(
                    f"Threshold {v}% on '{metric}' may fire on every tick "
                    "(sub-0.1% trip-wire)."
                )
        else:
            if abs(v) < _TRIPWIRE_ABS:
                warnings.append(
                    f"Threshold {v} on '{metric}' is below ₹100 — verify intent."
                )


def _validate_against_registry(draft: dict, errors: list[str]) -> None:
    """Use the existing evaluator validator + light schema checks."""
    from backend.api.algo.agent_evaluator import validate as _validate_tree
    cond = draft.get("conditions")
    if not isinstance(cond, dict) or not cond:
        errors.append("conditions tree is empty or not a dict")
        return
    tree_errs = _validate_tree(cond) or []
    errors.extend(tree_errs)


def draft_agent_from_prompt(prompt: str) -> AgentDraft:
    """
    Convert a natural-language prompt into a validated agent draft.

    Returns AgentDraft with:
      - draft     : ready-to-POST agent JSON (paper, inactive, one_shot)
      - errors    : registry / schema errors — non-empty means do not save
      - warnings  : operator-visible safety notes (proceed with care)
      - why_summary: one-line LLM summary
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return AgentDraft({}, ["Empty prompt"], [], "", "")

    if not is_enabled("genai"):
        return AgentDraft(
            {}, ["GenAI is disabled in this environment — enable in backend_config.yaml"],
            [], "", prompt,
        )

    try:
        from google import genai  # local import — keeps non-prod deploys fast
        from google.genai import types
    except ImportError:
        return AgentDraft({}, ["google-genai package not installed"], [], "", prompt)

    grammar = _grammar_snapshot()
    raw_tokens = grammar.pop("_raw", {})
    user_msg = _build_prompt(prompt, grammar)

    try:
        client = genai.Client(api_key=secrets["gemini_api_key"])
        response = client.models.generate_content(
            model=ramboq_config.get("genai_model", "gemini-2.5-flash"),
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.2,    # low; we want deterministic JSON
                max_output_tokens=2048,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=get_int("genai.thinking_budget", 512),
                ),
            ),
        )
        raw = (response.text or "").strip()
    except Exception as e:
        logger.warning(f"Gemini call failed in agent_ai: {e}")
        return AgentDraft({}, [f"LLM unavailable: {e}"], [], "", prompt)

    if not raw:
        return AgentDraft({}, ["LLM returned empty response"], [], "", prompt)

    try:
        parsed = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        logger.warning(f"AI draft JSON parse failed: {e}; raw[:200]={raw[:200]!r}")
        return AgentDraft({}, [f"LLM produced invalid JSON: {e}"], [], "", prompt)

    draft = parsed.get("draft") or {}
    why   = parsed.get("why_summary") or ""
    errors = list(parsed.get("errors") or [])
    warnings: list[str] = []

    if not isinstance(draft, dict):
        return AgentDraft({}, ["LLM 'draft' field is not a dict"], [], why, prompt)

    clamp_effects: dict[str, Any] = {}
    _clamp_safety(draft, warnings, clamp_effects)
    _scan_thresholds(draft, warnings)
    _validate_against_registry(draft, errors)
    _enrich_unknown_token_errors(errors, raw_tokens)

    return AgentDraft(
        draft=draft, errors=errors, warnings=warnings,
        why_summary=why, prompt=prompt, clamp_effects=clamp_effects,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Wave 3 — AI-drafted grammar TOKEN (metric / scope / channel / action)
# ═══════════════════════════════════════════════════════════════════════════
#
# Deliberately NOT a general "write any token kind" pipeline. Only the four
# kinds below are file-based resolvers with a uniform-enough call signature
# to statically check: condition/metric, condition/scope, notify/channel,
# action/action_type. `condition/operator` resolvers live in code
# (`grammar.py:OPERATORS`), never file-based. `notify/format` signatures
# vary per formatter (see `format_html_table.py`'s `(headers, rows)` vs. a
# hypothetical plain-text formatter) — no single signature contract to
# validate against yet. `template`/`log_tag` tokens carry no resolver at
# all (`template_body` / pure catalog metadata). Widening this set is a
# future decision, not an oversight.
_TOKEN_KIND_PREFIX: dict[tuple[str, str], str] = {
    ("condition", "metric"):  "metric",
    ("condition", "scope"):   "scope",
    ("notify",    "channel"): "channel",
    ("action",    "action_type"): "action",
}

# Lowercase identifier, optionally dot-segmented (e.g. "funds.total") —
# matches the real naming convention already used under grammar_tokens/
# (scope_funds_total.py backs the "funds.total" token).
_TOKEN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

# A resolver for these four kinds must stay read-only / notification-only.
# Any of these imports is flagged as a WARNING (not a hard error — the
# human reviewer decides), never auto-rejected, because the real safety
# boundary is "nothing here is ever executed by this process" (see
# `TokenDraft`'s docstring) — the scan exists to make the reviewer's job
# easier, not to replace the review.
_DANGEROUS_IMPORT_PREFIXES = (
    "backend.brokers", "backend.api.algo.actions_live", "subprocess", "os",
    "socket", "requests", "httpx", "urllib",
)
_DANGEROUS_CALL_NAMES = {"exec", "eval", "compile", "__import__", "open"}

# Wave 2's registration endpoint (`POST /api/admin/grammar/tokens`) makes a
# deployed resolver LIVE for exactly two of the four whitelisted kinds —
# metric and scope dispatch through `GrammarRegistry.metric()`/`.scope()`,
# confirmed by Wave 2's own reload-parity test. `notify/channel` dispatch
# (for the `kind='event'` agent pipeline that actually sends notifications)
# runs through `event_agents.py`'s hardcoded `CHANNELS` dict, not
# `REGISTRY.channel()` — the only real caller of that method anywhere in
# this codebase is `grammar_tokens/action_help.py`'s `help_lookup`, which
# is READ-ONLY reflection (the `help(<name>)` action), never a dispatch
# path. `action/action_type` dispatch runs through `actions.py`'s explicit
# `_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS` tables, and
# `is_known_action_type()` (the gate that decides whether an agent's `DO`
# clause may reference an action type at all) reads the CODE-level
# `SYSTEM_TOKENS` catalog, not this DB row — `REGISTRY.action()` has the
# same sole read-only `help_lookup` caller. A channel/action draft is
# still useful to generate (the human reviewer adds the extra wiring
# during the same deploy that creates the file), but it is NOT a drop-in
# like metric/scope — flagged here as a fixed, kind-level warning rather
# than silently implying parity the plan's own "converge on the same two
# steps" framing doesn't actually hold for these two kinds yet.
_TOKEN_KIND_EXTRA_WIRING_WARNING: dict[tuple[str, str], str] = {
    ("notify", "channel"): (
        "channel tokens are not live-dispatched through the grammar_tokens "
        "catalog today — event_agents.py's CHANNELS dict is the real "
        "dispatch table. Deploying this resolver also needs a new "
        "CHANNELS[...] entry (and an alert_routing.py rule if an agent "
        "should route to it by name) — registering it via "
        "POST /api/admin/grammar/tokens alone will not make it reachable."
    ),
    ("action", "action_type"): (
        "action tokens are not live-dispatched through the grammar_tokens "
        "catalog today — actions.py's _LIVE_ACTION_HANDLERS/"
        "_NOOP_ACTION_HANDLERS tables are the real dispatch, and "
        "is_known_action_type() reads the code-level SYSTEM_TOKENS "
        "catalog, not this DB row. Deploying this resolver also needs a "
        "dispatch-table entry and a matching agent_grammar.yaml row "
        "before an agent's DO clause can reference it."
    ),
}


@dataclass
class TokenDraft:
    """Result of `draft_token_from_prompt`.

    SAFETY BOUNDARY (read before touching the route that calls this):
    this module NEVER writes a file, NEVER imports/execs the drafted
    `resolver_source`/`test_source`, and NEVER calls the Wave 2
    registration endpoint. `draft` is plain JSON-safe data (strings,
    dicts, lists, None) — a reviewable DIFF, not a live artifact. A
    human (operator reviewing the /admin/mcp page, or a Claude Code
    session) is the only thing that ever turns this into real files,
    and only the normal `/impl`→`/ddev`→`/dprod` deploy pipeline is the
    only thing that ever makes a drafted resolver importable. Only
    *after* that deploy does Wave 2's `POST /api/admin/grammar/tokens`
    (already existing, already validates `resolver` dotted-path
    importability — see `routes/grammar.py:_validate_resolver_importable`)
    make it live.

    `draft` fields when present (absent/`None` on an early hard
    failure — see `errors`):
      grammar_kind, token_kind, token, description, params_schema
      resolver_source, resolver_function_name, resolver_module_path,
      resolver_file_path, test_source, test_file_path
    """
    draft: dict[str, Any]
    errors: list[str]
    warnings: list[str]
    why_summary: str
    prompt: str = ""


def _td_catalog_snapshot() -> dict[str, list[str]]:
    """One-line summaries of every ACTIVE token in the four AI-draftable
    kinds — reuses `_summarise_token` so the LLM sees value_type / units /
    enum_values / params_schema exactly like `_grammar_snapshot` does for
    agent drafting. Deliberately a sibling function, not a change to
    `_grammar_snapshot` itself — that function's shape is depended on by
    `draft_agent_from_prompt` and its existing tests."""
    from backend.api.algo.grammar_registry import REGISTRY
    snap: dict[str, list[str]] = {"metric": [], "scope": [], "channel": [], "action_type": []}
    for token in REGISTRY.tokens.values():
        if not token.is_active:
            continue
        if (token.grammar_kind, token.token_kind) in _TOKEN_KIND_PREFIX:
            snap[token.token_kind].append(_summarise_token(token, token.token_kind))
    for k in snap:
        snap[k].sort()
    return snap


_TOKEN_DRAFT_SYSTEM_PROMPT = """\
You are a strict JSON generator that drafts ONE new RamboQuant DSL grammar
token — a metric, scope, notify-channel, or action resolver — from an
operator's natural-language request. Your output is reviewed by a human
before any file is written or any code runs; nothing you produce executes
automatically.

Token kinds you may propose (grammar_kind/token_kind), each with its exact
Python call signature and a real example already deployed in this codebase:

  condition/metric  — computes a number from one position/account row.
    def _metric_cash(ctx, row):
        return _num_or_none(row.get('avail cash'))

  condition/scope  — selects a list of rows from the evaluation context.
    def _scope_funds_total(ctx):
        df = ctx.df_margins
        if df is None or df.empty:
            return []
        mask = df['account'].astype(str) == 'TOTAL'
        return [r.to_dict() for _, r in df[mask].iterrows()]

  notify/channel  — sends one notification. Signature exactly
  (title, body, tg=None, priority=None) -> None.
    def _send_ntfy(title, body, tg=None, priority=None):
        from backend.shared.helpers import alert_utils
        alert_utils.send_ntfy_alert(title, body, priority=priority)

  action/action_type  — an async DO-action. Must be declared with
  'async def', signature (ctx, params: dict) -> dict.
    async def my_action(ctx, params: dict) -> dict:
        ...
        return {"action": "my_action", "status": "done"}

Hard rules:
- Do NOT import backend.brokers, backend.api.algo.actions_live, subprocess,
  os, socket, requests, httpx, or urllib. A metric/scope/channel resolver
  must be read-only; an action resolver must stay inside the existing
  actions machinery. Broker order placement/cancellation already exists as
  reviewed action tokens — never reinvent it here.
- Do NOT call exec, eval, compile, __import__, or open.
- Do NOT propose a (grammar_kind, token_kind, token) triple that already
  exists — the operator request message lists every existing token name.
- The module source must contain exactly ONE top-level function (a module
  docstring and a function docstring are fine; a leading import of a real,
  safe helper is fine).
- token must be a lowercase identifier, optionally dot-segmented
  (e.g. "mean_drawdown_30d" or "funds.total").
- Write a real pytest test module that imports the function directly and
  asserts on its return value for at least one concrete input.

Return ONE JSON object with this exact shape (no markdown fences):
{
  "draft": {
    "grammar_kind": "condition" | "notify" | "action",
    "token_kind": "metric" | "scope" | "channel" | "action_type",
    "token": "<lowercase_snake_or.dotted_name>",
    "description": "<one-line description>",
    "params_schema": <schema object, or null if the token takes no params>,
    "resolver_source": "<complete python module source>",
    "test_source": "<complete pytest module source>"
  },
  "why_summary": "<one sentence describing what this token computes/does>",
  "errors": []
}

If the operator's request can't be expressed as one of the four kinds
above, set "errors" and leave "draft" minimal.
"""


def _td_build_prompt(
    user_prompt: str, snapshot: dict[str, list[str]],
    existing_names: set[tuple[str, str, str]],
) -> str:
    def _block(items: list[str]) -> str:
        return "\n".join(f"  - {it}" for it in items) if items else "  (none)"
    existing_block = "\n".join(
        f"  - {gk}/{tk}/{tok}" for (gk, tk, tok) in sorted(existing_names)
    ) or "  (none)"
    return (
        f"Operator request: {user_prompt.strip()}\n\n"
        f"Existing tokens — do NOT propose any of these (grammar_kind/"
        f"token_kind/token) triples again:\n{existing_block}\n\n"
        f"Existing metrics:\n{_block(snapshot['metric'])}\n"
        f"\nExisting scopes:\n{_block(snapshot['scope'])}\n"
        f"\nExisting channels:\n{_block(snapshot['channel'])}\n"
        f"\nExisting actions:\n{_block(snapshot['action_type'])}\n"
    )


def _td_call_gemini(user_msg: str) -> tuple[str, str | None]:
    """Same provider/call pattern `draft_agent_from_prompt` uses (local
    `google.genai` import, same model config key, same thinking-budget
    setting) — a dedicated helper (not a shared one) because a drafted
    resolver + test module is much larger than an agent condition tree,
    so this call uses a higher `max_output_tokens` and asks for a JSON
    mime type explicitly. Returns (raw_text, error) — error is None on a
    genuine (possibly empty) response, so an empty response is still
    distinguishable from a call failure by the caller."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "", "google-genai package not installed"
    try:
        client = genai.Client(api_key=secrets["gemini_api_key"])
        response = client.models.generate_content(
            model=ramboq_config.get("genai_model", "gemini-2.5-flash"),
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=_TOKEN_DRAFT_SYSTEM_PROMPT,
                temperature=0.2,
                max_output_tokens=8192,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(
                    thinking_budget=get_int("genai.thinking_budget", 512),
                ),
            ),
        )
        return (response.text or "").strip(), None
    except Exception as e:
        logger.warning(f"Gemini call failed in agent_ai (draft_token): {e}")
        return "", f"LLM unavailable: {e}"


def _td_validate_identity(
    raw_draft: dict, existing_names: set[tuple[str, str, str]],
) -> tuple[str, str, str, list[str]]:
    """Validate the LLM's proposed (grammar_kind, token_kind, token)
    against the whitelist + name regex + the caller-supplied existing-name
    set (case-insensitive — `buildCatalog()` lowercases, last-write-wins).
    Returns (grammar_kind, token_kind, token, errors) — the three strings
    are returned even when invalid/empty so the draft can still echo back
    what the LLM proposed."""
    errors: list[str] = []
    gk = str(raw_draft.get("grammar_kind") or "").strip()
    tk = str(raw_draft.get("token_kind") or "").strip()
    tok = str(raw_draft.get("token") or "").strip()
    if (gk, tk) not in _TOKEN_KIND_PREFIX:
        allowed = ", ".join(f"{a}/{b}" for a, b in _TOKEN_KIND_PREFIX)
        errors.append(
            f"unsupported grammar_kind/token_kind '{gk}/{tk}' — AI "
            f"drafting only supports: {allowed}"
        )
        return gk, tk, tok, errors
    if not tok or not _TOKEN_NAME_RE.match(tok):
        errors.append(
            f"token name '{tok}' is not a valid lowercase identifier "
            "(optionally dot-segmented, e.g. 'funds.total')"
        )
    elif (gk, tk, tok.lower()) in existing_names:
        errors.append(f"token '{tok}' already exists for {gk}/{tk}")
    return gk, tk, tok, errors


def _td_derive_paths(gk: str, tk: str, tok: str) -> tuple[str, str, str, str]:
    """Derive the module path / file path / test file path on the SERVER,
    from the whitelisted prefix + the verified token name — never from
    anything the LLM claims about where its own code should live. This is
    the critical safety boundary: a drafted resolver's eventual dotted
    path is a pure function of (grammar_kind, token_kind, token), so the
    LLM cannot point a 'metric' at an already-existing, already-reviewed
    function elsewhere in the codebase (e.g. a broker-order action) by
    simply naming it in a `resolver` field — this module never reads or
    trusts such a field."""
    prefix = _TOKEN_KIND_PREFIX[(gk, tk)]
    safe = tok.replace(".", "_")
    module_name = f"{prefix}_{safe}"
    module_path = f"backend.api.algo.grammar_tokens.{module_name}"
    file_path = f"backend/api/algo/grammar_tokens/{module_name}.py"
    test_file_path = f"backend/tests/grammar_tokens/test_{module_name}.py"
    return module_name, module_path, file_path, test_file_path


def _td_scan_dangerous_usage(label: str, tree: ast.AST) -> list[str]:
    """AST walk (not regex — regex misses `import x as y` aliasing and
    `import backend.brokers as b`) for imports/calls a read-only or
    notification-only resolver should not need. Returns WARNINGS, never
    errors — the human reviewer decides; see `TokenDraft`'s docstring for
    why a warning here is not the real safety boundary."""
    warnings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == p or alias.name.startswith(p + ".")
                       for p in _DANGEROUS_IMPORT_PREFIXES):
                    warnings.append(
                        f"{label} imports '{alias.name}' — review before "
                        "deploying; this token kind must stay read-only"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod == p or mod.startswith(p + ".") for p in _DANGEROUS_IMPORT_PREFIXES):
                warnings.append(
                    f"{label} imports from '{mod}' — review before "
                    "deploying; this token kind must stay read-only"
                )
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in _DANGEROUS_CALL_NAMES:
                warnings.append(f"{label} calls '{name}(...)' — review before deploying")
    return warnings


def _td_validate_python_module(
    label: str, source: Any, *, require_single_function: bool = True,
) -> tuple[list[str], list[str], ast.AST | None]:
    """ast.parse `source` (never executed). Hard error on empty/non-str
    or a syntax error.

    `require_single_function=True` (the resolver's own convention — one
    file, one function, matching every real file under
    `grammar_tokens/`) additionally errors on anything other than
    exactly one top-level function def. `False` (used for `test_source`
    — a real pytest module legitimately defines several `test_*`
    functions, exactly like every existing file under
    `backend/tests/grammar_tokens/`) only requires at least one.

    Returns (errors, warnings, first_function_def_node_or_None) — for
    `test_source` the returned node is informational only; no caller
    reads it today.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(source, str) or not source.strip():
        errors.append(f"{label} is empty")
        return errors, warnings, None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        errors.append(f"{label} does not parse: {type(e).__name__}: {e}")
        return errors, warnings, None
    top_level_fns = [
        n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if require_single_function:
        fn_node = top_level_fns[0] if len(top_level_fns) == 1 else None
        if len(top_level_fns) != 1:
            errors.append(
                f"{label} must define exactly one top-level function, "
                f"found {len(top_level_fns)}"
            )
    else:
        fn_node = top_level_fns[0] if top_level_fns else None
        if not top_level_fns:
            errors.append(f"{label} must define at least one top-level function")
    warnings.extend(_td_scan_dangerous_usage(label, tree))
    return errors, warnings, fn_node


def _td_validate_signature(tk: str, fn_node: ast.AST) -> list[str]:
    """Signature shape per token_kind, matching the real examples in
    `_TOKEN_DRAFT_SYSTEM_PROMPT` — a well-formedness check for the human
    reviewer, not a safety boundary (see module docstring)."""
    args = [a.arg for a in fn_node.args.args]
    errors: list[str] = []
    if tk == "metric" and len(args) < 2:
        errors.append(f"metric resolver must take (ctx, row) — found ({', '.join(args)})")
    elif tk == "scope" and len(args) < 1:
        errors.append(f"scope resolver must take (ctx) — found ({', '.join(args)})")
    elif tk == "channel" and len(args) < 2:
        errors.append(f"channel resolver must take (title, body, ...) — found ({', '.join(args)})")
    elif tk == "action_type":
        if not isinstance(fn_node, ast.AsyncFunctionDef):
            errors.append("action resolver must be declared 'async def'")
        if len(args) < 2:
            errors.append(f"action resolver must take (ctx, params) — found ({', '.join(args)})")
    return errors


def _td_validate_params_schema(schema: Any) -> list[str]:
    """Loose shape sanity check only — real validation happens when a
    human actually wires the schema into the deployed token."""
    if schema is None:
        return []
    if not isinstance(schema, dict):
        return [f"params_schema must be a JSON object, got {type(schema).__name__}"]
    if "properties" in schema and not isinstance(schema.get("properties"), dict):
        return ["params_schema.properties must be an object"]
    return []


def _td_check_test_imports_resolver(test_source: str, module_path: str, fn_name: str) -> list[str]:
    """Warn (never error — `test_source` may legitimately be malformed in
    other ways already caught above, and this is a well-formedness nudge
    only) when `test_source` has no `from <module_path> import <fn_name>`
    (or `<fn_name>` among multiple imported names). The hint-identity
    override rewrites `token` (and so `module_path`/`fn_name`) but cannot
    rewrite whatever import line the LLM already wrote inside
    `test_source` — without this check, a drifted import would pass with
    `errors=[]` and only fail once a human actually runs the test."""
    try:
        tree = ast.parse(test_source)
    except (SyntaxError, ValueError):
        return []  # already reported by _td_validate_python_module
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_path:
            if any(alias.name == fn_name for alias in node.names):
                return []
    return [
        f"test_source does not appear to import '{fn_name}' from "
        f"'{module_path}' — verify the test actually exercises this resolver"
    ]


def _td_build_validated_draft(
    raw_draft: dict, existing_names: set[tuple[str, str, str]],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Orchestrates the static-validation pipeline above into one draft
    dict + errors + warnings. Never imports/execs `resolver_source` or
    `test_source` — every check here is `ast`-based or a plain string/dict
    shape check."""
    errors: list[str] = []
    warnings: list[str] = []
    gk, tk, tok, id_errors = _td_validate_identity(raw_draft, existing_names)
    errors.extend(id_errors)

    # Fixed 12-key shape on EVERY non-`{}` return — including the
    # identity-error early-return below — so a caller (the /admin/mcp
    # UI) never has to branch on which keys happen to be present. The
    # five derived keys default to `None` here and are overwritten below
    # only when they could be safely computed.
    draft: dict[str, Any] = {
        "grammar_kind": gk, "token_kind": tk, "token": tok,
        "description": str(raw_draft.get("description") or "").strip(),
        "params_schema": raw_draft.get("params_schema"),
        "resolver_source": raw_draft.get("resolver_source"),
        "resolver_function_name": None,
        "resolver_module_path": None,
        "resolver_file_path": None,
        "resolver": None,
        "test_source": raw_draft.get("test_source"),
        "test_file_path": None,
    }

    if id_errors:
        # Identity is broken — paths can't be safely derived. Stop here;
        # the draft above still echoes what the LLM proposed for review,
        # with every derived key present (as None), never missing.
        return draft, errors, warnings

    module_name, module_path, file_path, test_file_path = _td_derive_paths(gk, tk, tok)
    draft["resolver_module_path"] = module_path
    draft["resolver_file_path"] = file_path
    draft["test_file_path"] = test_file_path

    extra_wiring = _TOKEN_KIND_EXTRA_WIRING_WARNING.get((gk, tk))
    if extra_wiring:
        warnings.append(extra_wiring)

    r_errors, r_warnings, fn_node = _td_validate_python_module(
        "resolver_source", draft["resolver_source"],
    )
    errors.extend(r_errors)
    warnings.extend(r_warnings)
    fn_name = fn_node.name if fn_node is not None else None
    draft["resolver_function_name"] = fn_name
    # `resolver` is the exact dotted path a human would POST to Wave 2's
    # `POST /api/admin/grammar/tokens` once this file is deployed — a
    # pure function of the server-derived module path + the function
    # name actually found in `resolver_source` by AST, never anything
    # the LLM's own JSON response claims (see `_td_derive_paths`'s
    # docstring for why that distinction is the safety boundary). `None`
    # when `resolver_source` didn't parse to exactly one function, same
    # as `resolver_function_name` above.
    draft["resolver"] = f"{module_path}.{fn_name}" if fn_name else None
    if fn_node is not None:
        errors.extend(_td_validate_signature(tk, fn_node))

    t_errors, t_warnings, _ = _td_validate_python_module(
        "test_source", draft["test_source"], require_single_function=False,
    )
    errors.extend(t_errors)
    warnings.extend(t_warnings)
    if fn_name and isinstance(draft["test_source"], str):
        warnings.extend(_td_check_test_imports_resolver(draft["test_source"], module_path, fn_name))

    errors.extend(_td_validate_params_schema(draft["params_schema"]))

    repo_root = Path(__file__).resolve().parents[3]
    if (repo_root / file_path).exists():
        warnings.append(
            f"a file already exists at {file_path} — check for a naming "
            "collision before writing"
        )

    return draft, errors, warnings


def _td_precheck_hint(
    grammar_kind: str | None, token_kind: str | None, token: str | None,
    existing_names: set[tuple[str, str, str]],
) -> list[str]:
    """When the operator supplies all three identity hints, run the
    cheap collision/unsupported-kind check BEFORE the LLM is ever
    called — same ordering precedent Wave 2's registration endpoint
    already uses (duplicate check before import validation). Returns
    errors; empty means proceed to the LLM call."""
    if not (grammar_kind and token_kind and token):
        return []
    if (grammar_kind, token_kind) not in _TOKEN_KIND_PREFIX:
        allowed = ", ".join(f"{a}/{b}" for a, b in _TOKEN_KIND_PREFIX)
        return [
            f"unsupported grammar_kind/token_kind '{grammar_kind}/{token_kind}' "
            f"— AI drafting only supports: {allowed}"
        ]
    hint_key = (grammar_kind, token_kind, token.strip().lower())
    if hint_key in existing_names:
        return [f"token '{token}' already exists for {grammar_kind}/{token_kind}"]
    return []


def draft_token_from_prompt(
    prompt: str,
    *,
    grammar_kind: str | None = None,
    token_kind: str | None = None,
    token: str | None = None,
    existing_names: set[tuple[str, str, str]] | None = None,
) -> TokenDraft:
    """
    Convert a natural-language prompt into a validated DSL-token draft.

    `grammar_kind`/`token_kind`/`token` are OPTIONAL operator-supplied
    hints, ALL-OR-NONE: unless all three are given, they are ignored
    entirely — no precheck, no override, exactly as if omitted. When
    all three ARE given: (1) a collision/unsupported-kind check runs
    BEFORE the LLM is ever called — the cheap check always runs first,
    same ordering Wave 2's registration endpoint already uses; (2) once
    the LLM responds, these three values OVERRIDE whatever identity the
    LLM proposed, so an explicit operator-chosen name can never drift.

    `existing_names` is the caller-supplied set of every
    `(grammar_kind, token_kind, token.lower())` triple already in the
    `grammar_tokens` table — ACTIVE AND INACTIVE (Wave 2's own DB
    uniqueness constraint has no `is_active` filter, so neither does
    this check). This module does no DB I/O itself — the route handler
    queries it, same separation `draft_agent_from_prompt`/
    `routes/agents.py:_cmd_ai_create` already use (this module drafts;
    the route persists).

    Returns TokenDraft with:
      - draft     : {} on an early hard failure (empty prompt, GenAI
                    disabled, LLM unavailable/empty/invalid JSON, a
                    pre-LLM collision/unsupported-kind hint rejection,
                    or a non-dict LLM 'draft' field) — otherwise always
                    populated, even when `errors` is non-empty, so the
                    operator can see exactly what the LLM proposed.
      - errors    : non-empty means do not write/deploy this draft.
      - warnings  : operator-visible review notes (e.g. a dangerous
                    import) — proceed with care, not a hard block.
      - why_summary: one-line LLM summary.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return TokenDraft({}, ["Empty prompt"], [], "", "")

    if not is_enabled("genai"):
        return TokenDraft(
            {}, ["GenAI is disabled in this environment — enable in backend_config.yaml"],
            [], "", prompt,
        )

    existing_names = existing_names or set()
    hinted = bool(grammar_kind and token_kind and token)
    precheck_errors = _td_precheck_hint(grammar_kind, token_kind, token, existing_names)
    if precheck_errors:
        return TokenDraft({}, precheck_errors, [], "", prompt)

    snapshot = _td_catalog_snapshot()
    user_msg = _td_build_prompt(prompt, snapshot, existing_names)
    raw, err = _td_call_gemini(user_msg)
    if err:
        return TokenDraft({}, [err], [], "", prompt)
    if not raw:
        return TokenDraft({}, ["LLM returned empty response"], [], "", prompt)

    try:
        parsed = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        logger.warning(f"AI token draft JSON parse failed: {e}; raw[:200]={raw[:200]!r}")
        return TokenDraft({}, [f"LLM produced invalid JSON: {e}"], [], "", prompt)

    # The LLM is instructed to return a JSON OBJECT, but nothing stops a
    # top-level array/string/number from parsing cleanly as valid JSON —
    # `parsed.get(...)` below would raise AttributeError on any of those,
    # turning a bad LLM response into an unhandled 500 instead of a
    # reported error. Guard before touching `parsed` at all.
    if not isinstance(parsed, dict):
        return TokenDraft({}, ["LLM response is not a JSON object"], [], "", prompt)

    raw_draft = parsed.get("draft") or {}
    why = str(parsed.get("why_summary") or "")
    raw_errors = parsed.get("errors")
    errors = [str(e) for e in raw_errors] if isinstance(raw_errors, list) else (
        [str(raw_errors)] if raw_errors else []
    )

    if not isinstance(raw_draft, dict):
        return TokenDraft({}, ["LLM 'draft' field is not a dict"], [], why, prompt)

    if hinted:
        raw_draft["grammar_kind"] = grammar_kind
        raw_draft["token_kind"] = token_kind
        raw_draft["token"] = token

    draft, draft_errors, warnings = _td_build_validated_draft(raw_draft, existing_names)
    errors.extend(draft_errors)

    return TokenDraft(draft=draft, errors=errors, warnings=warnings, why_summary=why, prompt=prompt)
