"""Gate for automated closes: record a held order instead of sending it."""
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import get_bool, get_int

from backend.api.algo.order_hold import HoldCategory, effective_hold, hold_record

logger = get_logger(__name__)

EXPIRY_CLOSE_RELEASED_KEY = "hold.expiry_close_released"
TEMPLATE_EXIT_RELEASED_KEY = "hold.template_exit_released"


def held_for(category: HoldCategory, override: bool | None = None) -> bool:
    """Generic hold-check for any `HoldCategory`.

    A per-order override wins when given. Otherwise the category's own
    global release switch (`hold.<category>_released`) decides, and the
    default is held. `expiry_close_held()` and `template_exit_held()` are
    thin wrappers over this for the two categories that existed before this
    was generalised — kept so existing callers/tests that monkeypatch those
    names by name keep working unchanged.
    """
    released = get_bool(f"hold.{category.value}_released", False)
    return effective_hold(category, override, {category.value: released})


def expiry_close_held() -> bool:
    """True when expiry closes must be held (global default is held)."""
    return held_for(HoldCategory.EXPIRY_CLOSE, None)


def template_exit_held(override: bool | None = None) -> bool:
    """True when template exit GTTs must be held.

    A per-order override (the ticket's Hold switch) wins. Otherwise the global
    switch decides, and the default is held.
    """
    return held_for(HoldCategory.TEMPLATE_EXIT,
                    None if override is None else bool(override))


async def record_held_order(category: HoldCategory, *, account: str, symbol: str,
                            exchange: str, side: str, qty: int, product: str,
                            reason: str, price_policy: str = "CHASE_MED",
                            agent_id: "int | None" = None) -> int | None:
    """Persist one HELD AlgoOrder for any hold category. Returns the row id.

    Generic form of the old `record_held_close` (which held one and only one
    category, `EXPIRY_CLOSE`, hardcoded). See the module docstring in
    `order_release.py` for how to wire up a new category end to end.

    `agent_id` — the firing agent's row id, when the caller is an agent
    action (e.g. ExpiryEngine, fired by expiry-day-*-itm-auto-close). Persisted
    on the row so a later release's resumed chase correctly labels its
    failure alerts "Agent"/"Agent Bracket" instead of defaulting to "Manual"
    — see `alert_utils._classify_order_origin_label`.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    held_at = datetime.now(timezone.utc)
    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty, product=product,
                initial_price=None, status="HELD", engine="live", mode="live",
                broker_order_id="", detail=f"HELD: {reason}", agent_id=agent_id,
                hold_json=hold_record(category, reason, price_policy, None, held_at),
            )
            s.add(row)
            await s.commit()
            row_id = row.id
        logger.info(f"[HOLD] {category.value} held: {side} {qty} {symbol} acct={account} id={row_id}")
        from backend.api.algo.order_events import write_event
        await write_event(row_id, "held", f"Held: {side} {qty} {symbol} ({reason})", {"reason": reason})
        return row_id
    except Exception as e:
        logger.error(f"[HOLD] could not record held order ({category.value}) for {symbol}: {e}")
        return None


async def template_exit_override(parent_row_id: int) -> bool | None:
    """The ticket's per-order template exit hold for a parent row, or None when unset."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with async_session() as s:
        return (await s.execute(select(AlgoOrder.template_hold_override)
                                .where(AlgoOrder.id == parent_row_id))).scalar_one_or_none()


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
