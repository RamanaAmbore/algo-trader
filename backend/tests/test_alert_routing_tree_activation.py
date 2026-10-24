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
    _ai_refine_is_tree_driven,
    _ai_refine_mentions_channel_intent,
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
#  Step 3 — SEED_VERSION 2 -> 3 transition
# ═══════════════════════════════════════════════════════════════════════

class TestSeedVersionBumpTransition:
    def test_seed_version_is_now_3(self):
        from backend.api.algo.event_agents import SEED_VERSION
        assert SEED_VERSION == 3

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

        class _FakeResult:
            def scalar_one_or_none(self):
                return row

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
        assert row.seed_version == ea.SEED_VERSION == 3
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

        class _FakeResult:
            def scalar_one_or_none(self):
                return row

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
        assert row.seed_version == ea.SEED_VERSION == 3
        new_hash = ea.compute_seed_spec_hash(row.conditions, row.events, row.actions)
        assert row.seed_spec_hash == new_hash, "hash must reflect the NEW write, not the old one"

        resolved = alert_routing.resolve_channels(
            name=row.slug, priority=row.priority, agent_events=row.events)
        assert resolved == pre_migration_events, (
            "the routing tree must resolve the updated (events=[]) row to the exact "
            "same channel set the pre-migration literal used to carry directly"
        )
