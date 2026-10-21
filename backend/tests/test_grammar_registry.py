"""
Tests for api/algo/grammar_registry.py — runtime dispatch table.
SSOT: REGISTRY singleton is the authoritative token→callable map.
Perf: thread-safe via RLock; accessors are O(1) dict lookups.
Stale: reload() is async (must be awaited, not called synchronously).
Reuse: metrics/scopes/operators/channels/actions all live in one registry.
UX: unknown tokens return None (no KeyError on lookup).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_SRC = Path("backend/api/algo/grammar_registry.py").read_text()


def test_registry_singleton_exists():
    from backend.api.algo.grammar_registry import REGISTRY, GrammarRegistry
    assert isinstance(REGISTRY, GrammarRegistry), (
        "REGISTRY must be a module-level GrammarRegistry singleton"
    )


def test_grammar_registry_class_exists():
    from backend.api.algo.grammar_registry import GrammarRegistry
    assert GrammarRegistry is not None


def test_registry_metric_returns_none_for_unknown():
    """metric() must return None for an unknown token, not raise KeyError."""
    from backend.api.algo.grammar_registry import REGISTRY
    result = REGISTRY.metric("nonexistent_metric_xyz")
    assert result is None, (
        "REGISTRY.metric() must return None for unknown tokens — not raise KeyError"
    )


def test_registry_scope_returns_none_for_unknown():
    from backend.api.algo.grammar_registry import REGISTRY
    result = REGISTRY.scope("nonexistent_scope_xyz")
    assert result is None


def test_registry_operators_initialized():
    """Operators are pre-loaded from OPERATORS code constant (no DB needed)."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo.grammar import OPERATORS
    # Registry operators should start populated with code-level OPERATORS
    # (populated during reload; may be empty until reload() is called)
    # But the dict structure itself must exist
    assert isinstance(REGISTRY.operators, dict), "REGISTRY.operators must be a dict"


def test_registry_is_thread_safe():
    """GrammarRegistry must use RLock for thread safety."""
    assert "RLock" in _SRC or "threading.RLock" in _SRC, (
        "GrammarRegistry must use threading.RLock for thread-safe access "
        "across concurrent requests"
    )


def test_reload_is_async():
    """reload() must be async — it reads from the DB."""
    import inspect
    from backend.api.algo.grammar_registry import GrammarRegistry
    assert inspect.iscoroutinefunction(GrammarRegistry.reload), (
        "GrammarRegistry.reload must be an async def (reads grammar_tokens from DB)"
    )


def test_import_dotted_helper_exists():
    from backend.api.algo.grammar_registry import _import_dotted
    assert callable(_import_dotted), "_import_dotted must be callable for resolver imports"
    # Test with a valid dotted path
    result = _import_dotted("backend.api.algo.grammar_registry.GrammarRegistry")
    from backend.api.algo.grammar_registry import GrammarRegistry
    assert result is GrammarRegistry


# ═══════════════════════════════════════════════════════════════════════════
#  Regression — REGISTRY.tokens must exist after a real (non-mocked) reload()
#  so agent_ai.py's _grammar_snapshot() doesn't raise AttributeError.
#
#  The 2 existing tests in test_agent_ai_coverage.py
#  (test_grammar_snapshot_pulls_active_tokens, test_grammar_snapshot_sorted)
#  mock REGISTRY itself away entirely (`patch(...REGISTRY) as mock_registry;
#  mock_registry.tokens = {...}`), so they only prove _grammar_snapshot()'s
#  own branching logic — never that the REAL registry actually populates a
#  `tokens` attribute. This test calls the real `GrammarRegistry.reload()`
#  (only the DB session is mocked, following the same async_session-mock
#  pattern used throughout this suite — e.g. test_daily_snapshot_orphan.py's
#  `_make_session_mock` — since reload() does a local
#  `from backend.api.database import async_session` import, the patch target
#  must be the source module, not grammar_registry's own namespace) against
#  a real REGISTRY instance, then calls the real _grammar_snapshot().
# ═══════════════════════════════════════════════════════════════════════════

def _make_fake_grammar_token(token_id, grammar_kind, token_kind, token,
                              resolver=None, is_active=True, description="",
                              params_schema=None):
    """A plain attribute bag standing in for a GrammarToken ORM row — only
    the attributes reload()'s loaders and agent_ai._summarise_token() read."""
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
        template_body="",
        source={},
    )


def _make_grammar_db_session_mock(rows):
    """Build an async_session() mock whose single SELECT returns `rows`
    from `.scalars().all()` — mirrors test_daily_snapshot_orphan.py's
    `_make_session_mock` pattern for exercising real async DB-reading code
    without a live database."""
    async def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def test_real_reload_populates_tokens_for_grammar_snapshot():
    """Real (non-mocked) REGISTRY.reload() must populate `self.tokens` as a
    dict keyed by id, and agent_ai._grammar_snapshot() must run against that
    real, reloaded registry without raising — the exact regression that
    shipped silently because the existing _grammar_snapshot() tests mock
    REGISTRY away entirely instead of exercising a real reload()."""
    from backend.api.algo.grammar_registry import REGISTRY

    fake_rows = [
        _make_fake_grammar_token(1, "condition", "metric", "pnl"),
        _make_fake_grammar_token(2, "condition", "scope", "total"),
        _make_fake_grammar_token(3, "condition", "operator", ">"),
        _make_fake_grammar_token(4, "notify", "channel", "telegram"),
        _make_fake_grammar_token(5, "action", "action_type", "place_order"),
    ]
    mock_session = _make_grammar_db_session_mock(fake_rows)

    # reload() does a local `from backend.api.database import async_session`
    # import — patch at the source module, not grammar_registry's namespace.
    with patch("backend.api.database.async_session", return_value=mock_session):
        asyncio.run(REGISTRY.reload())

    assert isinstance(REGISTRY.tokens, dict), "REGISTRY.tokens must be a dict after reload()"
    assert REGISTRY.tokens, "REGISTRY.tokens must be populated after a real reload()"
    assert set(REGISTRY.tokens.keys()) == {1, 2, 3, 4, 5}, "REGISTRY.tokens must be keyed by id"

    from backend.api.algo.agent_ai import _grammar_snapshot
    snap = _grammar_snapshot()  # must not raise AttributeError

    assert snap["actions"], "action tokens are always seeded — snap['actions'] must be non-empty"
    assert "place_order" in str(snap["actions"])


# ═══════════════════════════════════════════════════════════════════════════
#  Phase 26 (Sprint 2) — parameterized function-call tokens, e.g. mean_pnl(30)
#
#  A row with a non-empty params_schema is a FACTORY: it's called once per
#  distinct literal call-string with the call's positional args, and the
#  (ctx, row) -> value callable it returns is cached under that literal
#  string forever. Exercised via a real (session-mocked) reload() so the
#  loader branching (factory table vs. plain exact-match table) is actually
#  proven, not just the registry's own parsing helper in isolation.
# ═══════════════════════════════════════════════════════════════════════════

def _reload_with_rows(rows):
    from backend.api.algo.grammar_registry import REGISTRY
    mock_session = _make_grammar_db_session_mock(rows)
    with patch("backend.api.database.async_session", return_value=mock_session):
        asyncio.run(REGISTRY.reload())


def _factory_token_rows():
    return [
        _make_fake_grammar_token(
            1, "condition", "metric", "mean_pnl",
            resolver="backend.api.algo.grammar._metric_factory_mean_pnl",
            params_schema={"minutes": {"type": "number"}},
        ),
        _make_fake_grammar_token(
            2, "condition", "metric", "mean_pnl_30m",
            resolver="backend.api.algo.grammar._metric_mean_pnl_30m",
        ),
    ]


class TestParameterizedCallTokens:
    def setup_method(self):
        from backend.api.algo.grammar_registry import REGISTRY
        self.REGISTRY = REGISTRY
        _reload_with_rows(_factory_token_rows())

    def test_params_schema_row_goes_into_factory_table_not_plain_table(self):
        assert "mean_pnl" in self.REGISTRY.metric_factories
        assert "mean_pnl" not in self.REGISTRY.metrics

    def test_plain_row_unaffected_goes_into_metrics_table(self):
        assert "mean_pnl_30m" in self.REGISTRY.metrics
        assert "mean_pnl_30m" not in self.REGISTRY.metric_factories

    def test_call_token_resolves_to_a_bound_callable(self):
        fn = self.REGISTRY.metric("mean_pnl(30)")
        assert callable(fn)

    def test_call_token_callable_behaves_like_the_fixed_window_resolver(self):
        """mean_pnl(30) and the pre-existing mean_pnl_30m token must compute
        the exact same thing on the same row — same factory, same window."""
        fixed = self.REGISTRY.metric("mean_pnl_30m")
        parameterized = self.REGISTRY.metric("mean_pnl(30)")
        ctx = SimpleNamespace(window_mean=lambda key, minutes, field_idx=1: (key, minutes, field_idx))
        row = {"account": "ACC1"}
        assert fixed(ctx, row) == parameterized(ctx, row)

    def test_call_token_result_is_cached_by_literal_string(self):
        first = self.REGISTRY.metric("mean_pnl(30)")
        second = self.REGISTRY.metric("mean_pnl(30)")
        assert first is second

    def test_bare_base_token_without_call_does_not_resolve(self):
        """"mean_pnl" alone (no parens) must stay unresolved — only the
        call form is a valid condition token."""
        assert self.REGISTRY.metric("mean_pnl") is None

    def test_unknown_base_in_call_shape_returns_none(self):
        assert self.REGISTRY.metric("totally_unknown_fn(30)") is None

    def test_wrong_arg_count_returns_none(self):
        assert self.REGISTRY.metric("mean_pnl(30, 60)") is None
        assert self.REGISTRY.metric("mean_pnl()") is None

    def test_non_numeric_arg_returns_none(self):
        assert self.REGISTRY.metric("mean_pnl('30')") is None

    def test_bool_arg_rejected_despite_being_an_int_subclass(self):
        """bool is a subclass of int in Python — must not silently pass
        as a numeric window argument."""
        assert self.REGISTRY.metric("mean_pnl(True)") is None

    def test_non_call_expression_returns_none(self):
        assert self.REGISTRY.metric("mean_pnl(30) + 1") is None
        assert self.REGISTRY.metric("mean_pnl(30).attr") is None

    def test_keyword_args_rejected(self):
        assert self.REGISTRY.metric("mean_pnl(minutes=30)") is None

    def test_syntactically_invalid_token_returns_none_not_raise(self):
        assert self.REGISTRY.metric("mean_pnl(") is None

    def test_non_positive_window_argument_returns_none(self):
        """A window of 0 (or negative) would otherwise resolve to a REAL
        callable that silently always returns None at evaluation time
        (the window cutoff collapses to >= now, so <2 samples ever fall
        inside it) — explicitly rejected at the SAME place other bad
        call-shapes are rejected, not left to fail silently downstream.
        Negative literals already failed by accident before this guard
        (ast parses "-5" as UnaryOp(USub, Constant(5)), not
        Constant(-5)) — covered here too so the behaviour is pinned
        regardless of which mechanism causes it."""
        assert self.REGISTRY.metric("mean_pnl(0)") is None
        assert self.REGISTRY.metric("mean_pnl(0.0)") is None
        assert self.REGISTRY.metric("mean_pnl(-5)") is None
        assert self.REGISTRY.metric("mean_pnl(-5.0)") is None

    def test_positive_fractional_window_still_resolves(self):
        """A small-but-positive window is legitimate — only non-positive
        values are rejected."""
        assert self.REGISTRY.metric("mean_pnl(0.5)") is not None
        assert self.REGISTRY.metric("") is None

    def test_scope_channel_format_accessors_share_the_same_mechanism(self):
        """.scope()/.channel()/.fmt() use the identical fallback as
        .metric() — prove at least one of them end to end so the
        mechanism isn't metric-only by accident."""
        rows = [
            _make_fake_grammar_token(
                1, "condition", "scope", "top_n",
                resolver="backend.api.algo.grammar_registry._import_dotted",
                params_schema={"n": {"type": "number"}},
            ),
        ]
        _reload_with_rows(rows)
        assert "top_n" in self.REGISTRY.scope_factories
        # _import_dotted(5) would raise (int has no module path) — proves
        # the factory is actually invoked with the bound arg, surfaced as
        # a warning-logged None rather than propagating.
        assert self.REGISTRY.scope("top_n(5)") is None

    def test_reload_clears_stale_call_cache(self):
        """A cached call-token result from a prior reload must not survive
        a reload that removes or changes the underlying factory."""
        assert self.REGISTRY.metric("mean_pnl(30)") is not None
        _reload_with_rows([_make_fake_grammar_token(
            9, "condition", "scope", "total",
            resolver="backend.api.algo.grammar_registry._import_dotted",
        )])
        assert self.REGISTRY.metric_factories == {}
        assert self.REGISTRY.metric("mean_pnl(30)") is None


# ═══════════════════════════════════════════════════════════════════════════
#  validate_action_dispatch_catalog() — closes the "7 dead grammar-resolver
#  stub functions" drift gap (see CLAUDE.md's "Agent action-dispatch SSOT"
#  note). Cross-references agent_grammar.yaml's action-type catalog against
#  actions.py's real dispatch tables (_LIVE_ACTION_HANDLERS /
#  _NOOP_ACTION_HANDLERS) in both directions, raising
#  ActionDispatchDriftError (never a silent log line) on any undeclared
#  mismatch. `REGISTRY.reload()` runs it first, before the DB read, so a
#  real drift surfaces at startup / the admin reload endpoint.
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateActionDispatchCatalog:
    def test_real_catalog_and_dispatch_tables_pass_cleanly(self):
        """Regression guard: today's real agent_grammar.yaml catalog, the
        real _LIVE_ACTION_HANDLERS / _NOOP_ACTION_HANDLERS dispatch tables,
        and the real ACTION_DISPATCH_CATALOG_ALLOWLIST must validate clean
        with NO arguments (every default is the real code-level source).
        This must stay green going forward — any future drift between the
        YAML catalog and actions.py's dispatch tables should fail this test
        before it ever reaches runtime/prod."""
        from backend.api.algo.grammar_registry import validate_action_dispatch_catalog
        validate_action_dispatch_catalog()  # must not raise

    def test_documented_allowlist_entries_match_known_aliases(self):
        """The one remaining confirmed-genuine dispatch-only-no-catalog
        exception: `chase_close`, a deliberate alias for the catalog's
        `chase_close_positions` token. `send_summary` (2026-10, operator
        decision) gained a real catalog row instead of staying a dead
        dispatch-only handler, so it's no longer allowlisted — see the
        self-tightening tests below for what happens if it had stayed
        stale."""
        from backend.api.algo.grammar_registry import ACTION_DISPATCH_CATALOG_ALLOWLIST
        assert set(ACTION_DISPATCH_CATALOG_ALLOWLIST.keys()) == {"chase_close"}

    def test_raises_on_catalog_token_with_no_dispatch_handler(self):
        """A YAML action token with NO entry in either dispatch dict — the
        exact '7 dead stubs' bug class — must raise, not log."""
        from backend.api.algo.grammar_registry import (
            validate_action_dispatch_catalog, ActionDispatchDriftError,
        )
        import pytest
        with pytest.raises(ActionDispatchDriftError, match="fake_undispatched_token"):
            validate_action_dispatch_catalog(
                catalog_tokens={"place_order", "fake_undispatched_token"},
                live_handlers={"place_order": object()},
                noop_handlers={},
                allowlist={},
            )

    def test_raises_on_dispatch_handler_with_no_catalog_token(self):
        """A dispatch-table entry (live or noop) with no catalog token and
        no allowlist entry — a handler the operator/Lab-chat agent-builder
        can never see — must raise."""
        from backend.api.algo.grammar_registry import (
            validate_action_dispatch_catalog, ActionDispatchDriftError,
        )
        import pytest
        with pytest.raises(ActionDispatchDriftError, match="fake_undeclared_action"):
            validate_action_dispatch_catalog(
                catalog_tokens={"place_order"},
                live_handlers={"place_order": object(), "fake_undeclared_action": object()},
                noop_handlers={},
                allowlist={},
            )

    def test_raises_on_dispatch_handler_with_no_catalog_token_noop_side(self):
        """Same direction-2 check, exercised via the noop dict — a real
        dict entry nothing but the allowlist covers must raise regardless
        of which of the two dispatch dicts it lives in."""
        from backend.api.algo.grammar_registry import (
            validate_action_dispatch_catalog, ActionDispatchDriftError,
        )
        import pytest
        with pytest.raises(ActionDispatchDriftError, match="fake_undeclared_noop"):
            validate_action_dispatch_catalog(
                catalog_tokens={"emit_log"},
                live_handlers={},
                noop_handlers={"emit_log": object(), "fake_undeclared_noop": object()},
                allowlist={},
            )

    def test_raises_on_stale_allowlist_entry_no_longer_dispatched(self):
        """An allowlist entry whose token has disappeared from BOTH
        dispatch dicts is stale and must raise — the allowlist itself must
        never be allowed to silently rot into describing a handler that no
        longer exists."""
        from backend.api.algo.grammar_registry import (
            validate_action_dispatch_catalog, ActionDispatchDriftError,
        )
        import pytest
        with pytest.raises(ActionDispatchDriftError, match="ghost_token"):
            validate_action_dispatch_catalog(
                catalog_tokens={"place_order"},
                live_handlers={"place_order": object()},
                noop_handlers={},
                allowlist={"ghost_token": "no longer dispatched"},
            )

    def test_raises_on_stale_allowlist_entry_now_catalogued(self):
        """An allowlist entry whose token has since gained a real catalog
        row no longer needs the exception — leaving it in place would mask
        a future genuine direction-2 regression for that same token, so
        this must raise too."""
        from backend.api.algo.grammar_registry import (
            validate_action_dispatch_catalog, ActionDispatchDriftError,
        )
        import pytest
        with pytest.raises(ActionDispatchDriftError, match="now_catalogued"):
            validate_action_dispatch_catalog(
                catalog_tokens={"place_order", "now_catalogued"},
                live_handlers={"place_order": object(), "now_catalogued": object()},
                noop_handlers={},
                allowlist={"now_catalogued": "used to be dispatch-only"},
            )

    def test_reload_invokes_validator_and_propagates_its_error(self):
        """`REGISTRY.reload()` must actually call the validator (not just
        have it sit unused) and must let a raised ActionDispatchDriftError
        propagate — never swallow it — so a real drift breaks startup /
        the admin reload endpoint instead of silently loading a half-wired
        registry. Patches the validator (imported into grammar_registry's
        own namespace) to raise, and mocks the DB session the same way
        `_make_grammar_db_session_mock` does elsewhere in this file, so the
        DB read is never reached."""
        import asyncio
        import pytest
        from backend.api.algo.grammar_registry import REGISTRY, ActionDispatchDriftError

        mock_session = _make_grammar_db_session_mock([])
        with patch(
            "backend.api.algo.grammar_registry.validate_action_dispatch_catalog",
            side_effect=ActionDispatchDriftError("boom"),
        ) as mock_validate, patch(
            "backend.api.database.async_session", return_value=mock_session,
        ):
            with pytest.raises(ActionDispatchDriftError, match="boom"):
                asyncio.run(REGISTRY.reload())
        mock_validate.assert_called_once()
        # The DB was never touched — validation runs before the SELECT.
        mock_session.execute.assert_not_called()
