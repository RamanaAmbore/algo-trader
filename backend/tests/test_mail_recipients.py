"""Tests for the recipient-handling fix in `mail_utils.send_email`.

The original send_email had two bugs:
  1. Cc header set to the brand address, but the SMTP envelope only
     carried the To recipient → header/envelope mismatch caused some
     SMTP servers (Hostinger when a list group was involved) to reject.
  2. `email_id` was passed straight through to `smtplib.sendmail` as
     a string. A comma-separated input from misconfigured yaml went
     out as one malformed address.

Slice-Q (this fix):
  - Brand copy now goes via Bcc, included in the envelope so it's
    actually delivered.
  - email_id accepts string OR list; comma-separated strings are split.
  - De-dup case-insensitive so the brand isn't double-delivered when
    the recipient list already contains it.
  - Opt-out via secrets.mail_skip_bcc_brand.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.shared.helpers.mail_utils import (
    DEFAULT_MAIL_FROM,
    _normalise_recipients,
    send_email,
)


# ---------------------------------------------------------------------------
# _normalise_recipients — pure helper, no IO
# ---------------------------------------------------------------------------

class TestNormaliseRecipients:
    def test_single_string(self):
        assert _normalise_recipients("a@x.com") == ["a@x.com"]

    def test_comma_separated_string(self):
        assert _normalise_recipients("a@x.com, b@x.com") == ["a@x.com", "b@x.com"]

    def test_list_of_strings(self):
        assert _normalise_recipients(["a@x.com", "b@x.com"]) == ["a@x.com", "b@x.com"]

    def test_list_with_inner_commas(self):
        # Defensive: operator might mix list + comma-string forms.
        out = _normalise_recipients(["a@x.com, b@x.com", "c@x.com"])
        assert out == ["a@x.com", "b@x.com", "c@x.com"]

    def test_empty_inputs(self):
        assert _normalise_recipients("") == []
        assert _normalise_recipients(None) == []
        assert _normalise_recipients([]) == []

    def test_dedup_case_insensitive(self):
        # Casing of FIRST occurrence preserved; subsequent dups dropped.
        assert _normalise_recipients(["A@X.com", "a@x.com"]) == ["A@X.com"]

    def test_trailing_comma(self):
        assert _normalise_recipients("a@x.com, b@x.com,") == ["a@x.com", "b@x.com"]

    def test_whitespace_stripped(self):
        assert _normalise_recipients("  a@x.com  ,  b@x.com  ") == [
            "a@x.com", "b@x.com",
        ]

    def test_extra_whitespace_in_list(self):
        assert _normalise_recipients(["  a@x.com  ", " b@x.com "]) == [
            "a@x.com", "b@x.com",
        ]


# ---------------------------------------------------------------------------
# send_email — envelope + headers
# ---------------------------------------------------------------------------

_BASE_SECRETS = {
    "smtp_server":    "smtp.test",
    "smtp_port":      587,
    "smtp_user":      "auth@test.com",
    "smtp_pass":      "pw",
    "smtp_user_name": "Test Auth",
    "mail_from":      "brand@test.com",
    "mail_from_name": "Brand",
}


def _mock_smtp_context():
    """Return (cls, ctx, server) — a chain to patch _IPv4SMTP with.
    server is the inner mock with .sendmail / .login / .starttls."""
    server = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=server)
    ctx.__exit__ = MagicMock(return_value=False)
    cls = MagicMock(return_value=ctx)
    return cls, ctx, server


class TestSendEmailEnvelope:
    def _patches(self, secrets=None):
        """Common patch set used by every test in this class."""
        return [
            patch("backend.shared.helpers.mail_utils.secrets",
                  {**_BASE_SECRETS, **(secrets or {})}),
            patch("backend.shared.helpers.mail_utils.is_enabled",
                  return_value=True),
        ]

    def test_single_recipient_with_brand_bcc(self):
        """A single send goes to [recipient, brand] in the envelope; the
        message body has To header only (Bcc stays blind)."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email("Alice", "alice@example.com",
                               "Hello", "<p>Hi</p>")

        assert ok is True
        assert server.sendmail.call_count == 1
        from_addr, to_addrs, body = server.sendmail.call_args.args
        # Envelope sender = authenticated user
        assert from_addr == "auth@test.com"
        # Envelope contains the To + the brand Bcc
        assert set(to_addrs) == {"alice@example.com", "brand@test.com"}
        # Headers: To present, Bcc must NOT be in the message body
        assert "To: Alice <alice@example.com>" in body
        assert "From: Brand <brand@test.com>" in body
        assert "Bcc:" not in body
        assert "Cc:" not in body

    def test_comma_separated_string_input(self):
        """A misconfigured caller passes 'a@x.com, b@x.com' as the
        email_id — should split into two envelope recipients, not one
        malformed entry."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email(None, "a@x.com, b@x.com",
                               "Hello", "<p>Hi</p>")

        assert ok is True
        _, to_addrs, _ = server.sendmail.call_args.args
        assert set(to_addrs) == {"a@x.com", "b@x.com", "brand@test.com"}

    def test_list_input(self):
        """Caller passes a list — envelope carries every address + brand."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email(None, ["x@x.com", "y@x.com", "z@x.com"],
                               "Hello", "<p>Hi</p>")

        assert ok is True
        _, to_addrs, _ = server.sendmail.call_args.args
        assert set(to_addrs) == {"x@x.com", "y@x.com", "z@x.com",
                                  "brand@test.com"}

    def test_no_double_delivery_when_recipient_is_brand(self):
        """If the recipient list already includes the brand address,
        the Bcc copy is suppressed so the brand mailbox doesn't get
        the email twice."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email(None, ["brand@test.com", "other@x.com"],
                               "Hello", "<p>Hi</p>")

        assert ok is True
        _, to_addrs, _ = server.sendmail.call_args.args
        assert set(to_addrs) == {"brand@test.com", "other@x.com"}

    def test_dedup_recipient_matches_brand_case_insensitive(self):
        """Case-insensitive check on the brand-Bcc suppression."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email(None, "BRAND@test.com",
                               "Hello", "<p>Hi</p>")

        assert ok is True
        _, to_addrs, _ = server.sendmail.call_args.args
        assert to_addrs == ["BRAND@test.com"]

    def test_opt_out_via_mail_skip_bcc_brand(self):
        """`mail_skip_bcc_brand: True` in secrets drops the Bcc copy
        entirely — operator delivery is solely the recipient."""
        cls, _ctx, server = _mock_smtp_context()
        ps = self._patches({"mail_skip_bcc_brand": True})
        with ps[0], ps[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email(None, "alice@x.com", "Hello", "<p>Hi</p>")

        assert ok is True
        _, to_addrs, _ = server.sendmail.call_args.args
        assert to_addrs == ["alice@x.com"]

    def test_empty_recipient_returns_error(self):
        """Empty recipient returns (False, error) without touching SMTP."""
        with self._patches()[0], self._patches()[1]:
            ok, msg = send_email("Alice", "", "Hello", "<p>Hi</p>")
        assert ok is False
        assert "no recipient" in msg.lower()

    def test_smtp_recipients_refused_surfaces_addresses(self):
        """When the SMTP server rejects recipients, the error message
        carries which ones so the operator can diagnose."""
        import smtplib
        cls, _ctx, server = _mock_smtp_context()
        server.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
            {"badaddr@x.com": (550, b"User unknown")}
        )
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, msg = send_email(None, "badaddr@x.com",
                                 "Hello", "<p>Hi</p>")
        assert ok is False
        assert "recipients refused" in msg.lower()
        assert "badaddr@x.com" in msg

    def test_multi_recipient_to_header_no_display_name(self):
        """Multi-recipient sends drop the friendly name from the To
        header (it's ambiguous which address it belongs to). The
        envelope still controls actual delivery."""
        cls, _ctx, server = _mock_smtp_context()
        with self._patches()[0], self._patches()[1], \
             patch("backend.shared.helpers.mail_utils._IPv4SMTP", new=cls):
            ok, _ = send_email("Alice", "a@x.com, b@x.com",
                               "Hello", "<p>Hi</p>")
        assert ok is True
        _, _, body = server.sendmail.call_args.args
        # Multi-recipient: To carries the bare addresses, no display name.
        assert "To: a@x.com, b@x.com" in body
        assert "To: Alice" not in body
