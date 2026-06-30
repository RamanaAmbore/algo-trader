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

        # Map chase outcome → AlgoOrder.status.
        _OUTCOME_TO_STATUS = {
            "chase_fill":      "FILLED",
            "chase_unfilled":  "UNFILLED",
            "chase_cancelled": "CANCELLED",
            "chase_failed":    "REJECTED",
        }
        _new_status = _OUTCOME_TO_STATUS.get(outcome)

        agent_id = None
        # Audit fix — snapshot the fields the downstream Auto-TP + template
        # attach paths need (target_pct, target_abs, parent_order_id,
        # template_id, mode, product, exchange, transaction_type, account,
        # symbol, id, filled_quantity, quantity) BEFORE commit so we don't
        # have to re-open a second session to re-fetch the same row at
        # line ~120. Halves the DB round-trips on every chase fill event.
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
                # Snapshot AFTER the optional mutation + commit so the
                # downstream attach paths read post-commit values.
                _row_snap = {
                    "id":                int(row.id),
                    "target_pct":        row.target_pct,
                    "target_abs":        row.target_abs,
                    "parent_order_id":   row.parent_order_id,
                    "template_id":       row.template_id,
                    "account":           str(row.account or ""),
                    "symbol":            str(row.symbol or symbol),
                    "exchange":          str(row.exchange or "NFO"),
                    "transaction_type":  str(row.transaction_type or side),
                    "product":           str(row.product or "NRML"),
                    "mode":              str(row.mode or "live"),
                    "filled_quantity":   int(row.filled_quantity or 0),
                    "quantity":          int(row.quantity or qty),
                }

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
        # Audit fix — uses the pre-commit snapshot above instead of
        # re-fetching the same row in a second session. Pre-fix the
        # function paid two full DB round-trips per chase fill event;
        # now it pays one. The downstream `_fire_template_attach_on_fill`
        # opens its own session to read the row state independently.
        if outcome == "chase_fill" and final_price and _row_snap is not None:
            try:
                _filled_snap = _row_snap
                # Phase 3D #6 — gate the legacy single-target TP shim on
                # template_id IS NULL so a row with BOTH legacy
                # target_pct AND a template doesn't attach exits twice.
                if (_filled_snap.get("target_pct") or _filled_snap.get("target_abs")) \
                        and _filled_snap.get("parent_order_id") is None \
                        and _filled_snap.get("template_id") is None:
                    from backend.api.routes.orders import _arm_take_profit
                    asyncio.create_task(_arm_take_profit(
                        parent_row_id=_filled_snap["id"],
                        parent_account=_filled_snap["account"],
                        parent_symbol=_filled_snap["symbol"],
                        parent_exchange=_filled_snap["exchange"],
                        parent_side=_filled_snap["transaction_type"],
                        fill_price=float(final_price),
                        target_pct=float(_filled_snap.get("target_pct") or 0.0),
                        target_abs=(_filled_snap.get("target_abs")
                                    and float(_filled_snap.get("target_abs"))),
                        parent_mode=_filled_snap["mode"],
                    ))
                # Phase 0.5 — template attach on chase fill. Same
                # idempotency guard (attached_gtts_json populated →
                # skip) lives inside _fire_template_attach_on_fill,
                # so a race against the postback hook is safe.
                # Audit fix — drop the `mode == "live"` guard. The
                # paper engine has its own attach path
                # (`_update_algo_order`), but if a paper-mode row is
                # ever chased directly via `chase_order()` (e.g.
                # after a `recover_from_db` that re-hydrated mode),
                # the gate silently dropped the attach. The downstream
                # `apply_template_to_order(apply_path="live")` is the
                # right call for both modes per Sprint A intent.
                if _filled_snap.get("template_id") \
                        and _filled_snap.get("parent_order_id") is None:
                    from backend.api.routes.orders import (
                        _fire_template_attach_on_fill,
                    )
                    # Sprint B (#4) — size exit GTTs against the
                    # ACTUAL filled qty when the chase took partials.
                    _attach_qty = (
                        _filled_snap["filled_quantity"]
                        if _filled_snap["filled_quantity"] > 0
                        else _filled_snap["quantity"]
                    )
                    asyncio.create_task(_fire_template_attach_on_fill(
                        parent_row_id=_filled_snap["id"],
                        parent_account=_filled_snap["account"],
                        parent_symbol=_filled_snap["symbol"],
                        parent_exchange=_filled_snap["exchange"],
                        parent_side=_filled_snap["transaction_type"],
                        parent_qty=_attach_qty,
                        fill_price=float(final_price),
                        template_id=int(_filled_snap["template_id"]),
                        parent_product=_filled_snap["product"],
                    ))
            except Exception as _tp_e:
                logger.debug(f"_emit_chase_terminal TP arm failed: {_tp_e}")

    except Exception as _e:
        logger.debug(f"_emit_chase_terminal: {_e}")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chase")

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
                              current_limit: float | None = None) -> None:
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
                if _dirty:
                    await _s.commit()
    except Exception as _e:
        logger.debug(f"_sync_algo_order_id failed: {_e}")


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
        async with _async_session() as _s:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
            )).scalar_one_or_none()
            if row is None:
                return
            # `cumulative_filled` is monotonic — it can only grow as
            # the broker fills more. Take MAX against the prior DB
            # value so a late status poll that for any reason returns
            # a lower cumulative (rare, but seen on Kite during book
            # transitions) doesn't roll the row backwards.
            prior_filled = int(row.filled_quantity or 0)
            new_filled = max(prior_filled, int(cumulative_filled))
            if new_filled > int(total_qty):
                # Defensive clamp — should never happen but better to
                # cap at the order's total qty than write an inflated
                # value that template attach would then over-size GTTs
                # against.
                new_filled = int(total_qty)
            # No-op guard: skip the write when the cumulative value and
            # avg_price haven't changed — consecutive polls that return
            # the same broker state produce a redundant commit otherwise.
            new_avg_price = float(avg_price) if avg_price and float(avg_price) > 0 else None
            prior_fill_price = float(row.fill_price or 0)
            price_unchanged = (new_avg_price is None or abs(new_avg_price - prior_fill_price) < 0.001)
            if new_filled == prior_filled and price_unchanged:
                return
            row.filled_quantity = new_filled
            # Broker's cumulative average fill_price is authoritative.
            # Only overwrite when the broker reports a positive value
            # (zero would clobber a prior valid average).
            if new_avg_price is not None:
                row.fill_price = new_avg_price
            new_avg_display = float(row.fill_price or 0)
            row.detail = (
                f"PARTIAL {new_filled}/{total_qty} @ ₹{new_avg_display:,.2f} "
                f"(chasing residual {int(total_qty) - new_filled})"
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
                    await _run(_cancel_order, account, current_order_id, cfg.variety,
                               cfg.exchange)
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
            await _sync_algo_order_id(algo_order_id, current_order_id,
                                       current_limit=price)

            # Audit fix (C-2) — operator-kill race protection. The kill
            # path calls `mark_killed(broker_order_id)` synchronously,
            # but the chase loop's cancel-and-replace creates a NEW
            # broker_order_id between the old (which the operator's
            # kill recorded against) and the next iteration. Without
            # this check, an operator clicking Kill in the window
            # between (cancel-old) and (place-new) would have their
            # kill recorded against the now-vanished old id; the new
            # order would never see is_killed=True for ITS id and the
            # chase would silently run to completion. We re-check the
            # NEW id immediately after placement so the kill takes
            # effect on the very next iteration.
            if is_killed(current_order_id):
                logger.info(
                    f"Chase {symbol}: operator-kill detected for new "
                    f"broker_order_id={current_order_id} immediately "
                    f"after replace. Cancelling + terminating."
                )
                try:
                    await _run(_cancel_order, account, current_order_id, cfg.variety,
                               cfg.exchange)
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
                    attempts=attempt,
                    algo_order_id=algo_order_id,
                    error="Killed by operator",
                ))
                return result

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
                from backend.brokers.adapters.kite import from_kite_qty
                _lot = _lot_size_sync(cfg.exchange, symbol)
                filled_qty = from_kite_qty(cfg.exchange, _kite_filled, _lot)
            else:
                filled_qty = _kite_filled
            avg_price = status.get("average_price", 0)

            if order_status == "COMPLETE":
                result.status = ChaseStatus.FILLED
                result.fill_price = avg_price
                # Slippage uses the ACTUAL filled qty, not the original
                # ask. Pre-fix the formula `abs(diff) * quantity` over-
                # stated slippage on partial fills by quantity/filled_qty
                # — operator audit trail showed a misleadingly high
                # number that didn't match what actually traded.
                result.slippage = abs(avg_price - result.initial_price) * (filled_qty or quantity)
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

            # Audit fix (C-1): `filled_qty` is BROKER-CUMULATIVE (not
            # per-poll delta). Compute the delta vs the in-memory
            # `quantity - remaining_qty` so we only react to NEW fills.
            # Pre-fix the partial branch only fired ONCE (the first
            # poll where filled_qty > 0); subsequent partials were
            # silently dropped because `filled_qty < remaining_qty`
            # failed after the first decrement. _record_partial_fill
            # is now idempotent + cumulative-aware so we can call it
            # every poll cycle.
            _already_filled = quantity - remaining_qty
            _new_delta = filled_qty - _already_filled
            if filled_qty > 0 and _new_delta > 0 and filled_qty < quantity:
                # New partial since the last poll — chase the residual.
                remaining_qty = max(0, quantity - filled_qty)
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
                logger.info(
                    f"Chase {symbol}: partial fill +{_new_delta} "
                    f"(total {filled_qty}/{quantity}, remaining {remaining_qty})"
                )
                emit("partial_fill", {
                    "filled": filled_qty,
                    "delta": _new_delta,
                    "remaining": remaining_qty,
                })
                # _record_partial_fill MAX-clamps against the prior DB
                # value, so out-of-order polls or restarts can't roll
                # the row backwards or inflate it past `quantity`.
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
            await asyncio.sleep(cfg.interval_seconds)

    # Max attempts exhausted
    if current_order_id:
        try:
            await _run(_cancel_order, account, current_order_id, cfg.variety,
                       cfg.exchange)
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
