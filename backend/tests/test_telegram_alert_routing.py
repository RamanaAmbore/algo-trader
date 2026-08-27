"""
Tests for _send_telegram_info() Telegram channel routing fix.

Verifies that market summary alerts (market_open, market_close, visitor_report)
land in the RamboQuant alerts channel (telegram_chat_id_ramboquant) and NOT in
the deploy channel (telegram_chat_id_deploy).

Covers:
1. Primary ramboquant key is used when present
2. Fallback to telegram_chat_id when ramboquant key is absent
3. Deploy key is ignored — does not substitute for the ramboquant key
"""

import pytest
from unittest.mock import patch, MagicMock


class TestSendTelegramInfoUsesRamboquantKey:
    """Primary key must be telegram_chat_id_ramboquant, not telegram_chat_id_deploy."""

    def test_send_telegram_info_uses_ramboquant_key(self):
        """When both ramboquant and default keys are present, ramboquant wins."""
        fake_secrets = {
            'telegram_bot_token_ramboquant': 'token-rq',
            'telegram_chat_id_ramboquant':   'chat-rq-123',
            'telegram_bot_token':            'token-default',
            'telegram_chat_id':              'chat-default-456',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("test message")

            mock_requests.post.assert_called_once()
            call_kwargs = mock_requests.post.call_args
            # json body is the second positional arg or 'json' kwarg
            body = call_kwargs[1].get('json') or call_kwargs.kwargs.get('json', {})
            assert body.get('chat_id') == 'chat-rq-123', (
                f"Expected chat_id=chat-rq-123 (ramboquant key), got {body.get('chat_id')}"
            )

    def test_send_telegram_info_token_uses_ramboquant_key(self):
        """When ramboquant token is present, it is used in the API URL."""
        fake_secrets = {
            'telegram_bot_token_ramboquant': 'token-rq-999',
            'telegram_chat_id_ramboquant':   'chat-rq-123',
            'telegram_bot_token':            'token-default',
            'telegram_chat_id':              'chat-default-456',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("test token routing")

            mock_requests.post.assert_called_once()
            call_url = mock_requests.post.call_args[0][0]
            assert 'token-rq-999' in call_url, (
                f"Expected ramboquant token in URL, got: {call_url}"
            )


class TestSendTelegramInfoFallback:
    """Fallback to telegram_chat_id when ramboquant-specific key is absent."""

    def test_send_telegram_info_fallback_to_default(self):
        """When no ramboquant key, falls back to telegram_chat_id."""
        fake_secrets = {
            'telegram_bot_token': 'token-default',
            'telegram_chat_id':   'chat-default-789',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("fallback test")

            mock_requests.post.assert_called_once()
            body = mock_requests.post.call_args[1].get('json', {})
            assert body.get('chat_id') == 'chat-default-789', (
                f"Expected fallback chat_id=chat-default-789, got {body.get('chat_id')}"
            )

    def test_send_telegram_info_fallback_token_uses_default(self):
        """When no ramboquant token, falls back to telegram_bot_token."""
        fake_secrets = {
            'telegram_bot_token': 'token-default-only',
            'telegram_chat_id':   'chat-default-789',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("token fallback test")

            call_url = mock_requests.post.call_args[0][0]
            assert 'token-default-only' in call_url, (
                f"Expected default token in URL, got: {call_url}"
            )


class TestSendTelegramInfoIgnoresDeployKey:
    """Deploy key (telegram_chat_id_deploy) must NOT be read by _send_telegram_info."""

    def test_send_telegram_info_does_not_use_deploy_key(self):
        """When only deploy key is present (no ramboquant key), fallback fires — deploy key ignored."""
        fake_secrets = {
            'telegram_bot_token_deploy': 'token-deploy',
            'telegram_chat_id_deploy':   'chat-deploy-999',
            'telegram_bot_token':        'token-default',
            'telegram_chat_id':          'chat-default-111',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("deploy key ignored test")

            mock_requests.post.assert_called_once()
            body = mock_requests.post.call_args[1].get('json', {})
            actual_chat_id = body.get('chat_id')

            assert actual_chat_id != 'chat-deploy-999', (
                "Deploy chat_id must NOT be used by _send_telegram_info"
            )
            assert actual_chat_id == 'chat-default-111', (
                f"Expected fallback chat_id=chat-default-111, got {actual_chat_id}"
            )

    def test_send_telegram_info_deploy_token_not_used(self):
        """Deploy bot token must NOT appear in the API URL when only deploy key is present."""
        fake_secrets = {
            'telegram_bot_token_deploy': 'token-deploy-abc',
            'telegram_chat_id_deploy':   'chat-deploy-999',
            'telegram_bot_token':        'token-default-xyz',
            'telegram_chat_id':          'chat-default-111',
        }
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("deploy token not used test")

            call_url = mock_requests.post.call_args[0][0]
            assert 'token-deploy-abc' not in call_url, (
                f"Deploy token must NOT appear in Telegram API URL, got: {call_url}"
            )
            assert 'token-default-xyz' in call_url, (
                f"Default fallback token expected in URL, got: {call_url}"
            )

    def test_send_telegram_info_no_post_when_no_keys(self):
        """When neither ramboquant nor default keys are configured, no HTTP call is made."""
        fake_secrets: dict = {}
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch('backend.shared.helpers.alert_utils.secrets', fake_secrets), \
             patch('backend.shared.helpers.alert_utils.is_enabled', return_value=True), \
             patch('backend.shared.helpers.alert_utils.requests') as mock_requests:

            mock_requests.post.return_value = mock_resp

            from backend.shared.helpers.alert_utils import _send_telegram_info
            _send_telegram_info("no keys configured")

            mock_requests.post.assert_not_called()
