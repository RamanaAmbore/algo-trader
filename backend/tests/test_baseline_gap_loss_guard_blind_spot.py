"""2026-11 6D-audit fix — the critical-tier `loss-positions-total` agent
must still be ABLE to fire when a `baseline_stale` ("daily_book baseline
gap") row carries a real, large negative live P&L.

Context: a `baseline_stale` row (commit a0fd14fec) is one whose Day P&L
baseline couldn't be resolved, even though the broker confirms it carried
a non-zero quantity from before today (`pnl_math.is_baseline_gap`). The
original fix for that case short-circuited the row's contribution to
EVERY account/TOTAL Day P&L rollup to a hard 0.0 — including
`background.py`'s `_rebuild_positions_summary`, the exact feed
`loss-positions-total`'s `day_val`/`day_pct` leaves read via
`ctx.sum_positions`. If one account's EOD `daily_book` write genuinely
fails one night (a real, documented, pre-existing failure mode), every
position carried forward from that account the next session would be
flagged `baseline_stale=True` and silently contribute 0 to the Day P&L
total — even while genuinely losing a lot of real money that day. The
critical-tier loss guard would go silently blind in exactly the
direction CLAUDE.md's "Staleness indicator freeze rule" forbids.

This test drives the REAL production path end to end: a raw per-position
frame (one baseline_stale row with a large negative lifetime pnl, one
ordinary row) through the REAL `background.py:_rebuild_positions_summary`
to build the REAL `ctx.sum_positions` shape, then evaluates
`loss-positions-total`'s REAL condition tree (`backend/api/algo/
agent_engine.py:_LOSS_AGENTS`) against it via the real `evaluate()` —
confirming the agent fires, not that it silently passes as "no loss"."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.agent_evaluator import Context, evaluate


def _agent_cond():
    from backend.api.algo.agent_engine import _LOSS_AGENTS
    agent = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
    return agent["conditions"]


@pytest.fixture
def wired_registry(monkeypatch):
    """Wire the real resolvers this condition tree needs — same pattern
    as test_loss_positions_total_drawdown_gate.py's own fixture."""
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
    monkeypatch.setattr(REGISTRY, "_metric_call_cache", {})
    return REGISTRY


def _real_sum_positions_with_gap_row(gap_pnl: float, other_pnl: float = 1000.0) -> pd.DataFrame:
    """Build `ctx.sum_positions` via the REAL
    `background.py:_rebuild_positions_summary` production path — not a
    hand-built fixture — so this test actually exercises the fix, not a
    stand-in for it."""
    from backend.api.background import _rebuild_positions_summary

    raw = pd.DataFrame([
        {
            "account": "ZG0790", "realised": 0.0, "unrealised": gap_pnl,
            "pnl": gap_pnl, "prev_settlement_pnl": None,
            "baseline_stale": True, "prev_close": 8700.0, "quantity": 10,
        },
        {
            "account": "ZG0790", "realised": 0.0, "unrealised": other_pnl,
            "pnl": other_pnl, "prev_settlement_pnl": None,
            "baseline_stale": False, "prev_close": 0.0, "quantity": 5,
        },
    ])
    summary = _rebuild_positions_summary(raw)
    total_row = summary.loc[summary["account"] == "TOTAL"].iloc[0].to_dict()
    # day_change_pct_margin isn't produced by _rebuild_positions_summary;
    # the day_pct metric token tolerates its absence (falls back to
    # day_change_percentage), matching real production shape.
    return pd.DataFrame([total_row])


class TestBaselineGapRowStillAllowsCriticalAgentToFire:
    def test_large_real_loss_on_gap_row_fires_the_critical_tier_agent(self, wired_registry):
        """A baseline_stale row carrying a real -₹250,000 lifetime pnl
        (broker-confirmed carry-over, no resolvable baseline) must still
        reach loss-positions-total's day_val leaf (<=-100000) — never
        silently read as 0/no-loss."""
        sum_positions = _real_sum_positions_with_gap_row(gap_pnl=-250000.0, other_pnl=1000.0)
        total = sum_positions.iloc[0]
        assert total["day_change_val"] == -249000.0, (
            "sanity check: the gap row's real loss must reach the "
            "account/TOTAL rollup, not be zeroed"
        )

        ctx = Context(
            sum_positions=sum_positions,
            alert_state={"pnl_history": {("positions", "TOTAL"): []}},
            now=pd.Timestamp.utcnow(),
            rate_window_min=10.0,
            baseline_live=True,
        )
        matches = evaluate(_agent_cond(), ctx)
        assert matches, (
            "loss-positions-total must be ABLE to fire when a "
            "baseline_stale row carries a real, large loss — it must "
            "never be silently passed as 'no loss'"
        )
        fired_metrics = {m["metric"] for m in matches}
        assert "day_val" in fired_metrics, (
            f"expected the day_val<=-100000 leaf to fire, got {matches}"
        )

    def test_gap_row_with_no_real_loss_does_not_falsely_fire(self, wired_registry):
        """Sanity control: a baseline_stale row with a small/neutral
        lifetime pnl must NOT spuriously trip the agent — confirms this
        fix doesn't make the gate fire unconditionally on every gap row,
        only on one that genuinely carries a real loss/gain large enough
        to cross the threshold."""
        sum_positions = _real_sum_positions_with_gap_row(gap_pnl=500.0, other_pnl=1000.0)
        total = sum_positions.iloc[0]
        assert total["day_change_val"] == 1500.0

        ctx = Context(
            sum_positions=sum_positions,
            alert_state={"pnl_history": {("positions", "TOTAL"): []}},
            now=pd.Timestamp.utcnow(),
            rate_window_min=10.0,
            baseline_live=True,
        )
        matches = evaluate(_agent_cond(), ctx)
        assert not matches, (
            "a gap row with no real loss must not spuriously fire the "
            "critical-tier agent"
        )
