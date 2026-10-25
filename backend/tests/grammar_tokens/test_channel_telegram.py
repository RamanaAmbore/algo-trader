"""Tests for grammar_tokens/channel_telegram.py — `telegram` notify
channel."""

from unittest.mock import patch

from backend.api.algo.grammar_tokens.channel_telegram import _send_telegram_html


def test_sends_prebuilt_tg_body_when_given():
    with patch("backend.shared.helpers.alert_utils._send_telegram") as mock_send:
        _send_telegram_html("Title", "Body", tg="Already-built plain text")
    mock_send.assert_called_once_with("Already-built plain text")


def test_falls_back_to_title_and_body_when_no_tg():
    with patch("backend.shared.helpers.alert_utils._send_telegram") as mock_send:
        _send_telegram_html("Title", "Body")
    mock_send.assert_called_once_with("Title\nBody")
