"""
Tests for MCX pre-close agent configuration and scheduled fire_at_time override.

Covers:
1. market-preclose-mcx slug exists with correct fire_at_time="23:00" and name
2. market-close-mcx slug does NOT exist in BUILTIN_AGENTS
3. When _cycle_maybe_buffer_fire is called with an agent that has fire_at_time set,
   the EvalResult's condition_text is overridden to "Scheduled — {fire_at_time} IST"
4. When _cycle_maybe_buffer_fire is called with an agent without fire_at_time,
   the condition_text is NOT overridden (remains the auto-generated match text)
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


class TestMCXPrecloseAgentConfig:
    """MCX pre-close agent has correct configuration."""

    def test_market_preclose_mcx_exists_in_builtin_agents(self):
        """market-preclose-mcx slug exists in BUILTIN_AGENTS."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-preclose-mcx'), None)
        assert agent is not None, "market-preclose-mcx not found in BUILTIN_AGENTS"

    def test_market_preclose_mcx_has_correct_fire_at_time(self):
        """market-preclose-mcx has fire_at_time == '23:00'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-preclose-mcx'), None)
        assert agent is not None, "market-preclose-mcx not found"
        assert agent.get('fire_at_time') == '23:00', \
            f"Expected fire_at_time='23:00', got {agent.get('fire_at_time')}"

    def test_market_preclose_mcx_has_correct_name(self):
        """market-preclose-mcx has name == 'MCX pre-close'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-preclose-mcx'), None)
        assert agent is not None, "market-preclose-mcx not found"
        assert agent.get('name') == 'MCX pre-close', \
            f"Expected name='MCX pre-close', got {agent.get('name')}"

    def test_market_close_mcx_does_not_exist(self):
        """market-close-mcx slug does NOT exist in BUILTIN_AGENTS (retired)."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-close-mcx'), None)
        assert agent is None, "market-close-mcx should not exist in BUILTIN_AGENTS (retired)"

    def test_market_preclose_mcx_is_info_tier(self):
        """market-preclose-mcx should be info tier (notification only, low priority)."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-preclose-mcx'), None)
        assert agent is not None
        assert agent.get('tier') == 'info', \
            f"Expected tier='info', got {agent.get('tier')}"

    def test_market_preclose_mcx_is_active_status(self):
        """market-preclose-mcx should be active by default."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'market-preclose-mcx'), None)
        assert agent is not None
        assert agent.get('status') == 'active', \
            f"Expected status='active', got {agent.get('status')}"


class TestFireAtTimeConditionOverride:
    """_cycle_maybe_buffer_fire overrides condition_text when fire_at_time is set."""

    def _make_mock_agent(self, slug="test-agent", fire_at_time=None, **kwargs):
        """Helper to create a mock agent with common defaults."""
        agent = MagicMock()
        agent.slug = slug
        agent.name = "Test Agent"
        agent.fire_at_time = fire_at_time
        agent.debounce_minutes = kwargs.get('debounce_minutes', 0)
        agent.trigger_count = kwargs.get('trigger_count', 0)
        for k, v in kwargs.items():
            if k != 'debounce_minutes' and k != 'trigger_count':
                setattr(agent, k, v)
        return agent

    def test_cycle_maybe_buffer_fire_with_fire_at_time_overrides_condition_text(self):
        """When agent.fire_at_time is set, condition_text is overridden to 'Scheduled — HH:MM IST'."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-scheduled",
            fire_at_time="23:00",
        )
        matches = [
            {
                'scope': 'funds.any_acct',
                'metric': 'avail_margin',
                'value': 100000.0,
                'op': '>=',
                'threshold': -999999999,
                'row': {'account': 'ACC1'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True, "Agent should have fired"
        assert len(pending_dispatches) == 1, "Should have one pending dispatch"
        result = pending_dispatches[0]['result']
        assert result.condition_text == "Scheduled — 23:00 IST", \
            f"Expected 'Scheduled — 23:00 IST', got '{result.condition_text}'"

    def test_cycle_maybe_buffer_fire_without_fire_at_time_preserves_condition_text(self):
        """When agent.fire_at_time is None, condition_text is NOT overridden (remains auto-generated)."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-no-scheduled",
            fire_at_time=None,
        )
        matches = [
            {
                'scope': 'positions.any_acct',
                'metric': 'pnl',
                'value': -35000.0,
                'op': '<=',
                'threshold': -30000,
                'row': {'account': 'ACC1'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True, "Agent should have fired"
        assert len(pending_dispatches) == 1, "Should have one pending dispatch"
        result = pending_dispatches[0]['result']
        # Should be the auto-generated condition_text from _v2_build_evalresult
        # Format: "scope metric=value (threshold)" with thousands separator
        assert "positions.any_acct" in result.condition_text, \
            f"Expected auto-generated text with scope, got '{result.condition_text}'"
        assert "pnl=" in result.condition_text, \
            f"Expected metric in text, got '{result.condition_text}'"
        assert "(<= -30000)" in result.condition_text, \
            f"Expected threshold in text, got '{result.condition_text}'"

    def test_cycle_maybe_buffer_fire_fire_at_time_15_00(self):
        """fire_at_time='15:00' is correctly rendered as 'Scheduled — 15:00 IST'."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-nfo-expiry",
            fire_at_time="15:00",
        )
        matches = [
            {
                'scope': 'positions.expiring_today.nfo',
                'metric': 'is_itm',
                'value': 1.0,
                'op': '==',
                'threshold': 1.0,
                'row': {'account': 'ACC1'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True
        result = pending_dispatches[0]['result']
        assert result.condition_text == "Scheduled — 15:00 IST", \
            f"Expected 'Scheduled — 15:00 IST', got '{result.condition_text}'"

    def test_cycle_maybe_buffer_fire_fire_at_time_09_15(self):
        """fire_at_time='09:15' is correctly rendered as 'Scheduled — 09:15 IST'."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-nse-open",
            fire_at_time="09:15",
        )
        matches = [
            {
                'scope': 'funds.any_acct',
                'metric': 'avail_margin',
                'value': 500000.0,
                'op': '>=',
                'threshold': -999999999,
                'row': {'account': 'ACC1'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True
        result = pending_dispatches[0]['result']
        assert result.condition_text == "Scheduled — 09:15 IST", \
            f"Expected 'Scheduled — 09:15 IST', got '{result.condition_text}'"

    def test_cycle_maybe_buffer_fire_empty_fire_at_time_treated_as_none(self):
        """fire_at_time='' (empty string) is treated as falsy and condition_text is NOT overridden."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-empty-time",
            fire_at_time="",  # Empty string — falsy
        )
        matches = [
            {
                'scope': 'positions.any_acct',
                'metric': 'pnl',
                'value': -40000.0,
                'op': '<=',
                'threshold': -30000,
                'row': {'account': 'ACC1'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True
        result = pending_dispatches[0]['result']
        # Should be auto-generated, not overridden
        assert "positions.any_acct" in result.condition_text, \
            f"Expected auto-generated text with scope, got '{result.condition_text}'"
        assert "pnl=" in result.condition_text, \
            f"Expected metric in text, got '{result.condition_text}'"
        assert "Scheduled" not in result.condition_text, \
            f"Should not have 'Scheduled' override for empty fire_at_time, got '{result.condition_text}'"

    def test_cycle_maybe_buffer_fire_no_fire_at_time_attribute_treated_as_none(self):
        """Agent without fire_at_time attribute is treated safely."""
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire

        agent = self._make_mock_agent(
            slug="test-no-attr",
        )
        # Explicitly delete the fire_at_time attribute
        delattr(agent, 'fire_at_time')

        matches = [
            {
                'scope': 'positions.total',
                'metric': 'pnl',
                'value': -55000.0,
                'op': '<=',
                'threshold': -50000,
                'row': {'account': 'TOTAL'}
            }
        ]
        now = datetime.now(timezone.utc)
        cfg = {
            'rate_window_min': 10,
            'baseline_offset_min': 15,
            'cooldown_min': 30,
            'suppress_delta_abs': 15000,
            'suppress_delta_pct': 0.5,
        }
        pending_dispatches = []

        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches,
            now=now,
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=cfg,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert triggered is True
        result = pending_dispatches[0]['result']
        # Should be auto-generated, not overridden
        assert "positions.total" in result.condition_text, \
            f"Expected auto-generated text with scope, got '{result.condition_text}'"
        assert "pnl=" in result.condition_text, \
            f"Expected metric in text, got '{result.condition_text}'"
        assert "Scheduled" not in result.condition_text, \
            f"Should not have 'Scheduled' override when attribute missing, got '{result.condition_text}'"
