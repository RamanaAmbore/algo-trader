"""Gate for automated closes: record a held order instead of sending it."""
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import get_bool

from backend.api.algo.order_hold import HoldCategory, effective_hold, hold_record

logger = get_logger(__name__)

EXPIRY_CLOSE_RELEASED_KEY = "hold.expiry_close_released"


def expiry_close_held() -> bool:
    """True when expiry closes must be held (global default is held)."""
    released = get_bool(EXPIRY_CLOSE_RELEASED_KEY, False)
    return effective_hold(HoldCategory.EXPIRY_CLOSE, None, {"expiry_close": released})


async def record_held_close(*, account: str, symbol: str, exchange: str,
                            side: str, qty: int, product: str,
                            reason: str) -> int | None:
    """Persist one HELD AlgoOrder for an expiry close. Returns the row id."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    held_at = datetime.now(timezone.utc)
    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty, product=product,
                initial_price=None, status="HELD", engine="live", mode="live",
                broker_order_id="", detail=f"HELD: {reason}",
                hold_json=hold_record(HoldCategory.EXPIRY_CLOSE, reason,
                                      "CHASE_MED", None, held_at),
            )
            s.add(row)
            await s.commit()
            row_id = row.id
        logger.info(f"[HOLD] expiry close held: {side} {qty} {symbol} acct={account} id={row_id}")
        from backend.api.algo.order_events import write_event
        await write_event(row_id, "held", f"Held: {side} {qty} {symbol} ({reason})", {"reason": reason})
        return row_id
    except Exception as e:
        logger.error(f"[HOLD] could not record held close for {symbol}: {e}")
        return None
