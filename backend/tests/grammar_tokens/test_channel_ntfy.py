"""Tests for grammar_tokens/channel_ntfy.py — `ntfy` notify channel."""

from unittest.mock import patch

from backend.api.algo.grammar_tokens.channel_ntfy import _send_ntfy


def test_calls_send_ntfy_alert_with_title_body_priority():
    with patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_send:
        _send_ntfy("Loss alert", "cash < 0", priority="urgent")
    mock_send.assert_called_once_with("Loss alert", "cash < 0", priority="urgent")


def test_tg_param_is_accepted_but_ignored():
    """`tg` exists for call-signature parity with the other channel
    senders — `_send_ntfy` never uses it (ntfy has no HTML/plain-text
    body distinction)."""
    with patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_send:
        _send_ntfy("T", "B", tg="irrelevant")
    mock_send.assert_called_once_with("T", "B", priority=None)
