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
from unittest.mock import MagicMock, AsyncMock, patch
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
        """expiry-day-equity-itm-auto-close has fire_at_time='15:15'
        (matches order_hold_gate.cutoff_for('NFO')'s default
        lead_minutes_nfo=15: 15:30 close - 15 = 15:15). Fixed 2026-10
        (Sprint 1a) — was '15:00', which is BEFORE its own cutoff, so
        the action's before_cutoff() gate deferred every cycle."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS
        agent = next(
            (a for a in BUILTIN_AGENTS if a.get('slug') == 'expiry-day-equity-itm-auto-close'),
            None,
        )
        assert agent is not None
        assert agent.get('fire_at_time') == '15:15', (
            f"Expected fire_at_time='15:15', got {agent.get('fire_at_time')!r}"
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


class TestDeriveKindOpAware:
    """_v2_derive_kind no longer mislabels an always-true `>=` trick leaf on
    cash/avail_margin as a real floor breach — the fix for the operator-
    reported false-positive '-₹999,999,999' alert firing on every account.
    """

    def test_avail_margin_always_true_leaf_is_not_negative_margin(self):
        """The market-open-nse/market-preclose-mcx trick leaf (avail_margin
        >= -999999999) must NOT classify as 'negative_margin'."""
        from backend.api.algo.agent_engine import _v2_derive_kind
        kind = _v2_derive_kind('avail_margin', '>=')
        assert kind != 'negative_margin', (
            f"'>=' avail_margin leaf must not classify as negative_margin, got {kind!r}"
        )
        assert kind == 'static_abs'

    def test_avail_margin_real_floor_breach_still_negative_margin(self):
        """CRITICAL regression guard: the genuine loss-funds-negative shape
        (avail_margin < 0) must STILL classify as 'negative_margin'."""
        from backend.api.algo.agent_engine import _v2_derive_kind
        assert _v2_derive_kind('avail_margin', '<') == 'negative_margin'
        assert _v2_derive_kind('avail_margin', '<=') == 'negative_margin'

    def test_cash_real_floor_breach_still_negative_cash(self):
        """CRITICAL regression guard: cash < 0 must still classify as
        'negative_cash' — the other leg of loss-funds-negative."""
        from backend.api.algo.agent_engine import _v2_derive_kind
        assert _v2_derive_kind('cash', '<') == 'negative_cash'
        assert _v2_derive_kind('cash', '<=') == 'negative_cash'

    def test_cash_always_true_leaf_is_not_negative_cash(self):
        from backend.api.algo.agent_engine import _v2_derive_kind
        assert _v2_derive_kind('cash', '>=') != 'negative_cash'

    def test_derive_kind_default_op_falls_back_safely(self):
        """Calling with no op (legacy call shape) must not raise and must
        not accidentally classify as a floor-breach kind."""
        from backend.api.algo.agent_engine import _v2_derive_kind
        assert _v2_derive_kind('avail_margin') != 'negative_margin'
        assert _v2_derive_kind('cash') != 'negative_cash'


class TestFormatThresholdSentinelGuard:
    """_v2_format_threshold refuses to render an absurd sentinel magnitude
    (e.g. -999999999) as if it were a real ₹/% figure — belt-and-suspenders
    fix #3, independent of the kind-classification fix."""

    def test_sentinel_threshold_renders_neutral_label(self):
        from backend.api.algo.agent_engine import _v2_format_threshold
        assert _v2_format_threshold('static_abs', -999999999) == 'n/a'
        assert _v2_format_threshold('negative_margin', -999999999) == 'n/a'
        assert _v2_format_threshold('static_pct', -999999999) == 'n/a'

    def test_real_threshold_values_unaffected(self):
        """CRITICAL regression guard: real, small thresholds must render
        exactly as before (loss-funds-negative uses threshold=0)."""
        from backend.api.algo.agent_engine import _v2_format_threshold
        assert _v2_format_threshold('negative_margin', 0) == '-₹0'
        assert _v2_format_threshold('negative_cash', 0) == '-₹0'
        assert _v2_format_threshold('static_abs', -30000) == '-₹30,000'
        assert _v2_format_threshold('static_pct', -2.0) == '-2.00%'
        assert _v2_format_threshold('rate_abs', -10000) == '-₹10,000/min'


class TestMatchToAlertRowMarginFalsePositive:
    """End-to-end coverage of the operator-reported incident:
    _v2_match_to_alertrow must render the real fetched value/threshold for
    a genuine breach and must NOT fabricate '-₹999,999,999' for the
    schedule-only trick leaf."""

    def test_genuine_negative_margin_breach_unchanged(self):
        """CRITICAL regression guard — byte-for-byte: the real
        loss-funds-negative shape (avail_margin < 0) must render exactly as
        it did before this fix: kind, real fetched pnl, and threshold all
        untouched."""
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        match = {
            'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '<',
            'threshold': 0, 'value': -5000.0, 'row': {'account': 'ACC1'},
        }
        row = _v2_match_to_alertrow(match)
        assert row['kind'] == 'negative_margin'
        assert row['section'] == 'Funds'
        assert row['scope'] == 'ACC1'
        assert row['pnl'] == -5000.0, "pnl must come from the real fetched value"
        assert row['threshold'] == '-₹0'

    def test_genuine_negative_margin_breach_golden_render(self):
        """Byte-for-byte golden render of the real breach through the exact
        Telegram/email body builders — proves the fix does not touch this
        alert's operator-visible text at all."""
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        from backend.shared.helpers.alert_utils import _tg_alert_body, _email_alert_body
        match = {
            'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '<',
            'threshold': 0, 'value': -5000.0, 'row': {'account': 'ACC1'},
        }
        row = _v2_match_to_alertrow(match)
        tg_body = _tg_alert_body([row])
        assert tg_body == "▸ FND ACC1  -₹5K\n  Margin < 0  -₹0"
        email_html = _email_alert_body([row])
        assert "Margin &lt; 0" in email_html or "Margin < 0" in email_html
        assert "-₹0" in email_html
        assert "999,999,999" not in email_html

    def test_market_open_nse_trick_leaf_not_rendered_as_margin_breach(self):
        """The false-positive from the operator report: market-open-nse's
        always-true leaf must not render as 'Margin < 0 -₹999,999,999'."""
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        match = {
            'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '>=',
            'threshold': -999999999, 'value': 125000.5, 'row': {'account': 'ACC1'},
        }
        row = _v2_match_to_alertrow(match)
        assert row['kind'] != 'negative_margin'
        assert row['pnl'] == 125000.5, "real per-account margin must still surface"
        assert row['threshold'] == 'n/a'
        assert '999,999,999' not in str(row['threshold'])

    def test_expiry_mcx_risk_alert_sentinel_leaf_no_longer_shows_fabricated_number(self):
        """expiry-mcx-risk-alert also uses the -999999999 'always true'
        sentinel (pnl >= -999999999) — the format guard (fix #3) applies
        here too, independent of the kind-classification fix (its kind was
        already 'static_abs' and remains so)."""
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        match = {
            'metric': 'pnl', 'scope': 'positions.expiring_today.mcx_unhedged',
            'op': '>=', 'threshold': -999999999, 'value': -1200.0,
            'row': {'account': 'ACC1', 'pnl': -1200.0},
        }
        row = _v2_match_to_alertrow(match)
        assert row['kind'] == 'static_abs'
        assert row['threshold'] == 'n/a'
        assert '999,999,999' not in str(row['threshold'])

    def test_multiple_accounts_no_longer_show_identical_fabricated_figure(self):
        """The exact operator complaint: every account showed the IDENTICAL
        '-₹999,999,999' line. After the fix each account's row carries its
        own real value and a neutral (non-fabricated) threshold."""
        from backend.api.algo.agent_engine import _v2_match_to_alertrow
        matches = [
            {'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '>=',
             'threshold': -999999999, 'value': 50000.0, 'row': {'account': 'ACC1'}},
            {'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '>=',
             'threshold': -999999999, 'value': 200000.0, 'row': {'account': 'ACC2'}},
        ]
        rows = [_v2_match_to_alertrow(m) for m in matches]
        pnls = [r['pnl'] for r in rows]
        assert pnls == [50000.0, 200000.0], "each account must show its own real value"
        assert all(r['threshold'] == 'n/a' for r in rows)
        assert all(r['kind'] != 'negative_margin' for r in rows)


def _rich_extra(mock_logger) -> dict:
    for c in mock_logger.info.call_args_list:
        extra = c.kwargs.get("extra") or {}
        if extra.get("alert_event") == "rich_alert":
            return {"tg_table": extra["tg_table"], "email_table_html": extra["email_table_html"]}
    return {}


class TestSendRichAlertScheduledAgentClarity:
    """_v2_send_rich_alert renders a plain, clear informational line for
    schedule-only info/low tier agents instead of the kind/threshold table
    — addresses the operator's follow-up complaint: 'it should clearly
    tell what the alert is about. it is not clear from the alert.'"""

    @pytest.mark.asyncio
    async def test_scheduled_info_agent_renders_name_and_schedule_not_margin_table(self):
        from backend.api.algo.agent_engine import _v2_send_rich_alert
        agent = MagicMock()
        agent.slug = "market-open-nse"
        agent.name = "NSE market open"
        agent.fire_at_time = "09:15"
        agent.tier = "info"
        agent.actions = []
        matches = [{
            'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '>=',
            'threshold': -999999999, 'value': 125000.5, 'row': {'account': 'ACC1'},
        }]
        now = datetime.now(timezone.utc)
        captured = {}


        with patch('backend.api.algo.agent_engine.logger') as mock_log:
            sent = await _v2_send_rich_alert(agent, matches, now, sim_mode=False)
        captured.update(_rich_extra(mock_log))

        assert sent is True
        assert captured['tg_table'] == "NSE market open — Scheduled — 09:15 IST"
        assert "999,999,999" not in captured['tg_table']
        assert "Margin" not in captured['tg_table']
        assert "NSE market open" in captured['email_table_html']
        assert "999,999,999" not in captured['email_table_html']

    @pytest.mark.asyncio
    async def test_genuine_critical_agent_still_renders_full_table(self):
        """CRITICAL regression guard: a real (non-scheduled-info) alert like
        loss-funds-negative must still render the full kind/threshold table,
        completely untouched by the scheduled-agent clarity fix."""
        from backend.api.algo.agent_engine import _v2_send_rich_alert
        agent = MagicMock()
        agent.slug = "loss-funds-negative"
        agent.name = "Account funds gone negative (cash or margin)"
        agent.fire_at_time = None
        agent.tier = "critical"
        agent.actions = []
        matches = [{
            'metric': 'avail_margin', 'scope': 'funds.any_acct', 'op': '<',
            'threshold': 0, 'value': -5000.0, 'row': {'account': 'ACC1'},
        }]
        now = datetime.now(timezone.utc)
        captured = {}


        with patch('backend.api.algo.agent_engine.logger') as mock_log:
            sent = await _v2_send_rich_alert(agent, matches, now, sim_mode=False)
        captured.update(_rich_extra(mock_log))

        assert sent is True
        assert captured['tg_table'] == "▸ FND ACC1  -₹5K\n  Margin < 0  -₹0"
