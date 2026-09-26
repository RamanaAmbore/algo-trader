"""
Tests for the partial-outage guard (2026-09 alerts audit item 5).

Bug: `_fetch_positions_direct` (background.py) already raised on a FULL
outage (`_is_positions_outage` — all accounts failed) via `_is_positions_outage`,
which is correct and untouched. The gap was the PARTIAL case: some (not
all) accounts' frames carry `attrs['fetch_failed']=True` — `pd.concat`
silently drops that account's (non-)contribution from the resulting
`raw`/`summary` totals, so the TOTAL row understates the real P&L as if
the missing account had simply never existed. Two consumers read this
distorted TOTAL:
  1. `grammar._scope_positions_total` — feeds the `loss-positions-total`
     static day_val leaf straight from `sum_positions`'s TOTAL row.
  2. `agent_engine._update_pnl_history` — records that TOTAL row into
     `alert_state['pnl_history'][('positions', 'TOTAL')]`, corrupting a
     FUTURE tick's rate-of-change window.

Fix: `_positions_partial_outage_accounts()` (positions.py) detects the
partial shape; `_fetch_positions_direct` / `_rebuild_positions_summary`
carry it forward as a `.attrs['partial_outage']` flag (never a tuple-
shape change) on both the raw and summary frames. Both consumers above
check the attr and skip/freeze the TOTAL scope for that tick, while
leaving healthy per-account rows/buckets completely unaffected.

Five quality dimensions:
  SSOT        — single `_positions_partial_outage_accounts` helper,
                reused (not re-implemented) by both consumers via the
                shared `.attrs` flag
  Correctness — full-outage still raises; partial sets the flag; clean
                case carries no flag at all
  Reachability — the real chain (fetch -> rebuild -> scope / pnl_history)
                is exercised, not a hand-set attrs shortcut
  Reuse       — per-account (non-TOTAL) rows/buckets are proven
                unaffected — the regression guard that shows only TOTAL
                was frozen
  UX          — a fully-recovered/absent observation this tick is never
                misread as "condition recovered" (fix #5's existing
                contract), confirmed by asserting NO observation is
                recorded for the positions.total scope
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest


def _partial_outage_summary() -> "pd.DataFrame":
    """A positions summary frame shaped like the real post-
    `_rebuild_positions_summary` output, flagged with partial_outage —
    ACCT_A is healthy, ACCT_FAILED is missing (never contributed a row)."""
    df = pd.DataFrame([
        {'account': 'ACCT_A', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
        {'account': 'TOTAL', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
    ])
    df.attrs['partial_outage'] = ['ACCT_FAILED']
    return df


def _clean_summary() -> "pd.DataFrame":
    df = pd.DataFrame([
        {'account': 'ACCT_A', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
        {'account': 'TOTAL', 'pnl': 1000.0, 'day_change_val': 500.0,
         'day_change_percentage': 5.0},
    ])
    return df


# ---------------------------------------------------------------------------
# grammar._scope_positions_total
# ---------------------------------------------------------------------------

class TestScopePositionsTotalPartialOutageGuard:
    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        """REGISTRY is normally populated from the DB at app startup
        (`REGISTRY.reload()`); wire the real grammar.py resolvers in
        directly (auto-restored after each test) so `evaluate()` is
        exercised end-to-end without a DB session — same pattern as
        test_agent_evaluator.py."""
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import _metric_day_val, _scope_positions_total, OPERATORS

        monkeypatch.setitem(REGISTRY.metrics, 'day_val', _metric_day_val)
        monkeypatch.setitem(REGISTRY.scopes, 'positions.total', _scope_positions_total)
        monkeypatch.setitem(REGISTRY.operators, '<=', OPERATORS['<='])

    def test_returns_empty_when_partial_outage_flagged(self):
        from backend.api.algo.grammar import _scope_positions_total

        class _Ctx:
            sum_positions = _partial_outage_summary()

        assert _scope_positions_total(_Ctx()) == [], (
            "positions.total scope must return no rows when the summary "
            "carries a partial_outage flag — a distorted TOTAL must never "
            "be evaluated against any leaf this tick"
        )

    def test_returns_total_row_when_clean(self):
        from backend.api.algo.grammar import _scope_positions_total

        class _Ctx:
            sum_positions = _clean_summary()

        rows = _scope_positions_total(_Ctx())
        assert len(rows) == 1
        assert rows[0]['account'] == 'TOTAL'

    def test_no_observation_recorded_when_partial_outage(self):
        """End-to-end through the real evaluator: a leaf scoped to
        positions.total must produce ZERO observations when partial
        outage is flagged — confirming fix #5's 'absent from
        observations = untouched, never recovered' contract actually
        applies here (not just that the scope returns [])."""
        from backend.api.algo.agent_evaluator import Context, evaluate

        ctx = Context(sum_positions=_partial_outage_summary(), now=datetime.now(timezone.utc))
        cond = {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -30000}
        matches = evaluate(cond, ctx)

        assert matches == []
        assert ctx.observations == [], (
            f"Expected zero observations for positions.total during partial "
            f"outage, got {ctx.observations}"
        )

    def test_observation_recorded_when_clean(self):
        """Regression guard — the clean case must still record an
        observation (proving the partial-outage test above is actually
        exercising the guard, not some unrelated evaluator no-op)."""
        from backend.api.algo.agent_evaluator import Context, evaluate

        ctx = Context(sum_positions=_clean_summary(), now=datetime.now(timezone.utc))
        cond = {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -30000}
        evaluate(cond, ctx)

        assert len(ctx.observations) == 1
        assert ctx.observations[0]['account'] == 'TOTAL'


# ---------------------------------------------------------------------------
# agent_engine._update_pnl_history
# ---------------------------------------------------------------------------

class TestUpdatePnlHistoryPartialOutageGuard:
    def test_total_sample_skipped_but_healthy_account_sample_kept(self):
        from backend.api.algo.agent_engine import _update_pnl_history

        alert_state: dict = {}
        now = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
        market_state = {"nse_open": True, "mcx_open": False}

        _update_pnl_history(
            alert_state, now, _partial_outage_summary(), None,
            market_state=market_state,
        )

        hist = alert_state.get('pnl_history', {})
        assert ('positions', 'TOTAL') not in hist, (
            "TOTAL bucket must get NO sample this tick when partial_outage "
            "is flagged — recording it would poison a future rate-of-change "
            "window with a distorted (missing-account) total"
        )
        assert ('positions', 'ACCT_A') in hist, (
            "The healthy account's own bucket must still get its sample — "
            "only TOTAL is frozen, not the whole positions section"
        )
        assert hist[('positions', 'ACCT_A')][-1][1] == pytest.approx(500.0)

    def test_total_sample_recorded_when_clean(self):
        """Regression guard — TOTAL must still get its sample in the
        normal (no partial outage) case."""
        from backend.api.algo.agent_engine import _update_pnl_history

        alert_state: dict = {}
        now = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
        market_state = {"nse_open": True, "mcx_open": False}

        _update_pnl_history(
            alert_state, now, _clean_summary(), None,
            market_state=market_state,
        )

        hist = alert_state.get('pnl_history', {})
        assert ('positions', 'TOTAL') in hist
        assert hist[('positions', 'TOTAL')][-1][1] == pytest.approx(500.0)
