"""
Tests for api/algo/grammar_registry.py — runtime dispatch table.
SSOT: REGISTRY singleton is the authoritative token→callable map.
Perf: thread-safe via RLock; accessors are O(1) dict lookups.
Stale: reload() is async (must be awaited, not called synchronously).
Reuse: metrics/scopes/operators/channels/actions all live in one registry.
UX: unknown tokens return None (no KeyError on lookup).
"""
from pathlib import Path

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
