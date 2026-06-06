"""
Adaptive limit-order chase engine.

Market orders are not allowed for most options. This module places LIMIT orders
and progressively adjusts the price using market depth until filled.

Reusable: called by expiry engine, interpreter buy/sell, or any future strategy.

Usage:
    result = await chase_order(account, symbol, 'SELL', 50, exchange='NFO')
    # result: ChaseResult(order_id, fill_price, attempts, slippage, status)
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


async def _emit_chase_terminal(
    broker_order_id: str,
    outcome: str,          # chase_fill | chase_unfilled | chase_failed | chase_cancelled
    symbol: str,
    side: str,
    qty: int,
    *,
    final_price: float | None = None,
    attempts: int = 0,
    slippage: float | None = None,
    error: str | None = None,
) -> None:
    """Look up the AlgoOrder row by broker_order_id, update its terminal
    status, and write a chase-terminal AgentEvent if the order carries
    an agent_id.  Fire-and-forget.

    Previously this only wrote the AgentEvent and never touched
    AlgoOrder.status — chase-driven orders stayed at OPEN forever
    even though the chase loop had already filled/cancelled them.
    """
    try:
        from sqlalchemy import select as _sel
        from datetime import datetime, timezone
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrder as _AlgoOrder
        from backend.api.algo.agent_engine import record_chase_terminal

        # Map chase outcome → AlgoOrder.status.
        _OUTCOME_TO_STATUS = {
            "chase_fill":      "FILLED",
            "chase_unfilled":  "UNFILLED",
            "chase_cancelled": "CANCELLED",
            "chase_failed":    "REJECTED",
        }
        _new_status = _OUTCOME_TO_STATUS.get(outcome)

        agent_id = None
        async with _async_session() as _s:
            row = (await _s.execute(
                _sel(_AlgoOrder).where(_AlgoOrder.broker_order_id == broker_order_id)
            )).scalar_one_or_none()
            if row is not None:
                agent_id = getattr(row, "agent_id", None)
                if _new_status and row.status != _new_status:
                    row.status = _new_status
                    if attempts:
                        try:
                            row.attempts = int(attempts)
                        except (TypeError, ValueError):
                            pass
                    if _new_status == "FILLED":
                        if final_price is not None:
                            try:
                                row.fill_price = float(final_price)
                            except (TypeError, ValueError):
                                pass
                        row.filled_at = datetime.now(timezone.utc)
                    if error:
                        row.detail = (row.detail or "")[:200] + f" · {error[:120]}"
                    await _s.commit()

        await record_chase_terminal(
            agent_id=agent_id,
            outcome=outcome,
            symbol=symbol,
            side=side,
            qty=qty,
            final_price=final_price,
            attempts=attempts,
            slippage=slippage,
            error=error,
        )

        # Auto TP — arm take-profit child after a chase fill.
        # Only fires when the filled parent row has target_pct / target_abs
        # set and is itself a parent (parent_order_id IS NULL) so we never
        # create TP-of-TP chains.
        if outcome == "chase_fill" and final_price:
            try:
                from sqlalchemy import select as _sel2
                from backend.api.database import async_session as _as2
                from backend.api.models import AlgoOrder as _AO2
                async with _as2() as _s2:
                    _filled = (await _s2.execute(
                        _sel2(_AO2).where(_AO2.broker_order_id == broker_order_id)
                    )).scalar_one_or_none()
                    if (_filled is not None
                            and (_filled.target_pct or _filled.target_abs)
                            and _filled.parent_order_id is None):
                        from backend.api.routes.orders import _arm_take_profit
                        import asyncio as _aio3
                        _aio3.create_task(_arm_take_profit(
                            parent_row_id=_filled.id,
                            parent_account=str(_filled.account or ""),
                            parent_symbol=str(_filled.symbol or symbol),
                            parent_exchange=str(_filled.exchange or "NFO"),
                            parent_side=str(_filled.transaction_type or side),
                            fill_price=float(final_price),
                            target_pct=float(_filled.target_pct or 0.0),
                            target_abs=(_filled.target_abs
                                        and float(_filled.target_abs)),
                            parent_mode=str(_filled.mode or "live"),
                        ))
            except Exception as _tp_e:
                logger.debug(f"_emit_chase_terminal TP arm failed: {_tp_e}")

    except Exception as _e:
        logger.debug(f"_emit_chase_terminal: {_e}")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chase")

# Maximum consecutive broker errors before a chase loop is aborted and an
# alert is fired.  Distinct from `max_attempts` (the unfilled cap): this
# counts Kite SDK exceptions, not quote-crossing retries.
_MAX_CHASE_ERRORS = 3


class ChaseStatus(str, Enum):
    PENDING   = "pending"
    CHASING   = "chasing"
    FILLED    = "filled"
    PARTIAL   = "partial"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class ChaseResult:
    order_id: str = ""
    account: str = ""
    symbol: str = ""
    transaction_type: str = ""
    quantity: int = 0
    initial_price: float = 0.0
    fill_price: float = 0.0
    attempts: int = 0
    slippage: float = 0.0
    status: ChaseStatus = ChaseStatus.PENDING
    detail: str = ""


@dataclass
class ChaseConfig:
    interval_seconds: int = 20       # time between price adjustments
    aggression_step: float = 0.10    # spread fraction increase per attempt
    max_attempts: int = 20           # before giving up
    exchange: str = "NFO"
    product: str = "NRML"
    variety: str = "regular"
    validity: str = "DAY"
    rejection_backoff_seconds: int = 0
    # extra pause when an attempt comes back REJECTED / CANCELLED. 0 →
    # use cfg.interval_seconds (keeps spacing identical to a normal
    # re-quote cycle). A structural rejection (margin shortfall, tick
    # violation, permission gap) won't fix itself in milliseconds, so
    # waiting before the next place_order avoids hammering Kite's
    # 1-order/sec rate limit and gives the operator time to react.


def _get_broker(account: str):
    """Return the `Broker` adapter for `account`. Routes through the
    registry so the chase engine doesn't know or care whether the
    backing broker is Kite, Groww, Dhan, or anything we add later —
    every method called below is on the `Broker` ABC."""
    from backend.shared.brokers.registry import get_broker
    return get_broker(account)


def _get_depth(account: str, exchange: str, symbol: str) -> dict:
    """Fetch market depth for a symbol. Returns {buy: [...], sell: [...]}."""
    broker = _get_broker(account)
    key = f"{exchange}:{symbol}"
    data = broker.quote([key])
    if key not in data:
        raise ValueError(f"No quote data for {key}")
    return data[key].get("depth", {})


def _get_ltp(account: str, exchange: str, symbol: str) -> float:
    """Fetch last traded price."""
    broker = _get_broker(account)
    key = f"{exchange}:{symbol}"
    data = broker.ltp([key])
    return data.get(key, {}).get("last_price", 0.0)


def _calc_limit_price(depth: dict, transaction_type: str, attempt: int,
                      aggression_step: float,
                      exchange: str = "", symbol: str = "") -> float:
    """
    Calculate limit price from market depth, snapped to the
    instrument's actual tick.

    For SELL: start at mid, move toward best_bid with each attempt.
    For BUY:  start at mid, move toward best_ask with each attempt.

    The result is rounded to the contract's real tick_size (₹0.05 on
    NFO/NSE, ₹1 on MCX commodities, …). Without this, the chase would
    happily send ₹437.55 on a ₹1-tick contract and Kite rejects with
    "the entered price is not as per the ticker price".
    """
    buy_depth  = depth.get("buy", [])
    sell_depth = depth.get("sell", [])

    best_bid = buy_depth[0]["price"]  if buy_depth  and buy_depth[0]["price"]  > 0 else 0
    best_ask = sell_depth[0]["price"] if sell_depth and sell_depth[0]["price"] > 0 else 0

    if best_bid == 0 or best_ask == 0:
        # Fallback: use whichever is available
        return best_bid or best_ask or 0

    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2

    # Aggression: fraction of spread to cross toward market
    aggression = min(attempt * aggression_step, 0.95)

    tick = _tick_size_sync(exchange, symbol) if exchange and symbol else 0.05
    if transaction_type == "SELL":
        # Move from mid toward best_bid
        price = mid - (spread * aggression * 0.5)
        snapped = _snap_to_tick(price, tick)
        return max(snapped, best_bid)
    else:
        # BUY: move from mid toward best_ask
        price = mid + (spread * aggression * 0.5)
        snapped = _snap_to_tick(price, tick)
        return min(snapped, best_ask)


def _lot_size_sync(exchange: str, symbol: str) -> int:
    """Read lot_size from the in-process instruments cache (sync).

    The instruments cache is pre-warmed at startup and refreshed daily,
    so this read is almost always a dict lookup. Falls through to 1 on
    any miss — safe default for to_kite_qty (no translation for non-MCX
    exchanges, and MCX lot_size > 1 so raw_qty // 1 == raw_qty anyway).
    """
    try:
        from backend.api.cache import _store  # in-process dict, no I/O
        entry = _store.get("instruments")
        if entry is not None:
            _expires, resp = entry
            if resp is not None and hasattr(resp, "items"):
                for inst in resp.items:
                    if inst.e == exchange and inst.s == symbol:
                        return int(inst.ls) if inst.ls > 0 else 1
    except Exception:
        pass
    return 1


def _tick_size_sync(exchange: str, symbol: str) -> float:
    """Read tick_size from the in-process instruments cache (sync).

    Kite rejects orders whose LIMIT price isn't a multiple of the
    contract's tick — NFO/NSE = ₹0.05, MCX commodities like CRUDEOIL =
    ₹1, bullion = ₹1, etc. _calc_limit_price's plain `round(px, 2)`
    handles paise-tick correctly but lets through ₹437.55 on a ₹1-tick
    contract, which the exchange then rejects. We round to the actual
    tick before returning the chase price.

    Falls through to 0.05 on a miss — the NSE default that's been
    the implicit assumption everywhere else in this engine.
    """
    try:
        from backend.api.cache import _store
        entry = _store.get("instruments")
        if entry is not None:
            _expires, resp = entry
            if resp is not None and hasattr(resp, "items"):
                for inst in resp.items:
                    if inst.e == exchange and inst.s == symbol:
                        return float(inst.ts) if inst.ts > 0 else 0.05
    except Exception:
        pass
    return 0.05


def _snap_to_tick(price: float, tick: float) -> float:
    """Round `price` to the nearest valid `tick` multiple.

    Uses scaled-integer rounding when tick < 1 (0.05 in binary float
    misbehaves) and plain `round(px/tick)*tick` otherwise.
    """
    if price <= 0 or tick <= 0:
        return price
    if tick < 1:
        scale = round(1.0 / tick)
        return round(round(price * scale) / scale, 4)
    return round(round(price / tick) * tick, 4)


def _place_order(account: str, symbol: str, transaction_type: str,
                 quantity: int, price: float, cfg: ChaseConfig) -> str:
    """Place a limit order. Returns order_id."""
    broker    = _get_broker(account)
    lot_size  = _lot_size_sync(cfg.exchange, symbol)
    # normalise_qty handles per-broker contract↔lot translation
    # (Kite needs lots for MCX/NCO, qty for everything else). Default
    # is a no-op for brokers that always accept contracts.
    broker_qty = broker.normalise_qty(cfg.exchange, quantity, lot_size)
    order_id = broker.place_order(
        variety=cfg.variety,
        exchange=cfg.exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=broker_qty,
        product=cfg.product,
        order_type="LIMIT",
        price=price,
        validity=cfg.validity,
    )
    return str(order_id)


def _cancel_order(account: str, order_id: str, variety: str = "regular"):
    """Cancel an open order."""
    broker = _get_broker(account)
    broker.cancel_order(order_id, variety=variety)


def _order_status(account: str, order_id: str) -> dict:
    """Get order status. Returns dict with status, filled_quantity, etc."""
    broker = _get_broker(account)
    orders = broker.orders()
    for o in orders:
        if str(o.get("order_id")) == order_id:
            return o
    return {}


async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def chase_order(
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    cfg: ChaseConfig | None = None,
    on_event: Callable | None = None,
) -> ChaseResult:
    """
    Chase a limit order until filled.

    Args:
        account: Kite account ID (e.g. 'ZG0790')
        symbol: Trading symbol (e.g. 'NIFTY24APR25000CE')
        transaction_type: 'BUY' or 'SELL'
        quantity: Number of lots/shares
        cfg: Chase configuration (defaults used if None)
        on_event: Optional callback(event_type: str, detail: dict) for real-time updates

    Returns:
        ChaseResult with fill details
    """
    if cfg is None:
        # Pull defaults from /admin/settings → DB (algo.*). YAML
        # `algo:` block is the boot-time fallback baked into
        # ChaseConfig's dataclass defaults.
        from backend.shared.helpers.settings import get_int, get_float
        cfg = ChaseConfig(
            interval_seconds=get_int("algo.chase_interval_seconds", 20),
            aggression_step=get_float("algo.aggression_step", 0.10),
            max_attempts=get_int("algo.max_attempts", 20),
            rejection_backoff_seconds=get_int(
                "algo.chase_rejection_backoff_seconds", 0),
        )

    result = ChaseResult(
        account=account, symbol=symbol,
        transaction_type=transaction_type, quantity=quantity,
    )

    def emit(event_type: str, detail: dict = None):
        if on_event:
            try:
                on_event(event_type, {
                    "account": account, "symbol": symbol,
                    "transaction_type": transaction_type,
                    "quantity": quantity, **(detail or {}),
                })
            except Exception:
                pass

    current_order_id = None
    remaining_qty    = quantity
    consecutive_errors = 0        # reset on any successful broker call

    for attempt in range(1, cfg.max_attempts + 1):
        result.attempts = attempt
        result.status = ChaseStatus.CHASING

        try:
            # Get market depth
            depth = await _run(_get_depth, account, cfg.exchange, symbol)
            price = _calc_limit_price(depth, transaction_type, attempt,
                                       cfg.aggression_step,
                                       exchange=cfg.exchange, symbol=symbol)

            if price <= 0:
                logger.warning(f"Chase {symbol}: no valid price from depth at attempt {attempt}")
                await asyncio.sleep(cfg.interval_seconds)
                continue

            if attempt == 1:
                result.initial_price = price

            # Cancel previous order if exists
            if current_order_id:
                try:
                    await _run(_cancel_order, account, current_order_id, cfg.variety)
                    emit("order_cancelled", {"order_id": current_order_id, "attempt": attempt})
                except Exception as e:
                    logger.warning(f"Chase {symbol}: cancel failed: {e}")

            # Place new order
            current_order_id = await _run(
                _place_order, account, symbol, transaction_type, remaining_qty, price, cfg
            )
            consecutive_errors = 0   # successful placement resets the error streak
            result.order_id = current_order_id
            logger.info(f"Chase {symbol}: attempt {attempt}/{cfg.max_attempts} "
                        f"— {transaction_type} {remaining_qty} @ {price} (order {current_order_id})")
            emit("order_placed", {"order_id": current_order_id, "price": price, "attempt": attempt})

            # Wait for fill
            await asyncio.sleep(cfg.interval_seconds)

            # Check status
            status = await _run(_order_status, account, current_order_id)
            order_status = status.get("status", "").upper()
            filled_qty = status.get("filled_quantity", 0)
            avg_price = status.get("average_price", 0)

            if order_status == "COMPLETE":
                result.status = ChaseStatus.FILLED
                result.fill_price = avg_price
                result.slippage = abs(avg_price - result.initial_price) * quantity
                result.detail = f"Filled at {avg_price} in {attempt} attempts"
                emit("order_filled", {
                    "order_id": current_order_id, "fill_price": avg_price,
                    "attempts": attempt, "slippage": result.slippage,
                })
                logger.info(f"Chase {symbol}: FILLED @ {avg_price} "
                            f"(attempt {attempt}, slippage ₹{result.slippage:.2f})")
                import asyncio as _asyncio
                _asyncio.create_task(_emit_chase_terminal(
                    current_order_id, "chase_fill",
                    symbol, transaction_type, quantity,
                    final_price=avg_price, attempts=attempt,
                    slippage=result.slippage,
                ))
                return result

            if filled_qty > 0 and filled_qty < remaining_qty:
                # Partial fill — chase remaining
                remaining_qty -= filled_qty
                logger.info(f"Chase {symbol}: partial fill {filled_qty}, remaining {remaining_qty}")
                emit("partial_fill", {"filled": filled_qty, "remaining": remaining_qty})

            if order_status in ("CANCELLED", "REJECTED"):
                logger.warning(f"Chase {symbol}: order {order_status} — {status.get('status_message', '')}")
                current_order_id = None  # Need fresh order
                backoff = cfg.rejection_backoff_seconds or cfg.interval_seconds
                logger.info(f"Chase {symbol}: backing off {backoff}s before next place_order")
                await asyncio.sleep(backoff)

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Chase {symbol}: attempt {attempt} error "
                         f"({consecutive_errors}/{_MAX_CHASE_ERRORS} consecutive): {e}")
            emit("error", {"attempt": attempt, "error": str(e)})
            if consecutive_errors >= _MAX_CHASE_ERRORS:
                abort_msg = (
                    f"Chase abandoned after {consecutive_errors} consecutive errors "
                    f"({attempt} total attempts) — last error: {e}"
                )
                logger.error(f"Chase {symbol}: {abort_msg}")
                result.status = ChaseStatus.FAILED
                result.detail = abort_msg
                emit("chase_failed", {"attempts": attempt, "error": str(e),
                                      "reason": "consecutive_errors"})
                try:
                    if current_order_id:
                        await _run(_cancel_order, account, current_order_id, cfg.variety)
                except Exception:
                    pass
                try:
                    from backend.shared.helpers.alert_utils import send_order_failure_alert
                    send_order_failure_alert(
                        account=account, symbol=symbol,
                        exchange=cfg.exchange, side=transaction_type,
                        qty=quantity, mode="live", source="chase",
                        error=abort_msg,
                    )
                except Exception:
                    pass
                if current_order_id:
                    import asyncio as _asyncio
                    _asyncio.create_task(_emit_chase_terminal(
                        current_order_id, "chase_failed",
                        symbol, transaction_type, quantity,
                        attempts=attempt, error=abort_msg,
                    ))
                return result
            await asyncio.sleep(cfg.interval_seconds)

    # Max attempts exhausted
    if current_order_id:
        try:
            await _run(_cancel_order, account, current_order_id, cfg.variety)
        except Exception:
            pass

    result.status = ChaseStatus.FAILED
    result.detail = f"Failed after {cfg.max_attempts} attempts"
    emit("chase_failed", {"attempts": cfg.max_attempts})
    logger.error(f"Chase {symbol}: FAILED after {cfg.max_attempts} attempts")
    if result.order_id:
        import asyncio as _asyncio
        _asyncio.create_task(_emit_chase_terminal(
            result.order_id, "chase_unfilled",
            symbol, transaction_type, quantity,
            attempts=cfg.max_attempts,
        ))
    return result
