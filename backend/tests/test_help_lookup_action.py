"""
Item 3 (`help` action) + item 5 (structural DEBUG exemption) + item 7
(`emit_log` -> `log` rename, new `priority` param) coverage.

`help_lookup` (`backend/api/algo/actions.py`) is the resolver for the new
DSL `help` action — read-only catalog introspection, logged at DEBUG only.
It reverse-resolves a token name via `GrammarRegistry.find_tokens()` (new
helper, `grammar_registry.py`), then renders the catalog `description`,
the resolved callable's parameter signature, and its docstring for every
matching row — a name can be reused across `token_kind`s (e.g. a notify
`channel` token and an `action_type` token sharing the same string, which
is the REAL shape of the live catalog after this round's rename: a
`(notify, channel, log)` token already existed before this change, and
the renamed `(action, action_type, log)` token now shares that string).

`event_agents.py:_dispatch_agent`'s structural DEBUG exemption drops any
DEBUG-level record before even evaluating the matching agent's own
`conditions` — this is the direct regression guard for "a DEBUG-level
record must never reach a real notification channel, regardless of what
any event agent's own conditions claim to match."

`_rec_tracked_ntfy_priority` (same file) resolves a hand-typed
`log(level=..., priority=..., message=...)` call's explicit `priority`
param, overriding the matching event agent's own tier-derived ntfy
default — `debug(x)`'s compiled sugar never sets `priority`, so it always
falls back to the pre-existing tier-derived resolution unchanged.

Perf: no DB/network calls — reload() is exercised against a session-mock,
following the exact `_make_fake_grammar_token` / `_make_grammar_db_session_
mock` pattern `test_grammar_registry.py` already uses (duplicated here for
test-file self-containment, same convention other test files in this
suite use for the same harness).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
#  Shared harness — real (non-mocked) REGISTRY.reload() against a
#  session-mock, same pattern as test_grammar_registry.py.
# ═══════════════════════════════════════════════════════════════════════

def _make_fake_grammar_token(token_id, grammar_kind, token_kind, token,
                              resolver=None, is_active=True, description="",
                              params_schema=None):
    return SimpleNamespace(
        id=token_id,
        grammar_kind=grammar_kind,
        token_kind=token_kind,
        token=token,
        resolver=resolver,
        is_active=is_active,
        description=description,
        value_type=None,
        units=None,
        enum_values=None,
        params_schema=params_schema,
        template_body="fake template ${x}" if token_kind == "template" else "",
        source={},
    )


def _make_grammar_db_session_mock(rows):
    async def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _reload_with_rows(rows):
    """Sync helper — safe to call from a plain (non-async) fixture, where
    there is no event loop already running."""
    from backend.api.algo.grammar_registry import REGISTRY
    mock_session = _make_grammar_db_session_mock(rows)
    with patch("backend.api.database.async_session", return_value=mock_session):
        asyncio.run(REGISTRY.reload())


async def _areload_with_rows(rows):
    """Async-safe sibling — `asyncio.run()` cannot be called from inside
    an already-running event loop (pytest-asyncio's own loop), so a test
    coroutine that needs to re-reload mid-test must await this instead."""
    from backend.api.algo.grammar_registry import REGISTRY
    mock_session = _make_grammar_db_session_mock(rows)
    with patch("backend.api.database.async_session", return_value=mock_session):
        await REGISTRY.reload()


# Real, importable (ctx, row) -> value style functions with genuine
# docstrings/signatures, used as fake resolvers below — `_import_dotted`
# just does `importlib.import_module` + `getattr`, so pointing a fake
# row's resolver back at this very module works fine.

def _fake_metric_fn(ctx, row):
    """Return a fake pnl value for this matched row."""
    return 0.0


def _fake_scope_fn(ctx, account=None):
    """Yield every row in this fake scope."""
    return []


def _fake_channel_cls(title, body, tg=None):
    """Fake notify channel sender."""
    return None


def _fake_format_fn(value):
    """Format a fake value for display."""
    return str(value)


def _fixture_rows():
    return [
        _make_fake_grammar_token(
            1, "condition", "metric", "fake_pnl",
            resolver="backend.tests.test_help_lookup_action._fake_metric_fn",
            description="Fake pnl metric for help_lookup tests.",
        ),
        _make_fake_grammar_token(
            2, "condition", "scope", "fake_scope",
            resolver="backend.tests.test_help_lookup_action._fake_scope_fn",
            description="Fake scope for help_lookup tests.",
        ),
        # Real operator token — no resolver of its own (operators are
        # seeded from grammar.py's OPERATORS dict and merged into
        # tables['operators'] BEFORE the DB-row loop runs; a bare DB row
        # for an already-known operator doesn't need its own resolver to
        # resolve via REGISTRY.op()), but it must still exist as a DB row
        # so `find_tokens('>')` can find it.
        _make_fake_grammar_token(
            3, "condition", "operator", ">",
            resolver=None,
            description="Greater than.",
        ),
        _make_fake_grammar_token(
            4, "action", "action_type", "fake_action",
            resolver="backend.api.algo.actions.emit_log",
            description="Fake action for help_lookup tests.",
            params_schema={"message": {"type": "string", "required": True}},
        ),
        _make_fake_grammar_token(
            5, "notify", "channel", "fake_channel",
            resolver="backend.tests.test_help_lookup_action._fake_channel_cls",
            description="Fake notify channel.",
        ),
        _make_fake_grammar_token(
            6, "notify", "format", "fake_format",
            resolver="backend.tests.test_help_lookup_action._fake_format_fn",
            description="Fake notify format.",
        ),
        _make_fake_grammar_token(
            7, "notify", "template", "fake_template",
            resolver=None,
            description="Fake notify template.",
        ),
        # Dead/unwired resolver — a catalog row this codebase documents
        # as a real possibility (CLAUDE.md's "7 dead grammar-resolver
        # stub functions" precedent): no resolver at all.
        _make_fake_grammar_token(
            8, "action", "action_type", "dead_action",
            resolver=None,
            description="A dead action stub with no resolver.",
        ),
        # Multiple-match case: the SAME token string "shared_name" reused
        # across two different token_kinds — must return BOTH matches,
        # never guess which one was meant. Mirrors the real live catalog
        # shape after this round's rename: "log" is both a pre-existing
        # (notify, channel, log) token (no resolver) and the newly
        # renamed (action, action_type, log) token.
        _make_fake_grammar_token(
            9, "notify", "channel", "shared_name",
            resolver=None,
            description="Shared-name channel.",
        ),
        _make_fake_grammar_token(
            10, "action", "action_type", "shared_name",
            resolver="backend.api.algo.actions.emit_log",
            description="Shared-name action.",
        ),
    ]


@pytest.fixture(autouse=True)
def _reload_fixture_catalog():
    _reload_with_rows(_fixture_rows())
    yield


# ═══════════════════════════════════════════════════════════════════════
#  help_lookup — per-token_kind resolution
# ═══════════════════════════════════════════════════════════════════════

class TestHelpLookupResolution:
    @pytest.mark.asyncio
    async def test_metric_resolves_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_pnl"})
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["token_kind"] == "metric"
        assert m["description"] == "Fake pnl metric for help_lookup tests."
        assert m["signature"] == "(ctx, row)"
        assert "fake pnl value" in m["docstring"].lower()

    @pytest.mark.asyncio
    async def test_scope_resolves_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_scope"})
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["token_kind"] == "scope"
        assert m["signature"] == "(ctx, account=None)"
        assert "fake scope" in m["docstring"].lower()

    @pytest.mark.asyncio
    async def test_operator_resolves_via_base_operators_table(self):
        """`>` has no resolver of its own — it resolves via the base
        OPERATORS dict grammar.py seeds into the registry unconditionally
        (see `GrammarRegistry.reload()`). `find_tokens` must still find
        the catalog ROW (for its description), and `.op()` must still
        resolve a real, signature-introspectable lambda."""
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": ">"})
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["token_kind"] == "operator"
        assert m["description"] == "Greater than."
        assert m["signature"] == "(a, b)"
        assert m["docstring"] == "(no docstring)"

    @pytest.mark.asyncio
    async def test_action_type_resolves_via_fn_key(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_action"})
        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["token_kind"] == "action_type"
        # resolver points at the real emit_log function
        assert "params" in m["signature"]
        assert m["docstring"] != "(no docstring)"

    @pytest.mark.asyncio
    async def test_channel_resolves_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_channel"})
        m = result["matches"][0]
        assert m["token_kind"] == "channel"
        assert m["signature"] == "(title, body, tg=None)"
        assert "fake notify channel sender" in m["docstring"].lower()

    @pytest.mark.asyncio
    async def test_format_resolves_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_format"})
        m = result["matches"][0]
        assert m["token_kind"] == "format"
        assert m["signature"] == "(value)"
        assert "format a fake value" in m["docstring"].lower()

    @pytest.mark.asyncio
    async def test_template_resolves_body_as_a_non_callable_value(self):
        """A `template` token's bound value is a BODY STRING, not a
        callable — `.template()` returns the real body text. `_help_
        reflect` must render it explicitly (the actual value, truncated)
        rather than a misleading `str.__doc__`, and never silently claim
        "unavailable" when there IS a real, non-callable value."""
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "fake_template"})
        m = result["matches"][0]
        assert m["token_kind"] == "template"
        assert m["description"] == "Fake notify template."
        assert "fake template" in m["signature"]
        assert m["docstring"] == "(no docstring — not a callable)"

    @pytest.mark.asyncio
    async def test_dead_resolver_renders_explicit_unavailable_text_never_silent(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "dead_action"})
        m = result["matches"][0]
        assert m["description"] == "A dead action stub with no resolver."
        assert m["signature"] == "(signature unavailable)"
        assert m["docstring"] == "(no docstring)"

    @pytest.mark.asyncio
    async def test_missing_description_renders_explicit_placeholder(self):
        """A catalog row with an empty `description` (the model's own
        NOT NULL default) must still render something explicit, never
        an empty string silently."""
        from backend.api.algo.actions import help_lookup

        await _areload_with_rows([
            _make_fake_grammar_token(
                99, "action", "action_type", "no_desc_action",
                resolver="backend.tests.test_help_lookup_action._fake_metric_fn",
                description="",
            ),
        ])
        result = await help_lookup({}, {"name": "no_desc_action"})
        m = result["matches"][0]
        assert m["description"] == "(no catalog description)"

    @pytest.mark.asyncio
    async def test_multiple_matches_across_token_kinds_returns_all(self):
        """A name reused across token_kinds (the live catalog's own
        real shape for "log" post-rename) must return EVERY match, never
        guess which single one was meant."""
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "shared_name"})
        assert len(result["matches"]) == 2
        kinds = {m["token_kind"] for m in result["matches"]}
        assert kinds == {"channel", "action_type"}

    @pytest.mark.asyncio
    async def test_unknown_name_returns_no_matches_not_an_error(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "totally_unknown_xyz"})
        assert result["matches"] == []

    @pytest.mark.asyncio
    async def test_no_name_provided_degrades_gracefully(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {})
        assert result["matches"] == []
        assert result["name"] == ""


# ═══════════════════════════════════════════════════════════════════════
#  help_lookup never alert-eligible — no tags/alert_event extra, DEBUG only
# ═══════════════════════════════════════════════════════════════════════

class TestHelpLookupNeverAlertEligible:
    @pytest.mark.asyncio
    async def test_help_logs_at_debug_with_no_extra_kwarg(self, monkeypatch):
        import backend.api.algo.actions as actions_mod
        from backend.api.algo.actions import help_lookup

        calls = []

        def _capture(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(actions_mod.logger, "debug", _capture)
        await help_lookup({}, {"name": "fake_pnl"})

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert kwargs == {}, "help_lookup's log call must carry no extra={...} at all"

    @pytest.mark.asyncio
    async def test_help_dispatched_via_noop_handlers_is_wired(self):
        from backend.api.algo.actions import _al_run_noop_handler

        agent = SimpleNamespace(slug="test-agent")
        ok = await _al_run_noop_handler(agent, "help", {"name": "fake_pnl"}, {})
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════
#  Item 7 — `emit_log` -> `log` rename, new `priority` param
# ═══════════════════════════════════════════════════════════════════════

class TestLogPriorityParam:
    @pytest.mark.asyncio
    async def test_log_dispatch_key_resolves_to_emit_log_handler(self):
        """The DSL dispatch key is "log" post-rename — resolves to the
        exact same Python function (`emit_log`, name unchanged)."""
        from backend.api.algo.actions import _NOOP_ACTION_HANDLERS, _al_run_noop_handler

        assert "log" in _NOOP_ACTION_HANDLERS
        assert "emit_log" not in _NOOP_ACTION_HANDLERS
        assert _NOOP_ACTION_HANDLERS["log"].attr == "emit_log"

        agent = SimpleNamespace(slug="test-agent")
        ok = await _al_run_noop_handler(agent, "log", {"message": "hi"}, {})
        assert ok is True

    @pytest.mark.asyncio
    async def test_emit_log_with_priority_attaches_extra_and_returns_it(self, monkeypatch):
        import backend.api.algo.actions as actions_mod
        from backend.api.algo.actions import emit_log

        calls = []
        monkeypatch.setattr(
            actions_mod.logger, "warning",
            lambda *a, **kw: calls.append((a, kw)),
        )
        result = await emit_log({}, {"level": "warning", "priority": "urgent", "message": "x"})
        assert result["priority"] == "urgent"
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs == {"extra": {"priority": "urgent"}}

    @pytest.mark.asyncio
    async def test_emit_log_with_no_priority_passes_no_extra_kwarg(self, monkeypatch):
        """Zero-regression check — every pre-existing caller (no
        `priority` param) must get the EXACT same single-positional-arg
        logger call as before this change."""
        import backend.api.algo.actions as actions_mod
        from backend.api.algo.actions import emit_log

        calls = []
        monkeypatch.setattr(
            actions_mod.logger, "info",
            lambda *a, **kw: calls.append((a, kw)),
        )
        result = await emit_log({}, {"message": "plain"})
        assert result["priority"] is None
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert kwargs == {}
        assert len(args) == 1

    @pytest.mark.asyncio
    async def test_debug_sugar_compiled_record_carries_no_priority(self, monkeypatch):
        """`debug(x)` always compiles to `log(level="debug", message=...)`
        and nothing else — no priority, no urgency. Simulates the
        compiled call shape directly (the frontend compiler's own output
        is out of scope here)."""
        import backend.api.algo.actions as actions_mod
        from backend.api.algo.actions import emit_log

        calls = []
        monkeypatch.setattr(
            actions_mod.logger, "debug",
            lambda *a, **kw: calls.append((a, kw)),
        )
        result = await emit_log({}, {"level": "debug", "message": "ping"})
        assert result["priority"] is None
        assert calls[0][1] == {}


# ═══════════════════════════════════════════════════════════════════════
#  Item 5 — structural DEBUG exemption in event_agents.py:_dispatch_agent
# ═══════════════════════════════════════════════════════════════════════

class TestDebugExemption:
    @pytest.mark.asyncio
    async def test_debug_level_record_never_dispatches_even_when_conditions_match(self):
        """The direct regression guard for item 5: a DEBUG-level record
        with its `conditions` match forced to True (simulating a real
        seeded event agent whose condition WOULD match) must still NOT
        dispatch — the exemption is checked before `evaluate()` is even
        called. This test FAILS against the pre-fix code (no DEBUG
        check), since `evaluate` is patched to always return True."""
        import backend.api.algo.event_agents as ea

        agent = MagicMock()
        agent.slug = "fake-event-agent"
        agent.tier = "medium"
        agent.conditions = {}
        agent.actions = [{"type": "render", "render": "fake"}]
        agent.events = [{"channel": "ntfy", "enabled": True}]

        rec = {"level": "DEBUG", "extra": {"alert_event": "fake"}, "logger": "x", "message": "m"}

        with patch.object(ea, "evaluate", return_value=True) as mock_evaluate, \
             patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
             patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
            sent = await ea._dispatch_agent(agent, [rec])

        assert sent == 0
        mock_send.assert_not_called()
        # The exemption runs BEFORE conditions are evaluated at all.
        mock_evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_debug_level_record_dispatches_normally(self):
        """Regression: a non-DEBUG record with a matching condition must
        still dispatch exactly as before this change."""
        import backend.api.algo.event_agents as ea

        agent = MagicMock()
        agent.slug = "fake-event-agent"
        agent.tier = "medium"
        agent.conditions = {}
        agent.actions = [{"type": "render", "render": "fake"}]
        agent.events = [{"channel": "telegram", "enabled": True}]

        rec = {"level": "INFO", "extra": {}, "logger": "x", "message": "m"}

        with patch.object(ea, "evaluate", return_value=True), \
             patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
             patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
            sent = await ea._dispatch_agent(agent, [rec])

        assert sent == 1
        mock_send.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  Item 7 — explicit `priority` on a record overrides tier default
# ═══════════════════════════════════════════════════════════════════════

class TestRecordPriorityOverridesTier:
    def test_explicit_priority_wins_over_tier(self):
        import backend.api.algo.event_agents as ea

        rec = {"extra": {"priority": "urgent", "tier": "low"}}
        assert ea._rec_tracked_ntfy_priority(rec) == "urgent"

    def test_no_explicit_priority_falls_back_to_tier(self):
        """Zero-regression — a record with no explicit `priority` (every
        currently-matching event agent, and every `rich_alert` record,
        which carries `tier` but no `priority`) keeps the existing
        tier-derived resolution unchanged."""
        import backend.api.algo.event_agents as ea

        rec = {"extra": {"tier": "high"}}
        assert ea._rec_tracked_ntfy_priority(rec) == "high"

    def test_no_priority_and_no_tier_returns_none(self):
        import backend.api.algo.event_agents as ea

        assert ea._rec_tracked_ntfy_priority({"extra": {}}) is None
        assert ea._rec_tracked_ntfy_priority({}) is None

    @pytest.mark.asyncio
    async def test_dispatch_agent_uses_explicit_record_priority_for_ntfy(self):
        """End-to-end: a `log(level=warning, priority=urgent, message=...)`
        call's record overrides the matching event agent's OWN
        tier-derived default (here tier="low" -> "low", but the record's
        explicit "urgent" must win)."""
        import backend.api.algo.event_agents as ea

        agent = MagicMock()
        agent.slug = "fake-event-agent"
        agent.tier = "low"
        agent.conditions = {}
        agent.actions = [{"type": "render", "render": "fake"}]
        agent.events = [{"channel": "ntfy", "enabled": True}]  # no explicit per-channel priority

        rec = {"level": "WARNING", "extra": {"priority": "urgent"}, "logger": "x", "message": "m"}

        with patch.object(ea, "evaluate", return_value=True), \
             patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
             patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
            sent = await ea._dispatch_agent(agent, [rec])

        assert sent == 1
        sent_ch = mock_send.call_args_list[0].args[0]
        assert sent_ch["channel"] == "ntfy"
        assert sent_ch["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_dispatch_agent_leaves_channel_unmodified_when_record_has_no_priority(self):
        """Zero-regression: `log(...)` with no `priority` param injects
        NO override at the `_dispatch_record_channels` layer — the
        channel dict handed to `_send_channel` is unchanged, exactly as
        before this round. `_send_channel`'s OWN separate agent.tier
        fallback (pre-existing, unchanged) is what applies the
        tier-derived default once inside that function — not tested
        again here, since this round didn't touch it."""
        import backend.api.algo.event_agents as ea

        agent = MagicMock()
        agent.slug = "fake-event-agent"
        agent.tier = "high"
        agent.conditions = {}
        agent.actions = [{"type": "render", "render": "fake"}]
        agent.events = [{"channel": "ntfy", "enabled": True}]

        rec = {"level": "INFO", "extra": {}, "logger": "x", "message": "m"}

        with patch.object(ea, "evaluate", return_value=True), \
             patch.object(ea, "RENDERS", {ea._render_key(agent): lambda r: ("T", "B")}), \
             patch.object(ea, "_send_channel", new=AsyncMock(return_value=True)) as mock_send:
            sent = await ea._dispatch_agent(agent, [rec])

        assert sent == 1
        sent_ch = mock_send.call_args_list[0].args[0]
        assert "priority" not in sent_ch
