"""2026-10 routing-tree ACTIVATION plan (SEED_VERSION 2 -> 3).

Covers:
  Step 1 — `AgentInfo.resolved_channels` (read-only, populated only for
           `kind='event'` rows, never mutates `events`).
  Step 2 — `_cmd_ai_refine`'s channel-fork guard for a tree-driven event
           agent (one whose own `events` is currently empty).
  Step 3 — the `SEED_VERSION` 2 -> 3 transition itself: a hash-match row
           proceeds to 'update'; a hash-mismatch (operator-edited) row
           stays 'modified_skip', unaffected by which version it's
           being bumped to.
  Step 6 — `seed_action()`'s `modified_skip` branch now ALSO logs a real
           WARNING through `_EVENTS_LOGGER` (not just stderr).
  Follow-up — `_cmd_ai_refine`'s `trade_mode` guard: a refine must never
           silently flip an existing agent's `trade_mode`, regardless of
           what `draft_agent_from_prompt()` (via `_clamp_safety`) returns.
           The CREATE path (`agent ai create`) keeps forcing paper on a
           fresh draft, unchanged — see `test_agent_ai_coverage.py::
           test_clamp_safety_forces_paper` for that existing regression
           guard; a thin pointer test is also included here for locality.

The catch-all reference fix (Step 4) is covered in
`test_alert_routing.py::test_unrouted_alert_agent_events_is_derived_from_catch_all_channels`,
not duplicated here.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.agents import (
    AgentController,
    _agent_to_info,
    _ai_refine_filtered_warnings,
    _ai_refine_is_tree_driven,
    _ai_refine_mentions_channel_intent,
    _ai_refine_mentions_trade_mode_intent,
    _ai_refine_writable_fields,
    _event_agent_resolved_channels,
)


# ── Shared test double ──────────────────────────────────────────────────
#
# Covers every attribute `_agent_to_info` reads (plain + getattr-guarded)
# — same convention as test_agents_given_column_wiring.py's own `_Row`.

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
    """Same pattern as test_agents_interpret_auth_fix.py's own helper —
    a MagicMock async_session() whose execute().scalar_one_or_none()
    returns `agent` on every call (both the pre-LLM read and the
    post-LLM write-session re-read may reuse the same object; that's
    fine for these tests since nothing between the two reads changes
    the row out from under them)."""
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
#  Step 1 — AgentInfo.resolved_channels
# ═══════════════════════════════════════════════════════════════════════

class TestResolvedChannels:
    def test_cycle_agent_resolves_to_empty_list(self):
        row = _make_row(kind="cycle", priority="critical", events=[])
        assert _event_agent_resolved_channels(row) == []

    def test_cycle_agent_via_agent_to_info_is_empty(self):
        row = _make_row(kind="cycle", priority="critical")
        info = _agent_to_info(row)
        assert info.resolved_channels == []

    def test_event_agent_with_seeded_slug_resolves_per_slug_override(self):
        row = _make_row(kind="event", slug="fill-alert", priority=None, events=[])
        assert _event_agent_resolved_channels(row) == [
            {"channel": "telegram", "enabled": True},
        ]

    def test_event_agent_explicit_events_wins_over_per_slug_override(self):
        explicit = [{"channel": "email", "enabled": True}]
        row = _make_row(kind="event", slug="fill-alert", priority=None, events=explicit)
        assert _event_agent_resolved_channels(row) == explicit

    def test_event_agent_via_agent_to_info_is_populated_and_events_untouched(self):
        """The core safety invariant (plan Design item 1): the display
        path must never write the resolved channel list into `events`
        itself — an innocent no-op Save on the Automation page would
        otherwise PUT the resolved value back and permanently fork the
        agent off the routing tree."""
        row = _make_row(kind="event", slug="fill-alert", priority=None, events=[])
        info = _agent_to_info(row)
        assert info.resolved_channels == [{"channel": "telegram", "enabled": True}]
        assert info.events == [], "events must stay exactly what the row carries, untouched"

    def test_unrecognised_event_agent_falls_through_to_catch_all(self):
        from backend.api.algo import alert_routing
        row = _make_row(kind="event", slug="not-a-real-seeded-slug", priority=None, events=[])
        assert _event_agent_resolved_channels(row) == alert_routing.CATCH_ALL_CHANNELS

    def test_resolved_channels_is_a_deep_copy_not_a_live_reference(self):
        """Mutating the returned list (or an entry inside it) must
        never reach the shared `alert_routing` tables — this is a
        read-only display field."""
        from backend.api.algo import alert_routing
        before = [dict(ch) for ch in alert_routing._PER_SLUG_OVERRIDES["fill-alert"]]
        row = _make_row(kind="event", slug="fill-alert", priority=None, events=[])
        resolved = _event_agent_resolved_channels(row)
        resolved[0]["enabled"] = False
        resolved.append({"channel": "__sentinel__"})
        assert alert_routing._PER_SLUG_OVERRIDES["fill-alert"] == before


# ═══════════════════════════════════════════════════════════════════════
#  Step 2 — `_cmd_ai_refine`'s channel-fork guard
# ═══════════════════════════════════════════════════════════════════════

class TestAiRefineTreeDrivenHelpers:
    def test_is_tree_driven_true_for_empty_events_event_agent(self):
        assert _ai_refine_is_tree_driven(_make_row(kind="event", events=[])) is True

    def test_is_tree_driven_true_for_none_events_event_agent(self):
        assert _ai_refine_is_tree_driven(_make_row(kind="event", events=None)) is True

    def test_is_tree_driven_false_for_event_agent_with_explicit_events(self):
        agent = _make_row(kind="event", events=[{"channel": "email", "enabled": True}])
        assert _ai_refine_is_tree_driven(agent) is False

    def test_is_tree_driven_false_for_cycle_agent(self):
        assert _ai_refine_is_tree_driven(_make_row(kind="cycle", events=[])) is False

    @pytest.mark.parametrize("prompt", [
        "send this to telegram too",
        "add an email channel",
        "notify via ntfy",
        "change the notification channel",
        "ALERT VIA EMAIL please",
        "add a webhook",
    ])
    def test_mentions_channel_intent_true(self, prompt):
        assert _ai_refine_mentions_channel_intent(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "tighten the loss threshold to 2%",
        "only fire during market hours",
        "rename this agent to something clearer",
    ])
    def test_mentions_channel_intent_false(self, prompt):
        assert _ai_refine_mentions_channel_intent(prompt) is False

    def test_writable_fields_excludes_events_for_tree_driven_row(self):
        fields = _ai_refine_writable_fields(_make_row(kind="event", events=[]))
        assert "events" not in fields
        assert "conditions" in fields and "actions" in fields and "cooldown_minutes" in fields

    def test_writable_fields_includes_events_for_non_tree_driven_event_agent(self):
        row = _make_row(kind="event", events=[{"channel": "email", "enabled": True}])
        assert "events" in _ai_refine_writable_fields(row)

    def test_writable_fields_includes_events_for_cycle_agent(self):
        assert "events" in _ai_refine_writable_fields(_make_row(kind="cycle", events=[]))


class TestCmdAiRefineChannelForkGuard:
    @pytest.mark.asyncio
    async def test_tree_driven_agent_rejects_channel_prompt_without_calling_llm(self):
        controller = AgentController.__new__(AgentController)
        agent = _make_row(slug="fill-alert", kind="event", events=[], priority=None)
        session_factory, _session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt") as fake_draft:
            resp = await controller._cmd_ai_refine("fill-alert", "send this to telegram too")

        fake_draft.assert_not_called()
        assert resp.success is False
        assert "alert_routing" in resp.output or "routing tree" in resp.output.lower()

    @pytest.mark.asyncio
    async def test_tree_driven_agent_cannot_have_events_written_even_if_llm_proposes_it(self):
        """The REAL protection (not the keyword-based early rejection
        above): even for a non-channel-shaped instruction, if the LLM's
        response happens to include a different `events` value, the
        write-back loop must silently drop it for a tree-driven agent."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="fill-alert", kind="event", events=[], priority=None,
            conditions={"log": {"tag": "orders"}},
            actions=[{"type": "render", "render": "fill"}],
            cooldown_minutes=30,
        )
        session_factory, _session = _mock_session_with_agent(agent)

        fake_draft = SimpleNamespace(
            draft={
                "conditions": {"log": {"tag": "orders"}},
                "events": [{"channel": "email", "enabled": True}],  # LLM "helpfully" invents a channel
                "actions": [{"type": "render", "render": "fill"}],
                "cooldown_minutes": 5,
            },
            errors=[], warnings=[], why_summary="tightened cooldown",
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("fill-alert", "lower the cooldown to 5 minutes")

        assert agent.events == [], "events must never be written for a tree-driven agent"
        assert resp.success is True
        assert "cooldown_minutes" in resp.output
        assert "events" not in resp.output.split("Changed:")[1].split("\n")[0]

    @pytest.mark.asyncio
    async def test_non_tree_driven_event_agent_can_still_have_events_changed(self):
        """Confirms the guard is scoped to tree-driven agents only — an
        event agent whose `events` is already explicitly non-empty
        (an operator-authored ALERT statement, or a pinned/legacy row)
        is unaffected and can still have its channels refined."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="my-custom-agent", kind="event",
            events=[{"channel": "telegram", "enabled": True}], priority=None,
            conditions={"log": {"tag": "orders"}}, actions=[],
        )
        session_factory, _session = _mock_session_with_agent(agent)

        fake_draft = SimpleNamespace(
            draft={
                "conditions": {"log": {"tag": "orders"}},
                "events": [{"channel": "email", "enabled": True}],
                "actions": [],
            },
            errors=[], warnings=[], why_summary="switched to email",
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("my-custom-agent", "switch notifications to email")

        assert agent.events == [{"channel": "email", "enabled": True}]
        assert "events" in resp.output


# ═══════════════════════════════════════════════════════════════════════
#  Follow-up — `_cmd_ai_refine`'s `trade_mode` guard. `_clamp_safety`
#  (agent_ai.py) unconditionally forces `draft["trade_mode"] = "paper"`
#  on every `draft_agent_from_prompt()` call. That's correct for the
#  CREATE path, but `_cmd_ai_refine` reuses the same call to refine an
#  EXISTING agent — without a guard, any refine (even one about an
#  unrelated field) would silently flip an already-live agent back to
#  paper via the diff-apply loop.
# ═══════════════════════════════════════════════════════════════════════

class TestAiRefineTradeModeGuardHelpers:
    @pytest.mark.parametrize("prompt", [
        "switch this to live",
        "set to live",
        "go live with this agent",
        "going live now",
        "change trade mode to paper",
        "switch to paper",
        "put it back in paper mode",
        "SET TO LIVE please",
        "switch to paper trading",
    ])
    def test_mentions_trade_mode_intent_true(self, prompt):
        assert _ai_refine_mentions_trade_mode_intent(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "tighten the loss threshold to 2%",
        "only fire during market hours",
        "rename this agent to something clearer",
        "lower the cooldown to 5 minutes",
        # Domain near-misses this app's own vocabulary could trigger on
        # a naive bare-substring "live"/"paper" check (the original,
        # too-broad implementation) — a word-boundary, mode-context
        # regex must not flag these.
        "only for delivery holdings",
        "use live cash instead of sod cash",
        "this agent is alive and well, just lower the threshold",
        # Even the narrower `\bto\s+(live|paper)\b` pattern false-positives
        # on this app's own "cash"/"price"/"ltp"/"quote" vocabulary unless
        # explicitly excluded via a negative lookahead.
        "switch from sod_cash to live cash",
        "compare against live price, not to live quotes",
        "switch scope to live ltp",
    ])
    def test_mentions_trade_mode_intent_false(self, prompt):
        assert _ai_refine_mentions_trade_mode_intent(prompt) is False

    def test_writable_fields_never_includes_trade_mode_for_cycle_agent(self):
        fields = _ai_refine_writable_fields(_make_row(kind="cycle", trade_mode="live"))
        assert "trade_mode" not in fields
        assert "conditions" in fields and "cooldown_minutes" in fields

    def test_writable_fields_never_includes_trade_mode_for_tree_driven_event_agent(self):
        fields = _ai_refine_writable_fields(_make_row(kind="event", events=[], trade_mode="live"))
        assert "trade_mode" not in fields
        assert "events" not in fields

    def test_writable_fields_never_includes_trade_mode_for_non_tree_driven_event_agent(self):
        row = _make_row(kind="event", events=[{"channel": "email", "enabled": True}], trade_mode="live")
        fields = _ai_refine_writable_fields(row)
        assert "trade_mode" not in fields
        assert "events" in fields

    def test_filtered_warnings_strips_the_now_false_paper_mode_claim(self):
        """Coupled to `_clamp_safety`'s REAL output, not a hand-typed copy
        of its wording — if that wording is ever reworded, this test (and
        the filter it exercises) must be updated together, instead of
        both silently drifting out of sync while staying green."""
        from backend.api.algo.agent_ai import _clamp_safety
        real_warnings: list[str] = []
        _clamp_safety({"trade_mode": "live"}, real_warnings)
        assert real_warnings, "fixture sanity: _clamp_safety must warn for a non-paper draft"

        warnings = real_warnings + ["Threshold -5000.0 on 'pnl' may fire on every tick."]
        assert _ai_refine_filtered_warnings(warnings) == [
            "Threshold -5000.0 on 'pnl' may fire on every tick.",
        ]

    def test_filtered_warnings_passthrough_when_nothing_to_strip(self):
        warnings = ["Removed destructive action 'cancel_all_orders'."]
        assert _ai_refine_filtered_warnings(warnings) == warnings


class TestCmdAiRefineTradeModeGuard:
    @pytest.mark.asyncio
    async def test_live_cycle_agent_unrelated_refine_leaves_trade_mode_unchanged(self):
        """The core regression this task fixes: refining a live,
        real-money `kind='cycle'` agent about something unrelated
        (cooldown) must never flip `trade_mode` to paper, even though
        `_clamp_safety` (via the mocked `draft_agent_from_prompt`) always
        returns `trade_mode: "paper"` in its draft AND its own real
        warning text claiming the mode changed — mirroring real
        production behavior byte-for-byte. The response must not repeat
        that now-false claim (see `_ai_refine_filtered_warnings`): an
        operator who read "AI agents land in paper mode (was 'live')"
        and believed it could be misled into flipping a genuinely-still-
        live agent's P/L chip to paper, which would be the exact
        opposite of what they wanted."""
        controller = AgentController.__new__(AgentController)
        # Non-destructive action type deliberately. `close_position` and
        # the rest of `_DESTRUCTIVE_ACTIONS` would trigger a SEPARATE,
        # PRE-EXISTING defect of the same class as the one this commit
        # fixes, out of scope here (reported separately, not fixed in
        # this commit per "fix one issue at a time"): `_clamp_safety`
        # silently strips any destructive action from the draft, and
        # `_cmd_ai_refine`'s diff loop then WRITES that stripped
        # (now-empty) `actions` list back onto the real row — e.g.
        # `agent ai refine loss-pos-total-auto-close "lower the
        # cooldown"` would silently delete its `chase_close_positions`
        # safety action, with only a "Removed destructive action" line
        # (worded as draft advice, not as "this was just deleted from
        # your live agent") to notice by. `lifespan_type` has an
        # analogous conditional defect: `_clamp_safety` defaults it to
        # 'one_shot' when the LLM's draft omits it, and `lifespan_type`
        # IS in `_AI_REFINE_WRITABLE_FIELDS`, so a persistent agent could
        # silently become one-shot. Choosing a non-destructive action
        # here keeps this test isolated to the one question it exists
        # to answer: did `trade_mode` get written.
        agent = _make_row(
            slug="live-nifty-agent", kind="cycle", trade_mode="live",
            events=[], conditions={"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            actions=[{"type": "log", "level": "info"}], cooldown_minutes=30,
        )
        session_factory, _session = _mock_session_with_agent(agent)

        # Real `_clamp_safety` output (not a hand-typed copy of its
        # wording) — a draft whose trade_mode differs from 'paper' always
        # gets forced to 'paper' plus a warning claiming so.
        from backend.api.algo.agent_ai import _clamp_safety
        real_draft = {
            "conditions": {"metric": "pnl", "scope": "total", "op": "<", "value": -5000},
            "actions": [{"type": "log", "level": "info"}],
            "cooldown_minutes": 5,
            "trade_mode": "live",
        }
        real_warnings: list[str] = []
        _clamp_safety(real_draft, real_warnings)
        assert real_draft["trade_mode"] == "paper"
        fake_draft = SimpleNamespace(
            draft=real_draft, errors=[], warnings=real_warnings,
            why_summary="tightened cooldown",
        )
        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt", return_value=fake_draft):
            resp = await controller._cmd_ai_refine("live-nifty-agent", "lower the cooldown to 5 minutes")

        assert agent.trade_mode == "live", "trade_mode must never be written by a refine"
        assert resp.success is True
        assert "cooldown_minutes" in resp.output
        assert "trade_mode" not in resp.output.split("Changed:")[1].split("\n")[0]
        assert "paper mode" not in resp.output, (
            "the now-false 'AI agents land in paper mode' warning must be "
            "filtered out of a refine response — trade_mode was never "
            "actually written"
        )

    @pytest.mark.asyncio
    async def test_explicit_trade_mode_instruction_rejected_without_calling_llm(self):
        controller = AgentController.__new__(AgentController)
        agent = _make_row(slug="live-nifty-agent", kind="cycle", trade_mode="live", events=[])
        session_factory, _session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt") as fake_draft:
            resp = await controller._cmd_ai_refine("live-nifty-agent", "switch this to live")

        fake_draft.assert_not_called()
        assert resp.success is False
        assert "P/L chip" in resp.output
        assert agent.trade_mode == "live"

    @pytest.mark.asyncio
    async def test_explicit_trade_mode_instruction_rejected_for_tree_driven_event_agent_too(self):
        """The trade_mode keyword check is unconditional — unlike the
        channel-intent check, it isn't gated on `tree_driven`."""
        controller = AgentController.__new__(AgentController)
        agent = _make_row(
            slug="fill-alert", kind="event", events=[], priority=None, trade_mode="paper",
        )
        session_factory, _session = _mock_session_with_agent(agent)

        with patch("backend.api.routes.agents.async_session", session_factory), \
             patch("backend.api.algo.agent_ai.draft_agent_from_prompt") as fake_draft:
            resp = await controller._cmd_ai_refine("fill-alert", "set to live")

        fake_draft.assert_not_called()
        assert resp.success is False
        assert "P/L chip" in resp.output


class TestTradeModeNoteNeverRejectedByValidation:
    """`_ai_refine_prompt_context` feeds the LLM a `trade_mode_note` key
    (read-only guidance) instead of the real `trade_mode` value. If an
    LLM "helpfully" echoes that key back verbatim in its draft JSON, the
    real validation path it would hit — `_validate_against_registry`,
    called from `draft_agent_from_prompt` (not re-exercised here; see the
    method docstring below for exact scope) — must never reject it on
    that account. Same structural pattern already relied on in
    production for `resolved_channels_note` (the tree-driven `events`
    equivalent)."""

    def test_validate_against_registry_ignores_unknown_trade_mode_note_key(self):
        """Differential, against the REAL `agent_evaluator.validate` (no
        mock) — proves only that adding `trade_mode_note` produces
        IDENTICAL errors to the same conditions tree without it, so this
        fails the moment anyone adds a top-level-key whitelist/rejection
        to `_validate_against_registry` that doesn't know about this key.
        Only covers `_validate_against_registry` itself, not the LLM's
        own `errors` field (parsed separately in `draft_agent_from_prompt`,
        uncovered by any unit test here) nor the diff-apply loop (already
        covered elsewhere — `trade_mode_note` isn't in
        `_AI_REFINE_WRITABLE_FIELDS`, so it's never written regardless)."""
        from backend.api.algo.agent_ai import _validate_against_registry
        cond = {"metric": "pnl", "scope": "total", "op": "<", "value": -1000}
        base_errors: list[str] = []
        with_note_errors: list[str] = []
        _validate_against_registry({"conditions": cond}, base_errors)
        _validate_against_registry(
            {"conditions": cond,
             "trade_mode_note": "READ-ONLY — not shown and cannot be changed by this command."},
            with_note_errors,
        )
        assert with_note_errors == base_errors


class TestClampSafetyCreatePathUnchanged:
    """Thin pointer regression guard, co-located with the refine-guard
    tests above for locality. The full unit coverage of `_clamp_safety`
    itself lives in `test_agent_ai_coverage.py::test_clamp_safety_forces_paper`
    — this confirms the module this task was told NOT to touch
    (`backend/api/algo/agent_ai.py`) still forces paper unconditionally
    on a fresh create-path draft."""

    def test_clamp_safety_still_forces_paper_on_create(self):
        from backend.api.algo.agent_ai import _clamp_safety
        draft = {"trade_mode": "live"}
        warnings: list[str] = []
        _clamp_safety(draft, warnings)
        assert draft["trade_mode"] == "paper"
        assert any("paper mode" in w for w in warnings)


# ═══════════════════════════════════════════════════════════════════════
#  Step 3 — SEED_VERSION 2 -> 3 transition
# ═══════════════════════════════════════════════════════════════════════

class TestSeedVersionBumpTransition:
    def test_seed_version_is_now_3(self):
        """Historical note (2026-10-25): a second, unrelated bump
        (3 -> 4, the tag-unification pass — see `event_agents.SEED_VERSION`'s
        own comment) landed after this 2 -> 3 transition was documented.
        This test now asserts the CURRENT live value rather than the
        historical "3" — the mechanism under test (seed_action()'s
        version-bump behavior) is unaffected by which version number is
        current. 2026-10-25: bumped again, 4 -> 5, for the alert-channel
        collapse (see event_agents.SEED_VERSION's own comment)."""
        from backend.api.algo.event_agents import SEED_VERSION
        assert SEED_VERSION == 5

    def test_hash_match_row_from_old_version_updates_at_new_version(self):
        """A row last seeded at the OLD SEED_VERSION=2, whose current
        content still hashes to its stored seed_spec_hash (no operator
        edit happened), proceeds to 'update' once evaluated against the
        new SEED_VERSION=3 — this is the exact shape of all 23 live
        seeded agents (re-verified clean against both prod and dev via
        scripts/alert_seed_audit.py immediately before this bump
        shipped)."""
        from backend.api.algo.event_agents import seed_action, compute_seed_spec_hash, SEED_VERSION
        cond, ev, act = {"log": {"tag": "gtt"}}, [], []
        row = SimpleNamespace(
            seed_version=2, seed_spec_hash=compute_seed_spec_hash(cond, ev, act),
            conditions=cond, events=ev, actions=act,
        )
        assert seed_action(row, current_version=SEED_VERSION) == "update"

    def test_hash_mismatch_row_from_old_version_is_still_modified_skip(self):
        """An operator-edited row (current content no longer hashes to
        what seeding last wrote) stays 'modified_skip' regardless of
        which version it's being bumped to — the 2 -> 3 bump changes
        NOTHING about this protection."""
        from backend.api.algo.event_agents import seed_action, compute_seed_spec_hash, SEED_VERSION
        cond, act = {"log": {"tag": "gtt"}}, []
        original_hash = compute_seed_spec_hash(cond, [{"channel": "ntfy"}], act)
        row = SimpleNamespace(
            seed_version=2, seed_spec_hash=original_hash,
            conditions=cond, events=[{"channel": "telegram"}], actions=act,
        )
        assert seed_action(row, current_version=SEED_VERSION) == "modified_skip"

    def test_row_already_at_new_version_is_kept(self):
        from backend.api.algo.event_agents import seed_action, SEED_VERSION
        row = SimpleNamespace(seed_version=SEED_VERSION)
        assert seed_action(row, current_version=SEED_VERSION) == "keep"


# ═══════════════════════════════════════════════════════════════════════
#  Step 6 — modified_skip also logs a real WARNING (not stderr-only)
# ═══════════════════════════════════════════════════════════════════════

class TestModifiedSkipLogsRealWarning:
    @pytest.mark.asyncio
    async def test_seed_event_agents_logs_stderr_and_a_real_warning(self, monkeypatch, capsys, caplog):
        from backend.api.algo import event_agents as ea

        spec = ea.FILL_AGENT
        original_hash = ea.compute_seed_spec_hash(spec["conditions"], spec.get("events", []), spec["actions"])
        edited_events = [{"channel": "email", "enabled": True}]
        row = SimpleNamespace(
            slug=spec["slug"], name=spec["name"], status="active", kind="event",
            seed_version=2, seed_spec_hash=original_hash,
            conditions=spec["conditions"], events=edited_events, actions=spec["actions"],
        )

        class _FakeScalars:
            def all(self):
                return []

        class _FakeResult:
            def scalar_one_or_none(self):
                return row

            def scalars(self):
                # 2026-10-25 alert-channel collapse — `seed_event_agents()`
                # now also runs `_retire_collapsed_event_agents()` via a
                # second `.scalars().all()` query; empty here since this
                # fixture's one synthetic row isn't one of the 16 retired
                # slugs.
                return _FakeScalars()

        class _FakeSession:
            async def execute(self, *_a, **_k):
                return _FakeResult()

            async def commit(self):
                pass

            def add(self, *_a, **_k):
                raise AssertionError("insert must not be attempted for an existing row")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))
        import backend.api.database as db
        monkeypatch.setattr(db, "async_session", lambda: _FakeSession())

        with caplog.at_level("WARNING", logger=ea._EVENTS_LOGGER.name):
            ea._EVENTS_LOGGER.addHandler(caplog.handler)
            try:
                await ea.seed_event_agents()
            finally:
                ea._EVENTS_LOGGER.removeHandler(caplog.handler)

        assert row.events == edited_events, "operator's edit must survive the seed pass"
        # 2026-10-25: SEED_VERSION moved 3 -> 4 -> 5 (tag-unification,
        # then alert-channel-collapse) — this test's mechanism
        # (modified_skip still bumps seed_version) is unaffected; see
        # the historical note on test_seed_version_is_now_3 above.
        assert row.seed_version == ea.SEED_VERSION == 5
        # stderr line preserved (an existing sibling test pins this too).
        assert "operator-modified" in capsys.readouterr().err
        # New Step 6 behaviour — a real, visible WARNING through
        # _EVENTS_LOGGER (not the skip-listed module `_logger`).
        assert any("skipped" in r.getMessage() for r in caplog.records)
        assert all(r.name == "backend.api.algo.events" for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════
#  End-to-end update branch — behavior-neutral claim, in CI (not just a
#  one-off live DB audit). A pre-migration row (real pre-migration
#  `events` literal, real matching hash, seed_version=2 i.e. OLD code's
#  version) run through the REAL `seed_event_agents()` at the NEW
#  SEED_VERSION=3 must land exactly where the live-DB audit predicted:
#  events cleared to [], priority set, and the channel set the routing
#  tree resolves for the updated row is byte-identical to the frozen
#  pre-migration oracle (`test_alert_routing.py:_FROZEN_ORACLE`).
# ═══════════════════════════════════════════════════════════════════════

class TestSeedVersionBumpUpdateBranchEndToEnd:
    @pytest.mark.asyncio
    async def test_hash_match_row_updates_and_resolves_to_frozen_oracle(self, monkeypatch):
        from backend.api.algo import event_agents as ea
        from backend.api.algo import alert_routing

        spec = ea.FILL_AGENT
        assert spec.get("priority") == "medium", "fixture sanity: FILL_AGENT's own priority token"

        # Pre-migration shape: the row still carries the OLD literal
        # `events` this agent used to dispatch on directly, hashed
        # against that OLD content (what seeding actually wrote the
        # last time it ran, at SEED_VERSION=2).
        pre_migration_events = [{"channel": "telegram", "enabled": True}]
        pre_migration_hash = ea.compute_seed_spec_hash(
            spec["conditions"], pre_migration_events, spec["actions"])
        row = SimpleNamespace(
            slug=spec["slug"], name=spec["name"], status="active", kind="event",
            seed_version=2, seed_spec_hash=pre_migration_hash,
            conditions=spec["conditions"], events=pre_migration_events,
            actions=spec["actions"], priority=None,
        )

        class _FakeScalars:
            def all(self):
                return []

        class _FakeResult:
            def scalar_one_or_none(self):
                return row

            def scalars(self):
                # 2026-10-25 alert-channel collapse — see the sibling
                # fixture above for why this is needed.
                return _FakeScalars()

        class _FakeSession:
            async def execute(self, *_a, **_k):
                return _FakeResult()

            async def commit(self):
                pass

            def add(self, *_a, **_k):
                raise AssertionError("insert must not be attempted for an existing row")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))
        import backend.api.database as db
        monkeypatch.setattr(db, "async_session", lambda: _FakeSession())

        await ea.seed_event_agents()

        assert row.events == [], "post-migration spec carries no events key -> cleared to []"
        assert row.priority == "medium"
        # 2026-10-25: SEED_VERSION moved 3 -> 4 -> 5 (tag-unification,
        # then alert-channel-collapse) —
        # see the historical note on test_seed_version_is_now_3 above.
        assert row.seed_version == ea.SEED_VERSION == 5
        new_hash = ea.compute_seed_spec_hash(row.conditions, row.events, row.actions)
        assert row.seed_spec_hash == new_hash, "hash must reflect the NEW write, not the old one"

        resolved = alert_routing.resolve_channels(
            name=row.slug, priority=row.priority, agent_events=row.events)
        assert resolved == pre_migration_events, (
            "the routing tree must resolve the updated (events=[]) row to the exact "
            "same channel set the pre-migration literal used to carry directly"
        )
