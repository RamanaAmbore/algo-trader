"""Tests for agent_engine baseline and rate-metric handling.

Covers:
  - _update_pnl_history: session start/reset, per-day baseline anchor,
    fix #6b segment-anchored session_start + closed-market skip
  - agent_evaluator.Context.baseline_live: fix #6a per-LEAF opening gate
    (replaces the old whole-agent _v2_all_rate_metric/_cycle_baseline_not_ready
    gate — a mixed agent's non-rate leaves must keep evaluating during the
    opening window; only rate leaves self-gate)

Five quality dimensions:
  SSOT        — direct invocation of functions under test
  Correctness — session_start set/reset, state transitions, gate behavior
  Performance — no network I/O; pure functions
  Reuse       — shared datetime fixtures
  UX          — every assert has an f-string with actual/expected
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
import pytest
import pandas as pd
from backend.api.algo.agent_engine import (
    _update_pnl_history,
    _ae_has_pnl_leaf,
    _ae_should_reset_conditions,
    _ae_sync_existing_builtin,
)
from backend.api.algo.agent_evaluator import Context


def _now():
    """Canonical test timestamp: 2026-09-17 09:00 IST (market open)."""
    return datetime(2026, 9, 17, 9, 0, 0)


class TestUpdatePnlHistory:
    """_update_pnl_history manages session_start + pnl_history lifecycle."""

    def test_session_start_set_on_first_call(self):
        """Cold start (empty alert_state) sets session_start to now."""
        state = {}
        now = _now()
        _update_pnl_history(state, now, None, None)
        assert state['session_start'] == now, (
            f"Expected session_start={now}, got {state.get('session_start')}"
        )

    def test_session_start_reset_on_new_day(self):
        """When date changes, session_start resets and pnl_history clears."""
        yesterday = datetime(2026, 9, 16, 9, 0, 0)
        state = {
            'session_start': yesterday,
            'session_date': date(2026, 9, 16),
            'pnl_history': {'old': 'data'},
        }
        now = _now()
        _update_pnl_history(state, now, None, None)
        assert state['session_start'] == now, (
            f"Expected session_start reset to {now}, got {state.get('session_start')}"
        )
        assert state['pnl_history'] == {}, (
            f"Expected pnl_history cleared on new day, got {state.get('pnl_history')}"
        )

    def test_session_start_preserved_same_day(self):
        """When date unchanged, session_start stays as-is."""
        now = _now()
        original_start = now - timedelta(minutes=30)
        state = {
            'session_start': original_start,
            'session_date': now.date(),
        }
        _update_pnl_history(state, now, None, None)
        assert state['session_start'] == original_start, (
            f"Expected session_start preserved as {original_start}, got {state.get('session_start')}"
        )


class TestBaselineLivePerLeaf:
    """Fix #6a — the opening-baseline gate is now on Context.baseline_live,
    checked PER RATE LEAF (Context.rate_abs/rate_pct), not per-whole-agent.
    A mixed agent's non-rate leaves (pnl, day_val, day_pct, …) never
    consult baseline_live at all — only the two rate metrics do — so a
    mixed agent stays partially evaluable during the opening window,
    which the old whole-agent `_v2_all_rate_metric` gate could never do
    (real loss-rate agents mix a day_val/pnl leaf with a rate leaf, so
    the old gate never actually applied to them)."""

    def _hist(self, now, n=5, step_min=2):
        return [(now - timedelta(minutes=step_min * (n - 1 - i)), -1000.0 * i, -1.0 * i)
                for i in range(n)]

    def test_baseline_live_false_blocks_rate_abs(self):
        """baseline_live=False → rate_abs returns None even with rich history."""
        now = _now()
        ctx = Context(alert_state={'pnl_history': {('positions', 'TOTAL'): self._hist(now)}},
                      now=now, rate_window_min=10, baseline_live=False)
        result = ctx.rate_abs(('positions', 'TOTAL'))
        assert result is None, f"Expected None while baseline not live, got {result}"

    def test_baseline_live_false_blocks_rate_pct(self):
        now = _now()
        ctx = Context(alert_state={'pnl_history': {('positions', 'TOTAL'): self._hist(now)}},
                      now=now, rate_window_min=10, baseline_live=False)
        result = ctx.rate_pct(('positions', 'TOTAL'))
        assert result is None, f"Expected None while baseline not live, got {result}"

    def test_baseline_live_true_computes_rate(self):
        """baseline_live=True (default) → rate_abs computes normally when
        history is sufficient (fix #1's sample/span gate satisfied)."""
        now = _now()
        ctx = Context(alert_state={'pnl_history': {('positions', 'TOTAL'): self._hist(now)}},
                      now=now, rate_window_min=10, baseline_live=True)
        result = ctx.rate_abs(('positions', 'TOTAL'))
        assert result is not None, "Expected a real rate value when baseline is live"

    def test_baseline_live_default_true_backcompat(self):
        """Context() with baseline_live unset defaults to True — legacy/back-compat
        callers that don't set it explicitly keep the old always-live behaviour."""
        now = _now()
        ctx = Context(alert_state={'pnl_history': {('positions', 'TOTAL'): self._hist(now)}}, now=now)
        assert ctx.baseline_live is True, (
            f"Expected baseline_live default True, got {ctx.baseline_live}"
        )


class TestSegmentAnchoredSessionStart:
    """Fix #6b — session_start anchors to the actual market-open moment
    (via market_state's nse_open/mcx_open flags), not to whenever the
    background poller first ran today (~08:00 IST, which expired the
    15-min opening gate around 08:19 — 40+ min before NSE even opens)."""

    def test_no_segment_open_defers_session_start(self):
        """market_state present but nothing open yet → session_start still
        gets a value (cold-start fallback) but NO pnl_history is recorded."""
        state = {}
        now = datetime(2026, 9, 17, 8, 30, 0)
        market_state = {'nse_open': False, 'mcx_open': False}
        pos_df = pd.DataFrame([{'account': 'TOTAL', 'day_change_val': -1000.0,
                                 'day_change_percentage': -1.0}])
        _update_pnl_history(state, now, pos_df, None, market_state=market_state)
        assert state.get('pnl_history', {}) == {}, (
            f"Expected no history recorded while market closed, got {state.get('pnl_history')}"
        )

    def test_session_start_anchors_to_segment_open(self):
        """Once NSE opens at 09:15, session_start anchors to that moment,
        not to the (earlier) wall-clock time _update_pnl_history first ran."""
        state = {}
        cold_start = datetime(2026, 9, 17, 8, 5, 0)
        _update_pnl_history(state, cold_start, None, None,
                            market_state={'nse_open': False, 'mcx_open': False})

        nse_open_ts = datetime(2026, 9, 17, 9, 15, 0)
        _update_pnl_history(state, nse_open_ts, None, None,
                            market_state={'nse_open': True, 'mcx_open': False})
        assert state['session_start'] == nse_open_ts, (
            f"Expected session_start anchored to NSE open {nse_open_ts}, "
            f"got {state['session_start']}"
        )

    def test_session_start_uses_latest_opening_segment(self):
        """When MCX (09:00) opens before NSE (09:15), session_start tracks
        the LATER of the two opens — conservative, keeps the gate live
        until every currently-open segment has cleared its own open."""
        state = {}
        mcx_open_ts = datetime(2026, 9, 17, 9, 0, 0)
        _update_pnl_history(state, mcx_open_ts, None, None,
                            market_state={'nse_open': False, 'mcx_open': True})
        assert state['session_start'] == mcx_open_ts, (
            f"Expected session_start={mcx_open_ts} after MCX-only open, "
            f"got {state['session_start']}"
        )

        nse_open_ts = datetime(2026, 9, 17, 9, 15, 0)
        _update_pnl_history(state, nse_open_ts, None, None,
                            market_state={'nse_open': True, 'mcx_open': True})
        assert state['session_start'] == nse_open_ts, (
            f"Expected session_start advanced to the LATER open {nse_open_ts}, "
            f"got {state['session_start']}"
        )

    def test_history_recorded_once_a_segment_is_open(self):
        """Once any segment is open, pnl_history records normally."""
        state = {}
        now = datetime(2026, 9, 17, 9, 20, 0)
        pos_df = pd.DataFrame([{'account': 'TOTAL', 'day_change_val': -1000.0,
                                 'day_change_percentage': -1.0}])
        _update_pnl_history(state, now, pos_df, None,
                            market_state={'nse_open': True, 'mcx_open': False})
        assert ('positions', 'TOTAL') in state['pnl_history'], (
            "Expected a positions/TOTAL bucket once NSE is open"
        )

    def test_market_state_none_keeps_legacy_unconditional_behaviour(self):
        """market_state=None (sim / back-compat) never gates on open/closed —
        history is always recorded, matching the pre-fix behaviour."""
        state = {}
        now = datetime(2026, 9, 17, 3, 0, 0)  # deep closed-hours wall clock
        pos_df = pd.DataFrame([{'account': 'TOTAL', 'day_change_val': -1000.0,
                                 'day_change_percentage': -1.0}])
        _update_pnl_history(state, now, pos_df, None, market_state=None)
        assert ('positions', 'TOTAL') in state['pnl_history'], (
            "Expected history recorded regardless of market hours when market_state=None"
        )


class TestPnlHistoryDayChangeVal:
    """_update_pnl_history uses day_change_val for positions, pnl for holdings."""

    def test_pnl_history_positions_uses_day_change_val(self):
        """Positions rows store day_change_val, not pnl, in pnl_history."""
        state = {}
        now = datetime(2026, 9, 16, 10, 0, 0)
        pos_df = pd.DataFrame([{
            'account': 'ZD',
            'pnl': 99999.0,
            'day_change_val': -5000.0,
            'day_change_percentage': -1.5,
        }])
        _update_pnl_history(state, now, pos_df, None)
        bucket = state['pnl_history'][('positions', 'ZD')]
        assert len(bucket) == 1, (
            f"Expected 1 history entry, got {len(bucket)}"
        )
        _, val, pct = bucket[0]
        assert val == -5000.0, (
            f"Expected day_change_val=-5000.0 stored (not pnl=99999.0), got {val}"
        )
        assert pct == -1.5, (
            f"Expected day_change_percentage=-1.5, got {pct}"
        )

    def test_pnl_history_holdings_uses_pnl(self):
        """Holdings rows still store pnl (not day_change_val) in pnl_history."""
        state = {}
        now = datetime(2026, 9, 16, 10, 0, 0)
        hld_df = pd.DataFrame([{
            'account': 'ZD',
            'pnl': -8000.0,
            'pnl_percentage': -2.5,
            'day_change_val': 99999.0,  # must be ignored for holdings
        }])
        _update_pnl_history(state, now, None, hld_df)
        bucket = state['pnl_history'][('holdings', 'ZD')]
        assert len(bucket) == 1, (
            f"Expected 1 history entry, got {len(bucket)}"
        )
        _, val, pct = bucket[0]
        assert val == -8000.0, (
            f"Expected pnl=-8000.0 stored for holdings, got {val}"
        )
        assert pct == -2.5, (
            f"Expected pnl_percentage=-2.5, got {pct}"
        )


class TestAeShouldResetConditions:
    """_ae_should_reset_conditions detects stale pnl/pnl_pct leaves."""

    def test_detects_stale_pnl_leaf(self):
        """Top-level any: with a pnl leaf → True."""
        stale = {"any": [
            {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -50000},
        ]}
        clean = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
        ]}
        assert _ae_should_reset_conditions(stale, clean) is True, (
            "Expected True for stale pnl leaf"
        )

    def test_clean_conditions_return_false(self):
        """Conditions with only day_val/day_pct leaves → False."""
        clean = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
        ]}
        assert _ae_should_reset_conditions(clean, clean) is False, (
            "Expected False for clean day_val/day_pct conditions"
        )

    def test_detects_stale_pnl_pct_leaf(self):
        """pnl_pct leaf (not pnl_rate_pct) → True."""
        stale = {"any": [
            {"metric": "pnl_pct", "scope": "positions.any_acct", "op": "<=", "value": -2.0},
        ]}
        assert _ae_should_reset_conditions(stale, None) is True, (
            "Expected True for stale pnl_pct leaf"
        )

    def test_nested_stale_pnl_pct_detected(self):
        """Nested any: with mixed leaves — pnl_pct present → True."""
        nested_stale = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "pnl_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
        ]}
        clean = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
        ]}
        assert _ae_should_reset_conditions(nested_stale, clean) is True, (
            "Expected True when nested pnl_pct leaf detected"
        )

    def test_rate_metric_not_stale(self):
        """pnl_rate_abs and pnl_rate_pct are NOT stale metric names → False."""
        rate_cond = {"any": [
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}
        assert _ae_should_reset_conditions(rate_cond, None) is False, (
            "pnl_rate_abs / pnl_rate_pct are not stale — expected False"
        )

    def test_none_conditions_return_false(self):
        """None existing_cond → False (no-op for agents without conditions)."""
        assert _ae_should_reset_conditions(None, None) is False, (
            "Expected False when existing_cond is None"
        )

    def test_code_also_has_pnl_no_reset(self):
        """When code_cond also has a pnl leaf, do NOT reset (legitimate agent like loss-pos-total-auto-close)."""
        auto_close_cond = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -50000}
        assert _ae_should_reset_conditions(auto_close_cond, auto_close_cond) is False, (
            "Expected False: code also uses pnl — no migration needed"
        )

    def test_ae_has_pnl_leaf_direct(self):
        """_ae_has_pnl_leaf correctly identifies pnl leaves."""
        assert _ae_has_pnl_leaf({"metric": "pnl"}) is True
        assert _ae_has_pnl_leaf({"metric": "pnl_pct"}) is True
        assert _ae_has_pnl_leaf({"metric": "pnl_rate_abs"}) is False
        assert _ae_has_pnl_leaf({"metric": "day_val"}) is False
        assert _ae_has_pnl_leaf(None) is False


class TestAeSyncExistingBuiltinResetsStaleConditions:
    """_ae_sync_existing_builtin force-resets stale pnl/pnl_pct conditions."""

    def _make_existing(self, conditions):
        """Minimal Agent-like stub with mutable conditions."""
        from types import SimpleNamespace
        return SimpleNamespace(
            conditions=conditions,
            long_name=None,
            schedule="market_hours",
            tier="medium",
            topic="general",
            status="active",
            events=[],
            fire_at_time=None,
            last_fired=None,
        )

    def test_stale_pnl_leaf_gets_reset(self):
        """Existing conditions with pnl leaf are replaced by code conditions."""
        stale_cond = {"any": [
            {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -50000},
        ]}
        new_cond = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
        ]}
        existing = self._make_existing(stale_cond)
        agent_def = {
            "slug": "loss-positions-total",
            "conditions": new_cond,
            "long_name": "test",
        }
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == new_cond, (
            f"Expected conditions reset to new_cond, got {existing.conditions}"
        )

    def test_clean_conditions_not_overwritten(self):
        """Already-migrated conditions (day_val/day_pct) are not touched."""
        clean_cond = {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
        ]}
        existing = self._make_existing(clean_cond)
        agent_def = {
            "slug": "loss-positions-total",
            "conditions": clean_cond,
            "long_name": "test",
        }
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == clean_cond, (
            f"Expected clean conditions unchanged, got {existing.conditions}"
        )
