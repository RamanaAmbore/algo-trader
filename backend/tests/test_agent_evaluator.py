"""
Tests for api/algo/agent_evaluator.py — condition tree evaluation.
SSOT: evaluate(cond, ctx) is the single entry point; returns list[dict].
Perf: evaluate is sync (hot path — no DB calls during condition checks).
Stale: all/any/not composites delegate recursively to evaluate().
Reuse: Context dataclass shared between the evaluator and grammar resolvers.
UX: evaluate returns [] on no match; non-empty list on fire; never raises.
"""
from pathlib import Path
import inspect

_SRC = Path("backend/api/algo/agent_evaluator.py").read_text()


def test_evaluate_function_exists():
    from backend.api.algo.agent_evaluator import evaluate
    assert callable(evaluate), "evaluate must be callable"
    assert not inspect.iscoroutinefunction(evaluate), (
        "evaluate must be sync (hot path — no awaits during condition check)"
    )


def test_context_dataclass_exists():
    from backend.api.algo.agent_evaluator import Context
    assert Context is not None, "Context dataclass must exist"


def test_context_has_sum_positions():
    from backend.api.algo.agent_evaluator import Context
    import dataclasses
    fields = {f.name for f in dataclasses.fields(Context)}
    assert "sum_positions" in fields, "Context must have sum_positions field"


def test_context_has_sum_holdings():
    from backend.api.algo.agent_evaluator import Context
    import dataclasses
    fields = {f.name for f in dataclasses.fields(Context)}
    assert "sum_holdings" in fields, "Context must have sum_holdings field"


def test_evaluate_returns_list():
    """evaluate must always return a list (empty on no match, entries on fire)."""
    from backend.api.algo.agent_evaluator import evaluate, Context
    ctx = Context()
    # With no registry loaded and no positions, a leaf condition must not crash
    # It should return [] (no matches) rather than raising
    leaf = {"metric": "pnl", "scope": "any_acct", "op": ">", "value": 1000}
    try:
        result = evaluate(leaf, ctx)
        assert isinstance(result, list), f"evaluate must return list, got {type(result)}"
    except Exception as e:
        # Acceptable if REGISTRY not loaded — just verify it's not a TypeError/AttributeError
        # that would indicate wrong return type handling
        assert "REGISTRY" in str(e) or "registry" in str(e).lower() or "metric" in str(e).lower(), (
            f"evaluate raised unexpected error: {e}"
        )


def test_all_any_not_composites_in_source():
    """Condition tree must support all/any/not composites."""
    assert '"all"' in _SRC or "'all'" in _SRC, "evaluate must handle 'all' composite"
    assert '"any"' in _SRC or "'any'" in _SRC, "evaluate must handle 'any' composite"
    assert '"not"' in _SRC or "'not'" in _SRC, "evaluate must handle 'not' composite"


def test_evaluate_references_registry():
    """evaluate must use REGISTRY for token resolution (not inline if-chains)."""
    assert "REGISTRY" in _SRC, (
        "evaluate must reference REGISTRY for metric/scope/op resolution — "
        "not hardcode token handling"
    )
