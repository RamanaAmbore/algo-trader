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


def _dispatch_captured(agent_key: str, extra: dict) -> dict:
    """Run one stored record through the event path and capture what each channel receives."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from backend.api.algo import event_agents

    sent: dict = {}
    saved_channels = dict(event_agents.CHANNELS)
    saved_enabled = event_agents._channel_enabled
    event_agents.CHANNELS["telegram"] = ("telegram", lambda t, b, tg=None, **k: sent.__setitem__("tg", tg))
    event_agents.CHANNELS["ntfy"] = ("ntfy", lambda t, b, tg=None, priority=None: sent.__setitem__("ntfy", b))
    event_agents._channel_enabled = lambda cap: True
    try:
        spec = getattr(event_agents, agent_key)
        agent = SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})
        rec = {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "WARNING",
               "logger": "backend.api.algo.template_attach", "message": "x",
               "tags": ["warning", "orders"], "extra": {"tags": ["orders"], **extra}}
        asyncio.run(event_agents.dispatch([rec], [agent]))
    finally:
        event_agents.CHANNELS.clear()
        event_agents.CHANNELS.update(saved_channels)
        event_agents._channel_enabled = saved_enabled
    return sent


_IST_LABEL = "Tue, Oct 06 2026, 14:30 IST"


class TestTemplateGuardAlertEscaping:
    def _guard(self, **over):
        extra = {"event": "template_guard", "template_slug": "tmpl-1", "applies_to": "buy_option",
                 "parent_side": "BUY", "parent_symbol": "NIFTY24SEPFUT", "parent_account": "ZG0790",
                 "parent_qty": 1, "parent_fill_price": 100.0, "parent_order_id": 123,
                 "reason": "normal reason text", "ist_label": _IST_LABEL}
        extra.update(over)
        return _dispatch_captured("TEMPLATE_GUARD_AGENT", extra)

    def test_literal_lt_in_reason_does_not_truncate_ntfy_message(self):
        reason = "qty < lot_size — Arm exits manually if needed. Fix: rebalance the parent order."
        sent = self._guard(reason=reason)
        assert "qty &lt; lot_size" in sent["tg"]
        ntfy_body = sent["ntfy"]
        assert "qty < lot_size" in ntfy_body
        assert "Arm exits manually if needed" in ntfy_body, (
            "text after the unescaped '<' must survive — this is exactly "
            "the text the regex would have deleted pre-fix"
        )
        assert "Fix: rebalance the parent order." in ntfy_body
        assert "<code>" not in ntfy_body and "</code>" not in ntfy_body

    def test_literal_lt_in_applies_to_and_template_slug_preserved(self):
        sent = self._guard(template_slug="tmpl<1", applies_to="buy<sell")
        assert "tmpl<1" in sent["ntfy"]
        assert "buy<sell" in sent["ntfy"]
        assert "Arm exits manually if needed." in sent["ntfy"]

    def test_none_template_slug_does_not_crash(self):
        """`template.get("slug")` is `None` when a DB row has slug=NULL — the
        escaping must not turn that into an AttributeError on this path."""
        sent = self._guard(template_slug=None)
        assert "None" in sent["ntfy"]


class TestTemplateAttachFailAlertEscaping:
    def test_literal_lt_in_err_summary_does_not_truncate_ntfy_message(self):
        err = "G1 guard: qty < lot_size — Arm exits manually if needed."
        sent = _dispatch_captured("TEMPLATE_ATTACH_FAIL_AGENT", {
            "event": "template_attach_fail", "order_id": 456, "symbol": "NIFTY24SEPFUT",
            "account": "ZG0790", "err_summary": err, "ist_label": _IST_LABEL})
        assert "qty &lt; lot_size" in sent["tg"]
        assert "qty < lot_size" in sent["ntfy"]
        assert "Arm exits manually if needed." in sent["ntfy"]
        assert "<code>" not in sent["ntfy"]

    def test_error_short_still_truncated_to_160_chars_before_escaping(self):
        """Regression guard — the 160-char truncation contract (pre-existing)
        must survive the escaping fix unchanged."""
        from backend.shared.helpers.alert_utils import order_failure_messages

        error = "X" * 300

        with patch('backend.shared.helpers.alert_utils.config', _CFG_ORDER_FAILURE):
            tg_body, _subject, _html = order_failure_messages(
                masked="ZG####", symbol="NIFTY24SEPFUT", exchange="NFO",
                side="BUY", qty=1, mode="LIVE", source="ticket",
                error=error, suppressed_count=0, ist_disp="14:22 IST",
            )

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
