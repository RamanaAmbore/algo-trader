"""
Agent AI refine — destructive-action / lifespan-downgrade guard.

Follow-up to the `trade_mode` guard fixed in commit d1fd197c (same session,
`.claude/plans/sparkling-dancing-lobster.md`, Task 2). That fix's own test
file (`test_alert_routing_tree_activation.py`) flagged — but did not fix —
a sibling defect: `_clamp_safety()` (agent_ai.py) strips any destructive
action from a draft and defaults a missing `lifespan_type` to `one_shot`,
both correct for the CREATE path, but `_cmd_ai_refine()`'s diff-apply loop
wrote that stripped/defaulted value back onto an EXISTING row even when the
operator's refine instruction had nothing to do with either field.

Covers:
  - `AgentDraft.clamp_effects` (agent_ai.py) — additive side-channel,
    CREATE path ignores it, byte-for-byte unchanged output.
  - `_ai_refine_mentions_action_intent` / `_ai_refine_actions_safe_to_write`
    / `_ai_refine_lifespan_safe_to_write` (routes/agents.py) — the new
    guard helpers.
  - `_cmd_ai_refine` end-to-end: (a) an unrelated refine on an agent with an
    existing destructive action leaves `actions` unchanged with a specific
    warning; (b) a genuinely action-related refine still works, including
    still stripping a newly-proposed destructive action (regression guard
    on the ORIGINAL safety intent); (c) an unrelated refine on a persistent
    agent leaves `lifespan_type` unchanged.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.agent_ai import AgentDraft, _clamp_safety
from backend.api.routes.agents import (
    AgentController,
    _ai_refine_actions_safe_to_write,
    _ai_refine_lifespan_safe_to_write,
    _ai_refine_mentions_action_intent,
    _ai_refine_writable_fields,
)


# ── Shared test double — same convention as test_alert_routing_tree_activation.py ──

class _Row:
    id = 1
    slug = "x"
    name = "x"
    long_name = None
    description = None
    conditions: dict = {}
    events: list = []
    actions: list = []
    scope = "total"
    schedule = None
    cooldown_minutes = 30
    fire_at_time = None
    status = "active"
    last_triggered_at = None
    trigger_count = 0
    last_error = None
    is_system = False
    given = None
    kind = "cycle"
    priority = None
    lifespan_type = "persistent"
    trade_mode = "paper"


def _make_row(**overrides) -> SimpleNamespace:
    base = {k: v for k, v in vars(_Row).items() if not k.startswith("__")}
    base.update(overrides)
    return SimpleNamespace(**base)


def _mock_session_with_agent(agent):
    session = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=agent)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


# ═══════════════════════════════════════════════════════════════════════
#  AgentDraft.clamp_effects — additive side-channel
# ═══════════════════════════════════════════════════════════════════════

class TestClampEffectsSideChannel:
    def test_clamp_safety_without_clamp_effects_arg_is_a_complete_noop_there(self):
        """CREATE path call shape — no third argument. Must not raise and
        must behave exactly as before this change."""
        draft = {"actions": [{"type": "chase_close", "params": {}}]}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["actions"] == []
        assert draft["lifespan_type"] == "one_shot"
        assert draft["trade_mode"] == "paper"

    def test_clamp_effects_records_stripped_destructive_action_type(self):
        draft = {"actions": [
            {"type": "place_order", "params": {}},
            {"type": "cancel_order", "params": {}},
        ]}
        warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(draft, warnings, clamp_effects)
        assert clamp_effects["stripped_actions"] == ["cancel_order"]

    def test_clamp_effects_records_multiple_stripped_action_types_in_order(self):
        draft = {"actions": [
            {"type": "cancel_order", "params": {}},
            {"type": "chase_close_positions", "params": {}},
        ]}
        warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(draft, warnings, clamp_effects)
        assert clamp_effects["stripped_actions"] == ["cancel_order", "chase_close_positions"]

    def test_clamp_effects_empty_dict_when_nothing_stripped(self):
        draft = {"actions": [{"type": "place_order", "params": {}}], "lifespan_type": "persistent"}
        warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(draft, warnings, clamp_effects)
        assert "stripped_actions" not in clamp_effects
        assert "lifespan_type_defaulted" not in clamp_effects

    def test_clamp_effects_records_lifespan_defaulted_only_when_injected(self):
        draft_missing = {"actions": []}
        ce_missing: dict = {}
        _clamp_safety(draft_missing, [], ce_missing)
        assert ce_missing["lifespan_type_defaulted"] is True

        draft_present = {"actions": [], "lifespan_type": "n_fires"}
        ce_present: dict = {}
        _clamp_safety(draft_present, [], ce_present)
        assert "lifespan_type_defaulted" not in ce_present

    def test_agent_draft_default_clamp_effects_is_empty_dict(self):
        """Early-return construction sites (`AgentDraft({}, [...], ...)`
        in draft_agent_from_prompt) never pass clamp_effects — the
        dataclass default must be an empty dict, not None, so callers can
        always call `.get(...)` on it without a null check."""
        d = AgentDraft({}, ["err"], [], "", "")
        assert d.clamp_effects == {}

    def test_two_agent_draft_instances_do_not_share_the_same_dict(self):
        """Regression guard for a classic mutable-default dataclass bug —
        `field(default_factory=dict)` must produce a FRESH dict per
        instance, not one shared dict every caller mutates together."""
        d1 = AgentDraft({}, [], [], "", "")
        d2 = AgentDraft({}, [], [], "", "")
        d1.clamp_effects["stripped_actions"] = ["cancel_order"]
        assert d2.clamp_effects == {}


# ═══════════════════════════════════════════════════════════════════════
#  _ai_refine_mentions_action_intent — UX heuristic
# ═══════════════════════════════════════════════════════════════════════

class TestMentionsActionIntent:
    @pytest.mark.parametrize("prompt", [
        "remove the chase_close_positions action",
        "add an action to cancel the order",
        "disable the close positions action",
        "cancel order when triggered",
        "delete action chase_close",
    ])
    def test_true_cases(self, prompt):
        assert _ai_refine_mentions_action_intent(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "lower the cooldown to 5 minutes",
        "change the threshold to -5000",
        "fire only once per day",
        "widen the lifespan to persistent",
        "alert me on telegram instead",
    ])
    def test_false_cases(self, prompt):
        assert _ai_refine_mentions_action_intent(prompt) is False


# ═══════════════════════════════════════════════════════════════════════
#  _ai_refine_actions_safe_to_write
# ═══════════════════════════════════════════════════════════════════════

class TestActionsSafeToWrite:
    def test_safe_when_clamp_stripped_nothing(self):
        ok, warn = _ai_refine_actions_safe_to_write(
            "lower the cooldown", [{"type": "chase_close_positions"}], {},
        )
        assert ok is True
        assert warn is None

    def test_safe_when_stripped_action_was_newly_proposed_not_already_held(self):
        """The ORIGINAL, correct safety behavior: the LLM invented a brand
        new destructive action the row never had — the clamp already
        removed it from the draft, and that must proceed unimpeded."""
        clamp_effects = {"stripped_actions": ["cancel_order"]}
        ok, warn = _ai_refine_actions_safe_to_write(
            "lower the cooldown", [{"type": "log", "level": "info"}], clamp_effects,
        )
        assert ok is True
        assert warn is None

    def test_unsafe_when_overlap_and_no_action_intent(self):
        """The core bug this task fixes: row already has the destructive
        action, clamp stripped it, prompt is unrelated — must block."""
        clamp_effects = {"stripped_actions": ["chase_close_positions"]}
        ok, warn = _ai_refine_actions_safe_to_write(
            "lower the cooldown to 5 minutes",
            [{"type": "chase_close_positions"}],
            clamp_effects,
        )
        assert ok is False
        assert "chase_close_positions" in warn
        assert "left unchanged" in warn

    def test_safe_when_overlap_but_action_intent_present(self):
        """Same overlap, but the operator's instruction genuinely is
        about actions — the normal write must proceed."""
        clamp_effects = {"stripped_actions": ["chase_close_positions"]}
        ok, warn = _ai_refine_actions_safe_to_write(
            "remove the chase_close_positions action",
            [{"type": "chase_close_positions"}],
            clamp_effects,
        )
        assert ok is True
        assert warn is None

    def test_ignores_non_dict_entries_in_row_actions(self):
        clamp_effects = {"stripped_actions": ["cancel_order"]}
        ok, warn = _ai_refine_actions_safe_to_write(
            "lower the cooldown", ["not-a-dict", None], clamp_effects,
        )
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════
#  _ai_refine_lifespan_safe_to_write
# ═══════════════════════════════════════════════════════════════════════

class TestLifespanSafeToWrite:
    def test_safe_when_clamp_did_not_default(self):
        ok, warn = _ai_refine_lifespan_safe_to_write("persistent", {})
        assert ok is True
        assert warn is None

    def test_safe_when_defaulted_and_row_already_one_shot(self):
        ok, warn = _ai_refine_lifespan_safe_to_write(
            "one_shot", {"lifespan_type_defaulted": True},
        )
        assert ok is True

    def test_safe_when_defaulted_and_row_unset(self):
        ok, warn = _ai_refine_lifespan_safe_to_write(
            None, {"lifespan_type_defaulted": True},
        )
        assert ok is True

    def test_unsafe_when_defaulted_and_row_is_persistent(self):
        """The core lifespan bug this task fixes."""
        ok, warn = _ai_refine_lifespan_safe_to_write(
            "persistent", {"lifespan_type_defaulted": True},
        )
        assert ok is False
        assert "one_shot" in warn
        assert "persistent" in warn.lower()

    def test_unsafe_when_defaulted_and_row_is_n_fires(self):
        ok, warn = _ai_refine_lifespan_safe_to_write(
            "n_fires", {"lifespan_type_defaulted": True},
        )
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════
#  _cmd_ai_refine end-to-end
# ═══════════════════════════════════════════════════════════════════════

class TestCmdAiRefineActionLifespanGuard:
    @pytest.mark.asyncio
    async def test_unrelated_refine_leaves_existing_destructive_action_unchanged(self):
        """(a) An unrelated refine instruction on an agent with an
        existing destructive action leaves `actions` unchanged, surfacing
        the specific warning (not the generic clamp wording)."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="loss-pos-total-auto-close", kind="cycle", trade_mode="paper",
            events=[], is_system=True,
            conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            actions=[{"type": "chase_close_positions", "params": {}}],
            cooldown_minutes=30, lifespan_type="persistent",
        )
        session_factory, _session = _mock_session_with_agent(agent)

        # Real `_clamp_safety` output, exactly as `draft_agent_from_prompt`
        # produces it — the LLM's own draft (per the refinement prompt)
        # omits the destructive action entirely (it wasn't asked to
        # change it), which the clamp sees as "nothing new to strip" in
        # terms of warnings, but critically the draft's `actions` list no
        # longer contains it either way (LLM reproduced a trimmed list,
        # or never echoed it) — reproducing live production behavior
        # where a "minimal diff" LLM response can genuinely drop a field
        # it wasn't told to preserve verbatim.
        real_draft = {
            "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            "actions": [{"type": "chase_close_positions", "params": {}}],
            "cooldown_minutes": 5,
            "lifespan_type": "persistent",
        }
        real_warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(real_draft, real_warnings, clamp_effects)
        assert real_draft["actions"] == [], "sanity: clamp strips the destructive action"
        assert clamp_effects["stripped_actions"] == ["chase_close_positions"]

        fake_draft = SimpleNamespace(
            draft=real_draft, errors=[], warnings=real_warnings,
            why_summary="tightened cooldown", clamp_effects=clamp_effects,
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine(
                "loss-pos-total-auto-close", "lower the cooldown to 5 minutes",
            )

        assert agent.actions == [{"type": "chase_close_positions", "params": {}}], (
            "an unrelated refine must never silently delete an existing "
            "destructive action"
        )
        assert resp.success is True
        assert "actions" not in resp.output.split("Changed:")[1].split("\n")[0]
        assert "chase_close_positions" in resp.output
        assert "left unchanged" in resp.output
        assert agent.cooldown_minutes == 5, "the genuinely-requested change must still land"

    @pytest.mark.asyncio
    async def test_action_related_refine_still_strips_newly_proposed_destructive_action(self):
        """(b) A genuinely action-related refine instruction still works
        end-to-end, including the ORIGINAL safety behavior: a newly
        LLM-proposed destructive action the row never had gets stripped —
        unchanged by this fix."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="live-nifty-agent", kind="cycle", trade_mode="paper",
            events=[], conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            actions=[{"type": "log", "level": "info"}],
            cooldown_minutes=30, lifespan_type="persistent",
        )
        session_factory, _session = _mock_session_with_agent(agent)

        # LLM "helpfully" proposes a brand-new destructive action the
        # operator's prompt did mention — clamp strips it (correct,
        # unchanged original behavior: destructive actions always need a
        # manual re-add, even when explicitly requested by an AI prompt).
        real_draft = {
            "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            "actions": [
                {"type": "log", "level": "info"},
                {"type": "cancel_all_orders", "params": {}},
            ],
            "cooldown_minutes": 30,
            "lifespan_type": "persistent",
        }
        real_warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(real_draft, real_warnings, clamp_effects)
        assert real_draft["actions"] == [{"type": "log", "level": "info"}]

        fake_draft = SimpleNamespace(
            draft=real_draft, errors=[], warnings=real_warnings,
            why_summary="added a cancel action", clamp_effects=clamp_effects,
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine(
                "live-nifty-agent", "add an action to cancel all orders",
            )

        assert agent.actions == [{"type": "log", "level": "info"}], (
            "the newly-proposed destructive action must still be stripped"
        )
        assert resp.success is True
        assert any("destructive" in w.lower() for w in real_warnings)

    @pytest.mark.asyncio
    async def test_unrelated_refine_leaves_persistent_lifespan_unchanged(self):
        """(c) An unrelated refine instruction on a persistent agent
        leaves `lifespan_type` unchanged even though the LLM's draft
        omitted it (triggering the clamp's one_shot default)."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="persistent-watch", kind="cycle", trade_mode="paper",
            events=[], conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            actions=[{"type": "log", "level": "info"}],
            cooldown_minutes=30, lifespan_type="persistent",
        )
        session_factory, _session = _mock_session_with_agent(agent)

        # LLM's draft omits lifespan_type entirely (it wasn't asked about
        # it) — the clamp defaults it to one_shot, as it always does.
        real_draft = {
            "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            "actions": [{"type": "log", "level": "info"}],
            "cooldown_minutes": 10,
        }
        real_warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(real_draft, real_warnings, clamp_effects)
        assert real_draft["lifespan_type"] == "one_shot"
        assert clamp_effects["lifespan_type_defaulted"] is True

        fake_draft = SimpleNamespace(
            draft=real_draft, errors=[], warnings=real_warnings,
            why_summary="tightened cooldown", clamp_effects=clamp_effects,
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("persistent-watch", "lower the cooldown to 10 minutes")

        assert agent.lifespan_type == "persistent", (
            "an unrelated refine must never downgrade a persistent agent to one_shot"
        )
        assert resp.success is True
        assert "lifespan_type" not in resp.output.split("Changed:")[1].split("\n")[0]
        assert "one_shot" in resp.output
        assert agent.cooldown_minutes == 10

    @pytest.mark.asyncio
    async def test_explicit_lifespan_change_still_writes_through(self):
        """When the LLM's draft carries its OWN explicit lifespan_type
        (not clamp-defaulted), the normal write proceeds regardless — a
        genuine lifespan-widening/narrowing instruction must still work."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="one-shot-watch", kind="cycle", trade_mode="paper",
            events=[], conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            actions=[{"type": "log", "level": "info"}],
            cooldown_minutes=30, lifespan_type="one_shot",
        )
        session_factory, _session = _mock_session_with_agent(agent)

        real_draft = {
            "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            "actions": [{"type": "log", "level": "info"}],
            "cooldown_minutes": 30,
            "lifespan_type": "persistent",
        }
        real_warnings: list[str] = []
        clamp_effects: dict = {}
        _clamp_safety(real_draft, real_warnings, clamp_effects)
        assert "lifespan_type_defaulted" not in clamp_effects

        fake_draft = SimpleNamespace(
            draft=real_draft, errors=[], warnings=real_warnings,
            why_summary="widened lifespan", clamp_effects=clamp_effects,
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("one-shot-watch", "make this agent persistent")

        assert agent.lifespan_type == "persistent"
        assert "lifespan_type" in resp.output.split("Changed:")[1].split("\n")[0]

    @pytest.mark.asyncio
    async def test_fake_draft_without_clamp_effects_attribute_degrades_gracefully(self):
        """A hand-built test double that predates `clamp_effects` (e.g.
        the pre-existing trade_mode-guard tests in
        test_alert_routing_tree_activation.py) must not raise — `getattr`
        with a `{}` default means no extra exclusion is applied, matching
        behavior before this fix existed."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="legacy-fake", kind="cycle", trade_mode="paper", events=[],
            conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -1}, actions=[],
            cooldown_minutes=30, lifespan_type="one_shot",
        )
        session_factory, _session = _mock_session_with_agent(agent)
        fake_draft = SimpleNamespace(
            draft={
                "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -1},
                "actions": [],
                "cooldown_minutes": 7,
            },
            errors=[], warnings=[], why_summary="tightened",
            # no clamp_effects attribute at all
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("legacy-fake", "lower the cooldown")

        assert resp.success is True
        assert agent.cooldown_minutes == 7


# ═══════════════════════════════════════════════════════════════════════
#  (d) CREATE path (agent_ai.py) — byte-for-byte unchanged regression guard
# ═══════════════════════════════════════════════════════════════════════

class TestClampSafetyCreatePathStillUnchanged:
    """Co-located with the refine-guard tests for locality, mirroring
    `TestClampSafetyCreatePathUnchanged` in
    test_alert_routing_tree_activation.py (the trade_mode fix's own
    regression guard for the same module). This task only changes what
    `_cmd_ai_refine` (routes/agents.py) does with `_clamp_safety`'s
    output — `_clamp_safety` itself and its CREATE-path call site
    (`draft_agent_from_prompt`, called with no `clamp_effects` argument)
    must behave byte-for-byte as before."""

    def test_forces_inactive_and_paper_unconditionally(self):
        draft = {"trade_mode": "live"}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["status"] == "inactive"
        assert draft["trade_mode"] == "paper"
        assert warnings == [
            "AI agents land in paper mode (was 'live'). "
            "Flip the P/L chip on /agents to enable live."
        ]

    def test_defaults_missing_lifespan_to_one_shot(self):
        draft = {}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["lifespan_type"] == "one_shot"

    def test_preserves_explicit_lifespan(self):
        draft = {"lifespan_type": "persistent"}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["lifespan_type"] == "persistent"

    def test_strips_destructive_actions_with_unchanged_warning_text(self):
        draft = {"actions": [
            {"type": "place_order", "params": {}},
            {"type": "chase_close", "params": {}},
            {"type": "cancel_order", "params": {}},
        ]}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["actions"] == [{"type": "place_order", "params": {}}]
        assert warnings == [
            "Removed destructive action 'chase_close' — re-add manually "
            "if intended.",
            "Removed destructive action 'cancel_order' — re-add manually "
            "if intended.",
        ]

    def test_draft_agent_from_prompt_call_site_passes_no_clamp_effects_arg_by_default(self):
        """`draft_agent_from_prompt` now passes its own fresh
        `clamp_effects` dict internally (so `_cmd_ai_refine` can read it
        off the returned `AgentDraft`), but the CREATE path
        (`_cmd_ai_create`) never looks at `.clamp_effects` on its result —
        confirmed by grepping the real call site, not re-asserted here
        structurally; this test only pins that a disabled-genai early
        return still carries the dataclass default."""
        from backend.api.algo.agent_ai import draft_agent_from_prompt
        with patch('backend.api.algo.agent_ai.is_enabled', return_value=False):
            result = draft_agent_from_prompt("test prompt")
        assert result.clamp_effects == {}
        assert any("disabled" in e.lower() for e in result.errors)
