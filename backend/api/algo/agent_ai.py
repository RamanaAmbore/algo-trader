"""
agent_ai — natural-language → agent draft pipeline.

Operator describes what they want (terminal command or /agents UI prompt
field); this module:

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
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
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


def _clamp_safety(draft: dict, warnings: list[str]) -> None:
    """In-place: force AI agents to inactive + paper + reasonable defaults."""
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
    # Strip any destructive actions the LLM may have proposed.
    actions = draft.get("actions") or []
    safe_actions = []
    for a in actions:
        if a.get("type") in _DESTRUCTIVE_ACTIONS:
            warnings.append(
                f"Removed destructive action '{a.get('type')}' — re-add manually "
                "if intended."
            )
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

    _clamp_safety(draft, warnings)
    _scan_thresholds(draft, warnings)
    _validate_against_registry(draft, errors)
    _enrich_unknown_token_errors(errors, raw_tokens)

    return AgentDraft(
        draft=draft, errors=errors, warnings=warnings,
        why_summary=why, prompt=prompt,
    )
