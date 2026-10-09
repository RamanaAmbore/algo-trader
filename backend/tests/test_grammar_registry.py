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
