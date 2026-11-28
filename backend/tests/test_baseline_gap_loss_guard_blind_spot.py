"""2026-11 6D-audit fix — a critical/high-tier positions loss guard
must still be ABLE to fire when a `baseline_stale` ("daily_book baseline
gap") row carries a real, large negative live P&L.

Context: a `baseline_stale` row (commit a0fd14fec) is one whose Day P&L
baseline couldn't be resolved, even though the broker confirms it carried
a non-zero quantity from before today (`pnl_math.is_baseline_gap`). The
original fix for that case short-circuited the row's contribution to
EVERY account/TOTAL Day P&L rollup to a hard 0.0 — including
`background.py`'s `_rebuild_positions_summary`, the exact feed a
market-P&L day_val/day_pct leaf reads via `ctx.sum_positions`. If one
account's EOD `daily_book` write genuinely fails one night (a real,
documented, pre-existing failure mode), every position carried forward
from that account the next session would be flagged
`baseline_stale=True` and silently contribute 0 to the Day P&L total —
even while genuinely losing a lot of real money that day. A critical-
tier loss guard reading that feed would go silently blind in exactly the
direction CLAUDE.md's "Staleness indicator freeze rule" forbids.

SUPERSEDED, in part, 2026-11-28: `loss-positions-total` (the agent this
file originally targeted) was redesigned to pure F&O THEORETICAL loss —
it no longer reads `sum_positions`/`day_change_val` at all, so it cannot
inherit a `baseline_stale`-driven market-P&L zeroing bug by
construction. `TestLossPositionsTotalConditionTreeNoLongerReadsMarketPnl`
below confirms this structurally. `loss-positions-acct` (high-tier,
per-account) STILL reads `day_val`/`day_pct` off the exact same
`sum_positions` feed, so the original regression concern remains live
for IT — `TestBaselineGapRowStillAllowsHighTierAcctAgentToFire` below
re-points the original end-to-end scenario at that agent instead.

Both tests drive the REAL production path end to end: a raw per-position
frame (one baseline_stale row with a large negative lifetime pnl, one
ordinary row) through the REAL `background.py:_rebuild_positions_summary`
to build the REAL `ctx.sum_positions` shape, then evaluate the real
condition tree via the real `evaluate()` — confirming the agent fires,
not that it silently passes as "no loss"."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.api.algo.agent_evaluator import Context, evaluate


def _agent_cond(slug):
    from backend.api.algo.agent_engine import _LOSS_AGENTS
    agent = next(a for a in _LOSS_AGENTS if a["slug"] == slug)
    return agent["conditions"]


@pytest.fixture
def wired_registry(monkeypatch):
    """Wire the real resolvers `loss-positions-acct`'s condition tree
    needs — same pattern as test_loss_positions_total_drawdown_gate.py's
    own (now-retired) fixture."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo import grammar as g
    from backend.api.algo.grammar_tokens.metric_day_val import _metric_day_val
    from backend.api.algo.grammar_tokens.metric_day_pct import _metric_day_pct
    from backend.api.algo.grammar_tokens.scope_positions_any_acct import _scope_positions_any_acct

    monkeypatch.setitem(REGISTRY.metrics, "day_val", _metric_day_val)
    monkeypatch.setitem(REGISTRY.metrics, "day_pct", _metric_day_pct)
    monkeypatch.setitem(REGISTRY.scopes, "positions.any_acct", _scope_positions_any_acct)
    monkeypatch.setitem(REGISTRY.operators, "<=", g.OPERATORS["<="])
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
    return _rebuild_positions_summary(raw)


class TestLossPositionsTotalConditionTreeNoLongerReadsMarketPnl:
    def test_no_day_val_or_day_pct_leaf_survives(self):
        """The 2026-11-28 pure-theoretical redesign retired the market-
        P&L leaves entirely — this agent can no longer inherit the
        `baseline_stale` zeroing bug this file was originally written
        to guard against, because it no longer reads `sum_positions`/
        `day_change_val` at all. See CLAUDE.md's dated entry and
        test_loss_positions_total_theoretical_redesign.py for the
        agent's own current coverage."""
        cond = _agent_cond("loss-positions-total")

        # Walk the condition tree directly — agent_engine.py exports no
        # recursive metric-collector, and this test only needs a plain
        # recursive `any`/`all` walk (same shape `test_loss_agents.py`'s
        # own `_collect_metrics` uses, duplicated here rather than
        # imported since it's test-module-local there).
        def _walk(c):
            out = set()
            if not isinstance(c, dict):
                return out
            for key in ("all", "any"):
                for child in (c.get(key) or []):
                    out |= _walk(child)
            if "metric" in c:
                out.add(c["metric"])
            return out

        all_metrics = _walk(cond)
        assert "day_val" not in all_metrics and "day_pct" not in all_metrics, (
            f"loss-positions-total must never read market day_val/day_pct "
            f"again, got {all_metrics}"
        )


class TestBaselineGapRowStillAllowsHighTierAcctAgentToFire:
    """`loss-positions-acct` still reads day_val/day_pct off the exact
    same `sum_positions` feed `loss-positions-total` USED to read before
    its 2026-11-28 redesign — the original regression concern remains
    live for this sibling agent."""

    def test_large_real_loss_on_gap_row_fires_the_acct_agent(self, wired_registry):
        """A baseline_stale row carrying a real -₹250,000 lifetime pnl
        (broker-confirmed carry-over, no resolvable baseline) must still
        reach loss-positions-acct's day_val leaf — never silently read
        as 0/no-loss."""
        sum_positions = _real_sum_positions_with_gap_row(gap_pnl=-250000.0, other_pnl=1000.0)
        acct_row = sum_positions.loc[sum_positions["account"] == "ZG0790"].iloc[0]
        assert acct_row["day_change_val"] == -249000.0, (
            "sanity check: the gap row's real loss must reach the "
            "account rollup, not be zeroed"
        )

        ctx = Context(
            sum_positions=sum_positions,
            alert_state={"pnl_history": {}},
            now=pd.Timestamp.utcnow(),
            rate_window_min=10.0,
            baseline_live=True,
        )
        matches = evaluate(_agent_cond("loss-positions-acct"), ctx)
        assert matches, (
            "loss-positions-acct must be ABLE to fire when a "
            "baseline_stale row carries a real, large loss — it must "
            "never be silently passed as 'no loss'"
        )
        fired_metrics = {m["metric"] for m in matches}
        assert "day_val" in fired_metrics, (
            f"expected the day_val leaf to fire, got {matches}"
        )

    def test_gap_row_with_no_real_loss_does_not_falsely_fire(self, wired_registry):
        """Sanity control: a baseline_stale row with a small/neutral
        lifetime pnl must NOT spuriously trip the agent."""
        sum_positions = _real_sum_positions_with_gap_row(gap_pnl=500.0, other_pnl=1000.0)
        acct_row = sum_positions.loc[sum_positions["account"] == "ZG0790"].iloc[0]
        assert acct_row["day_change_val"] == 1500.0

        ctx = Context(
            sum_positions=sum_positions,
            alert_state={"pnl_history": {}},
            now=pd.Timestamp.utcnow(),
            rate_window_min=10.0,
            baseline_live=True,
        )
        matches = evaluate(_agent_cond("loss-positions-acct"), ctx)
        assert not matches, (
            "a gap row with no real loss must not spuriously fire the agent"
        )
