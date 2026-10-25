"""Notify channel resolver — `telegram_info`: Telegram via the dedicated
'info' key (secrets.telegram_chat_id_info), bypassing the regular
'telegram' capability gate — used by low-noise informational alerts
(e.g. market-summary)."""

from __future__ import annotations


def _send_telegram_info_html(title: str, body: str, tg: "str | None" = None) -> None:
    """Plain-text send (see `channel_telegram.py:_send_telegram_html`'s
    docstring for why the `_html` suffix is kept despite no HTML being
    built here any more)."""
    from backend.shared.helpers import alert_utils
    alert_utils._send_telegram_info(tg or f"{title}\n{body}")
