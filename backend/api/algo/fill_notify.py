"""Fill alerts: one ntfy and one Telegram message per live order that reaches FILLED.

Called from the FILLED write path, which runs once per transition, so a fill
notifies once regardless of which detector (postback, chase, reconcile,
Dhan/Groww sync) caught it. Paper, sim, and replay fills are skipped.
"""
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.shared.helpers.alert_utils import _send_telegram, send_ntfy_alert
from backend.shared.helpers.utils import is_enabled, mask_account

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


def format_fill_message(row) -> tuple[str, str]:
    side = str(row.transaction_type or "").upper() or "ORDER"
    qty = int(row.quantity or 0)
    symbol = str(row.symbol or "")
    title = f"Order filled: {side} {qty} {symbol}"
    body = "\n".join([
        f"Account: {mask_account(str(row.account or ''))}",
        f"{side} {qty} {symbol} ({row.exchange or '-'}) @ {float(row.fill_price or 0):.2f}",
        f"Product: {row.product or '-'}",
        f"Order id: {row.id}",
        f"Time: {datetime.now(_IST):%H:%M:%S} IST",
    ])
    return title, body


async def notify_fills(rows: list) -> None:
    live = [r for r in rows if str(getattr(r, "mode", "")) == "live"]
    if not live:
        return
    want_ntfy = is_enabled("ntfy")
    want_tg = is_enabled("telegram")
    if not (want_ntfy or want_tg):
        return
    for row in live:
        title, body = format_fill_message(row)
        if want_ntfy:
            try:
                await asyncio.to_thread(send_ntfy_alert, title, body)
            except Exception as e:
                logger.warning(f"fill ntfy alert failed for order_id={row.id}: {e}")
        if want_tg:
            try:
                await asyncio.to_thread(
                    _send_telegram, f"<b>{html.escape(title)}</b>\n{html.escape(body)}",
                )
            except Exception as e:
                logger.warning(f"fill telegram alert failed for order_id={row.id}: {e}")
