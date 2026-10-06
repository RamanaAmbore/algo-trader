"""Fill records: each FILLED transition is logged once, tagged orders, for the fill event agent.

The record carries the mode, so the agent (not this module) decides which fills alert.
Delivery lives in event_agents.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


def format_fill_message(row, when: datetime | None = None) -> tuple[str, str]:
    side = str(row.transaction_type or "").upper() or "ORDER"
    qty = int(row.quantity or 0)
    symbol = str(row.symbol or "")
    title = f"Order filled: {side} {qty} {symbol}"
    body = "\n".join([
        f"Account: {row.account or '-'}",
        f"{side} {qty} {symbol} ({row.exchange or '-'}) @ {float(row.fill_price or 0):.2f}",
        f"Product: {row.product or '-'}",
        f"Order id: {row.id}",
        f"Time: {(when or datetime.now(_IST)).astimezone(_IST):%H:%M:%S} IST",
    ])
    return title, body


async def notify_fills(rows: list) -> None:
    for row in rows:
        try:
            logger.info(
                "order filled",
                extra={
                    "tags": ["orders"],
                    "event": "filled",
                    "mode": str(getattr(row, "mode", "")),
                    "order_id": row.id,
                    "account": row.account,
                    "symbol": row.symbol,
                    "exchange": row.exchange,
                    "transaction_type": row.transaction_type,
                    "quantity": row.quantity,
                    "fill_price": row.fill_price,
                    "product": row.product,
                },
            )
        except Exception as e:
            logger.warning(f"fill log failed for order_id={row.id}: {e}")
