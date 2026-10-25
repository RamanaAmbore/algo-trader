"""Tests for grammar_tokens/channel_telegram_info.py — `telegram_info`
notify channel."""

from unittest.mock import patch

from backend.api.algo.grammar_tokens.channel_telegram_info import _send_telegram_info_html


def test_sends_via_the_dedicated_info_key():
    with patch("backend.shared.helpers.alert_utils._send_telegram_info") as mock_send:
        _send_telegram_info_html("Title", "Body")
    mock_send.assert_called_once_with("Title\nBody")


def test_prebuilt_tg_body_wins_over_title_and_body():
    with patch("backend.shared.helpers.alert_utils._send_telegram_info") as mock_send:
        _send_telegram_info_html("Title", "Body", tg="Prebuilt")
    mock_send.assert_called_once_with("Prebuilt")
