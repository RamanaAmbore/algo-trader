"""
Regression tests for the ntfy HTML-tag-stripping fix (commit 27010bd9)
unescaped-input follow-up (2026-09-27 council audit, Bug 2).

`_html_to_plain()` (backend/shared/helpers/alert_utils.py) is only safe for
strings where dynamic content was run through `html.escape()` BEFORE being
embedded — `_HTML_TAG_RE` (`<[^>]+>`) matches from any literal '<' in
UNescaped dynamic content through to the NEXT '>' anywhere later in the
string, silently deleting everything in between (including real alert
text). Three call sites fed `_alert_route()` raw, unescaped dynamic
content:

  1. template_attach.py `_fire_guard_alert` — reason / applies_to /
     template_slug
  2. template_attach.py `_fire_attach_fail_alert` — err_summary
  3. alert_utils.py `_send_order_failure_messages` — error_short

Each is exercised here with a literal '<' with no matching '>' before the
message's own closing `</code>` tag (this codebase's own guard comparison
text, e.g. "qty < lot_size", is exactly this shape) and asserts the FULL
message — including text AFTER the '<' — survives to the ntfy payload,
while Telegram correctly receives the HTML-escaped (`&lt;`) form.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


_CFG_GUARD = {
    'deploy_branch': 'main',
    'alert_routing': {'template_guard': {'telegram': 'ops', 'ntfy': 'urgent', 'email': False}},
}
_CFG_ATTACH_FAIL = {
    'deploy_branch': 'main',
    'alert_routing': {'template_attach_fail': {'telegram': 'ops', 'ntfy': 'urgent', 'email': False}},
}
_CFG_ORDER_FAILURE = {
    'deploy_branch': 'main',
    'alert_routing': {'order_failure': {'telegram': 'ops', 'ntfy': 'urgent', 'email': False}},
}


class TestTemplateGuardAlertEscaping:
    def test_literal_lt_in_reason_does_not_truncate_ntfy_message(self):
        from backend.api.algo.template_attach import _fire_guard_alert

        # This codebase's own guard comparison text (G1/G2 style) — a
        # literal '<' with a trailing operator-action sentence after it.
        reason = "qty < lot_size — Arm exits manually if needed. Fix: rebalance the parent order."

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _CFG_GUARD):

            _fire_guard_alert(
                template_slug="tmpl-1", applies_to="buy_option",
                parent_side="BUY", parent_symbol="NIFTY24SEPFUT",
                parent_account="ZG0790", parent_qty=1,
                parent_fill_price=100.0, parent_order_id=123,
                reason=reason,
            )

        mock_tg.assert_called_once()
        tg_body = mock_tg.call_args[0][0]
        assert "qty &lt; lot_size" in tg_body

        mock_ntfy.assert_called_once()
        ntfy_body = mock_ntfy.call_args[0][1]
        assert "qty < lot_size" in ntfy_body
        assert "Arm exits manually if needed" in ntfy_body, (
            "text after the unescaped '<' must survive — this is exactly "
            "the text the regex would have deleted pre-fix"
        )
        assert "Fix: rebalance the parent order." in ntfy_body
        # Never leak literal tag syntax into the plain-text ntfy body.
        assert "<code>" not in ntfy_body and "</code>" not in ntfy_body

    def test_literal_lt_in_applies_to_and_template_slug_preserved(self):
        from backend.api.algo.template_attach import _fire_guard_alert

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _CFG_GUARD):

            _fire_guard_alert(
                template_slug="tmpl<1", applies_to="buy<sell",
                parent_side="BUY", parent_symbol="NIFTY24SEPFUT",
                parent_account="ZG0790", parent_qty=1,
                parent_fill_price=100.0, parent_order_id=123,
                reason="normal reason text",
            )

        ntfy_body = mock_ntfy.call_args[0][1]
        assert "tmpl<1" in ntfy_body
        assert "buy<sell" in ntfy_body
        assert "Arm exits manually if needed." in ntfy_body

    def test_none_template_slug_does_not_crash(self):
        """`template.get("slug")` is `None` (not the dict-default) when a
        DB row has slug=NULL — the escaping fix must not turn that into
        an AttributeError on this fire-and-forget, must-never-block path."""
        from backend.api.algo.template_attach import _fire_guard_alert

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _CFG_GUARD):

            _fire_guard_alert(
                template_slug=None, applies_to="buy_option",
                parent_side="BUY", parent_symbol="NIFTY24SEPFUT",
                parent_account="ZG0790", parent_qty=1,
                parent_fill_price=100.0, parent_order_id=123,
                reason="normal reason text",
            )

        mock_tg.assert_called_once()
        mock_ntfy.assert_called_once()
        assert "None" in mock_ntfy.call_args[0][1]


class TestTemplateAttachFailAlertEscaping:
    def test_literal_lt_in_err_summary_does_not_truncate_ntfy_message(self):
        from backend.api.algo.template_attach import _fire_attach_fail_alert

        err = "G1 guard: qty < lot_size — Arm exits manually if needed."

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _CFG_ATTACH_FAIL):

            _fire_attach_fail_alert(
                order_id=456, symbol="NIFTY24SEPFUT", account="ZG0790",
                errors=[err],
            )

        mock_tg.assert_called_once()
        tg_body = mock_tg.call_args[0][0]
        assert "qty &lt; lot_size" in tg_body

        mock_ntfy.assert_called_once()
        ntfy_body = mock_ntfy.call_args[0][1]
        assert "qty < lot_size" in ntfy_body
        assert "Arm exits manually if needed." in ntfy_body
        assert "<code>" not in ntfy_body and "</code>" not in ntfy_body


class TestOrderFailureAlertEscaping:
    def test_literal_lt_in_error_short_does_not_truncate_ntfy_message(self):
        from backend.shared.helpers.alert_utils import _send_order_failure_messages

        error = "Rejected: qty < lot_size. Please retry with a valid multiple."

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _CFG_ORDER_FAILURE), \
             patch('backend.shared.helpers.alert_utils.get_alert_recipients', return_value=[]):

            _send_order_failure_messages(
                masked="ZG####", symbol="NIFTY24SEPFUT", exchange="NFO",
                side="BUY", qty=1, mode="LIVE", source="ticket",
                error=error, suppressed_count=0, ist_disp="14:22 IST",
            )

        mock_tg.assert_called_once()
        tg_body = mock_tg.call_args[0][0]
        assert "qty &lt; lot_size" in tg_body

        mock_ntfy.assert_called_once()
        ntfy_body = mock_ntfy.call_args[0][1]
        assert "qty < lot_size" in ntfy_body
        assert "Please retry with a valid multiple." in ntfy_body, (
            "text after the unescaped '<' must survive to ntfy"
        )
        assert "<code>" not in ntfy_body and "</code>" not in ntfy_body

    def test_error_short_still_truncated_to_160_chars_before_escaping(self):
        """Regression guard — the 160-char truncation contract (pre-existing)
        must survive the escaping fix unchanged."""
        from backend.shared.helpers.alert_utils import _send_order_failure_messages

        error = "X" * 300

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert'), \
             patch('backend.shared.helpers.alert_utils.config', _CFG_ORDER_FAILURE), \
             patch('backend.shared.helpers.alert_utils.get_alert_recipients', return_value=[]):

            _send_order_failure_messages(
                masked="ZG####", symbol="NIFTY24SEPFUT", exchange="NFO",
                side="BUY", qty=1, mode="LIVE", source="ticket",
                error=error, suppressed_count=0, ist_disp="14:22 IST",
            )

        tg_body = mock_tg.call_args[0][0]
        assert "X" * 160 in tg_body
        assert "X" * 161 not in tg_body


class TestDispatchStillCorrectRegression:
    """The one already-correct call site (_dispatch's tg_table) must be
    unaffected by this fix — regression guard mirroring the existing
    TestAlertRouteNtfyPlainText suite."""

    def test_dispatch_alert_still_escapes_correctly(self):
        _cfg = {
            'deploy_branch': 'main',
            'alert_routing': {'agent_alert': {'telegram': 'ops', 'ntfy': 'urgent', 'email': False}},
        }
        tg_table = "COND: pnl < -5000\nACCT   SYMBOL   PNL\nacct1  NIFTY    -5230.50"

        with patch('backend.shared.helpers.alert_utils._send_telegram') as mock_tg, \
             patch('backend.shared.helpers.alert_utils.send_ntfy_alert') as mock_ntfy, \
             patch('backend.shared.helpers.alert_utils.config', _cfg):

            from backend.shared.helpers.alert_utils import _dispatch

            _dispatch('alert', '14:22 IST', tg_table, '<html>email</html>', 'Loss threshold hit')

            ntfy_body = mock_ntfy.call_args[0][1]
            assert "pnl < -5000" in ntfy_body
            assert "-5230.50" in ntfy_body
