"""
SUPERSEDED by the 2026-10 plain-text reversal.

The 2026-10-23 fix this file originally pinned treated `generic_notice`
(`_render_deploy_sync`) bodies as HTML-safe: `body` (ntfy + escape-fallback
slot) ran through `_html_to_plain(body)`, and `tg` (Telegram's real
HTML-rendering slot) carried the raw body verbatim.

The 2026-10 plain-text reversal removes HTML from the whole pipeline —
Telegram no longer requests `parse_mode: HTML`, and every real producer
(gtt_asymmetric, oco_double_fire, ticker_recovered, ticker_degraded,
deploy_out_of_sync, visitor_report, user_registered, the unrouted
catch-all) now builds `extra["body"]` as plain text. `_render_deploy_sync`
therefore no longer calls `_html_to_plain` at all — it is a pure,
byte-identical passthrough of `body` into BOTH tuple slots.

This file now pins that simpler invariant: `_render_deploy_sync` never
transforms its input, so a literal `<b>` in some future caller's body
(however unlikely now that every producer is plain text) displays
verbatim rather than being silently stripped — the exact failure mode the
old `_html_to_plain` call risked for genuinely plain data containing a
bare `<`/`>` (see CLAUDE.md's "Payoff chart Exp P&L basis"-adjacent note
on `_html_to_plain`'s documented, narrow safety contract).
"""
from __future__ import annotations

import pytest

from backend.api.algo import event_agents

pytestmark = pytest.mark.asyncio


def _rec(title: str, body: str) -> dict:
    return {"extra": {"title": title, "body": body}}


class TestRenderDeploySyncIsAPurePassthrough:
    async def test_body_is_returned_verbatim_in_both_slots(self):
        """No transformation at all — both tuple slots are byte-identical
        to the input `body`, even one that happens to contain `<b>` text
        (a legacy-shaped caller, or a literal comparison operator like
        "qty < lot_size") — nothing strips or escapes it."""
        body = "<b>Visitors</b>\nIP 1.2.3.4 — 12 hits"
        out = event_agents._render_deploy_sync(_rec("Visitor report", body))
        title, plain_slot, tg_slot = out[0], out[1], out[2]
        assert title == "Visitor report"
        assert plain_slot == body
        assert tg_slot == body

    async def test_plain_body_with_no_tags_is_unchanged_in_both_slots(self):
        """DEPLOY_SYNC_AGENT / gtt_asymmetric / oco_double_fire /
        ticker_recovered / ticker_degraded / visitor_report /
        user_registered all build genuinely plain-text bodies in
        production — confirms the passthrough is correct for the real
        shape every one of them actually sends."""
        plain = "dev and main are 3 commits apart"
        out = event_agents._render_deploy_sync(_rec("Deploy out of sync", plain))
        assert out[1] == plain
        assert out[2] == plain

    async def test_missing_title_falls_back_to_default(self):
        out = event_agents._render_deploy_sync({"extra": {"body": "x"}})
        assert out[0] == "Deploy out of sync"


class TestSendChannelEndToEnd:
    """Drives the real `_send_channel` dispatch, not just the renderer in
    isolation — confirms plain text actually reaches ntfy and Telegram
    unchanged."""

    @pytest.fixture
    def agent(self):
        from types import SimpleNamespace
        return SimpleNamespace(slug="t", tier="medium")

    async def test_ntfy_channel_receives_the_plain_body_verbatim(self, agent, monkeypatch):
        calls = []
        monkeypatch.setitem(
            event_agents.CHANNELS, "ntfy",
            ("ntfy", lambda t, b, *_a, **_k: calls.append((t, b))),
        )
        monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
        body = "Visitors\nIP 1.2.3.4 — 12 hits"
        out = event_agents._render_deploy_sync(_rec("Visitor report", body))
        ok = await event_agents._send_channel(
            {"channel": "ntfy", "enabled": True, "gate": False}, out, agent,
        )
        assert ok
        assert len(calls) == 1
        _, received_body = calls[0]
        assert received_body == body

    async def test_telegram_info_channel_receives_plain_text_no_parse_mode_markup(self, agent, monkeypatch):
        """2026-10 plain-text reversal: Telegram shows plain text, not
        bold — there is no `<b>` in the real producer's body any more
        (visitor_report's own `<b>Visitors</b>` wrap was fixed at the
        source, `background.py`), and nothing in this renderer or
        `_send_telegram_info` adds HTML back."""
        captured = {}
        from backend.shared.helpers import alert_utils

        def _fake_send_telegram_info(msg):
            captured["msg"] = msg

        monkeypatch.setattr(alert_utils, "_send_telegram_info", _fake_send_telegram_info)
        monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
        body = "Visitors\nIP 1.2.3.4 — 12 hits"
        out = event_agents._render_deploy_sync(_rec("Visitor report", body))
        ok = await event_agents._send_channel(
            {"channel": "telegram_info", "enabled": True, "gate": False}, out, agent,
        )
        assert ok
        msg = captured["msg"]
        assert msg == body
        assert "<b>" not in msg and "</b>" not in msg
