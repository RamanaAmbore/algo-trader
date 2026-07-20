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
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from sqlalchemy import select as _sql_select

from backend.api.cache import _store as _cache_store
from backend.api.database import async_session as _async_session
from backend.api.models import AlgoOrder as _AlgoOrder
from backend.brokers.registry import get_broker as _get_broker_registry
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


_OUTCOME_TO_STATUS: dict[str, str] = {
    "chase_fill":      "FILLED",
    "chase_unfilled":  "UNFILLED",
    "chase_cancelled": "CANCELLED",
    "chase_failed":    "REJECTED",
}


def _chase_apply_terminal_mutation(
    row,
    new_status: str,
    attempts: int,
    final_price: float | None,
    error: str | None,
) -> None:
    """Mutate an AlgoOrder ORM row in-place for a terminal chase outcome.

    Called inside an active SQLAlchemy session before commit. Applies status
    transition, attempts, fill_price/filled_at (FILLED only), and error detail.
    """
    row.status = new_status
    if attempts:
        try:
            row.attempts = int(attempts)
        except (TypeError, ValueError):
            pass
    if new_status == "FILLED":
        if final_price is not None:
            try:
                row.fill_price = float(final_price)
            except (TypeError, ValueError):
                pass
        row.filled_at = datetime.now(timezone.utc)
    if error:
        row.detail = (row.detail or "")[:200] + f" · {error[:120]}"


def _chase_snapshot_algo_row(row, broker_order_id: str) -> dict:
    """Snapshot all AlgoOrder fields needed by downstream attach paths.

    Called AFTER the optional mutation + commit so readers see post-commit values.
    Returns a plain dict — intentionally no ORM references.
    """
    return {
        "id":                int(row.id),
        "target_pct":        row.target_pct,
        "target_abs":        row.target_abs,
        "parent_order_id":   row.parent_order_id,
        "template_id":       row.template_id,
        "account":           str(row.account or ""),
        "symbol":            str(row.symbol or broker_order_id),
        "exchange":          str(row.exchange or "NFO"),
        "transaction_type":  str(row.transaction_type or ""),
        "product":           str(row.product or "NRML"),
        "mode":              str(row.mode or "live"),
        "filled_quantity":   int(row.filled_quantity or 0),
        "quantity":          int(row.quantity or 0),
    }


async def _chase_terminal_update_db(
    algo_order_id: int | None,
    broker_order_id: str,
    outcome: str,
    attempts: int,
    final_price: float | None,
    error: str | None,
) -> tuple[int | None, dict | None]:
    """Look up AlgoOrder, mutate status/fill fields, commit, return (agent_id, row_snap).

    Lookup priority: algo_order_id (Phase 0.5 — chase passes the row
    id explicitly, immune to mid-chase broker_order_id mutation) →
    broker_order_id (legacy callers that don't yet pass the row id).

    Snapshots all fields needed by the downstream auto-TP + template
    attach paths AFTER the optional mutation + commit so subsequent
    readers see post-commit values. Returns (None, None) when no row
    is found.
    """
    _new_status = _OUTCOME_TO_STATUS.get(outcome)

    agent_id = None
    _row_snap: dict | None = None
    async with _async_session() as _s:
        row = None
        if algo_order_id is not None:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
            )).scalar_one_or_none()
        if row is None:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(
                    _AlgoOrder.broker_order_id == broker_order_id
                )
            )).scalar_one_or_none()
        if row is not None:
            agent_id = getattr(row, "agent_id", None)
            if _new_status and row.status != _new_status:
                _chase_apply_terminal_mutation(row, _new_status, attempts, final_price, error)
                await _s.commit()
            # Snapshot AFTER the optional mutation + commit so the
            # downstream attach paths read post-commit values.
            _row_snap = _chase_snapshot_algo_row(row, broker_order_id)
    return agent_id, _row_snap


def _ch_maybe_fire_auto_tp(snap: dict, final_price: float) -> None:
    """Fire legacy single-target TP shim when template_id IS NULL."""
    if not (snap.get("target_pct") or snap.get("target_abs")):
        return
    if snap.get("parent_order_id") is not None:
        return
    if snap.get("template_id") is not None:
        return
    from backend.api.routes.orders import _arm_take_profit
    asyncio.create_task(_arm_take_profit(
        parent_row_id=snap["id"],
        parent_account=snap["account"],
        parent_symbol=snap["symbol"],
        parent_exchange=snap["exchange"],
        parent_side=snap["transaction_type"],
        fill_price=float(final_price),
        target_pct=float(snap.get("target_pct") or 0.0),
        target_abs=(snap.get("target_abs") and float(snap.get("target_abs"))),
        parent_mode=snap["mode"],
    ))


def _ch_maybe_fire_template_attach(snap: dict, final_price: float) -> None:
    """Fire template attach task on chase fill (idempotent via GTT guard)."""
    if not snap.get("template_id"):
        return
    if snap.get("parent_order_id") is not None:
        return
    from backend.api.routes.orders import _fire_template_attach_on_fill
    # Sprint B (#4) — size exit GTTs against the ACTUAL filled qty
    # when the chase took partials.
    attach_qty = (
        snap["filled_quantity"] if snap["filled_quantity"] > 0 else snap["quantity"]
    )
    asyncio.create_task(_fire_template_attach_on_fill(
        parent_row_id=snap["id"],
        parent_account=snap["account"],
        parent_symbol=snap["symbol"],
        parent_exchange=snap["exchange"],
        parent_side=snap["transaction_type"],
        parent_qty=attach_qty,
        fill_price=float(final_price),
        template_id=int(snap["template_id"]),
        parent_product=snap["product"],
    ))


def _chase_terminal_fire_fill_hooks(
    row_snap: dict,
    outcome: str,
    final_price: float | None,
) -> None:
    """Fire auto-TP shim and/or template attach task on chase_fill.

    Only runs when outcome == 'chase_fill' and a filled row_snap is
    available. Both hooks are gated on parent_order_id IS NULL to
    prevent TP-of-TP chains. This function is synchronous — it only
    calls asyncio.create_task(); no awaits.
    """
    if outcome != "chase_fill" or not final_price or row_snap is None:
        return
    try:
        _ch_maybe_fire_auto_tp(row_snap, final_price)
        _ch_maybe_fire_template_attach(row_snap, final_price)
    except Exception as _tp_e:
        logger.debug(f"_emit_chase_terminal TP arm failed: {_tp_e}")


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
        from backend.api.algo.agent_engine import record_chase_terminal  # circular: lazy OK

        # Audit fix — snapshot the fields the downstream Auto-TP + template
        # attach paths need BEFORE commit so we don't have to re-open a
        # second session to re-fetch the same row. Halves the DB
        # round-trips on every chase fill event.
        agent_id, _row_snap = await _chase_terminal_update_db(
            algo_order_id, broker_order_id, outcome, attempts, final_price, error
        )

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
        # TP-of-TP chains).
        _chase_terminal_fire_fill_hooks(_row_snap, outcome, final_price)

    except Exception as _e:
        logger.debug(f"_emit_chase_terminal: {_e}")

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chase")

# Maximum consecutive broker errors before a chase loop is aborted and an
# alert is fired.  Distinct from `max_attempts` (the unfilled cap): this
# counts Kite SDK exceptions, not quote-crossing retries.
# 3 consecutive broker errors abort the chase before Kite's rate-limit
# cool-off (30s per _RATE_LIMIT_COOLOFF_SECONDS in registry.py) would
# extend the retry window indefinitely against a broken session.
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
# 1 h — operator session length cap; a killed order that "comes back"
# after 60 min was almost certainly a fresh placement on a new session,
# not a resurrection of the killed one. Prevents stale kill flags from
# surviving across trading days.
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
    intent: str | None = None
    # forwarded to broker.place_order so the adapter ceiling in kite.py
    # treats close orders correctly (intent="close" bypasses the 50-lot cap).


def _get_broker(account: str):
    """Return the `Broker` adapter for `account`. Routes through the
    registry so the chase engine doesn't know or care whether the
    backing broker is Kite, Groww, Dhan, or anything we add later —
    every method called below is on the `Broker` ABC."""
    return _get_broker_registry(account)


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


def _ch_snap_sell_price(mid: float, spread: float, aggression: float,
                        tick: float, best_bid: float) -> float:
    """Compute tick-snapped SELL limit price: mid → best_bid as aggression grows."""
    return max(_snap_to_tick(mid - spread * aggression * 0.5, tick), best_bid)


def _ch_snap_buy_price(mid: float, spread: float, aggression: float,
                       tick: float, best_ask: float) -> float:
    """Compute tick-snapped BUY limit price: mid → best_ask as aggression grows."""
    return min(_snap_to_tick(mid + spread * aggression * 0.5, tick), best_ask)


def _ch_extract_best_bid_ask(depth: dict) -> "tuple[float, float]":
    """Extract best bid and ask from depth dict. Returns (0, 0) on missing / zero price."""
    buy_side  = depth.get("buy",  [])
    sell_side = depth.get("sell", [])
    bid = buy_side[0]["price"]  if buy_side  and buy_side[0]["price"]  > 0 else 0.0
    ask = sell_side[0]["price"] if sell_side and sell_side[0]["price"] > 0 else 0.0
    return bid, ask


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
    best_bid, best_ask = _ch_extract_best_bid_ask(depth)

    if best_bid == 0 or best_ask == 0:
        return best_bid or best_ask or 0

    spread     = best_ask - best_bid
    mid        = (best_bid + best_ask) / 2
    aggression = min(attempt * aggression_step, 0.95)
    tick       = _tick_size_sync(exchange, symbol) if exchange and symbol else 0.05

    if transaction_type == "SELL":
        return _ch_snap_sell_price(mid, spread, aggression, tick, best_bid)
    return _ch_snap_buy_price(mid, spread, aggression, tick, best_ask)


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
    """Return (lot_size, tick_size) for a contract in O(1).

    On cache miss:
      - MCX/NCO: returns (0, 0.05). lot_size=0 is the "unknown" sentinel;
        callers (especially to_kite_qty via normalise_qty) must check for
        0 and refuse to place the order rather than dividing by 1 and
        sending raw_qty as LOTS — the root cause of the CRUDEOIL 100×
        oversize incident (lot_size cached as 1 → 100 ÷ 1 = 100 LOTS).
      - All other exchanges: returns (1, 0.05) — the NSE default; no
        translation is applied in to_kite_qty for non-MCX symbols, so
        lot_size=1 is a safe no-op there.
    """
    _mcx = exchange in ("MCX", "NCO")
    _miss = (0 if _mcx else 1, 0.05)
    try:
        entry = _cache_store.get("instruments")
        if entry is None:
            return _miss
        _expires, resp = entry
        if resp is None or not hasattr(resp, "items"):
            return _miss
        _ensure_instrument_index(resp)
        return _INSTRUMENT_INDEX.get((exchange, symbol), _miss)
    except Exception:
        return _miss


def _lot_size_sync(exchange: str, symbol: str) -> int:
    """O(1) lot_size lookup. See `_instrument_specs` for cache shape.

    Returns 0 for MCX/NCO on a cache miss — the sentinel that tells
    normalise_qty (→ to_kite_qty) to raise rather than silently divide
    by 1 and send raw_qty as lots (100× oversize on CRUDEOIL).
    Returns 1 for non-MCX misses (safe no-op — to_kite_qty doesn't
    translate non-MCX symbols)."""
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
        intent=cfg.intent,
    )
    return str(order_id)


def _cancel_order(account: str, order_id: str, variety: str = "regular",
                  exchange: str = ""):
    """Cancel an open order.
    Slice Q — `exchange` is forwarded to broker.cancel_order so Groww's
    segment resolver uses the correct segment instead of defaulting to NSE
    and silently failing for MCX/NFO cancels."""
    broker = _get_broker(account)
    if exchange:
        broker.cancel_order(order_id, variety=variety, exchange=exchange)
    else:
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
                              new_broker_order_id: str,
                              current_limit: float | None = None,
                              interval_seconds: int | None = None) -> None:
    """Update AlgoOrder.broker_order_id to the latest one the chase
    just placed. Best-effort — never raises. Phase 0.5 — without this
    every chase cancel-and-replace orphaned the row from its broker
    id, so the terminal lookup in _emit_chase_terminal (and the
    postback handler) couldn't match the row by broker_order_id.

    Audit fix (M-6) — also writes `current_limit` so the chase panel
    shows the LIVE re-quoted limit price instead of the FIRST
    attempt's initial_price. Pre-fix the UI rendered initial_price
    on every chase row regardless of how many cancel-and-replaces
    had moved the broker order's limit; after 3+ iterations the
    operator saw a stale entry price.

    Chase timing fix — writes `last_attempt_at` (epoch seconds, now())
    and `next_attempt_at` (last_attempt_at + interval_seconds) on
    every cancel-and-replace so the chase panel can display a live
    countdown to the next re-quote. Without these columns the UI had
    no way to distinguish "active" from "stalled" chases mid-flight.
    `interval_seconds` is forwarded from ChaseConfig so the next-attempt
    display matches the actual configured cadence.
    """
    if algo_order_id is None or not new_broker_order_id:
        return
    try:
        async with _async_session() as _s:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
            )).scalar_one_or_none()
            if row is not None:
                _dirty = False
                if row.broker_order_id != str(new_broker_order_id):
                    row.broker_order_id = str(new_broker_order_id)
                    _dirty = True
                if current_limit is not None and float(current_limit) > 0:
                    if row.current_limit != float(current_limit):
                        row.current_limit = float(current_limit)
                        _dirty = True
                # Write timing fields so the chase panel shows a live
                # countdown. Both columns are nullable; skip on models
                # that predate the migration (hasattr guard).
                _now = _time.time()
                if hasattr(row, "last_attempt_at"):
                    row.last_attempt_at = _now
                    _dirty = True
                if hasattr(row, "next_attempt_at") and interval_seconds is not None:
                    row.next_attempt_at = _now + float(interval_seconds)
                    _dirty = True
                if hasattr(row, "interval_seconds") and interval_seconds is not None:
                    row.interval_seconds = int(interval_seconds)
                    _dirty = True
                if _dirty:
                    await _s.commit()
    except Exception as _e:
        logger.debug(f"_sync_algo_order_id failed: {_e}")


def _ch_compute_new_filled(prior_filled: int, cumulative_filled: int,
                           total_qty: int) -> int:
    """MAX-clamp cumulative broker fill count: monotonic + bounded by total."""
    new_filled = max(prior_filled, int(cumulative_filled))
    # Defensive cap: prevent template attach from over-sizing exit GTTs.
    return min(new_filled, int(total_qty))


def _ch_partial_fill_no_change(
    new_filled: int, prior_filled: int,
    new_avg_price: "float | None", prior_fill_price: float,
) -> bool:
    """Return True when both qty and price are unchanged — skip the write."""
    price_unchanged = (
        new_avg_price is None or abs(new_avg_price - prior_fill_price) < 0.001
    )
    return new_filled == prior_filled and price_unchanged


async def _ch_write_partial_fill_row(
    algo_order_id: int, cumulative_filled: int, avg_price: float, total_qty: int,
) -> None:
    """DB write for one partial-fill update (session body extracted from _record_partial_fill)."""
    async with _async_session() as _s:
        row = (await _s.execute(
            _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
        )).scalar_one_or_none()
        if row is None:
            return
        prior_filled     = int(row.filled_quantity or 0)
        new_filled       = _ch_compute_new_filled(prior_filled, cumulative_filled, total_qty)
        new_avg_price    = float(avg_price) if avg_price and float(avg_price) > 0 else None
        prior_fill_price = float(row.fill_price or 0)
        if _ch_partial_fill_no_change(new_filled, prior_filled, new_avg_price, prior_fill_price):
            return
        row.filled_quantity = new_filled
        if new_avg_price is not None:
            row.fill_price = new_avg_price
        new_avg_display = float(row.fill_price or 0)
        row.detail = (
            f"PARTIAL {new_filled}/{total_qty} @ ₹{new_avg_display:,.2f} "
            f"(chasing residual {int(total_qty) - new_filled})"
        )[:240]
        await _s.commit()


async def _record_partial_fill(algo_order_id: int | None,
                                cumulative_filled: int,
                                avg_price: float,
                                total_qty: int) -> None:
    """Sprint B (#4) — persist partial-fill state on the AlgoOrder row.

    Audit fix (C-1): the `cumulative_filled` argument is the BROKER'S
    cumulative `filled_quantity` from its status poll — NOT a per-call
    delta. Kite reports cumulative (so does Dhan + Groww post-status-
    map). Pre-fix this function added the cumulative value to the
    prior DB value every call, so a chase recovered from DB after a
    restart would inflate `filled_quantity` past `total_qty` (prior
    30 + cumulative 30 = 60 even though only 30 actually traded). The
    template-attach path then sized exit GTTs against the inflated
    value, over-hedging.

    `avg_price` is the broker's running cumulative average fill price
    (most SDKs return cumulative average, not delta-weighted average).
    We store it directly — the broker's number is authoritative.

    Accumulates `filled_quantity` across partials so:
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
    if algo_order_id is None or cumulative_filled <= 0:
        return
    try:
        await _ch_write_partial_fill_row(
            algo_order_id, cumulative_filled, avg_price, total_qty,
        )
    except Exception as _e:
        logger.debug(f"_record_partial_fill failed: {_e}")


# ── Poll-status terminal handlers (extracted to reduce CC) ──────────────

async def _ch_poll_handle_complete(
    result: "ChaseResult", avg_price: float, filled_qty: int,
    quantity: int, attempt: int, remaining_qty: int,
    current_order_id: str, symbol: str, transaction_type: str,
    algo_order_id: "int | None", emit: Callable,
) -> "tuple[str, int]":
    """Handle COMPLETE status: update result + schedule terminal event."""
    result.status    = ChaseStatus.FILLED
    result.fill_price = avg_price
    result.slippage   = abs(avg_price - result.initial_price) * (filled_qty or quantity)
    result.detail     = f"Filled at {avg_price} in {attempt} attempts"
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
        slippage=result.slippage, algo_order_id=algo_order_id,
    ))
    return "filled", remaining_qty


def _ch_poll_handle_rejected(
    result: "ChaseResult", status: dict, attempt: int, remaining_qty: int,
    account: str, symbol: str, transaction_type: str, quantity: int,
    current_order_id: str, cfg: "ChaseConfig",
    algo_order_id: "int | None", emit: Callable,
) -> "tuple[str, int]":
    """Handle REJECTED status: log, alert, schedule terminal event."""
    status_msg = status.get("status_message", "") or "rejected by broker"
    abort_msg  = f"Order rejected by broker: {status_msg}"
    logger.error(f"Chase {symbol}: REJECTED — {status_msg}. Aborting chase.")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {
        "attempts": attempt, "error": abort_msg,
        "reason": "broker_rejected", "status_message": status_msg,
    })
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            error=abort_msg,
        )
    except Exception:
        pass
    import asyncio as _asyncio
    _asyncio.create_task(_emit_chase_terminal(
        current_order_id, "chase_failed",
        symbol, transaction_type, quantity,
        attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
    ))
    return "rejected", remaining_qty


def _ch_poll_handle_cancelled(
    result: "ChaseResult", status: dict, attempt: int, remaining_qty: int,
    current_order_id: str, symbol: str, transaction_type: str, quantity: int,
    algo_order_id: "int | None", emit: Callable,
) -> "tuple[str, int]":
    """Handle CANCELLED status: distinguish operator-kill from broker auto-cancel."""
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
        import asyncio as _asyncio
        _asyncio.create_task(_emit_chase_terminal(
            current_order_id, "chase_cancelled",
            symbol, transaction_type, quantity,
            attempts=attempt, algo_order_id=algo_order_id,
        ))
        return "killed", remaining_qty
    # Non-operator cancel (broker auto-cancel, circuit, session-end, Kite-app).
    return "cancelled_continue", remaining_qty


async def _chase_poll_status(
    account: str,
    current_order_id: str,
    cfg: ChaseConfig,
    symbol: str,
    transaction_type: str,
    quantity: int,
    result: ChaseResult,
    attempt: int,
    remaining_qty: int,
    algo_order_id: int | None,
    emit: Callable,
) -> tuple[str | None, int]:
    """Check order status after the per-attempt sleep. Mutates result in-place.

    Returns (signal, new_remaining_qty) where signal is one of:
      'filled'            — order fully complete; caller should return result
      'killed'            — operator cancelled; caller should return result
      'rejected'          — broker rejected; caller should return result
      'cancelled_continue'— broker/external cancel, NOT operator; caller resets
                            current_order_id and sleeps backoff before continuing
      None                — partial fill or no notable status; caller continues loop

    The consecutive_errors counter is NOT managed here — it lives in
    chase_order's outer except block so broker exceptions (raised before
    this helper is called) are still caught correctly.
    """
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
        from backend.brokers.adapters.kite import from_kite_qty
        _lot = _lot_size_sync(cfg.exchange, symbol)
        filled_qty = from_kite_qty(cfg.exchange, _kite_filled, _lot)
    else:
        filled_qty = _kite_filled
    avg_price = status.get("average_price", 0)

    if order_status == "COMPLETE":
        return await _ch_poll_handle_complete(
            result, avg_price, filled_qty, quantity, attempt, remaining_qty,
            current_order_id, symbol, transaction_type, algo_order_id, emit,
        )

    # Audit fix (C-1): partial fill — react to NEW fills since last poll.
    _already_filled = quantity - remaining_qty
    _new_delta = filled_qty - _already_filled
    if filled_qty > 0 and _new_delta > 0 and filled_qty < quantity:
        remaining_qty = max(0, quantity - filled_qty)
        result.status = ChaseStatus.PARTIAL
        logger.info(
            f"Chase {symbol}: partial fill +{_new_delta} "
            f"(total {filled_qty}/{quantity}, remaining {remaining_qty})"
        )
        emit("partial_fill", {"filled": filled_qty, "delta": _new_delta,
                              "remaining": remaining_qty})
        await _record_partial_fill(algo_order_id, filled_qty, avg_price, quantity)

    if order_status == "REJECTED":
        return _ch_poll_handle_rejected(
            result, status, attempt, remaining_qty,
            account, symbol, transaction_type, quantity,
            current_order_id, cfg, algo_order_id, emit,
        )

    if order_status == "CANCELLED":
        return _ch_poll_handle_cancelled(
            result, status, attempt, remaining_qty,
            current_order_id, symbol, transaction_type, quantity,
            algo_order_id, emit,
        )

    # No terminal status — partial fill or benign (e.g. OPEN/TRIGGER_PENDING).
    return None, remaining_qty


def _chase_default_cfg() -> "ChaseConfig":
    """Build a ChaseConfig from /admin/settings (DB → YAML fallback).

    Extracted from chase_order to keep that function's branch count low.
    Lazy-imports settings helpers to avoid circular imports at module load.
    """
    from backend.shared.helpers.settings import get_float, get_int
    return ChaseConfig(
        interval_seconds=get_int("algo.chase_interval_seconds", 20),
        aggression_step=get_float("algo.aggression_step", 0.10),
        max_attempts=get_int("algo.max_attempts", 20),
        rejection_backoff_seconds=get_int(
            "algo.chase_rejection_backoff_seconds", 0),
    )


async def _chase_abort_on_consecutive_errors(
    consecutive_errors: int,
    attempt: int,
    last_error: Exception,
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    current_order_id: str | None,
    cfg: "ChaseConfig",
    result: "ChaseResult",
    emit: Callable,
    algo_order_id: int | None,
) -> "ChaseResult":
    """Handle consecutive-error abort in the chase loop.

    Fires an alert, cancels the live order (best-effort), schedules the
    terminal event task, and returns the mutated result ready for `return`.
    Only called when `consecutive_errors >= _MAX_CHASE_ERRORS`.
    """
    abort_msg = (
        f"Chase abandoned after {consecutive_errors} consecutive errors "
        f"({attempt} total attempts) — last error: {last_error}"
    )
    logger.error(f"Chase {symbol}: {abort_msg}")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {"attempts": attempt, "error": str(last_error),
                          "reason": "consecutive_errors"})
    try:
        if current_order_id:
            await _run(_cancel_order, account, current_order_id, cfg.variety,
                       cfg.exchange)
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


# ── chase_order inner-loop helpers (extracted to reduce CC) ─────────────

async def _ch_handle_poll_signal(
    signal: "str | None",
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    symbol: str,
) -> "tuple[bool, str | None]":
    """Interpret the poll signal and apply the cancelled_continue backoff if needed.

    Returns (done, new_current_order_id).
    done=True means the caller should immediately return result (terminal outcome).
    """
    if signal in ("filled", "killed", "rejected"):
        return True, current_order_id
    if signal == "cancelled_continue":
        backoff = cfg.rejection_backoff_seconds or cfg.interval_seconds
        logger.info(f"Chase {symbol}: backing off {backoff}s before next place_order")
        await asyncio.sleep(backoff)
        return False, None   # reset current_order_id
    return False, current_order_id


async def _ch_handle_attempt_error(
    exc: Exception,
    consecutive_errors: int,
    attempt: int,
    symbol: str,
    account: str,
    transaction_type: str,
    quantity: int,
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    result: "ChaseResult",
    emit: Callable,
    algo_order_id: "int | None",
) -> "tuple[ChaseResult | None, int]":
    """Handle a broker exception in the chase loop.

    Returns (abort_result, new_consecutive_errors).
    abort_result is non-None when the caller should immediately return it.
    """
    consecutive_errors += 1
    logger.error(
        f"Chase {symbol}: attempt {attempt} error "
        f"({consecutive_errors}/{_MAX_CHASE_ERRORS} consecutive): {exc}"
    )
    emit("error", {"attempt": attempt, "error": str(exc)})
    if consecutive_errors >= _MAX_CHASE_ERRORS:
        abort = await _chase_abort_on_consecutive_errors(
            consecutive_errors, attempt, exc,
            account, symbol, transaction_type, quantity,
            current_order_id, cfg, result, emit, algo_order_id,
        )
        return abort, consecutive_errors
    await asyncio.sleep(cfg.interval_seconds)
    return None, consecutive_errors


async def _ch_exhaust_max_attempts(
    result: "ChaseResult",
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    account: str, symbol: str, transaction_type: str,
    quantity: int, algo_order_id: "int | None", emit: Callable,
) -> "ChaseResult":
    """Handle max-attempts exhaustion: cancel live order, set FAILED, schedule terminal."""
    if current_order_id:
        try:
            await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
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
            attempts=cfg.max_attempts, algo_order_id=algo_order_id,
        ))
    return result


def _ch_make_emit(
    on_event: "Callable | None",
    account: str, symbol: str, transaction_type: str, quantity: int,
) -> Callable:
    """Return a fire-and-forget event emitter bound to the current chase context."""
    def emit(event_type: str, detail: dict = None) -> None:
        if on_event:
            try:
                on_event(event_type, {
                    "account": account, "symbol": symbol,
                    "transaction_type": transaction_type,
                    "quantity": quantity, **(detail or {}),
                })
            except Exception:
                pass
    return emit


async def _ch_cancel_previous(
    account: str, current_order_id: "str | None",
    cfg: "ChaseConfig", symbol: str, attempt: int, emit: Callable,
) -> None:
    """Cancel the previous broker order (best-effort, never raises)."""
    if not current_order_id:
        return
    try:
        await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
        emit("order_cancelled", {"order_id": current_order_id, "attempt": attempt})
    except Exception as e:
        logger.warning(f"Chase {symbol}: cancel failed: {e}")


async def _ch_post_replace_kill_check(
    account: str, current_order_id: str,
    cfg: "ChaseConfig", result: "ChaseResult",
    symbol: str, transaction_type: str, quantity: int,
    attempt: int, algo_order_id: "int | None", emit: Callable,
) -> bool:
    """Check and handle operator-kill race immediately after place_order.

    Returns True when the kill was detected and the caller should
    `return result`.  Returns False (normal path) when no kill is pending.
    """
    if not is_killed(current_order_id):
        return False
    logger.info(
        f"Chase {symbol}: operator-kill detected for new "
        f"broker_order_id={current_order_id} immediately "
        f"after replace. Cancelling + terminating."
    )
    try:
        await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
    except Exception as _ke:
        logger.warning(f"Chase {symbol}: post-replace kill cancel failed: {_ke}")
    result.status = ChaseStatus.CANCELLED
    result.detail = "Killed by operator (post-replace race)"
    emit("order_cancelled", {
        "order_id": current_order_id, "attempt": attempt,
        "reason": "operator-kill-post-replace",
    })
    import asyncio as _asyncio
    _asyncio.create_task(_emit_chase_terminal(
        current_order_id, "chase_cancelled",
        symbol, transaction_type, quantity,
        attempts=attempt, algo_order_id=algo_order_id,
        error="Killed by operator",
    ))
    return True


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
        cfg = _chase_default_cfg()

    result = ChaseResult(
        account=account, symbol=symbol,
        transaction_type=transaction_type, quantity=quantity,
    )

    emit = _ch_make_emit(on_event, account, symbol, transaction_type, quantity)
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

            # Cancel previous order (best-effort, never raises)
            await _ch_cancel_previous(account, current_order_id, cfg, symbol, attempt, emit)

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
            await _sync_algo_order_id(
                algo_order_id, current_order_id,
                current_limit=price,
                interval_seconds=cfg.interval_seconds,
            )

            # Audit fix (C-2) — operator-kill race: re-check the NEW id
            # immediately after place_order so a kill in the replace window
            # takes effect before the sleep, not one iteration later.
            if await _ch_post_replace_kill_check(
                account, current_order_id, cfg, result,
                symbol, transaction_type, quantity, attempt, algo_order_id, emit,
            ):
                return result

            # Wait for fill
            await asyncio.sleep(cfg.interval_seconds)

            signal, remaining_qty = await _chase_poll_status(
                account, current_order_id, cfg, symbol, transaction_type,
                quantity, result, attempt, remaining_qty, algo_order_id, emit,
            )
            done, current_order_id = await _ch_handle_poll_signal(
                signal, current_order_id, cfg, symbol,
            )
            if done:
                return result

        except Exception as e:
            abort, consecutive_errors = await _ch_handle_attempt_error(
                e, consecutive_errors, attempt, symbol,
                account, transaction_type, quantity, current_order_id,
                cfg, result, emit, algo_order_id,
            )
            if abort is not None:
                return abort

    return await _ch_exhaust_max_attempts(
        result, current_order_id, cfg,
        account, symbol, transaction_type, quantity, algo_order_id, emit,
    )
