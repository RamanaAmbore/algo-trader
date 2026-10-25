"""Notify channel resolver — `telegram`: Telegram group defined by
secrets.telegram_chat_id."""

from __future__ import annotations


def _send_telegram_html(title: str, body: str, tg: "str | None" = None) -> None:
    """Despite the `_html` suffix (kept for name stability — several
    tests `patch.object`/`CHANNELS`-swap this function by name), this is
    now a plain-text send: 2026-10 reversal, no `<b>`/`<code>` wrap and
    no `html.escape()` — `tg`, when a renderer supplies one, is already
    plain text (every renderer in `event_agents.py` builds it that way
    now)."""
    from backend.shared.helpers import alert_utils
    alert_utils._send_telegram(tg or f"{title}\n{body}")
