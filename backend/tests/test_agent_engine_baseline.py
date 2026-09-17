"""Tests for agent_engine baseline and rate-metric handling.

Covers:
  - _update_pnl_history: session start/reset, per-day baseline anchor
  - _v2_all_rate_metric: detect pure-rate agents vs mixed-condition agents
  - _cycle_baseline_not_ready: baseline gate logic for pure-rate agents

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
    _v2_all_rate_metric,
    _cycle_baseline_not_ready,
    _ae_has_pnl_leaf,
    _ae_should_reset_conditions,
    _ae_sync_existing_builtin,
)


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


class TestV2AllRateMetric:
    """_v2_all_rate_metric detects pure-rate vs mixed condition trees."""

    def test_v2_all_rate_metric_pure_rate(self):
        """All leaves contain _rate_ → True."""
        cond = {
            "any": [
                {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.total", "value": -6000},
                {"metric": "pnl_rate_pct", "op": "<=", "scope": "positions.total", "value": -0.25}
            ]
        }
        result = _v2_all_rate_metric(cond)
        assert result is True, (
            f"Expected _v2_all_rate_metric(pure rate) = True, got {result}"
        )

    def test_v2_all_rate_metric_mixed(self):
        """Some leaves without _rate_ → False."""
        cond = {
            "any": [
                {"metric": "pnl", "op": "<=", "scope": "positions.total", "value": -50000},
                {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.total", "value": -6000}
            ]
        }
        result = _v2_all_rate_metric(cond)
        assert result is False, (
            f"Expected _v2_all_rate_metric(mixed) = False, got {result}"
        )

    def test_v2_all_rate_metric_pure_static(self):
        """All leaves are static (no _rate_) → False."""
        cond = {
            "any": [
                {"metric": "pnl", "op": "<=", "scope": "positions.total", "value": -50000},
                {"metric": "day_pct", "op": "<=", "scope": "positions.total", "value": -3.0}
            ]
        }
        result = _v2_all_rate_metric(cond)
        assert result is False, (
            f"Expected _v2_all_rate_metric(pure static) = False, got {result}"
        )

    def test_v2_all_rate_metric_nested_all(self):
        """Nested 'all' with pure-rate children → True."""
        cond = {
            "all": [
                {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.any_acct", "value": -3000},
                {"metric": "pnl_rate_pct", "op": "<=", "scope": "positions.any_acct", "value": -0.25}
            ]
        }
        result = _v2_all_rate_metric(cond)
        assert result is True, (
            f"Expected _v2_all_rate_metric(nested all, pure rate) = True, got {result}"
        )


class TestCycleBaselineNotReady:
    """_cycle_baseline_not_ready gates pure-rate agents during baseline window."""

    def test_cycle_baseline_not_ready_mixed_agent_never_blocks(self):
        """Mixed-condition agent (pnl + pnl_rate_abs) → always False (not blocked)."""
        class FakeAgent:
            conditions = {
                "any": [
                    {"metric": "pnl", "op": "<=", "scope": "positions.total", "value": -50000},
                    {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.total", "value": -6000},
                ]
            }

        state = {}  # no session_start
        now = _now()
        cfg = {"baseline_offset_min": 15}
        result = _cycle_baseline_not_ready(FakeAgent(), state, now, cfg, bypass_schedule=False)
        assert result is False, (
            f"Expected mixed-condition agent NOT blocked (False), got {result}"
        )

    def test_cycle_baseline_not_ready_pure_rate_blocks_without_start(self):
        """Pure-rate agent with no session_start → True (blocked)."""
        class FakeAgent:
            conditions = {
                "any": [
                    {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.any_acct", "value": -3000},
                    {"metric": "pnl_rate_pct", "op": "<=", "scope": "positions.any_acct", "value": -0.25},
                ]
            }

        state = {}  # no session_start
        now = _now()
        cfg = {"baseline_offset_min": 15}
        result = _cycle_baseline_not_ready(FakeAgent(), state, now, cfg, bypass_schedule=False)
        assert result is True, (
            f"Expected pure-rate agent blocked without session_start (True), got {result}"
        )

    def test_cycle_baseline_not_ready_pure_rate_unblocks_after_offset(self):
        """Pure-rate agent with session_start N min ago → False (not blocked)."""
        class FakeAgent:
            conditions = {
                "any": [
                    {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.any_acct", "value": -3000},
                ]
            }

        now = _now()
        session_start = now - timedelta(minutes=20)  # 20 minutes ago
        state = {"session_start": session_start}
        cfg = {"baseline_offset_min": 15}  # offset is 15 min
        result = _cycle_baseline_not_ready(FakeAgent(), state, now, cfg, bypass_schedule=False)
        assert result is False, (
            f"Expected pure-rate agent NOT blocked after offset (False), got {result}"
        )

    def test_cycle_baseline_not_ready_bypass_schedule_always_unblocks(self):
        """With bypass_schedule=True, always return False regardless of state."""
        class FakeAgent:
            conditions = {
                "any": [
                    {"metric": "pnl_rate_abs", "op": "<=", "scope": "positions.any_acct", "value": -3000},
                ]
            }

        state = {}  # no session_start
        now = _now()
        cfg = {"baseline_offset_min": 15}
        result = _cycle_baseline_not_ready(FakeAgent(), state, now, cfg, bypass_schedule=True)
        assert result is False, (
            f"Expected bypass_schedule=True → always False, got {result}"
        )

    def test_cycle_baseline_not_ready_pure_rate_blocks_within_offset(self):
        """Pure-rate agent with session_start less than offset ago → True (blocked)."""
        class FakeAgent:
            conditions = {
                "any": [
                    {"metric": "pnl_rate_pct", "op": "<=", "scope": "positions.total", "value": -0.5},
                ]
            }

        now = _now()
        session_start = now - timedelta(minutes=10)  # 10 minutes ago
        state = {"session_start": session_start}
        cfg = {"baseline_offset_min": 15}  # offset is 15 min
        result = _cycle_baseline_not_ready(FakeAgent(), state, now, cfg, bypass_schedule=False)
        assert result is True, (
            f"Expected pure-rate agent blocked within offset (True), got {result}"
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
