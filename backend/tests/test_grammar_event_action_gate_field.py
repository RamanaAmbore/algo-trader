"""
Sprint 18 — `gate` field schema declarations for event-agent decompile.

The frontend's `event_stmt` CLI grammar (`ALERT log_tag=... render=...
<channels>`) can decompile a stored kind='event' Agent row back to CLI
text only when every key on every `events[]`/`actions[]` entry is
declared somewhere a catalog-driven validator can look it up.
`gate` — present at the CHANNEL level on 8 of 15 real seeded agents
(`event_agents.py:SEEDED_AGENTS`) and at the ACTION level on 1
(`ERROR_AGENT`) — had no such declaration anywhere, even though it
already worked functionally (`event_agents.py:_send_channel` /
`_gate_spec`/`_gate_passes`). This is a pure schema/documentation
addition: the field's real behavior is unchanged.

Two DISTINCT, unrelated meanings share the one key name:
  - Channel-level `gate` (default True): when active, skips the send
    unless `_channel_enabled(capability)` passes. `gate: false` bypasses
    that capability check for this one channel entry.
  - Action-level `gate` (default False, only on the `render` action):
    when `true`, routes the fired record through the shared RepeatGate
    (dedup collapsing repeat-identical records into one alert with a
    `repeats` count).

Covers:
  1. `telegram`/`email`/`ntfy` channel tokens declare an optional
     boolean `gate` in their `params_schema` (agent_grammar.yaml).
  2. The declaration survives onto the wire shape `routes/grammar.py`'s
     `GrammarTokenController` actually serves (`GrammarTokenOut`).
  3. A new `event_actions.render.params_schema` YAML section declares
     the render action's own optional `gate`, exposed via
     `grammar.get_render_action_params_schema()`.
  4. The render schema is NOT merged into the `action_type` token
     family `is_known_action_type()`/`get_action_params_schema()`
     reads — a kind='cycle' agent's DO action list must still reject an
     unknown `render` action type exactly as before this change.
  5. Every real seeded agent's `events`/`actions` entries are now fully
     describable by a declared schema — including `telegram_info`
     (closed 2026-10-12; was the one remaining gap, causing `market-
     summary`'s Edit/decompile to fail with "not declared in its catalog
     schema") — this is a structural regression guard: a FUTURE
     undeclared key reintroduces a decompile failure and must fail this
     test, not silently slip through.

Perf: no DB / network calls — in-memory catalog + a fake GrammarToken
row for the wire-shape check, same pattern as
test_grammar_catalog_place_order_constraints.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.algo import grammar
from backend.api.algo import event_agents as ea
from backend.api.routes import grammar as g


def _live_channel_token(name: str) -> dict:
    for tok in grammar.SYSTEM_TOKENS:
        if tok.get("grammar_kind") == "notify" and tok.get("token_kind") == "channel" and tok.get("token") == name:
            return tok
    raise AssertionError(f"channel token {name!r} not found in SYSTEM_TOKENS")


# ── channel-level gate (telegram / email / ntfy) ────────────────────────

class TestChannelGateFieldSchema:
    @pytest.mark.parametrize("channel,capability", [
        ("telegram", "telegram"),
        ("email", "mail"),
        ("ntfy", "ntfy"),
    ])
    def test_channel_declares_optional_boolean_gate(self, channel, capability):
        schema = _live_channel_token(channel)["params_schema"]
        assert "gate" in schema, f"{channel}'s params_schema has no 'gate' entry"
        spec = schema["gate"]
        assert spec["type"] == "boolean"
        assert spec["required"] is False

    def test_ntfy_still_declares_priority_alongside_gate(self):
        """Adding `gate` must not clobber ntfy's pre-existing `priority` key."""
        schema = _live_channel_token("ntfy")["params_schema"]
        assert "priority" in schema
        assert schema["priority"]["type"] == "enum"

    def test_log_channel_does_not_declare_gate(self):
        """`log`'s capability is None (`_channel_enabled(None)` is always
        True) — `gate` has no effect there and is deliberately NOT
        declared, per event_agents.py:CHANNELS."""
        schema = _live_channel_token("log").get("params_schema") or {}
        assert "gate" not in schema

    def test_telegram_info_channel_is_declared_with_gate(self):
        """Closed 2026-10-12 — `telegram_info` (event_agents.py:CHANNELS,
        used only by the seeded `market-summary` agent) had no catalog
        row at all, so the frontend CLI grammar's Edit/decompile path
        failed with "unknown channel 'telegram_info'" (or, once the
        `gate: false` key is reached, "not declared in its catalog
        schema") for that one agent. Distinct from `log` (capability also
        None, but no seeded agent sets `gate` on it) — `telegram_info`
        DOES need the declaration since market-summary's own event entry
        carries `gate: false`."""
        schema = _live_channel_token("telegram_info")["params_schema"]
        assert "gate" in schema
        assert schema["gate"]["type"] == "boolean"
        assert schema["gate"]["required"] is False

    def test_market_summary_agent_events_fully_resolve_against_declared_schema(self):
        """The real seeded agent this gap affected — confirms its exact
        stored `events[]` entry (channel + extra keys) all resolve
        against a declared schema, the backend-side half of the
        decompile fix (the frontend decompiler itself is covered by a
        Vitest round-trip test)."""
        agent = next(a for a in ea.SEEDED_AGENTS if a["slug"] == "market-summary")
        for ev in agent.get("events") or []:
            schema = _channel_params_schema(ev["channel"])
            assert schema, f"channel {ev['channel']!r} has no declared schema at all"
            for key in ev:
                if key in _CHANNEL_ENTRY_RESERVED_KEYS:
                    continue
                assert key in schema, f"{ev['channel']}.{key} not declared"

    def test_gate_field_survives_onto_grammar_token_wire_shape(self):
        """End-to-end: the same shape `GET /api/admin/grammar/tokens`
        serves via `GrammarTokenController` / `routes/grammar.py:_to_out`."""
        live = _live_channel_token("telegram")
        fake_row = SimpleNamespace(
            id=1, grammar_kind="notify", token_kind="channel", token="telegram",
            value_type="enum", units=None, description=live["description"],
            resolver=None, params_schema=live["params_schema"], enum_values=None,
            template_body=None, is_system=True, is_active=True,
        )
        out = g._to_out(fake_row)
        assert out.params_schema is not None
        assert "gate" in out.params_schema
        assert out.params_schema["gate"]["type"] == "boolean"


# ── action-level gate (render action) ───────────────────────────────────

class TestRenderActionGateFieldSchema:
    def test_render_action_declares_optional_boolean_gate(self):
        schema = grammar.get_render_action_params_schema()
        assert "gate" in schema
        assert schema["gate"]["type"] == "boolean"
        assert schema["gate"]["required"] is False

    def test_render_schema_is_not_merged_into_action_type_catalog(self):
        """`render` must stay OUT of the dispatchable `action_type` token
        family — merging it would make a kind='cycle' agent's DO list
        wrongly accept a `render` action with no real handler (see
        routes/agents.py:_age_validate_action_entries, which is never
        even called for kind='event' agents)."""
        assert grammar.is_known_action_type("render") is False
        assert grammar.get_action_params_schema("render") == {}


# ── structural regression guard over the real seeded agents ────────────

_CHANNEL_ENTRY_RESERVED_KEYS = {"channel", "enabled", "message"}
_RENDER_ACTION_RESERVED_KEYS = {"type", "render"}

# Known, deferred gaps — Sprint 18 PLAN item 2 scopes these out
# explicitly. If one of these channels/keys gets a real catalog
# declaration later, remove it here so this test tightens automatically.
# `telegram_info` was closed 2026-10-12 (agent_grammar.yaml now declares
# it with a `gate` param) — removed from this set; see
# TestTelegramInfoChannelDeclared below for its own positive coverage.
_KNOWN_UNDECLARED_CHANNELS: set[str] = set()


def _channel_params_schema(channel: str) -> dict:
    for tok in grammar.SYSTEM_TOKENS:
        if tok.get("grammar_kind") == "notify" and tok.get("token_kind") == "channel" and tok.get("token") == channel:
            return tok.get("params_schema") or {}
    return {}


class TestSeededAgentsGateKeysAreDeclared:
    """Every real seeded event agent's extra `events[]`/`actions[]` keys
    must resolve against a declared schema — except the explicitly
    allow-listed, still-deferred gaps."""

    def test_every_channel_level_extra_key_is_declared_or_known_gap(self):
        undeclared: list[str] = []
        for agent in ea.SEEDED_AGENTS:
            for ev in agent.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                channel = ev.get("channel")
                if channel in _KNOWN_UNDECLARED_CHANNELS:
                    continue
                schema = _channel_params_schema(channel)
                for key in ev:
                    if key in _CHANNEL_ENTRY_RESERVED_KEYS:
                        continue
                    if key not in schema:
                        undeclared.append(f"{agent['slug']}:{channel}.{key}")
        assert undeclared == [], f"undeclared channel-level keys found: {undeclared}"

    def test_every_render_action_extra_key_is_declared(self):
        schema = grammar.get_render_action_params_schema()
        undeclared: list[str] = []
        for agent in ea.SEEDED_AGENTS:
            for act in agent.get("actions") or []:
                if not isinstance(act, dict) or act.get("type") != "render":
                    continue
                for key in act:
                    if key in _RENDER_ACTION_RESERVED_KEYS:
                        continue
                    if key not in schema:
                        undeclared.append(f"{agent['slug']}:actions.{key}")
        assert undeclared == [], f"undeclared render-action keys found: {undeclared}"

    def test_inventory_channel_level_gate_agent_count(self):
        """Regression guard on the confirmed inventory (Sprint 18, updated
        2026-10 audit fix for the 5 agents that replaced the deleted
        `alert_utils._alert_route()` dispatcher; 2026-10-23 order-poll
        consolidation added `order-correction-alert`, gated the same way
        as `order-failure-alert` since both are safety-critical and must
        never be silenced by a dev capability toggle): 14 of the real
        seeded agents carry a channel-level `gate` key. If this count
        changes, the commit's own inventory claim is now wrong — update
        both in lockstep, don't just bump the number here."""
        slugs = {
            agent["slug"]
            for agent in ea.SEEDED_AGENTS
            if any(isinstance(ev, dict) and "gate" in ev for ev in (agent.get("events") or []))
        }
        assert slugs == {
            "order-failure-alert", "order-correction-alert", "template-guard-alert",
            "template-attach-fail-alert",
            "mcp-ping-alert", "deploy-sync-alert", "agent-alert-rich",
            "market-summary", "gtt-not-accepted-alert",
            "gtt-asymmetric-alert", "oco-double-fire-alert",
            "ticker-recovered-alert", "ticker-degraded-alert", "visitor-report-alert",
        }

    def test_inventory_action_level_gate_agent_count(self):
        """Regression guard: only `error-alert` sets the action-level
        (render) `gate: true` today."""
        slugs = {
            agent["slug"]
            for agent in ea.SEEDED_AGENTS
            for act in (agent.get("actions") or [])
            if isinstance(act, dict) and act.get("type") == "render" and "gate" in act
        }
        assert slugs == {"error-alert"}
