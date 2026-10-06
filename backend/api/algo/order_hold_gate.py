"""Gate for automated closes: record a held order instead of sending it."""
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import get_bool, get_int

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


TEMPLATE_EXIT_RELEASED_KEY = "hold.template_exit_released"


def template_exit_held() -> bool:
    """True when template exit GTTs must be held (global default is held)."""
    released = get_bool(TEMPLATE_EXIT_RELEASED_KEY, False)
    return effective_hold(HoldCategory.TEMPLATE_EXIT, None, {"template_exit": released})


async def hold_template_exit(parent_row_id: int, symbol: str) -> None:
    """Mark a filled parent so its exit GTTs wait for release."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select

    held_at = datetime.now(timezone.utc)
    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == parent_row_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return
        row.hold_json = hold_record(HoldCategory.TEMPLATE_EXIT,
                                    "template exits held until released",
                                    "n/a", None, held_at)
        await s.commit()
    from backend.api.algo.order_events import write_event
    await write_event(parent_row_id, "held", f"Template exits held for {symbol}",
                      {"category": "template_exit"})


_CLOSE_HM = {"NFO": (15, 30), "BFO": (15, 30), "NSE": (15, 30), "MCX": (23, 30), "NCO": (23, 30)}


def cutoff_for(exchange: str, now_ist: datetime | None = None) -> datetime:
    """Cut-off for today's expiry close: session close minus the lead time (IST)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    ex = (exchange or "").upper()
    now = now_ist or datetime.now(ZoneInfo("Asia/Kolkata"))
    hh, mm = _CLOSE_HM.get(ex, (15, 30))
    default_lead = 30 if ex in ("MCX", "NCO") else 15
    lead = get_int(f"hold.lead_minutes_{ex.lower()}", default_lead)
    close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return close - timedelta(minutes=lead)


def before_cutoff(exchange: str, now_ist: datetime | None = None) -> bool:
    """True while the expiry scan must wait for the cut-off."""
    from zoneinfo import ZoneInfo
    now = now_ist or datetime.now(ZoneInfo("Asia/Kolkata"))
    return now < cutoff_for(exchange, now)
