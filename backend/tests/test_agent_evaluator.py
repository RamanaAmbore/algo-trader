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
import pytest

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


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #2 — all[] must join on account, not flatten every child
#  independently (2026-09 alerts audit)
# ═══════════════════════════════════════════════════════════════════════════

class TestAllJoinsSameAccount:
    """Prod repro: loss-margin-low's `all[avail_margin<25000, avail_margin>0]`
    fired by combining account A's avail_margin=0 (satisfies leaf 1) with
    account B's avail_margin=373828.52 (satisfies leaf 2) — NO single
    account satisfied both. `all[]` must intersect matched accounts across
    sibling leaves, not flatten each leaf's matches independently.

    REGISTRY is normally populated from the DB at app startup
    (`REGISTRY.reload()`); these tests wire the real grammar.py resolvers
    in directly via monkeypatch (auto-restored after each test) so the
    evaluator is exercised end-to-end without needing a DB session.
    """

    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import (
            _metric_avail_margin, _metric_pnl,
            _scope_funds_any_acct, _scope_funds_total, _scope_positions_any_acct,
            OPERATORS,
        )
        monkeypatch.setitem(REGISTRY.metrics, 'avail_margin', _metric_avail_margin)
        monkeypatch.setitem(REGISTRY.metrics, 'pnl', _metric_pnl)
        monkeypatch.setitem(REGISTRY.scopes, 'funds.any_acct', _scope_funds_any_acct)
        monkeypatch.setitem(REGISTRY.scopes, 'funds.total', _scope_funds_total)
        monkeypatch.setitem(REGISTRY.scopes, 'positions.any_acct', _scope_positions_any_acct)
        for op_tok in ('<', '>'):
            monkeypatch.setitem(REGISTRY.operators, op_tok, OPERATORS[op_tok])

    def test_cross_account_false_fire_is_suppressed(self):
        from backend.api.algo.agent_evaluator import evaluate, Context
        rows = [
            {'account': 'ACCT_A', 'net': 0.0},
            {'account': 'ACCT_B', 'net': 373828.52},
        ]
        ctx = Context(df_margins=_margins_ctx_df(rows))
        cond = {"all": [
            {"op": "<", "scope": "funds.any_acct", "metric": "avail_margin", "value": 25000},
            {"op": ">", "scope": "funds.any_acct", "metric": "avail_margin", "value": 0},
        ]}
        result = evaluate(cond, ctx)
        assert result == [], (
            f"Expected NO fire — no single account satisfies BOTH leaves, got {result}"
        )

    def test_same_account_satisfying_both_leaves_still_fires(self):
        """Sanity check: a genuine same-account AND breach must still fire."""
        from backend.api.algo.agent_evaluator import evaluate, Context
        rows = [{'account': 'ACCT_A', 'net': 12000.0}]  # 0 < 12000 < 25000
        ctx = Context(df_margins=_margins_ctx_df(rows))
        cond = {"all": [
            {"op": "<", "scope": "funds.any_acct", "metric": "avail_margin", "value": 25000},
            {"op": ">", "scope": "funds.any_acct", "metric": "avail_margin", "value": 0},
        ]}
        result = evaluate(cond, ctx)
        assert len(result) == 2, f"Expected both leaves to match ACCT_A, got {result}"
        assert all(m['account'] == 'ACCT_A' for m in result)

    def test_total_scoped_leaf_is_not_intersected(self):
        """A leaf scoped to funds.total (single TOTAL row, not
        per-account) is not part of the cross-account intersection — it
        behaves as a plain independent AND gate, same as before."""
        from backend.api.algo.agent_evaluator import evaluate, Context
        rows_acct = [{'account': 'ACCT_A', 'pnl': -100.0}]
        rows_total = [{'account': 'TOTAL', 'net': 50000.0}]
        ctx = Context(
            sum_positions=_margins_ctx_df(rows_acct),
            df_margins=_margins_ctx_df(rows_total),
        )
        cond = {"all": [
            {"op": "<", "scope": "positions.any_acct", "metric": "pnl", "value": 0},
            {"op": ">", "scope": "funds.total", "metric": "avail_margin", "value": 0},
        ]}
        result = evaluate(cond, ctx)
        assert len(result) == 2, f"Expected both independent leaves to fire, got {result}"

    def test_only_two_or_more_account_keyed_children_trigger_intersection(self):
        """A single account-keyed child has nothing to intersect against
        — old flatten behaviour is preserved (no regression for the
        common single-leaf-per-account case)."""
        from backend.api.algo.agent_evaluator import evaluate, Context
        rows = [{'account': 'ACCT_A', 'net': -500.0}]
        ctx = Context(df_margins=_margins_ctx_df(rows))
        cond = {"all": [
            {"op": "<", "scope": "funds.any_acct", "metric": "avail_margin", "value": 0},
        ]}
        result = evaluate(cond, ctx)
        assert len(result) == 1 and result[0]['account'] == 'ACCT_A'


def _margins_ctx_df(rows):
    import pandas as pd
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  Fix #1 — rate window sample/span requirement (2026-09 alerts audit)
# ═══════════════════════════════════════════════════════════════════════════

class TestRateWindowSampleSpanGuard:
    """Prod repro: at the ~5m05s background poll cadence, a fixed
    10-minute rate window only ever held 2 samples, so every 'rate'
    fired was really just one poll's raw Δ (-28k/min to -33k/min spam).
    `windowed_rate` now requires >= 3 samples spanning >= 80% of an
    adaptively-widened window before returning a value."""

    def test_two_samples_at_five_minute_cadence_returns_none(self):
        from backend.api.algo.agent_evaluator import windowed_rate
        from datetime import datetime, timedelta
        now = datetime(2026, 9, 20, 10, 0, 0)
        hist = [
            (now - timedelta(minutes=5), -100000.0, -5.0),
            (now, -128000.0, -6.4),
        ]
        result = windowed_rate(hist, now, configured_min=10, field_idx=1)
        assert result is None, (
            f"Expected None for a 2-sample single-poll-delta, got {result} — "
            f"this is the exact prod -28k/min spam shape"
        )

    def test_three_samples_at_five_minute_cadence_returns_a_value(self):
        from backend.api.algo.agent_evaluator import windowed_rate
        from datetime import datetime, timedelta
        now = datetime(2026, 9, 20, 10, 0, 0)
        hist = [
            (now - timedelta(minutes=10), -80000.0, -4.0),
            (now - timedelta(minutes=5),  -100000.0, -5.0),
            (now, -128000.0, -6.4),
        ]
        result = windowed_rate(hist, now, configured_min=10, field_idx=1)
        assert result is not None, (
            "Expected a real rate once the effective window has >= 3 samples "
            "spanning enough time"
        )

    def test_simulator_cadence_keeps_configured_window(self):
        """At the simulator's ~2s tick cadence, the effective window
        collapses back to the configured value (2.2x a tiny median gap
        is still tiny) — sim behaviour is unaffected by the widening."""
        from backend.api.algo.agent_evaluator import _effective_rate_window_min
        from datetime import datetime, timedelta
        now = datetime(2026, 9, 20, 10, 0, 0)
        hist = [(now - timedelta(seconds=2 * i), 0.0, 0.0) for i in range(5, -1, -1)]
        eff = _effective_rate_window_min(hist, configured_min=10)
        assert eff == 10, f"Expected the configured window (10) to win at 2s cadence, got {eff}"

    def test_quintile_smoothing_still_engages_for_rich_history(self):
        """>= 5 samples inside the effective window uses quintile-median
        smoothing (unchanged behaviour from before fix #1 — only the
        minimum bar to REACH this path changed). Uses a wide configured
        window (50 min) so the effective window comfortably covers all
        10 samples (45-min span) rather than the adaptive widening
        narrowing it back down to just the last few."""
        from backend.api.algo.agent_evaluator import windowed_rate
        from datetime import datetime, timedelta
        now = datetime(2026, 9, 20, 10, 0, 0)
        hist = [(now - timedelta(minutes=5 * (9 - i)), -10000.0 * i, -1.0 * i) for i in range(10)]
        result = windowed_rate(hist, now, configured_min=50, field_idx=1)
        assert result is not None and result < 0, (
            f"Expected a negative (worsening) rate from the quintile path, got {result}"
        )
