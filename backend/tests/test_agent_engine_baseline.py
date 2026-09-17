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
from backend.api.algo.agent_engine import (
    _update_pnl_history,
    _v2_all_rate_metric,
    _cycle_baseline_not_ready,
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
