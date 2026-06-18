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
    algo_order_id: int | None = None,
) -> None:
    """Look up the AlgoOrder row + update its terminal status, write a
    chase-terminal AgentEvent, and on chase_fill fire any attached
    template's GTT + wing. Fire-and-forget.

    Lookup priority: algo_order_id (Phase 0.5 — chase passes the row
    id explicitly, immune to mid-chase broker_order_id mutation) →
    broker_order_id (legacy callers that don't yet pass the row id).
    Without the algo_order_id path, chased orders silently failed to
    flip to FILLED because their AlgoOrder row stayed pinned to the
    FIRST broker_order_id while _emit_chase_terminal was called with
    the LATEST one.
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
            row = None
            if algo_order_id is not None:
                row = (await _s.execute(
                    _sel(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                )).scalar_one_or_none()
            if row is None:
                row = (await _s.execute(
                    _sel(_AlgoOrder).where(
                        _AlgoOrder.broker_order_id == broker_order_id
                    )
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

        # Auto TP + Template attach — fire after a chase fill on a
        # parent row (parent_order_id IS NULL so we never create
        # TP-of-TP chains). Same lookup priority: algo_order_id first
        # then broker_order_id.
        if outcome == "chase_fill" and final_price:
            try:
                from sqlalchemy import select as _sel2
                from backend.api.database import async_session as _as2
                from backend.api.models import AlgoOrder as _AO2
                async with _as2() as _s2:
                    _filled = None
                    if algo_order_id is not None:
                        _filled = (await _s2.execute(
                            _sel2(_AO2).where(_AO2.id == int(algo_order_id))
                        )).scalar_one_or_none()
                    if _filled is None:
                        _filled = (await _s2.execute(
                            _sel2(_AO2).where(_AO2.broker_order_id == broker_order_id)
                        )).scalar_one_or_none()
                    # Phase 3D #6 — gate the legacy single-target TP
                    # shim on template_id IS NULL so a row with BOTH
                    # legacy target_pct AND a template doesn't attach
                    # exits twice. Template path supersedes whenever
                    # both are set; the postback handler already
                    # orders things this way (template attach fires
                    # after _arm_take_profit and its GTT becomes the
                    # operative exit) but chase fires both tasks
                    # unconditionally and either may win the race.
                    if (_filled is not None
                            and (_filled.target_pct or _filled.target_abs)
                            and _filled.parent_order_id is None
                            and _filled.template_id is None):
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
                    # Phase 0.5 — template attach on chase fill. Same
                    # idempotency guard (attached_gtts_json populated →
                    # skip) lives inside _fire_template_attach_on_fill,
                    # so a race against the postback hook is safe.
                    if (_filled is not None
                            and _filled.template_id
                            and _filled.parent_order_id is None
                            and (_filled.mode or "") == "live"):
                        from backend.api.routes.orders import (
                            _fire_template_attach_on_fill,
                        )
                        import asyncio as _aio4
                        # Sprint B (#4) — size exit GTTs against the
                        # ACTUAL filled qty when the chase took partials.
                        # Pre-fix the attach sized against the original
                        # ask quantity, over-sizing the exit by any
                        # already-filled portion. `filled_quantity > 0`
                        # is the chase's truth-of-record for "how much
                        # actually traded"; `quantity` is "how much the
                        # operator asked for".
                        _attach_qty = (
                            int(_filled.filled_quantity)
                            if int(_filled.filled_quantity or 0) > 0
                            else int(_filled.quantity or qty)
                        )
                        _aio4.create_task(_fire_template_attach_on_fill(
                            parent_row_id=int(_filled.id),
                            parent_account=str(_filled.account or ""),
                            parent_symbol=str(_filled.symbol or symbol),
                            parent_exchange=str(_filled.exchange or "NFO"),
                            parent_side=str(_filled.transaction_type or side),
                            parent_qty=_attach_qty,
                            fill_price=float(final_price),
                            template_id=int(_filled.template_id),
                            parent_product=str(_filled.product or "NRML"),
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


# ── Operator kill signal ─────────────────────────────────────────────
# When the operator clicks Kill in the chase panel, the kill_chase
# route cancels the live broker order AND adds its broker_order_id
# here. The chase loop checks this set before placing each new
# attempt — if the *previous* attempt's broker_order_id is killed,
# the loop terminates instead of re-placing. Without this signal the
# loop's CANCELLED-status handler treated the operator kill the same
# way it treats a broker auto-cancel (circuit/session-end) and
# silently placed a new order — operator saw the row "reappear" on
# the next poll.
#
# Sprint B (audit #10): switched from `set[str]` to `dict[str, float]`
# (broker_order_id → expire_epoch) so stale entries self-prune. A
# previously-killed order ages out 60 minutes after the kill, which
# is well past the longest realistic chase lifecycle (max_attempts ×
# interval_seconds for the slowest config = 30 × 30s = 15 min). The
# lazy sweep happens inside `is_killed()` — every read drops every
# entry past its expiry — so the dict stays bounded without a
# dedicated background task.
import time as _time
_KILLED_TTL_SECONDS = 3600
_KILLED_ORDER_IDS: dict[str, float] = {}
_KILLED_LOCK = __import__("threading").Lock()


def mark_killed(broker_order_id: str) -> None:
    """Signal the chase loop that this broker_order_id was killed by
    the operator. The loop checks `is_killed()` after every status
    poll and terminates instead of placing a fresh order. Idempotent;
    multiple kills (same id) are fine."""
    if not broker_order_id:
        return
    with _KILLED_LOCK:
        _KILLED_ORDER_IDS[str(broker_order_id)] = _time.monotonic() + _KILLED_TTL_SECONDS


def is_killed(broker_order_id: str) -> bool:
    if not broker_order_id:
        return False
    with _KILLED_LOCK:
        now = _time.monotonic()
        # Lazy sweep — drop every expired entry on read so the dict
        # stays bounded without a background task. Iterating in a list
        # snapshot so we can mutate during traversal.
        for k, expires in list(_KILLED_ORDER_IDS.items()):
            if expires <= now:
                _KILLED_ORDER_IDS.pop(k, None)
        return str(broker_order_id) in _KILLED_ORDER_IDS


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


# O(1) (exchange, symbol) → (lot_size, tick_size) index built on top
# of the instruments cache. The cache itself is a 75k-row list — the
# legacy `for inst in resp.items` scan in `_lot_size_sync` and
# `_tick_size_sync` cost ~75k comparisons per chase attempt. A
# 5-attempt chase = 10 full scans (lot + tick lookups); a 20-attempt
# MCX chase = 40. This dict is built once per cache generation
# (lazily on first lookup; rebuilt when the underlying response id
# changes — detected by the `id(resp)` guard) so subsequent reads
# are O(1) regardless of how many chase loops are concurrent.
_INSTRUMENT_INDEX: dict[tuple[str, str], tuple[int, float]] = {}
_INSTRUMENT_INDEX_FOR: int = 0   # id(resp) the current index was built from


def _ensure_instrument_index(resp) -> None:
    """Build (or rebuild) `_INSTRUMENT_INDEX` from the given instruments
    response when the cache rotated since the last build."""
    global _INSTRUMENT_INDEX, _INSTRUMENT_INDEX_FOR
    rid = id(resp)
    if rid == _INSTRUMENT_INDEX_FOR and _INSTRUMENT_INDEX:
        return
    new_index: dict[tuple[str, str], tuple[int, float]] = {}
    for inst in resp.items:
        new_index[(inst.e, inst.s)] = (
            int(inst.ls) if inst.ls > 0 else 1,
            float(inst.ts) if inst.ts > 0 else 0.05,
        )
    _INSTRUMENT_INDEX = new_index
    _INSTRUMENT_INDEX_FOR = rid


def _instrument_specs(exchange: str, symbol: str) -> tuple[int, float]:
    """Return (lot_size, tick_size) for a contract in O(1). Falls
    through to (1, 0.05) — the NSE default — on any miss (cache cold,
    symbol not in instruments dump, etc.)."""
    try:
        from backend.api.cache import _store
        entry = _store.get("instruments")
        if entry is None:
            return (1, 0.05)
        _expires, resp = entry
        if resp is None or not hasattr(resp, "items"):
            return (1, 0.05)
        _ensure_instrument_index(resp)
        return _INSTRUMENT_INDEX.get((exchange, symbol), (1, 0.05))
    except Exception:
        return (1, 0.05)


def _lot_size_sync(exchange: str, symbol: str) -> int:
    """O(1) lot_size lookup. See `_instrument_specs` for cache shape.

    Falls through to 1 on a miss — safe default for to_kite_qty (no
    translation for non-MCX exchanges, and MCX lot_size > 1 so
    raw_qty // 1 == raw_qty anyway)."""
    return _instrument_specs(exchange, symbol)[0]


def _tick_size_sync(exchange: str, symbol: str) -> float:
    """O(1) tick_size lookup. See `_instrument_specs` for cache shape.

    Kite rejects orders whose LIMIT price isn't a multiple of the
    contract's tick — NFO/NSE = ₹0.05, MCX commodities like CRUDEOIL =
    ₹1, bullion = ₹1, etc. _calc_limit_price's plain `round(px, 2)`
    handles paise-tick correctly but lets through ₹437.55 on a ₹1-tick
    contract, which the exchange then rejects. We round to the actual
    tick before returning the chase price.

    Falls through to 0.05 on a miss — the NSE default that's been
    the implicit assumption everywhere else in this engine."""
    return _instrument_specs(exchange, symbol)[1]


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
    """Get order status. Returns dict with status, filled_quantity, etc.

    Calls `broker.order_status(order_id)` — the Broker ABC's
    targeted single-order endpoint. Kite implements this via
    `order_history(order_id)` so we pay for one order's lifecycle
    instead of the entire day's order book on every 20-s chase
    status poll. Dhan / Groww fall back to the default
    `orders()`-filter implementation until their SDKs expose a
    targeted endpoint."""
    broker = _get_broker(account)
    return broker.order_status(order_id)


async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def _sync_algo_order_id(algo_order_id: int | None,
                              new_broker_order_id: str) -> None:
    """Update AlgoOrder.broker_order_id to the latest one the chase
    just placed. Best-effort — never raises. Phase 0.5 — without this
    every chase cancel-and-replace orphaned the row from its broker
    id, so the terminal lookup in _emit_chase_terminal (and the
    postback handler) couldn't match the row by broker_order_id.
    """
    if algo_order_id is None or not new_broker_order_id:
        return
    try:
        from sqlalchemy import select as _sel
        from backend.api.database import async_session as _as
        from backend.api.models import AlgoOrder as _AO
        async with _as() as _s:
            row = (await _s.execute(
                _sel(_AO).where(_AO.id == int(algo_order_id))
            )).scalar_one_or_none()
            if row is not None and row.broker_order_id != str(new_broker_order_id):
                row.broker_order_id = str(new_broker_order_id)
                await _s.commit()
    except Exception as _e:
        logger.debug(f"_sync_algo_order_id failed: {_e}")


async def _record_partial_fill(algo_order_id: int | None,
                                filled_qty: int,
                                fill_price: float,
                                total_qty: int) -> None:
    """Sprint B (#4) — persist partial-fill state on the AlgoOrder row.

    Accumulates `filled_quantity` across partials and rolls a fill-
    qty-weighted `fill_price`. The terminal handler later overwrites
    `fill_price` with the broker's final average — this interim value
    keeps the row truthful between partials so:
      - the order log shows live `filled/total` progress instead of
        appearing stuck at 0,
      - the UNFILLED give-up path reports the actual unfilled remainder
        instead of restating the original qty,
      - the template attach path sees the accurate `quantity` and
        sizes exit GTTs against the residual, not the original lot.

    Best-effort. Never raises — partial-fill telemetry is
    informational, the chase loop continues whether the write
    succeeds or not.
    """
    if algo_order_id is None or filled_qty <= 0:
        return
    try:
        from sqlalchemy import select as _sel
        from backend.api.database import async_session as _as
        from backend.api.models import AlgoOrder as _AO
        async with _as() as _s:
            row = (await _s.execute(
                _sel(_AO).where(_AO.id == int(algo_order_id))
            )).scalar_one_or_none()
            if row is None:
                return
            prior_filled = int(row.filled_quantity or 0)
            prior_avg    = float(row.fill_price or 0)
            new_filled   = prior_filled + int(filled_qty)
            # Weighted-average fill price across partials. Drops back
            # to current fill_price when no prior partials exist.
            if prior_filled > 0 and prior_avg > 0 and fill_price > 0:
                new_avg = (
                    (prior_avg * prior_filled) + (float(fill_price) * filled_qty)
                ) / new_filled
            else:
                new_avg = float(fill_price or 0)
            row.filled_quantity = new_filled
            if new_avg > 0:
                row.fill_price = new_avg
            row.detail = (
                f"PARTIAL {new_filled}/{total_qty} @ ₹{new_avg:,.2f} "
                f"(chasing residual {total_qty - new_filled})"
            )[:240]
            await _s.commit()
    except Exception as _e:
        logger.debug(f"_record_partial_fill failed: {_e}")


async def chase_order(
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    cfg: ChaseConfig | None = None,
    on_event: Callable | None = None,
    algo_order_id: int | None = None,
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
        algo_order_id: Optional AlgoOrder row id — when set, the chase
                       loop syncs broker_order_id on every successful
                       re-place and passes the id into all
                       _emit_chase_terminal calls. Required for any
                       chased order that has a template attached
                       (Phase 0.5).

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
            # Phase 0.5 — keep the AlgoOrder row's broker_order_id in
            # lockstep with the chase loop's current order so the
            # terminal handler + postback handler + chase panel all
            # see the LATEST broker id, not the FIRST one.
            await _sync_algo_order_id(algo_order_id, current_order_id)

            # Wait for fill
            await asyncio.sleep(cfg.interval_seconds)

            # Check status
            status = await _run(_order_status, account, current_order_id)
            order_status = status.get("status", "").upper()
            # Sprint D — Kite reports `filled_quantity` in WHATEVER
            # units `place_order` was given. For MCX/NCO we placed in
            # LOTS (translate_qty divides by lot_size), so the status
            # filled_quantity is also in lots — but our `remaining_qty`
            # / `quantity` track CONTRACTS. Without the reverse-
            # translate, every MCX partial-fill comparison fires
            # (1 lot < 100 contracts always) and AlgoOrder.filled_qty
            # accumulated as lots into a contracts column. Reverse-
            # translate once here so downstream math is in one unit.
            _kite_filled = int(status.get("filled_quantity", 0) or 0)
            if cfg.exchange in ("MCX", "NCO") and _kite_filled > 0:
                from backend.shared.brokers.kite import from_kite_qty
                _lot = _lot_size_sync(cfg.exchange, symbol)
                filled_qty = from_kite_qty(cfg.exchange, _kite_filled, _lot)
            else:
                filled_qty = _kite_filled
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
                    algo_order_id=algo_order_id,
                ))
                return result

            if filled_qty > 0 and filled_qty < remaining_qty:
                # Partial fill — chase the residual.
                remaining_qty -= filled_qty
                # Sprint E (audit) — flip the in-memory status to
                # PARTIAL so the ChaseResult returned to callers
                # reflects partial-fill state. Pre-fix the enum value
                # existed but was never set anywhere; downstream
                # consumers (chase panel, MCP audit log) couldn't
                # distinguish "still chasing the residual" from "no
                # progress yet". Persistent state on the AlgoOrder row
                # still uses OPEN / FILLED / UNFILLED — PARTIAL is an
                # in-process classifier, not a DB-status.
                result.status = ChaseStatus.PARTIAL
                logger.info(f"Chase {symbol}: partial fill {filled_qty}, remaining {remaining_qty}")
                emit("partial_fill", {"filled": filled_qty, "remaining": remaining_qty})
                # Sprint B (audit #4) — persist the partial state on the
                # AlgoOrder row so downstream readers see the truth:
                #   • `filled_quantity` accumulates across partials so the
                #     order log + reconcile path know how much actually
                #     traded.
                #   • `fill_price` rolls the avg-of-fills weighting so a
                #     subsequent terminal-fill (or UNFILLED give-up) row
                #     surfaces the right blended average.
                # Pre-fix `remaining_qty -= filled_qty` was in-memory only;
                # if the chase eventually hit max_attempts the UNFILLED
                # row showed the ORIGINAL quantity as unfilled (over-
                # stated). Worse, the template-attach path read
                # `_filled.quantity` to size the exit GTT and would
                # over-size by the already-traded portion.
                await _record_partial_fill(
                    algo_order_id, filled_qty, avg_price, quantity
                )

            if order_status == "REJECTED":
                # Broker rejected the order — invalid product / no permission /
                # margin shortfall / tick violation / price band, etc. The
                # same parameters will be rejected again next attempt, so we
                # abort the chase immediately rather than burn through
                # `max_attempts` re-submitting an order Kite has already
                # said no to. Operator gets an alert with the broker's
                # status_message so they can fix the underlying issue.
                status_msg = status.get("status_message", "") or "rejected by broker"
                abort_msg = f"Order rejected by broker: {status_msg}"
                logger.error(f"Chase {symbol}: REJECTED — {status_msg}. Aborting chase.")
                result.status = ChaseStatus.FAILED
                result.detail = abort_msg
                rejected_order_id = current_order_id
                current_order_id = None
                emit("chase_failed", {
                    "attempts": attempt, "error": abort_msg,
                    "reason": "broker_rejected",
                    "status_message": status_msg,
                })
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
                if rejected_order_id:
                    import asyncio as _asyncio
                    _asyncio.create_task(_emit_chase_terminal(
                        rejected_order_id, "chase_failed",
                        symbol, transaction_type, quantity,
                        attempts=attempt, error=abort_msg,
                        algo_order_id=algo_order_id,
                    ))
                return result

            if order_status == "CANCELLED":
                # External cancel — three possible sources:
                #   (a) operator clicked Kill in the chase panel
                #   (b) broker auto-cancelled (circuit / session-end)
                #   (c) operator cancelled directly in Kite app
                # For (a), the kill route adds the broker_order_id to
                # `_KILLED_ORDER_IDS` BEFORE issuing the broker cancel.
                # Without this check the loop treated every CANCELLED
                # as a transient hiccup and silently re-placed — the
                # operator's kill was effectively ignored.
                killed_by_op = is_killed(current_order_id)
                logger.warning(
                    f"Chase {symbol}: order CANCELLED "
                    f"(operator_kill={killed_by_op}) — "
                    f"{status.get('status_message', '')}"
                )
                if killed_by_op:
                    result.status = ChaseStatus.CANCELLED
                    result.detail = "Chase cancelled by operator"
                    emit("chase_cancelled", {"attempts": attempt})
                    cancelled_order_id = current_order_id
                    current_order_id = None
                    if cancelled_order_id:
                        import asyncio as _asyncio
                        _asyncio.create_task(_emit_chase_terminal(
                            cancelled_order_id, "chase_cancelled",
                            symbol, transaction_type, quantity,
                            attempts=attempt,
                            algo_order_id=algo_order_id,
                        ))
                    return result
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
                        algo_order_id=algo_order_id,
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
            algo_order_id=algo_order_id,
        ))
    return result
