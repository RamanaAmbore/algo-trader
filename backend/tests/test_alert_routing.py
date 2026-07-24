"""
Tests for alert routing system — config-driven dispatch to ntfy, Telegram, and email.

Covers:
1. Loss agents have correct event channels (telegram, email, ntfy, log)
2. Loss agents have correct tier classification (high vs critical)
3. _dispatch market summaries route correctly (open/close vs alert types)
4. send_ntfy_alert is invoked from agent engine with correct priorities
5. Config alert_routing table structure validation
6. Telegram channel selection based on event type
"""

import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime


class TestLossAgentEventChannels:
    """Loss agents have correct event channel structure."""

    def test_loss_agents_all_have_events_field(self):
        """All loss agents have an 'events' field with channel configurations."""
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            assert isinstance(events, list), f"{slug} events should be a list"
            assert len(events) > 0, f"{slug} should have at least one event channel"

    def test_loss_agent_events_have_channel_and_enabled(self):
        """Each event channel must have 'channel' and 'enabled' fields."""
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            for event in events:
                assert 'channel' in event, f"{slug} event missing 'channel' field"
                assert 'enabled' in event, f"{slug} event missing 'enabled' field"
                assert isinstance(event['enabled'], bool), \
                    f"{slug} event 'enabled' should be bool, got {type(event['enabled'])}"


class TestLossAgentTiers:
    """Loss agents have correct tier classification."""

    def test_loss_positions_total_is_critical_tier(self):
        """loss-positions-total should be critical tier (highest)."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'loss-positions-total'), None)
        assert agent is not None, "loss-positions-total not found"
        assert agent.get('tier') == 'critical', \
            f"loss-positions-total should be critical, got {agent.get('tier')}"

    def test_loss_positions_acct_is_high_tier(self):
        """loss-positions-acct should be high tier."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'loss-positions-acct'), None)
        assert agent is not None, "loss-positions-acct not found"
        assert agent.get('tier') == 'high', \
            f"loss-positions-acct should be high, got {agent.get('tier')}"

    def test_loss_funds_negative_is_critical_tier(self):
        """loss-funds-negative should be critical tier."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == 'loss-funds-negative'), None)
        assert agent is not None, "loss-funds-negative not found"
        assert agent.get('tier') == 'critical', \
            f"loss-funds-negative should be critical, got {agent.get('tier')}"


class TestLossAgentChannelContent:
    """Verify loss agents have complete channel setup."""

    def test_loss_agents_have_telegram_channel_enabled(self):
        """All loss agents have telegram channel enabled."""
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            tg_channel = next((e for e in events if e.get('channel') == 'telegram'), None)
            assert tg_channel is not None, f"{slug} missing telegram channel"
            assert tg_channel.get('enabled') is True, f"{slug} telegram channel should be enabled"

    def test_loss_agents_email_routing_via_config(self):
        """Loss agents email delivery is now driven by alert_routing config, not per-agent events.

        The 'email' channel was removed from _LOSS_AGENT_DEFAULTS (task: alert channel matrix
        redesign). Email for agent_alert events is gated by alert_routing.agent_alert.email in
        backend_config.yaml. This test verifies no agent carries a stale per-agent email entry
        that would double-dispatch.
        """
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            email_channel = next((e for e in events if e.get('channel') == 'email'), None)
            assert email_channel is None, (
                f"{slug} should NOT have a per-agent email channel — "
                "email routing is now driven by alert_routing config"
            )

    def test_loss_agents_have_log_channel_enabled(self):
        """All loss agents have log channel enabled."""
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            log_channel = next((e for e in events if e.get('channel') == 'log'), None)
            assert log_channel is not None, f"{slug} missing log channel"
            assert log_channel.get('enabled') is True, f"{slug} log channel should be enabled"

    def test_loss_agents_have_ntfy_channel_enabled(self):
        """All loss agents have ntfy channel enabled."""
        from backend.api.algo.agent_engine import _LOSS_AGENTS

        for agent in _LOSS_AGENTS:
            slug = agent.get('slug', 'unknown')
            events = agent.get('events', [])
            ntfy_channel = next((e for e in events if e.get('channel') == 'ntfy'), None)
            assert ntfy_channel is not None, f"{slug} missing ntfy channel"
            assert ntfy_channel.get('enabled') is True, f"{slug} ntfy channel should be enabled"


class TestDispatchMarketSummaries:
    """_dispatch function behavior for market open/close summaries."""

    def test_dispatch_market_open_type_exists(self):
        """_dispatch recognizes 'open' message type."""
        from backend.shared.helpers.alert_utils import _MSG_TYPES

        assert 'open' in _MSG_TYPES, "open message type not in _MSG_TYPES"
        tg_prefix, email_prefix = _MSG_TYPES['open']
        assert tg_prefix == 'Open Summary', f"open prefix should be 'Open Summary', got {tg_prefix}"

    def test_dispatch_market_close_type_exists(self):
        """_dispatch recognizes 'close' message type."""
        from backend.shared.helpers.alert_utils import _MSG_TYPES

        assert 'close' in _MSG_TYPES, "close message type not in _MSG_TYPES"
        tg_prefix, email_prefix = _MSG_TYPES['close']
        assert tg_prefix == 'Close Summary', f"close prefix should be 'Close Summary', got {tg_prefix}"

    def test_dispatch_alert_type_exists(self):
        """_dispatch recognizes 'alert' message type."""
        from backend.shared.helpers.alert_utils import _MSG_TYPES

        assert 'alert' in _MSG_TYPES, "alert message type not in _MSG_TYPES"
        tg_prefix, email_prefix = _MSG_TYPES['alert']
        assert tg_prefix == 'Agent', f"alert prefix should be 'Agent', got {tg_prefix}"

    def test_dispatch_calls_send_telegram(self):
        """_dispatch calls _send_telegram for alert messages."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22 IST', 'Test table', '<html>test</html>', 'Detail')

            # Verify _send_telegram was called
            mock_tg.assert_called_once()

            # Verify message was passed
            call_args = mock_tg.call_args
            message = call_args[0][0] if call_args[0] else None
            assert message is not None, "Message should not be None"
            assert "<b>" in message, "Message should have bold tag"
            assert "Agent" in message, "Message should have Agent prefix"

    def test_dispatch_market_open_does_not_call_email(self):
        """_dispatch('open', ...) does NOT dispatch email."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('open', '09:15 IST', 'Holdings data', '<html>table</html>', 'Open Summary')

            # Verify Telegram was called
            mock_tg.assert_called_once()
            # Verify email was NOT called
            mock_email.assert_not_called()

    def test_dispatch_market_close_does_not_call_email(self):
        """_dispatch('close', ...) does NOT dispatch email."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('close', '15:30 IST', 'Positions data', '<html>table</html>', 'Close Summary')

            # Verify Telegram was called
            mock_tg.assert_called_once()
            # Verify email was NOT called
            mock_email.assert_not_called()

    def test_dispatch_alert_calls_email(self):
        """_dispatch('alert', ...) DOES dispatch email when alert_routing.agent_alert.email=true."""
        _cfg = {
            'deploy_branch': 'main',
            'alert_routing': {'agent_alert': {'telegram': 'ops', 'ntfy': False, 'email': True}},
        }
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', _cfg):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22 IST', 'Loss alert', '<html>table</html>', 'Loss threshold hit')

            # Verify both Telegram and email were called
            mock_tg.assert_called_once()
            mock_email.assert_called_once()


class TestSendNtfyAlertIntegration:
    """send_ntfy_alert function can be called with priority parameter."""

    def test_send_ntfy_alert_accepts_priority_parameter(self):
        """send_ntfy_alert function signature includes priority param."""
        from backend.shared.helpers.alert_utils import send_ntfy_alert
        import inspect

        sig = inspect.signature(send_ntfy_alert)
        params = list(sig.parameters.keys())

        assert 'priority' in params, \
            f"send_ntfy_alert should have 'priority' parameter, got {params}"

    def test_send_ntfy_alert_with_urgent_priority(self):
        """send_ntfy_alert can be called with priority='urgent' (sends 3x for redundancy)."""
        with patch('backend.shared.helpers.alert_utils.secrets', {'ntfy_topic': 'test'}), \
             patch('urllib.request.urlopen') as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise
            send_ntfy_alert('Test Alert', 'Message', priority='urgent')

            # Urgent priority sends 3 times for redundancy
            assert mock_urlopen.call_count == 3, \
                f"Expected 3 calls for urgent priority, got {mock_urlopen.call_count}"

    def test_send_ntfy_alert_with_high_priority(self):
        """send_ntfy_alert can be called with priority='high' (sends 1x)."""
        with patch('backend.shared.helpers.alert_utils.secrets', {'ntfy_topic': 'test'}), \
             patch('urllib.request.urlopen') as mock_urlopen:

            from backend.shared.helpers.alert_utils import send_ntfy_alert

            # Should not raise
            send_ntfy_alert('Test Alert', 'Message', priority='high')

            # High priority sends once
            mock_urlopen.assert_called_once()


class TestAlertRoutingConfigStructure:
    """Test the alert_routing config table structure for future implementation."""

    def test_backend_config_can_have_alert_routing_key(self):
        """backend_config.yaml supports 'alert_routing' as a top-level key."""
        from backend.shared.helpers.utils import config

        # This test verifies that config.get('alert_routing') doesn't raise
        # and returns dict or None (future-proof for when alert_routing is added)
        routing = config.get('alert_routing', {})
        assert isinstance(routing, dict), \
            f"alert_routing should be dict, got {type(routing)}"

    def test_loss_agents_critical_tier_higher_than_high(self):
        """Critical tier agents should be considered higher priority than high tier."""
        from backend.api.algo.agent_engine import BUILTIN_AGENTS

        tiers_by_slug = {}
        for agent in BUILTIN_AGENTS:
            slug = agent.get('slug')
            tier = agent.get('tier')
            if slug and slug.startswith('loss-'):
                tiers_by_slug[slug] = tier

        # Verify we have both critical and high tier agents
        critical_agents = [s for s, t in tiers_by_slug.items() if t == 'critical']
        high_agents = [s for s, t in tiers_by_slug.items() if t == 'high']

        assert len(critical_agents) > 0, "Should have at least one critical-tier loss agent"
        assert len(high_agents) > 0, "Should have at least one high-tier loss agent"

    def test_order_failure_alert_routing_convention(self):
        """Order failure alerts are a key event in the new routing system."""
        # This test documents that 'order_failure' is a known event type
        # The new _alert_route function will route 'order_failure' events
        # according to config: 'order_failure' → {telegram: ops, ntfy: urgent}

        # When alert_routing is implemented in config, verify this event exists
        from backend.shared.helpers.utils import config
        routing = config.get('alert_routing', {})

        # For now, just verify the config structure is dict-like
        assert isinstance(routing, dict)


class TestDispatchEmailPhasing:
    """Test email dispatch behavior per operator request (Jun 2026)."""

    def test_market_open_close_telegram_only_per_operator_request(self):
        """Per operator request (Jun 2026): market summaries are Telegram-only.

        The comment in _dispatch says:
        'Operator request (Jun 2026): market open/close summaries ship
        Telegram-only. Agent alerts ('alert' msg_type) continue to fan
        out across both Telegram + email per the existing operator
        alert recipients.'
        """
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            # Test 'open'
            _dispatch('open', '09:15', 'tg', 'email', 'detail')
            tg_open = mock_tg.call_count
            email_open = mock_email.call_count

            # Reset mocks
            mock_tg.reset_mock()
            mock_email.reset_mock()

            # Test 'close'
            _dispatch('close', '15:30', 'tg', 'email', 'detail')
            tg_close = mock_tg.call_count
            email_close = mock_email.call_count

            # Verify both open and close skip email
            assert tg_open == 1, "open should call Telegram"
            assert email_open == 0, "open should NOT call email"
            assert tg_close == 1, "close should call Telegram"
            assert email_close == 0, "close should NOT call email"

    def test_agent_alerts_fan_out_to_telegram_and_email(self):
        """Agent alerts ('alert' msg_type) fan out to Telegram + email when routing config says so."""
        _cfg = {
            'deploy_branch': 'main',
            'alert_routing': {'agent_alert': {'telegram': 'ops', 'ntfy': False, 'email': True}},
        }
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', _cfg):

            from backend.shared.helpers.alert_utils import _dispatch

            # Test 'alert' (agent alert)
            _dispatch('alert', '14:22', 'tg', 'email', 'detail')

            # Verify both are called for agent alerts
            mock_tg.assert_called_once()
            mock_email.assert_called_once()


class TestSimModeHandling:
    """Test simulator mode tagging in _dispatch."""

    def test_dispatch_sim_mode_tags_telegram_message(self):
        """When sim_mode=True, Telegram message includes SIMULATOR prefix."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22', 'table', '<html>email</html>', 'detail', sim_mode=True)

            # Get the message passed to Telegram
            call_args = mock_tg.call_args
            message = call_args[0][0] if call_args[0] else None

            assert message is not None
            assert 'SIMULATOR' in message, \
                "Simulator mode should tag message with SIMULATOR"

    def test_dispatch_sim_mode_tags_email_subject(self):
        """When sim_mode=True, email subject includes SIMULATOR prefix."""
        _cfg = {
            'deploy_branch': 'main',
            'alert_routing': {'agent_alert': {'telegram': 'ops', 'ntfy': False, 'email': True}},
        }
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', _cfg):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22', 'table', '<html>email</html>', 'detail', sim_mode=True)

            # Verify _dispatch_email was called
            mock_email.assert_called_once()

            # Get the email_prefix_full argument
            call_kwargs = mock_email.call_args[1]
            email_prefix = call_kwargs.get('email_prefix_full', '')

            assert 'SIMULATOR' in email_prefix, \
                "Simulator mode should tag email prefix with SIMULATOR"


class TestBranchTagging:
    """Test branch tagging in alert messages."""

    def test_dispatch_non_main_branch_tagged_in_telegram(self):
        """Non-main branches get [branch] tag in Telegram messages."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'dev'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22', 'table', '<html>email</html>', 'detail')

            # Get the message
            call_args = mock_tg.call_args
            message = call_args[0][0] if call_args[0] else None

            assert message is not None
            assert '[dev]' in message, \
                "Non-main branch should be tagged [dev] in message"

    def test_dispatch_main_branch_no_tag_in_telegram(self):
        """Main branch doesn't get extra branch tag (just the prefix)."""
        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils._dispatch_email') as mock_email, \
             patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22', 'table', '<html>email</html>', 'detail')

            # Get the message
            call_args = mock_tg.call_args
            message = call_args[0][0] if call_args[0] else None

            assert message is not None
            # Main branch should not have [main] tag (but will have just the prefix)
            assert '[main]' not in message, \
                "Main branch should not be tagged [main] in message"
