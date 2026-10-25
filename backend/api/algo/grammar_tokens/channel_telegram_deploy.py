"""Notify channel resolver — `telegram_deploy`: Telegram via the
dedicated deploy-alerts key (secrets.telegram_bot_token_deploy /
telegram_chat_id_deploy), bypassing the regular 'telegram' capability
gate — used exclusively by deploy-sync-alert."""

from __future__ import annotations


def _send_telegram_deploy_html(title: str, body: str, tg: "str | None" = None) -> None:
    """Deploy-sync-drift alerts (`deploy-sync-alert`) post to a DEDICATED
    Telegram bot/chat — secrets `telegram_bot_token_deploy`/
    `telegram_chat_id_deploy`, falling back to the general ops bot/chat
    when either is absent — the SAME fallback pair `webhook/
    notify_deploy.py` (the standalone deploy script) already uses for its
    own, separate deploy-OK/FAIL pings. Operator instruction (2026-10):
    this agent must reach "the telegram code deployment alerts channel
    alone" — never the generic `telegram` channel, never ntfy, never
    email. Plain text, same convention as every other send in
    `event_agents.py` post-reversal."""
    from backend.shared.helpers import alert_utils
    from backend.shared.helpers.utils import secrets
    token = secrets.get('telegram_bot_token_deploy') or secrets.get('telegram_bot_token', '')
    chat_id = secrets.get('telegram_chat_id_deploy') or secrets.get('telegram_chat_id', '')
    alert_utils._send_telegram(tg or f"{title}\n{body}", token=token, chat_id=chat_id)
