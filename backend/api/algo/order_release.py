"""Release a held automated order: re-check the position and price, then send and chase."""
import asyncio
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger

from backend.api.algo.order_hold import parse_hold_record, release_price

logger = get_logger(__name__)


def position_matches(side: str, expected_qty: int, net_qty: int) -> tuple[bool, str]:
    """A SELL close needs a long position of expected_qty; a BUY close needs a short one."""
    want = expected_qty if side.upper() == "SELL" else -expected_qty
    if net_qty == want:
        return True, "ok"
    return False, f"position changed: net {net_qty}, expected {want}"


def _find_net_qty(positions: dict, symbol: str, exchange: str) -> int | None:
    rows = (positions or {}).get("net") or []
    for r in rows:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper() \
                and str(r.get("exchange", "")).upper() == exchange.upper():
            return int(r.get("quantity") or 0)
    return 0


async def release_held_order(order_id: int, actor: str) -> dict:
    """Return {'ok': bool, 'reason': str, 'status': str}. Sends and chases only when all checks pass."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    from backend.brokers import get_broker
    from backend.api.routes.orders_helpers import _ensure_tick_index, _TICK_INDEX

    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "order not found", "status": ""}
        if row.status != "HELD":
            return {"ok": False, "reason": f"order is {row.status}, not HELD", "status": row.status}
        rec = parse_hold_record(row.hold_json) or {}

        try:
            broker = get_broker(row.account)
            positions = await asyncio.get_running_loop().run_in_executor(None, broker.positions)
            net = _find_net_qty(positions, row.symbol, row.exchange)
            ok, why = position_matches(row.transaction_type, int(row.quantity), net or 0)
            if not ok:
                return {"ok": False, "reason": why, "status": "HELD"}

            key = f"{row.exchange}:{row.symbol}"
            quote = (await asyncio.get_running_loop().run_in_executor(
                None, broker.quote, [key])) or {}
            q = quote.get(key) or {}
            depth = q.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0].get("price")
            ask = (depth.get("sell") or [{}])[0].get("price")
            await _ensure_tick_index()
            tick = _TICK_INDEX.get((row.exchange.upper(), row.symbol.upper()), 0.0)
            ok, price, why = release_price(
                bid, ask, q.get("last_price"), tick,
                float(q.get("lower_circuit_limit") or 0),
                float(q.get("upper_circuit_limit") or 0) or 1e12,
            )
            if not ok:
                return {"ok": False, "reason": f"price check failed: {why}", "status": "HELD"}
        except Exception as e:
            logger.error(f"[RELEASE] check failed for order {order_id}: {e}")
            return {"ok": False, "reason": f"broker check failed: {e}", "status": "HELD"}

        row.status = "OPEN"
        row.detail = f"{row.detail or ''} · released by {actor} at {datetime.now(timezone.utc).isoformat()}"
        await s.commit()
        account, symbol, exchange = row.account, row.symbol, row.exchange
        side, qty, product, row_id = row.transaction_type, int(row.quantity), row.product, row.id

    from backend.api.algo.order_events import write_event
    await write_event(row_id, "released", f"Released by {actor}: {side} {qty} {symbol}", {"actor": actor})

    asyncio.create_task(_chase_released(row_id, account, symbol, exchange, side, qty, product))
    logger.info(f"[RELEASE] order {row_id} released by {actor}")
    return {"ok": True, "reason": "released; chasing", "status": "OPEN"}


async def _chase_released(row_id, account, symbol, exchange, side, qty, product) -> None:
    from backend.api.algo.chase import chase_order, ChaseConfig
    cfg = ChaseConfig(interval_seconds=20, aggression_step=0.10, max_attempts=20,
                      exchange=exchange, product=product, intent="close")
    try:
        await chase_order(account=account, symbol=symbol, transaction_type=side,
                          quantity=qty, cfg=cfg, algo_order_id=row_id)
    except Exception as e:
        logger.error(f"[RELEASE] chase failed for order {row_id}: {e}")
