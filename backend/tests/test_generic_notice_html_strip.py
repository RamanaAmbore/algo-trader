"""
2026-10-23 fix — raw HTML tags leaking into Telegram/ntfy for `generic_notice`
(`_render_deploy_sync`) alerts, e.g. VISITOR_REPORT_AGENT.

`background.py`'s visitor-report send builds its body as
`f"<b>Visitors</b>\\n{tg_body}"` (genuine HTML, intended for Telegram's
parse_mode=HTML rendering). `_render_deploy_sync` used to return only a
2-tuple `(title, body)` with no `tg` (HTML) slot, so `_send_channel` passed
`tg=None` to `_send_telegram_info_html`/`_send_telegram_html`, whose
`tg or f"<b>{html.escape(title)}</b>\\n{html.escape(body)}"` fallback then
double-escaped the already-literal `<b>` tags into `&lt;b&gt;` — Telegram
displayed the raw tag text instead of bold. ntfy (reads `body` directly,
never `tg`) would show the same raw tags verbatim for any generic_notice
caller that embeds HTML.

Fixed the same way every other HTML-capable renderer in this file already
does: `body` (position 2, ntfy + escape-fallback) = `_html_to_plain(body)`,
`tg` (position 3, Telegram's real HTML slot) = the raw body.
"""
from __future__ import annotations

import html as html_module

import pytest

from backend.api.algo import event_agents

pytestmark = pytest.mark.asyncio


def _rec(title: str, body: str) -> dict:
    return {"extra": {"title": title, "body": body}}


class TestRenderDeploySyncHtmlSeparation:
    async def test_html_body_is_stripped_for_the_plain_slot(self):
        """Confirmed to fail pre-fix: old code returned body verbatim
        (with the <b> tags still in it) as the 2nd tuple element."""
        out = event_agents._render_deploy_sync(
            _rec("Visitor report", "<b>Visitors</b>\nIP 1.2.3.4 — 12 hits")
        )
        title, body = out[0], out[1]
        assert title == "Visitor report"
        assert "<b>" not in body
        assert "</b>" not in body
        assert "Visitors" in body
        assert "IP 1.2.3.4 — 12 hits" in body

    async def test_html_body_is_preserved_verbatim_in_the_tg_slot(self):
        """Confirmed to fail pre-fix: old code returned only a 2-tuple,
        so out[2] didn't exist at all."""
        raw = "<b>Visitors</b>\nIP 1.2.3.4 — 12 hits"
        out = event_agents._render_deploy_sync(_rec("Visitor report", raw))
        assert len(out) == 3
        assert out[2] == raw

    async def test_plain_body_with_no_tags_is_unchanged_in_both_slots(self):
        """DEPLOY_SYNC_AGENT / gtt_asymmetric / oco_double_fire /
        ticker_recovered / ticker_degraded all currently build genuinely
        plain-text bodies — confirm this fix is a no-op for them."""
        plain = "dev and main are 3 commits apart"
        out = event_agents._render_deploy_sync(_rec("Deploy out of sync", plain))
        assert out[1] == plain
        assert out[2] == plain

    async def test_missing_title_falls_back_to_default(self):
        out = event_agents._render_deploy_sync({"extra": {"body": "x"}})
        assert out[0] == "Deploy out of sync"


class TestSendChannelEndToEnd:
    """Drives the real `_send_channel` dispatch, not just the renderer in
    isolation — confirms the fix actually reaches ntfy and Telegram, not
    just that the tuple shape changed."""

    @pytest.fixture
    def agent(self):
        from types import SimpleNamespace
        return SimpleNamespace(slug="t", tier="medium")

    async def test_ntfy_channel_receives_plain_body_not_raw_html(self, agent, monkeypatch):
        calls = []
        monkeypatch.setitem(
            event_agents.CHANNELS, "ntfy",
            ("ntfy", lambda t, b, *_a, **_k: calls.append((t, b))),
        )
        monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
        out = event_agents._render_deploy_sync(
            _rec("Visitor report", "<b>Visitors</b>\nIP 1.2.3.4 — 12 hits")
        )
        ok = await event_agents._send_channel(
            {"channel": "ntfy", "enabled": True, "gate": False}, out, agent,
        )
        assert ok
        assert len(calls) == 1
        _, body = calls[0]
        assert "<b>" not in body

    async def test_telegram_info_channel_renders_real_html_not_double_escaped(self, agent, monkeypatch):
        """The actual bug: pre-fix, Telegram would receive the literal
        text "&lt;b&gt;Visitors&lt;/b&gt;" (displayed as "<b>Visitors</b>"
        rather than bold) because `tg` was always None and the escape
        fallback double-escaped an already-HTML body."""
        captured = {}
        from backend.shared.helpers import alert_utils

        def _fake_send_telegram_info(msg):
            captured["msg"] = msg

        monkeypatch.setattr(alert_utils, "_send_telegram_info", _fake_send_telegram_info)
        monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
        out = event_agents._render_deploy_sync(
            _rec("Visitor report", "<b>Visitors</b>\nIP 1.2.3.4 — 12 hits")
        )
        ok = await event_agents._send_channel(
            {"channel": "telegram_info", "enabled": True, "gate": False}, out, agent,
        )
        assert ok
        msg = captured["msg"]
        # The real <b> tag must survive untouched (so Telegram's
        # parse_mode=HTML renders it as bold) — not escaped to &lt;b&gt;.
        assert "<b>Visitors</b>" in msg
        assert html_module.escape("<b>Visitors</b>") not in msg
