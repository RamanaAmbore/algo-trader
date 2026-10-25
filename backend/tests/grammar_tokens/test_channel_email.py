"""Tests for grammar_tokens/channel_email.py — `email` notify channel."""

from unittest.mock import patch

from backend.api.algo.grammar_tokens.channel_email import _send_email_channel


def test_noop_when_no_email_tuple():
    with patch("backend.shared.helpers.mail_utils.send_email") as mock_send:
        _send_email_channel("Title", "Body", email=None)
    mock_send.assert_not_called()


def test_sends_to_every_resolved_recipient():
    with patch("backend.api.algo.event_agents.resolve_email_recipients",
               return_value=["a@x.com", "b@y.com"]), \
         patch("backend.shared.helpers.mail_utils.send_email") as mock_send:
        _send_email_channel("Title", "Body", email=("Subject", "<p>html</p>"))
    assert mock_send.call_count == 2
    mock_send.assert_any_call("RamboQuant", "a@x.com", "Subject", "<p>html</p>")
    mock_send.assert_any_call("RamboQuant", "b@y.com", "Subject", "<p>html</p>")


def test_a_single_recipient_failure_does_not_block_the_others():
    with patch("backend.api.algo.event_agents.resolve_email_recipients",
               return_value=["bad@x.com", "good@y.com"]), \
         patch("backend.shared.helpers.mail_utils.send_email",
               side_effect=[RuntimeError("smtp down"), None]) as mock_send:
        _send_email_channel("Title", "Body", email=("Subject", "<p>html</p>"))
    assert mock_send.call_count == 2
