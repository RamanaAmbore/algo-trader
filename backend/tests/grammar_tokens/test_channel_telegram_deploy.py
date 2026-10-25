"""Tests for grammar_tokens/channel_telegram_deploy.py —
`telegram_deploy` notify channel."""

from unittest.mock import patch

from backend.api.algo.grammar_tokens.channel_telegram_deploy import _send_telegram_deploy_html


def test_uses_dedicated_deploy_secrets_when_set():
    fake_secrets = {
        "telegram_bot_token_deploy": "deploy-token",
        "telegram_chat_id_deploy": "deploy-chat",
        "telegram_bot_token": "general-token",
        "telegram_chat_id": "general-chat",
    }
    with patch("backend.shared.helpers.utils.secrets", fake_secrets), \
         patch("backend.shared.helpers.alert_utils._send_telegram") as mock_send:
        _send_telegram_deploy_html("Title", "Body")
    mock_send.assert_called_once_with("Title\nBody", token="deploy-token", chat_id="deploy-chat")


def test_falls_back_to_general_telegram_secrets_when_deploy_keys_absent():
    fake_secrets = {
        "telegram_bot_token": "general-token",
        "telegram_chat_id": "general-chat",
    }
    with patch("backend.shared.helpers.utils.secrets", fake_secrets), \
         patch("backend.shared.helpers.alert_utils._send_telegram") as mock_send:
        _send_telegram_deploy_html("Title", "Body")
    mock_send.assert_called_once_with("Title\nBody", token="general-token", chat_id="general-chat")
