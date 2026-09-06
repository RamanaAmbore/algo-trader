"""
Tests for MCX pre-close agent configuration and scheduled fire_at_time override.

Covers:
1. market-preclose-mcx slug exists with correct fire_at_time="23:00" and name
2. market-close-mcx slug does NOT exist in BUILTIN_AGENTS
3. When _cycle_maybe_buffer_fire is called with an info-tier agent that has
   fire_at_time set, the EvalResult's condition_text is overridden to
   "Scheduled — {fire_at_time} IST"
4. When _cycle_maybe_buffer_fire is called with an agent without fire_at_time,
   the condition_text is NOT overridden (remains the auto-generated match text)
5. A critical-tier fire_at_time agent does NOT get the "Scheduled — …" override
   so expiry-day auto-close agents emit their real condition text
6. BUILTIN_AGENTS confirms expiry-day auto-close agents are critical tier with
   fire_at_time set
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

    def _make_mock_agent(self, slug="test-agent", fire_at_time=None, tier='info', **kwargs):
        """Helper to create a mock agent with common defaults.

        tier defaults to 'info' so scheduled notification agents get the
        "Scheduled — HH:MM IST" override.  Pass tier='critical' / 'high' /
        'medium' to test that critical agents preserve their real condition text.
        """
        agent = MagicMock()
        agent.slug = slug
        agent.name = "Test Agent"
        agent.fire_at_time = fire_at_time
        agent.tier = tier
        agent.debounce_minutes = kwargs.get('debounce_minutes', 0)
        agent.trigger_count = kwargs.get('trigger_count', 0)
        for k, v in kwargs.items():
            if k not in ('debounce_minutes', 'trigger_count'):
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


class TestFireAtTimeTierGating:
    """Critical/high/medium tier agents with fire_at_time preserve real condition text.

    Regression guard for expiry-day auto-close agents: they are tier='critical' and
    must NOT have their condition_text replaced with "Scheduled — HH:MM IST".
    """

    _CFG = {
        'rate_window_min': 10,
        'baseline_offset_min': 15,
        'cooldown_min': 30,
        'suppress_delta_abs': 15000,
        'suppress_delta_pct': 0.5,
    }
    _ITM_MATCHES = [
        {
            'scope': 'positions.expiring_today.nfo',
            'metric': 'is_itm',
            'value': 1.0,
            'op': '==',
            'threshold': 1.0,
            'row': {'account': 'ACC1'},
        }
    ]

    def _call_buffer_fire(self, agent, matches=None):
        from datetime import datetime, timezone
        from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire
        pending = []
        triggered = _cycle_maybe_buffer_fire(
            agent,
            matches or self._ITM_MATCHES,
            now=datetime.now(timezone.utc),
            bypass_suppression=True,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg=self._CFG,
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending,
        )
        return triggered, pending

    def _make_agent(self, slug, tier, fire_at_time):
        agent = MagicMock()
        agent.slug = slug
        agent.name = "Test Agent"
        agent.tier = tier
        agent.fire_at_time = fire_at_time
        agent.debounce_minutes = 0
        agent.trigger_count = 0
        return agent

    def test_critical_tier_fire_at_time_preserves_condition_text(self):
        """tier='critical' + fire_at_time='15:00' → condition_text NOT overridden."""
        agent = self._make_agent(
            slug="expiry-day-equity-itm-auto-close",
            tier="critical",
            fire_at_time="15:00",
        )
        triggered, pending = self._call_buffer_fire(agent)
        assert triggered is True
        result = pending[0]['result']
        assert "Scheduled" not in result.condition_text, (
            f"Critical-tier agent must NOT get 'Scheduled' override; "
            f"got condition_text='{result.condition_text}'"
        )
        # Real condition text should reference the actual match scope/metric
        assert "positions.expiring_today.nfo" in result.condition_text or "is_itm" in result.condition_text, (
            f"Expected real condition text, got '{result.condition_text}'"
        )

    def test_critical_tier_mcx_fire_at_time_preserves_condition_text(self):
        """tier='critical' + fire_at_time='23:00' (MCX expiry) → condition_text NOT overridden."""
        mcx_matches = [
            {
                'scope': 'positions.expiring_today.mcx_unhedged',
                'metric': 'is_itm',
                'value': 1.0,
                'op': '==',
                'threshold': 1.0,
                'row': {'account': 'ACC1'},
            }
        ]
        agent = self._make_agent(
            slug="expiry-day-commodity-itm-auto-close",
            tier="critical",
            fire_at_time="23:00",
        )
        triggered, pending = self._call_buffer_fire(agent, matches=mcx_matches)
        assert triggered is True
        result = pending[0]['result']
        assert "Scheduled" not in result.condition_text, (
            f"Critical-tier MCX expiry agent must NOT get 'Scheduled' override; "
            f"got condition_text='{result.condition_text}'"
        )

    def test_high_tier_fire_at_time_preserves_condition_text(self):
        """tier='high' + fire_at_time set → condition_text NOT overridden."""
        agent = self._make_agent(
            slug="some-high-tier-agent",
            tier="high",
            fire_at_time="12:00",
        )
        triggered, pending = self._call_buffer_fire(agent)
        assert triggered is True
        assert "Scheduled" not in pending[0]['result'].condition_text

    def test_medium_tier_fire_at_time_preserves_condition_text(self):
        """tier='medium' + fire_at_time set → condition_text NOT overridden."""
        agent = self._make_agent(
            slug="some-medium-tier-agent",
            tier="medium",
            fire_at_time="10:00",
        )
        triggered, pending = self._call_buffer_fire(agent)
        assert triggered is True
        assert "Scheduled" not in pending[0]['result'].condition_text

    def test_info_tier_fire_at_time_gets_scheduled_override(self):
        """tier='info' + fire_at_time='09:15' → condition_text IS overridden."""
        agent = self._make_agent(
            slug="market-open-nse",
            tier="info",
            fire_at_time="09:15",
        )
        funds_matches = [
            {
                'scope': 'funds.any_acct',
                'metric': 'avail_margin',
                'value': 500000.0,
                'op': '>=',
                'threshold': -999999999,
                'row': {'account': 'ACC1'},
            }
        ]
        triggered, pending = self._call_buffer_fire(agent, matches=funds_matches)
        assert triggered is True
        assert pending[0]['result'].condition_text == "Scheduled — 09:15 IST"

    def test_low_tier_fire_at_time_gets_scheduled_override(self):
        """tier='low' + fire_at_time set → condition_text IS overridden (same as info)."""
        agent = self._make_agent(
            slug="some-low-tier-agent",
            tier="low",
            fire_at_time="14:00",
        )
        triggered, pending = self._call_buffer_fire(agent)
        assert triggered is True
        assert pending[0]['result'].condition_text == "Scheduled — 14:00 IST"


class TestExpiryAutoCloseAgentBuiltins:
    """Verify BUILTIN_AGENTS expiry-day auto-close agents have the correct configuration.

    These are the agents most affected by Fix 1: they are tier='critical' with
    fire_at_time set, so they must NOT get the 'Scheduled — …' label.
    """

    def test_expiry_day_equity_itm_auto_close_is_critical_tier(self):
        """expiry-day-equity-itm-auto-close has tier='critical'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agent = next(
            (a for a in BUILTIN_AGENTS if a.get('slug') == 'expiry-day-equity-itm-auto-close'),
            None,
        )
        assert agent is not None, "expiry-day-equity-itm-auto-close not found in BUILTIN_AGENTS"
        assert agent.get('tier') == 'critical', (
            f"Expected tier='critical', got {agent.get('tier')!r}"
        )

    def test_expiry_day_equity_itm_auto_close_has_fire_at_time(self):
        """expiry-day-equity-itm-auto-close has fire_at_time='15:00'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agent = next(
            (a for a in BUILTIN_AGENTS if a.get('slug') == 'expiry-day-equity-itm-auto-close'),
            None,
        )
        assert agent is not None
        assert agent.get('fire_at_time') == '15:00', (
            f"Expected fire_at_time='15:00', got {agent.get('fire_at_time')!r}"
        )

    def test_expiry_day_commodity_itm_auto_close_is_critical_tier(self):
        """expiry-day-commodity-itm-auto-close has tier='critical'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agent = next(
            (a for a in BUILTIN_AGENTS if a.get('slug') == 'expiry-day-commodity-itm-auto-close'),
            None,
        )
        assert agent is not None, "expiry-day-commodity-itm-auto-close not found in BUILTIN_AGENTS"
        assert agent.get('tier') == 'critical', (
            f"Expected tier='critical', got {agent.get('tier')!r}"
        )

    def test_expiry_day_commodity_itm_auto_close_has_fire_at_time(self):
        """expiry-day-commodity-itm-auto-close has fire_at_time='23:00'."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agent = next(
            (a for a in BUILTIN_AGENTS if a.get('slug') == 'expiry-day-commodity-itm-auto-close'),
            None,
        )
        assert agent is not None
        assert agent.get('fire_at_time') == '23:00', (
            f"Expected fire_at_time='23:00', got {agent.get('fire_at_time')!r}"
        )
