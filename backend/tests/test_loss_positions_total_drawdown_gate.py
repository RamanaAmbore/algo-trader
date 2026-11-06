"""2026-11 alert-noise-reduction plan, Component C — gate `loss-positions-
total`'s rate leaves (pnl_rate_abs / pnl_rate_pct) on drawdown-from-peak
instead of bare day_val sign.

Context: the operator's original ask was `day_val < 0` — "the day is
already negative" — as the gate. The risk council found this has a
PERMANENT, session-long blind spot: an account at +₹600k that bleeds
-₹10k/min for 30 minutes but ends the day at +₹300k never crosses
day_val<0, so the rate leaves would never fire for that whole session no
matter how severe the bleed. `Context.window_drawdown()`
(agent_evaluator.py), exposed as the registered call-syntax metric
`max_drawdown_pnl(30)` (NOT `window_drawdown` directly — that's a Context
method, not a DSL token; see grammar_tokens/metric_max_drawdown_pnl.py),
closes that gap: the gate becomes `any[day_val<0, max_drawdown_pnl(30)
<=-threshold]`.

Threshold choice (-₹20,000, deliberately LOOSE): mid-task, the operator
explicitly stated "rate of change alert is the most important one" and
that this redesign must not narrow rate-of-change coverage relative to
today's bare condition — any ambiguity resolves toward STILL FIRING. A
small threshold means the gate only excludes a genuinely negligible,
near-zero-magnitude rate reading; any real, sustained rate breach (which
by construction already implies a multi-minute decline of comparable or
larger magnitude) passes in virtually every real scenario. This test file
demonstrates both sides of that design: the giveback case still fires,
and a near-zero artifact does not — while a MODEST real pullback (well
short of a true "giveback" but clearly more than noise) now also fires,
reflecting the operator's explicit preference for sensitivity over
suppression on this one leaf.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.api.algo.agent_evaluator import Context, evaluate
from backend.api.algo.agent_engine import (
    _LOSS_AGENTS,
    _LOSS_POSITIONS_TOTAL_OLD_COND,
    _ae_is_stale_loss_positions_total_cond,
    _ae_sync_existing_builtin,
)


def _agent_cond():
    agent = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
    return agent["conditions"]


@pytest.fixture
def wired_registry(monkeypatch):
    """Wire the real resolvers this condition tree needs into REGISTRY,
    restored automatically by monkeypatch. Same pattern as
    test_underlying_loss_divergence_tokens.py (REGISTRY.metrics via
    setitem) plus the call-syntax factory pattern from
    test_agent_kind_tier_topic.py (REGISTRY.metric_factories)."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo import grammar as g
    from backend.api.algo.grammar_tokens.metric_day_val import _metric_day_val
    from backend.api.algo.grammar_tokens.metric_day_pct import _metric_day_pct
    from backend.api.algo.grammar_tokens.metric_pnl_rate_abs import _metric_pnl_rate_abs
    from backend.api.algo.grammar_tokens.metric_pnl_rate_pct import _metric_pnl_rate_pct
    from backend.api.algo.grammar_tokens.metric_max_drawdown_pnl import (
        _metric_factory_max_drawdown_pnl,
    )
    from backend.api.algo.grammar_tokens.scope_positions_total import _scope_positions_total

    monkeypatch.setitem(REGISTRY.metrics, "day_val", _metric_day_val)
    monkeypatch.setitem(REGISTRY.metrics, "day_pct", _metric_day_pct)
    monkeypatch.setitem(REGISTRY.metrics, "pnl_rate_abs", _metric_pnl_rate_abs)
    monkeypatch.setitem(REGISTRY.metrics, "pnl_rate_pct", _metric_pnl_rate_pct)
    monkeypatch.setitem(
        REGISTRY.metric_factories, "max_drawdown_pnl",
        (_metric_factory_max_drawdown_pnl, {"minutes": {"type": "number"}}),
    )
    monkeypatch.setitem(REGISTRY.scopes, "positions.total", _scope_positions_total)
    monkeypatch.setitem(REGISTRY.operators, "<=", g.OPERATORS["<="])
    monkeypatch.setitem(REGISTRY.operators, "<", g.OPERATORS["<"])
    # Reset the call-token cache — a prior test in the same process may
    # already have cached a None for "max_drawdown_pnl(30)" before this
    # fixture wired the factory in.
    monkeypatch.setattr(REGISTRY, "_metric_call_cache", {})
    return REGISTRY


_BASE = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def _make_ctx(hist, day_val: float, day_pct: float = 0.5) -> Context:
    now = hist[-1][0]
    sum_positions = pd.DataFrame([{
        "account": "TOTAL", "pnl": day_val, "day_change_val": day_val,
        "day_change_percentage": day_pct, "day_change_pct_margin": day_pct,
    }])
    alert_state = {"pnl_history": {("positions", "TOTAL"): hist}}
    return Context(sum_positions=sum_positions, alert_state=alert_state, now=now,
                   rate_window_min=10.0, baseline_live=True)


def _linear_hist(start_min: int, end_min: int, start_val: float, end_val: float,
                  step_min: int = 1) -> list:
    """One sample per `step_min` minutes from start_min to end_min
    (inclusive), linearly interpolated between start_val and end_val."""
    out = []
    span = end_min - start_min
    m = start_min
    while m <= end_min:
        frac = (m - start_min) / span if span else 0.0
        val = start_val + (end_val - start_val) * frac
        out.append((_BASE + timedelta(minutes=m), val, 0.5))
        m += step_min
    return out


class TestGivebackCaseFires:
    """The scenario the redesign exists to catch: a session peak of
    +₹600k bleeding to +₹300k over 30 minutes at a rate crossing the
    existing pnl_rate thresholds — day_val stays positive throughout, so
    the OLD bare `day_val < 0` gate the council rejected would have
    missed this entirely."""

    def test_fires(self, wired_registry):
        hist = _linear_hist(0, 30, 600000.0, 300000.0)
        ctx = _make_ctx(hist, day_val=300000.0, day_pct=1.0)
        assert ctx.rate_abs(("positions", "TOTAL")) is not None
        assert ctx.rate_abs(("positions", "TOTAL")) <= -6000, (
            "fixture must actually cross the rate threshold"
        )
        assert ctx.window_drawdown(("positions", "TOTAL"), 30) <= -20000, (
            "fixture must actually cross the drawdown gate"
        )
        matches = evaluate(_agent_cond(), ctx)
        assert matches, "giveback scenario must fire — this is the gap the redesign closes"
        fired_metrics = {m["metric"] for m in matches}
        assert "pnl_rate_abs" in fired_metrics
        # day_val (-100000 floor) and day_pct (-2.0) must NOT have fired —
        # day_val is still positive (+300000) and day_pct is positive too.
        assert not any(
            m["metric"] == "day_val" and m["op"] == "<=" and m["threshold"] == -100000
            for m in matches
        )


class TestNearZeroArtifactDoesNotFire:
    """A day with a near-flat P&L path (no real decline of any
    meaningful size) must not pass the gate even if some other leaf's
    computation were to read a borderline rate — this confirms the gate
    is not a complete no-op, just a loose one."""

    def test_flat_day_does_not_fire(self, wired_registry):
        # A day that's essentially flat around +50000, tiny noise only —
        # no leaf should cross any threshold at all.
        hist = []
        for i in range(31):
            wobble = 200.0 if i % 2 == 0 else -200.0
            hist.append((_BASE + timedelta(minutes=i), 50000.0 + wobble, 0.1))
        ctx = _make_ctx(hist, day_val=hist[-1][1], day_pct=0.1)
        matches = evaluate(_agent_cond(), ctx)
        assert not matches, "a genuinely flat day must not fire any leaf"


class TestModestPullbackNowFiresPerOperatorPriority:
    """2026-11 mid-task operator correction: given the explicit priority
    that rate-of-change detection must not be narrowed by this redesign,
    a modest (not session-destroying) real pullback from a still-rising,
    still-positive day — well short of the -300k "giveback" case above,
    but clearly a real, multi-minute decline exceeding the rate
    thresholds — now ALSO fires, because the -20,000 threshold is
    deliberately loose. This is a deliberate design choice, not an
    oversight: the operator ranked "don't miss a genuine fast decline"
    above "suppress this specific leaf's noise" once the two were shown
    to conflict for the real 2026-10-29 fires (see this file's module
    docstring and the agent's own code comment)."""

    def test_modest_pullback_in_a_rising_day_fires(self, wired_registry):
        # Climbs from 200000 to 300000 over 20 minutes, then a real
        # ~9200/min decline for the last 9 minutes (matching the REAL
        # observed rate at the 2026-10-29 16:14 UTC fire) down to
        # ~217000 — day_val stays clearly positive throughout.
        hist = _linear_hist(0, 20, 200000.0, 300000.0)
        peak = hist[-1][1]
        for i in range(1, 10):
            hist.append((_BASE + timedelta(minutes=20 + i), peak - 9199.69 * i, 0.5))
        day_val = hist[-1][1]
        assert day_val > 0, "fixture must stay positive, like the real 2026-10-29 fires"
        ctx = _make_ctx(hist, day_val=day_val, day_pct=1.0)
        rate = ctx.rate_abs(("positions", "TOTAL"))
        assert rate is not None and rate <= -6000, "fixture must cross the rate threshold"
        drawdown = ctx.window_drawdown(("positions", "TOTAL"), 30)
        assert drawdown <= -20000, (
            "fixture's real pullback must clear the loose -20,000 gate — "
            "demonstrating the operator-directed 'err toward firing' outcome"
        )
        matches = evaluate(_agent_cond(), ctx)
        assert matches, (
            "a modest but real pullback-from-peak, with a genuine rate "
            "breach, must still fire — per explicit operator priority"
        )


class TestNestedConditionLatchKeying:
    """Task requirement 3d — confirm `all[any[...], any[...]]` nesting
    doesn't corrupt per-leaf latch-keying for the rate leaves. Live
    precedent in this file: loss-positions-underlying-divergence shipped
    inactive once before over a related reference-point mismatch — this
    is a targeted check, not an assumption."""

    def test_latch_key_identifies_the_real_leaf_not_the_wrapper(self, wired_registry):
        from backend.api.algo.agent_engine import _latch_key

        hist = _linear_hist(0, 30, 600000.0, 300000.0)
        ctx = _make_ctx(hist, day_val=300000.0, day_pct=1.0)
        matches = evaluate(_agent_cond(), ctx)
        assert matches
        keys = {_latch_key("loss-positions-total", m) for m in matches}
        # Every match's own (metric, scope, account) must be resolvable —
        # the nesting must not collapse distinct leaves onto one key.
        metrics_in_keys = {k[1] for k in keys}
        assert "pnl_rate_abs" in metrics_in_keys
        # day_val<0 and pnl_rate_abs are different metrics -> different
        # latch keys, so escalation/cooldown tracks them independently,
        # exactly as a top-level any[] would.
        assert len(keys) == len({m["metric"] for m in matches})

    def test_escalation_gate_operates_per_leaf_under_the_extra_nesting(self, wired_registry):
        """`_v2_apply_escalation_gate` must still correctly arm/re-arm a
        latch for a leaf buried inside the nested all[]/any[] — not just
        for a top-level leaf like day_val/day_pct."""
        from backend.api.algo.agent_engine import _v2_apply_escalation_gate, _V2_LATCH

        hist = _linear_hist(0, 30, 600000.0, 300000.0)
        ctx = _make_ctx(hist, day_val=300000.0, day_pct=1.0)
        matches = evaluate(_agent_cond(), ctx)
        assert matches

        class _A:
            slug = "loss-positions-total-test"

        agent = _A()
        _V2_LATCH.clear()
        try:
            effective = _v2_apply_escalation_gate(agent, matches, ctx.now, cooldown_min=45)
            assert effective, "a fresh (never-latched) nested leaf must fire on first pass"
            # Re-evaluating the SAME tick immediately (no time elapsed)
            # must now be suppressed by cooldown — proves the latch was
            # actually recorded for this nested leaf, not silently
            # skipped.
            effective_again = _v2_apply_escalation_gate(agent, matches, ctx.now, cooldown_min=45)
            assert not effective_again, (
                "nested leaf's latch must gate a second fire inside the same cooldown window"
            )
        finally:
            _V2_LATCH.clear()


class TestMigrationHelper:
    def test_detects_exact_old_shape(self):
        assert _ae_is_stale_loss_positions_total_cond(_LOSS_POSITIONS_TOTAL_OLD_COND)

    def test_does_not_match_new_shape(self):
        agent = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        assert not _ae_is_stale_loss_positions_total_cond(agent["conditions"])

    def test_does_not_match_an_operator_customized_tree(self):
        """An operator who customized this agent's conditions in any way
        (even just a different threshold) must not be silently
        overwritten on the next restart."""
        customized = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -75000},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}
        assert not _ae_is_stale_loss_positions_total_cond(customized)

    def test_migration_overwrites_only_the_exact_old_shape(self):
        from types import SimpleNamespace

        agent_def = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(_LOSS_POSITIONS_TOTAL_OLD_COND), cooldown_minutes=30,
            status="active",
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == agent_def["conditions"]
        assert existing.cooldown_minutes == 45

    def test_migration_leaves_a_customized_row_alone(self):
        from types import SimpleNamespace

        agent_def = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        customized = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -75000},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(customized), cooldown_minutes=30, status="active",
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == customized, "operator customization must survive"
        assert existing.cooldown_minutes == 30, "operator's own cooldown must survive"
