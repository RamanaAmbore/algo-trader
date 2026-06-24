"""
Orders endpoints.

GET  /api/orders/             — list all orders across all accounts (cached 15s)
POST /api/orders/ticket       — primary order-placement entry point. Routes by
                                mode (paper / live), records an AlgoOrder row,
                                supports chase + chase_aggressiveness. Every
                                frontend surface flows through this endpoint.
POST /api/orders/preflight    — pre-validate an order before it hits the broker.
                                Returns structured blockers (MARGIN_SHORTFALL,
                                SEGMENT_INACTIVE, QTY_FREEZE, ACCOUNT_UNKNOWN)
                                with actionable fix text. Does not place an order.
POST /api/orders/place        — DEPRECATED. Direct-broker placement, kept for
                                external scripts only. New integrations must
                                use /ticket. Hits log a deprecation warning.
PUT  /api/orders/{order_id}   — modify an open order
DELETE /api/orders/{order_id} — cancel an open order
POST /api/orders/postback     — Kite postback: real-time order status updates
GET  /api/accounts/           — list accounts (masked display + unmasked ID for order form)
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone

from typing import Optional

import msgspec
import pandas as pd
from litestar import Controller, Request, delete, get, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK

from backend.api.auth_guard import jwt_guard, auth_or_demo_guard, admin_guard, is_admin_request, is_authenticated_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.routes.ws import broadcast
from backend.api.schemas import (
    AccountInfo,
    AccountsResponse,
    BasketGroup,
    BasketGroupResult,
    BasketLegResult,
    BasketMarginGroupResult,
    BasketMarginResponse,
    BasketOrderRequest,
    BasketOrderResponse,
    CancelOrderResponse,
    ModifyOrderRequest,
    ModifyOrderResponse,
    OrderRow,
    OrdersResponse,
    PlaceOrderRequest,
    PlaceOrderResponse,
    ReconcileSingleRequest,
    TicketOrderRequest,
    TicketOrderResponse,
    TicketPreviewRequest,
    TicketPreviewResponse,
)
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_column, secrets

logger = get_logger(__name__)

_VARIETIES   = {"regular", "amo", "co"}
_ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
_PRODUCTS    = {"CNC", "MIS", "NRML"}
_TXN_TYPES   = {"BUY", "SELL"}
_EXCHANGES   = {"NSE", "BSE", "NFO", "CDS", "MCX", "BFO"}
_VALIDITIES  = {"DAY", "IOC"}

_ORDERS_TTL  = 15   # orders refresh faster — 15s cache

# ── Live-order circuit breaker ────────────────────────────────────────
# Stop the operator (or an agent) from re-attempting the same rejected
# order again and again. Track rejection timestamps per
# (account, symbol, side, qty) tuple. After REJECTION_THRESHOLD
# rejections in the last REJECTION_WINDOW_S seconds, the next attempt
# returns 423 (Locked) without hitting the broker, and a Telegram +
# email alert fires so the operator knows the breaker tripped.
#
# Module-level state — lost on service restart. That's fine; the
# operator's debug session rarely exceeds a single working day.
import time as _time

_REJECTION_TRACKER: dict[str, list[float]] = {}
_REJECTION_WINDOW_S = 3600       # 1 hour
_REJECTION_THRESHOLD = 3
_BREAKER_ALERT_COOLDOWN_S = 600  # 10 min between breaker-trip alerts per key
_BREAKER_LAST_ALERT: dict[str, float] = {}


def _rejection_key(account: str, symbol: str, side: str, qty: int) -> str:
    return f"{account}|{symbol}|{side}|{qty}"


def _prune_rejection_window(key: str) -> None:
    now = _time.time()
    cutoff = now - _REJECTION_WINDOW_S
    _REJECTION_TRACKER[key] = [t for t in _REJECTION_TRACKER.get(key, []) if t > cutoff]


def _rejection_count(key: str) -> int:
    _prune_rejection_window(key)
    return len(_REJECTION_TRACKER.get(key, []))


def _record_rejection(key: str) -> int:
    """Append now() to the rejection list for this key, return new count."""
    _prune_rejection_window(key)
    _REJECTION_TRACKER.setdefault(key, []).append(_time.time())
    return len(_REJECTION_TRACKER[key])


def _clear_rejections(key: str) -> None:
    """Reset on a successful placement."""
    _REJECTION_TRACKER.pop(key, None)
    _BREAKER_LAST_ALERT.pop(key, None)


def _maybe_send_breaker_alert(key: str, account: str, symbol: str,
                               side: str, qty: int, reason: str) -> None:
    """Fire one alert per key per cooldown window. Returns silently on
    any send failure so the trip itself isn't blocked by infra issues."""
    now = _time.time()
    last = _BREAKER_LAST_ALERT.get(key, 0)
    if now - last < _BREAKER_ALERT_COOLDOWN_S:
        return
    _BREAKER_LAST_ALERT[key] = now
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange="—",
            side=side, qty=qty, mode="live",
            source="circuit-breaker",
            error=(f"BREAKER TRIPPED — {_REJECTION_THRESHOLD}+ rejections in "
                   f"the last {_REJECTION_WINDOW_S//60} min. "
                   f"Further submits blocked until the breaker resets. "
                   f"Last reason: {reason[:200]}"),
        )
    except Exception as e:
        logger.warning(f"[BREAKER] alert dispatch failed for {key}: {e}")


# Tick-size index — built on first lookup from the instruments cache,
# rebuilt only when the cache version stamp changes. Pre-fix the
# lookup did a linear scan through ~10-50k instrument rows on EVERY
# `_align_price_to_tick` call; the ticket route calls it twice per
# placement (price + trigger_price), so a single order paid ~100k
# linear iterations. Indexed by (exchange, symbol) tuple → O(1).
_TICK_INDEX: dict[tuple[str, str], float] = {}
_TICK_INDEX_STAMP: object | None = None


def _rebuild_tick_index(items) -> None:
    """Rebuild the (exchange, symbol) → tick_size dict from the
    instruments cache. Called once per cache refresh; subsequent
    `_align_price_to_tick` calls hit the dict directly."""
    global _TICK_INDEX
    new_index: dict[tuple[str, str], float] = {}
    for inst in items:
        ts = float(inst.ts or 0)
        if ts > 0:
            new_index[(inst.e.upper(), inst.s.upper())] = ts
    _TICK_INDEX = new_index


async def _align_price_to_tick(exchange: str, symbol: str,
                                price: float | None) -> float | None:
    """Snap *price* to the nearest valid tick for the instrument.

    Kite rejects orders whose LIMIT / trigger price isn't a multiple
    of the contract's `tick_size` with "Exchange invalid price — the
    entered price is not as per ticker price". Operators routinely
    enter ₹9961.50 for a MCX commodity that ticks at ₹1 (whole
    rupees); we round to the nearest valid tick before sending.

    Reads tick_size from the in-process `_TICK_INDEX` (O(1) dict
    lookup, built lazily from the instruments cache). Returns the
    input unchanged when:
      - price is None or 0
      - the instrument isn't in the cache (let Kite reject explicitly)
      - tick_size resolves to a non-positive value (defensive)

    Rounding policy: half-up to the nearest tick, then clamped to the
    same tick grid. For options with tick=0.05, 12.37 → 12.35;
    for commodities with tick=1, 9961.50 → 9962.
    """
    global _TICK_INDEX_STAMP
    if price is None or price == 0:
        return price
    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        # `resp` is the cached InstrumentsResponse object. Identity
        # comparison is enough — get_or_fetch returns the SAME instance
        # while the cache entry is valid, then a new instance on refresh.
        # When the identity flips we rebuild the index.
        if resp is not _TICK_INDEX_STAMP or not _TICK_INDEX:
            _rebuild_tick_index(resp.items if resp else [])
            _TICK_INDEX_STAMP = resp
    except Exception:
        return price
    tick = _TICK_INDEX.get(((exchange or "").upper(), (symbol or "").upper()))
    if not tick or tick <= 0:
        return price
    # Round half-up to the nearest tick. Using integer division on a
    # scaled-up price avoids float-rounding artefacts (0.05 ticks in
    # binary float misbehave).
    scale = round(1.0 / tick) if tick < 1 else 1
    if scale > 1:
        scaled = round(float(price) * scale)
        aligned = scaled / scale
    else:
        aligned = round(float(price) / tick) * tick
    aligned = round(aligned, 4)
    if aligned != price:
        logger.info(f"[TICK] aligned {symbol} price {price} → {aligned} (tick={tick})")
    return aligned


async def _enforce_capacity_guard(
    *,
    strategy_id: int,
    account: str,
    tradingsymbol: str,
    side_kite: str,
    quantity: int,
    price_hint: Optional[float],
) -> None:
    """Pre-trade capacity guard for the strategy attached to this order.

    Raises 403 when (current_open_notional + new_notional) would
    exceed `Strategy.capacity_cap_inr`. Returns silently otherwise.

    Skip semantics:
      - Strategy not found → silent skip. Order placement isn't the
        right surface to surface a stale strategy_id; the operator
        will see the bad attribution on /strategies.
      - capacity_cap_inr is NULL → silent skip (no cap configured).
      - Close intent detected → silent skip (reduces exposure; the
        FIFO match consumes existing lots).

    Pricing for new notional:
      1. price_hint (operator-typed limit / SL price) — preferred,
         matches Kite's accept-validation pricing.
      2. KiteTicker tick_map LTP for the symbol — covers MARKET orders
         when the operator's book has the symbol subscribed.
      3. broker.ltp() one-shot — last-resort batched call (rare, only
         for unsubscribed MARKET orders).
      4. Hard-fail with 503 if no price can be resolved — capacity
         math can't run without a price; refusing here is safer than
         letting an unbounded order through.

    The new-notional formula is intentionally OVER-CONSERVATIVE for
    pyramiding scenarios where part of the order would consume an
    existing lot — qty × price treats every contract as new exposure.
    Trade-off: false positives ("near the cap") in marginal cases,
    no false negatives. Operator can always raise the cap or split
    the order.
    """
    if quantity <= 0:
        return
    from backend.api.database import async_session
    from backend.api.models import Strategy, StrategyLot
    from backend.api.algo.lot_ledger import detect_close_intent
    from sqlalchemy import select as _select, func as _func

    async with async_session() as s:
        strat = await s.get(Strategy, int(strategy_id))
        if strat is None or strat.capacity_cap_inr is None:
            return
        cap = float(strat.capacity_cap_inr)
        if cap <= 0:
            return
        # Close-intent → skip. The FIFO matcher will consume existing
        # lots; net exposure goes DOWN, never up.
        is_close = await detect_close_intent(
            s,
            strategy_id=int(strategy_id),
            account=account,
            symbol=tradingsymbol,
            side_kite=side_kite,
        )
        if is_close:
            return
        # Current open notional = Σ remaining_qty × open_price across
        # open lots for THIS strategy (book-wide; cap is per-strategy,
        # not per-account).
        open_notional = (await s.execute(
            _select(_func.coalesce(
                _func.sum(StrategyLot.remaining_qty * StrategyLot.open_price),
                0.0,
            )).where(
                StrategyLot.strategy_id == int(strategy_id),
                StrategyLot.remaining_qty > 0,
            )
        )).scalar_one() or 0.0
        open_notional = float(open_notional)

    # Resolve new-notional price.
    px: Optional[float] = (float(price_hint) if price_hint and price_hint > 0
                           else None)
    if px is None:
        # Ticker first (zero broker quota).
        try:
            from backend.shared.helpers.kite_ticker import _ticker
            t = _ticker.get_ltp_by_sym(tradingsymbol.upper())
            if t is not None and t > 0:
                px = float(t)
        except Exception:
            pass
    if px is None:
        # Broker fallback. Single batched call; failure → 503 (we
        # cannot risk-check the order without a price).
        try:
            from backend.shared.brokers.registry import get_price_broker
            broker = get_price_broker()
            # Exchange resolution: use NFO as the safe default for F&O
            # symbols; broker.ltp accepts EXCH:SYM keys.
            key = f"NFO:{tradingsymbol.upper()}"
            quote = await asyncio.to_thread(broker.ltp, [key])
            v = (quote or {}).get(key)
            if isinstance(v, dict):
                lp = float(v.get("last_price") or 0.0)
                if lp > 0:
                    px = lp
        except Exception:
            px = None
    if px is None or px <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                "Capacity guard cannot resolve price for "
                f"{tradingsymbol} — pass an explicit limit price or "
                "retry once the ticker has the symbol subscribed."
            ),
        )

    new_notional = float(quantity) * px
    projected = open_notional + new_notional
    if projected > cap:
        breach = projected - cap
        raise HTTPException(
            status_code=403,
            detail=(
                f"Capacity cap breach — strategy cap ₹{cap:,.0f}, "
                f"current open ₹{open_notional:,.0f}, "
                f"this order ₹{new_notional:,.0f}. "
                f"Would exceed by ₹{breach:,.0f}. "
                f"Reduce qty or raise the cap on /strategies."
            ),
        )


async def _process_broker_postback(
    *,
    broker_id: str,
    order_id: str,
    status: str,            # Kite-canonical status string
    account: str,
    symbol: str,
    txn: str,
    qty,
    price,
    exchange: str = "",
    status_message: str = "",
) -> None:
    """Shared post-broker-postback pipeline used by Dhan + Groww
    handlers (Kite has its own inline logic with HMAC validation).

    Same fan-out as the Kite path:
      1. AlgoOrder row update by broker_order_id match
      2. invalidate `orders` / `positions` / `holdings` on terminal
      3. broadcast `order_update` + `position_filled` (on COMPLETE)
         + `book_changed` (on terminal) WS events
      4. audit-log entry tagged `category='order.fill|cancel|reject|expired'`

    Best-effort: never raises. Failures log + drop so the broker's
    webhook gets a 200 OK and stops retrying.
    """
    from sqlalchemy import select as _sql_select
    from backend.api.database import async_session as _async_s
    from backend.api.models import AlgoOrder as _AO
    masked = mask_account(account)

    logger.info(
        f"{broker_id} postback: {order_id} [{masked}] {status} {txn} "
        f"{qty} {symbol} price={price} msg={status_message}"
    )

    _terminal = status in ("COMPLETE", "CANCELLED", "REJECTED", "EXPIRED")

    # Sync AlgoOrder row + record event.
    try:
        from backend.api.algo.order_events import write_event as _write_event
        _KITE_STATUS_MAP = {
            "COMPLETE":  "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED":  "REJECTED",
            "EXPIRED":   "UNFILLED",
        }
        _new_status = _KITE_STATUS_MAP.get(status)

        _filled_rows: list = []
        async with _async_s() as _s:
            _rows = (await _s.execute(
                _sql_select(_AO).where(_AO.broker_order_id == str(order_id))
            )).scalars().all()
            for _r in _rows:
                if _new_status and _r.status != _new_status:
                    _r.status = _new_status
                    if _new_status == "FILLED":
                        try:
                            _r.fill_price = float(price) if price else _r.fill_price
                        except (TypeError, ValueError):
                            pass
                        _r.filled_at = datetime.now(timezone.utc)
                        _filled_rows.append(_r)
                    _r.detail = ((_r.detail or "")[:200]
                                 + f" · {broker_id} postback {status}"
                                 + (f": {status_message}" if status_message else ""))
            await _s.commit()
            for _r in _rows:
                try:
                    await _write_event(
                        _r.id, "broker_postback",
                        f"{status}{(' · ' + status_message) if status_message else ''}",
                        payload={"broker_id": broker_id, "broker_order_id": order_id,
                                 "status": status, "qty": qty, "price": price},
                    )
                except Exception as _we:
                    logger.debug(f"order_events write skipped: {_we}")
        # Fire template-attach on FILL — mirrors the Kite postback path
        # (`_pb_event`). Idempotency lives inside
        # `_fire_template_attach_on_fill` (attached_gtts_json check) so a
        # duplicate postback can't double-place TP/SL GTTs.
        for _r in _filled_rows:
            try:
                _maybe_fire_template_attach_for_reconcile(_r)
            except Exception as _te:
                logger.warning(
                    f"{broker_id} postback template-attach failed for #{_r.id}: {_te}"
                )
    except Exception as e:
        logger.warning(f"{broker_id} postback row sync failed: {e}")

    # Audit trail
    try:
        from backend.api.audit import write_audit_event
        # Audit category mapping. Pre-fix EXPIRED + unknown statuses
        # fell through to "order.fill" which mislabelled them in
        # /admin/audit's Orders pill. EXPIRED gets its own category;
        # truly-unknown statuses bucket to "order" (the generic).
        # (Slice P3.)
        _cat = ("order.fill"    if status == "COMPLETE"
                else "order.cancel"  if status == "CANCELLED"
                else "order.reject"  if status == "REJECTED"
                else "order.expired" if status == "EXPIRED"
                else "order")
        write_audit_event(
            category=_cat,
            action=f"BROKER_{status}",
            actor_username=broker_id,
            actor_role="system",
            target_type="broker_order",
            target_id=order_id or None,
            summary=(f"{status} {txn} {qty} {symbol} @₹{price} acct={masked}"
                     + (f" msg={status_message}" if status_message else ""))[:1000],
        )
    except Exception as _aud:
        logger.debug(f"{broker_id} postback audit write skipped: {_aud}")

    # Cache invalidation + WS broadcasts (mirrors the Kite path).
    try:
        invalidate("orders")
        if _terminal:
            for _key in ("positions", "holdings"):
                try:
                    invalidate(_key)
                except Exception:
                    pass

        broadcast(json.dumps({
            "event": "order_update",
            "order_id": order_id, "account": masked, "status": status,
            "tradingsymbol": symbol, "transaction_type": txn,
            "quantity": qty, "price": price, "status_message": status_message,
        }))

        if status == "COMPLETE":
            try:
                _qty_int = int(qty or 0)
                if _qty_int > 0:
                    _side_sign = 1 if (txn or "").upper() == "BUY" else -1
                    broadcast(json.dumps({
                        "event": "position_filled",
                        "account": masked, "exchange": exchange,
                        "tradingsymbol": symbol,
                        "qty": _qty_int * _side_sign,
                        "fill_price": float(price or 0),
                        "ts": int(_time.time() * 1000),
                        "order_id": order_id,
                    }))
            except Exception as _pe:
                logger.debug(f"position_filled broadcast skipped: {_pe}")

        if _terminal:
            broadcast(json.dumps({
                "event": "book_changed",
                "account": masked, "exchange": exchange,
                "tradingsymbol": symbol, "reason": status,
                "ts": int(_time.time() * 1000),
            }))
    except Exception as _be:
        logger.warning(f"{broker_id} postback fan-out failed: {_be}")


def _broker_for(account: str):
    """Return the `Broker` adapter for `account`. Replaces the prior
    `_kite_for(account)` helper that exposed a raw KiteConnect handle.
    All downstream callers use Broker ABC methods (place_order,
    modify_order, cancel_order, orders, etc.) so the order routes are
    now broker-agnostic — adding a Groww/Dhan account requires no
    edits here."""
    from backend.shared.brokers.registry import get_broker
    try:
        return get_broker(account)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account '{account}' not found")


def _live_chase_config(aggressiveness: str):
    """Map operator-facing L/M/H aggressiveness to ChaseConfig.

    Industry analogue: IBKR Adaptive Algo Patient / Normal / Urgent.
      low   — patient: long interval, small aggression step, more
              attempts. Order rests near midpoint, eases into
              taking liquidity only when the market doesn't come.
      med   — balanced (chase.py defaults).
      high  — urgent: short interval, big aggression step, fewer
              attempts. Cross the spread fast.

    Engine-side defaults still come from /admin/settings (algo.*)
    when the request doesn't carry an aggressiveness override.
    Default: 'low' — the operator's standing instruction is "be
    patient on entry"; callers explicitly bump to med/high when
    they want more fill speed at the cost of slippage.
    """
    from backend.api.algo.chase import ChaseConfig
    a = (aggressiveness or "low").lower()
    if a == "high":
        return ChaseConfig(interval_seconds=10, aggression_step=0.25,
                           max_attempts=10)
    if a == "med":
        return ChaseConfig(interval_seconds=20, aggression_step=0.10,
                           max_attempts=20)
    # low (default) — patient: peg passively, ease into the
    # spread only after enough ticks pass.
    return ChaseConfig(interval_seconds=30, aggression_step=0.05,
                       max_attempts=30)


async def _start_live_chase(account: str, symbol: str, exchange: str,
                            transaction_type: str, quantity: int,
                            aggressiveness: str,
                            algo_order_id: int | None = None) -> str:
    """Place + chase a LIVE order in the background.

    Spawns `chase_order()` as an asyncio task and synchronously
    returns the first broker order_id (so the ticket can confirm
    placement to the operator immediately). The chase task keeps
    running after this returns — re-quoting the limit per the
    aggressiveness config until the order fills or the attempt
    cap is hit.

    Phase 0.5 — algo_order_id is plumbed into chase_order so it can
    keep AlgoOrder.broker_order_id in lockstep with each replace, and
    so its terminal handler can identify the row even when broker_id
    has mutated since the original place. Without this, chased orders
    silently failed to flip to FILLED in the DB → templates never
    attached on fill.
    """
    from backend.api.algo.chase import chase_order
    cfg = _live_chase_config(aggressiveness)
    cfg.exchange = exchange or "NFO"

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def on_event(evt: str, detail: dict):
        # First order_placed event resolves the future. Subsequent
        # events (cancel + re-place per attempt) are no-ops here
        # — the chase task keeps logging them via chase.py's own
        # `logger.info` calls.
        if not fut.done():
            if evt == "order_placed":
                fut.set_result(str(detail.get("order_id") or ""))
            elif evt in ("error", "chase_failed"):
                fut.set_exception(RuntimeError(
                    detail.get("error") or "chase failed before initial placement"
                ))

    asyncio.create_task(chase_order(
        account=account, symbol=symbol,
        transaction_type=transaction_type, quantity=quantity,
        cfg=cfg, on_event=on_event,
        algo_order_id=algo_order_id,
    ))

    # 15 s timeout — chase_order's first iteration fetches depth
    # and fires place_order; even a cold market should land
    # under 5 s. 15 s gives Kite room for a slow first call.
    return await asyncio.wait_for(fut, timeout=15.0)


def _validate_place(req: PlaceOrderRequest) -> None:
    errors = []
    if req.variety not in _VARIETIES:
        errors.append(f"variety must be one of {_VARIETIES}")
    if req.exchange not in _EXCHANGES:
        errors.append(f"exchange must be one of {_EXCHANGES}")
    if req.transaction_type not in _TXN_TYPES:
        errors.append("transaction_type must be BUY or SELL")
    if req.order_type not in _ORDER_TYPES:
        errors.append(f"order_type must be one of {_ORDER_TYPES}")
    if req.product not in _PRODUCTS:
        errors.append(f"product must be one of {_PRODUCTS}")
    if req.validity not in _VALIDITIES:
        errors.append("validity must be DAY or IOC")
    if req.order_type in ("LIMIT", "SL") and not req.price:
        errors.append("price is required for LIMIT / SL orders")
    if req.order_type in ("SL", "SL-M") and not req.trigger_price:
        errors.append("trigger_price is required for SL / SL-M orders")
    if req.quantity <= 0:
        errors.append("quantity must be > 0")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


def _row_from_dict(d: dict, account: str) -> OrderRow:
    return OrderRow(
        order_id=str(d.get("order_id", "")),
        account=account,
        exchange=str(d.get("exchange", "")),
        tradingsymbol=str(d.get("tradingsymbol", "")),
        transaction_type=str(d.get("transaction_type", "")),
        quantity=int(d.get("quantity") or 0),
        pending_quantity=int(d.get("pending_quantity") or 0),
        filled_quantity=int(d.get("filled_quantity") or 0),
        price=float(d.get("price") or 0),
        trigger_price=float(d.get("trigger_price") or 0),
        average_price=float(d.get("average_price") or 0),
        status=str(d.get("status", "")),
        order_type=str(d.get("order_type", "")),
        product=str(d.get("product", "")),
        variety=str(d.get("variety", "")),
        order_timestamp=str(d.get("order_timestamp", "")),
        exchange_timestamp=str(d.get("exchange_timestamp") or ""),
        status_message=str(d.get("status_message") or ""),
        tag=str(d.get("tag") or ""),
    )


def _fetch_orders() -> OrdersResponse:
    from concurrent.futures import ThreadPoolExecutor

    from backend.shared.brokers.registry import all_brokers

    brokers = list(all_brokers())
    if not brokers:
        return OrdersResponse(rows=[], refreshed_at=timestamp_display())

    def _one_account(broker) -> list[OrderRow]:  # type: ignore[no-untyped-def]
        account = broker.account
        try:
            return [_row_from_dict(o, account) for o in reversed(broker.orders() or [])]
        except Exception as e:
            logger.error(f"Orders list failed for {account}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=min(len(brokers), 4)) as pool:
        results = list(pool.map(_one_account, brokers))

    rows: list[OrderRow] = [row for chunk in results for row in chunk]
    return OrdersResponse(rows=rows, refreshed_at=timestamp_display())


class AlgoOrderEventInfo(msgspec.Struct):
    """One row from the per-order timeline."""
    id: int
    order_id: int
    ts: str          # ISO-8601 UTC timestamp
    kind: str
    message: str
    payload_json: str | None


class AlgoOrderInfo(msgspec.Struct, kw_only=True):
    """Shape exposed to the frontend Order-log tab. Thin wrapper over the
    AlgoOrder row — adds a display-ready price string would be nice but
    the frontend formats it for locale anyway.

    Hotfix 2026-06-20 — `kw_only=True` because the field order interleaves
    required fields (`attempts`, `status`, `engine`, `mode`, `detail`,
    `created_at`) after optional ones (`current_limit`, `fill_price`).
    msgspec ≥ a recent version refuses to load this without kw_only. Every
    construction site passes by keyword anyway, so kw_only is the lowest-
    risk fix — no reordering needed."""
    id: int
    account: str
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    initial_price: float | None
    # Audit fix (M-6) — current re-quoted limit; the chase loop
    # updates this on every cancel-and-replace via _sync_algo_order_id.
    # The chase panel renders this in place of initial_price when set,
    # so the limit column reflects the LIVE broker limit instead of
    # the first attempt's price.
    current_limit: float | None = None
    fill_price: float | None = None
    # How many times the chase engine re-quoted this order before a
    # terminal state. Bumped live on every `modify` event so the
    # Order tab can show "chase #3" as it's happening, not just
    # after fill/unfilled.
    attempts: int
    status: str
    engine: str
    mode: str
    detail: str | None
    created_at: str
    # Basket / TP metadata — None on legacy rows.
    target_pct: float | None = None
    target_abs: float | None = None
    parent_order_id: int | None = None
    basket_tag: str | None = None
    # Phase 2 — surface template attachment so the chase panel +
    # /orders log can show a "Tmpl ✓" chip on rows that picked up
    # auto-brackets at submit. attached_gtts_json is the post-fill
    # JSON list of {kind, label, id} dicts persisted by
    # _fire_template_attach_on_fill. Both null on legacy rows or on
    # rows that picked the 'none' template.
    template_id: int | None = None
    attached_gtts_json: str | None = None
    # Sprint B — broker's running cumulative filled quantity. Lets the
    # frontend show partial-fill progress on OPEN/CANCELLED rows without
    # relying on the detail string for parsing.
    filled_quantity: int | None = None
    # Reverse linkage — every AlgoOrder row that points to THIS row via
    # parent_order_id. Lets the OrderCard render a "wing: #N" chip on
    # the parent so the operator sees the auto-attached protective leg
    # without having to scroll through the activity log. Empty for
    # standalone orders; one or more ids when a template's wing
    # (or any future child mechanism) attached on fill.
    child_order_ids: list[int] = []


async def _fetch_child_order_ids(session, parent_ids: list[int]) -> dict[int, list[int]]:
    """One round-trip lookup of child rows for the given parents. Returns
    {parent_id: [child_id, ...]}. Empty dict when parent_ids is empty so
    callers can early-return."""
    if not parent_ids:
        return {}
    from sqlalchemy import select as _sql_select
    from backend.api.models import AlgoOrder as _AlgoOrder
    children = (await session.execute(
        _sql_select(_AlgoOrder.id, _AlgoOrder.parent_order_id)
        .where(_AlgoOrder.parent_order_id.in_(parent_ids))
    )).all()
    out: dict[int, list[int]] = {}
    for child_id, parent_id in children:
        out.setdefault(int(parent_id), []).append(int(child_id))
    return out


def _resolve_target_pct(override: float | None) -> float:
    """Return the effective TP fraction for a new order.

    Priority:
      1. explicit `override` from the request (including 0.0 to disable TP)
      2. `algo.default_target_pct` DB setting
      3. hard-coded fallback of 0.30
    A negative value is clamped to 0 (disabled).
    """
    if override is not None:
        return max(0.0, float(override))
    from backend.shared.helpers.settings import get_float
    return max(0.0, get_float("algo.default_target_pct", 0.30))


def _ticket_overrides_dict(data) -> dict:
    """Pack the override fields from a TicketOrderRequest /
    TicketPreviewRequest into the dict shape apply_template_to_order
    expects.

    Includes the legacy `target_pct` → `tp_pct` shim:
      - target_pct is fractional (0.30 = +30%) for v1 callers
      - tp_pct on the override dict is % units (30.0 = +30%) to match
        templates_seed / OrderTemplate columns
      - When both are set, the explicit tp_pct_override wins
    """
    overrides: dict = {
        "tp_pct":             data.tp_pct_override,
        "sl_pct":             data.sl_pct_override,
        "wing_premium_pct":   data.wing_premium_pct_override,
        "wing_strike_offset": data.wing_strike_offset_override,
    }
    if overrides["tp_pct"] is None and getattr(data, "target_pct", None) is not None:
        try:
            overrides["tp_pct"] = float(data.target_pct) * 100.0
        except (TypeError, ValueError):
            pass
    return overrides


async def _maybe_attach_template_to_ticket(
    data, account: str, sym: str, side: str, qty: int,
    algo_order_id: int | None,
) -> dict | None:
    """Run the template-attach RESOLVER for a paper ticket order and
    return the planned artefacts WITHOUT placing any broker orders.

    Phase 3C #1 — pre-fix this called apply_template_to_order with
    `apply_path="auto"`. When SimDriver was inactive (which is
    almost always for a real paper ticket) "auto" routed to the
    LIVE path and silently placed real Kite GTTs against the
    operator's submitted LIMIT price BEFORE the parent ever filled.
    Two correctness problems compounded:
      1. The parent might fill at a price OTHER than data.price
         (MARKET orders, partial slip, IOC) so the exit triggers
         got computed off the wrong reference.
      2. The paper engine itself was never told about the template,
         so its own fill events never fired the attach.

    Fix: always run the resolver in PREVIEW mode here so the API
    response carries the planned chips for the OrderTicket preview,
    but no broker order is sent. The paper engine fires the actual
    attach when its order fills via _fire_template_attach_on_fill
    using the engine's reported fill_price.
    """
    if algo_order_id is None:
        return None
    from backend.api.algo.template_attach import apply_template_to_order
    try:
        result = await apply_template_to_order(
            template_id=data.template_id,
            template_slug=None,
            overrides=_ticket_overrides_dict(data),
            parent_account=account,
            parent_symbol=sym,
            parent_side=side,
            parent_qty=qty,
            parent_exchange=(data.exchange or "NFO"),
            parent_fill_price=float(data.price or 0.0),
            parent_product=(data.product or "NRML"),
            parent_order_id=algo_order_id,
            apply_path="preview",
        )
    except Exception as e:
        logger.error(f"[TICKET-TEMPLATE] preview failed for #{algo_order_id}: {e}")
        return None
    if result is None:
        return None
    return result.to_dict()


def _build_overrides_json(leg) -> str | None:
    """Serialize per-leg template parameter overrides into a JSON string
    for persistence on AlgoOrder.template_overrides_json. Returns None
    when no overrides were supplied (caller leaves the DB column null).

    Mirrors the override keys `apply_template_to_order` expects in its
    `overrides` dict so the postback handler can pass the parsed JSON
    straight through.
    """
    payload = {}
    # Audit fix — also serialize sl_trail_pct + tp_scales_json so
    # operator overrides of those fields persist through the postback
    # handler's override-replay path (the attach pipeline already
    # honors both fields when present in the overrides dict; the
    # serializer just wasn't carrying them).
    for src_key, dst_key in (
        ("tp_pct_override",             "tp_pct"),
        ("sl_pct_override",             "sl_pct"),
        ("wing_premium_pct_override",   "wing_premium_pct"),
        ("wing_strike_offset_override", "wing_strike_offset"),
        ("sl_trail_pct_override",       "sl_trail_pct"),
        ("tp_scales_json_override",     "tp_scales_json"),
    ):
        v = getattr(leg, src_key, None)
        if v is not None:
            payload[dst_key] = v
    if not payload:
        return None
    import json as _json
    return _json.dumps(payload)


async def _attach_basket_leg_template(
    *,
    algo_order_id: int | None,
    template_id: int | None,
    account: str,
    sym: str,
    side: str,
    qty: int,
    exch: str,
    price: float,
    product: str,
) -> None:
    """Best-effort template ATTACH-AT-FILL plan for one basket leg.

    Sprint A fix (audit finding #7): the previous shape called
    `apply_template_to_order(apply_path="auto")` at submit time using
    the operator's limit `price` as a synthetic fill price, which
    placed broker GTTs at the wrong reference if the parent filled
    away from the limit. Now we just persist `template_id` on the
    AlgoOrder row (done at the caller) — the postback handler / chase
    terminal will fire the real attach via
    `_fire_template_attach_on_fill` with the actual fill price.

    Kept as a no-op shim for callsite compatibility; future work can
    inline the persist step here if it makes the basket flow clearer.
    """
    return


# Phase 3D #4 — per-parent-row in-process lock so the postback
# handler and chase terminal can't both pass the
# `attached_gtts_json is None` check simultaneously and double-place
# the GTT at the broker. uvicorn runs with --workers 1 in prod, so
# in-process locking is sufficient; the meta-lock guards the registry
# write itself.
#
# Audit fix (M-5) — strong dict with TTL replaces the prior
# WeakValueDictionary. The weakref pattern was "safe in current
# deployment" (single-worker asyncio + the caller's local strong ref
# keeps the entry alive across `async with`) but fragile to future
# call-signature changes: any refactor that introduced an extra
# `await` between the lock-mint and the `async with` acquisition
# could let the GC reclaim the lock mid-handoff, allowing two waiters
# to acquire DIFFERENT lock objects for the same parent_row_id and
# double-place the GTT. Switching to a strong dict eliminates that
# class of bug; the TTL sweep keeps memory bounded by retiring entries
# that haven't been touched in `_TPL_LOCK_TTL_S` seconds (default 1 h
# — well beyond the worst-case fill-to-attach latency including a
# slow reconcile sweep).
import time as _time
_TEMPLATE_ATTACH_LOCKS: dict[int, tuple[asyncio.Lock, float]] = {}
_TEMPLATE_ATTACH_META_LOCK = asyncio.Lock()
# 1 h — longest realistic live-chase window is ~30 min (max_attempts ×
# interval); 1 h is 2× headroom so a slow reconcile sweep after market
# close still finds the lock before it expires.
_TPL_LOCK_TTL_S = 3600


async def _get_template_attach_lock(parent_row_id: int) -> "asyncio.Lock":
    """Lazily mint one asyncio.Lock per parent_row_id. The meta-lock
    only protects the registry's get-or-create; the per-row lock then
    serialises the read–decide–write triplet inside the fire fn.

    Lazy TTL sweep: every get-or-create scans the registry for entries
    older than _TPL_LOCK_TTL_S and drops them. Operator-facing fill
    flows complete well within the TTL (chase + postback + reconcile
    all measure in seconds to minutes), so eviction never races a
    live waiter — by the time an entry's age exceeds the TTL its
    attach has long since committed `attached_gtts_json`."""
    async with _TEMPLATE_ATTACH_META_LOCK:
        now = _time.monotonic()
        # Lazy sweep — drop stale entries. Inline rather than a
        # background task so the registry stays bounded without an
        # extra sweep coroutine.
        _stale = [k for k, (_, ts) in _TEMPLATE_ATTACH_LOCKS.items()
                  if now - ts > _TPL_LOCK_TTL_S]
        for k in _stale:
            _TEMPLATE_ATTACH_LOCKS.pop(k, None)
        entry = _TEMPLATE_ATTACH_LOCKS.get(parent_row_id)
        if entry is None:
            lk = asyncio.Lock()
            _TEMPLATE_ATTACH_LOCKS[parent_row_id] = (lk, now)
            return lk
        # Bump the timestamp on every access — entries don't expire
        # while a row is being actively reconciled.
        _TEMPLATE_ATTACH_LOCKS[parent_row_id] = (entry[0], now)
        return entry[0]


def _maybe_fire_template_attach_for_reconcile(row) -> None:
    """Sprint A helper — when the reconcile path flips an AlgoOrder to
    FILLED, fire the template attach if the row carries a template_id
    and is a parent (parent_order_id IS NULL). Same idempotency guard
    inside `_fire_template_attach_on_fill` ensures duplicate firings
    (postback arriving after reconcile) are safe."""
    try:
        # Audit fix — mode safety. Reconcile must only fire template
        # attach for LIVE rows. The single-order reconcile endpoint
        # (`/{broker_order_id}/reconcile`) is operator-driven and could
        # accept a paper-mode row by mistake; without this guard the
        # attach would route through `apply_path="live"` and place real
        # Kite GTTs for a position that doesn't exist at the broker.
        # Bulk `/algo/reconcile` already filters mode='live' upstream, so
        # the guard is a no-op there.
        if (row.mode or "").lower() != "live":
            return
        if not (row.template_id and row.parent_order_id is None):
            return
        if not row.fill_price:
            return
        # Sprint B (#4) — partial fills get their actual filled qty.
        _attach_qty = (
            int(row.filled_quantity)
            if int(row.filled_quantity or 0) > 0
            else int(row.quantity or 0)
        )
        asyncio.create_task(_fire_template_attach_on_fill(
            parent_row_id=int(row.id),
            parent_account=str(row.account),
            parent_symbol=str(row.symbol),
            parent_exchange=str(row.exchange or "NFO"),
            parent_side=str(row.transaction_type),
            parent_qty=_attach_qty,
            fill_price=float(row.fill_price),
            template_id=int(row.template_id),
            parent_product=str(row.product or "NRML"),
        ))
    except Exception as e:
        logger.warning(f"reconcile template attach failed for #{row.id}: {e}")


async def _fire_template_attach_on_fill(
    *,
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    parent_side: str,
    parent_qty: int,
    fill_price: float,
    template_id: int,
    parent_product: str = "NRML",
) -> None:
    """Fire apply_plan_live for a templated parent order that just
    flipped to FILLED via Kite/Dhan/Groww postback.

    Persists the returned gtt_ids back onto AlgoOrder.attached_gtts_json
    so the operator can cancel them later if they manually close the
    parent. Errors are logged but never raised — the postback ACK has
    already returned to the broker.

    Idempotency: if attached_gtts_json is already populated for this
    parent, skip — postbacks can arrive multiple times for the same
    fill on Kite (delivery retry) and we don't want duplicate GTTs.

    `parent_product` — exit GTT legs inherit this. NRML for F&O carry,
    MIS for intraday equity / F&O, CNC for delivery. Defaulted only as
    a back-compat shim for callers that don't have the row's product
    in hand; real callers (postback handler, chase terminal) read it
    off AlgoOrder.product (Phase 3C #2).
    """
    if not fill_price or fill_price <= 0:
        return
    # Phase 3D #4 — serialise concurrent calls for the same parent_row_id
    # so the postback handler and chase terminal can't both pass the
    # `attached_gtts_json is None` idempotency check simultaneously
    # and double-place GTTs at the broker. The lock is per-row +
    # in-process (uvicorn --workers 1 on prod) so there's zero
    # contention against unrelated fills. Implementation is a 2-line
    # guard in the inner body: acquire the lock, run the existing
    # _attach_body, release.
    _row_lock = await _get_template_attach_lock(parent_row_id)
    async with _row_lock:
        try:
            import json as _json
            from sqlalchemy import select as _sel_t
            from backend.api.database import async_session as _async_s
            from backend.api.models import AlgoOrder as _AO
            from backend.api.algo.template_attach import apply_template_to_order

            _row_overrides: dict = {}
            async with _async_s() as _s:
                _row = (await _s.execute(
                    _sel_t(_AO).where(_AO.id == parent_row_id)
                )).scalar_one_or_none()
                if _row is None:
                    logger.warning(
                        f"[TPL-ATTACH] parent row #{parent_row_id} vanished "
                        f"before postback fired"
                    )
                    return
                if _row.attached_gtts_json:
                    # Duplicate postback — already attached.
                    return
                # Phase 2 of the template/on-fill rework: pull the
                # operator's per-submit overrides off the row so the
                # attach reflects them. Empty dict when the column is
                # null (no overrides supplied at submit time).
                if _row.template_overrides_json:
                    try:
                        _parsed = _json.loads(_row.template_overrides_json)
                        if isinstance(_parsed, dict):
                            _row_overrides = _parsed
                    except Exception as _e:
                        logger.warning(
                            f"[TPL-ATTACH] could not parse template_overrides_json "
                            f"for parent #{parent_row_id}: {_e}"
                        )

            result = await apply_template_to_order(
                template_id=template_id,
                template_slug=None,
                overrides=_row_overrides,
                parent_account=parent_account,
                parent_symbol=parent_symbol,
                parent_side=parent_side,
                parent_qty=parent_qty,
                parent_exchange=parent_exchange,
                parent_fill_price=fill_price,
                parent_product=parent_product,
                parent_order_id=parent_row_id,
                apply_path="live",
            )
            if result is None:
                return
    
            attached = []
            # Sprint C — flatten sibling_pairs into a placed_id → sibling_id
            # lookup so the loop below can stamp the pointer per entry.
            # Pairs are bidirectional (either leg firing should cancel the
            # other), but we record both directions to keep the post-fire
            # cleanup simple — the watcher reads `sibling_id` directly
            # off the firing entry, no second lookup needed.
            _sibling_by_id: dict[str, str] = {}
            for a, b in (result.sibling_pairs or []):
                if a and b:
                    _sibling_by_id[a] = b
                    _sibling_by_id[b] = a
            for spec in (result.plan.gtts or []):
                if not spec.placed_id:
                    continue
                entry = {
                    "kind":  "gtt",
                    "label": spec.label,
                    "id":    spec.placed_id,
                }
                # Sprint C — emulated OCO entries carry a `sibling_id`
                # pointer so the pair-watcher background task can cancel
                # the survivor when one leg fires. Native OCO entries
                # (single broker id, two-leg trigger_type) leave this
                # absent — the broker handles the cancel atomically.
                _sib = _sibling_by_id.get(str(spec.placed_id))
                if _sib:
                    entry["sibling_id"] = _sib
                    entry["parent_account"]  = str(result.plan.parent_account)
                    entry["parent_exchange"] = str(result.plan.parent_exchange)
                # Phase 3B — when this leg carries a trailing stop, persist
                # the metadata so _task_trail_stop can find + advance the
                # trigger without re-loading the template (operator may
                # have edited it post-fill). highest_ltp + low_ltp seed
                # from parent fill price; the poller updates them in-place.
                if spec.sl_trail_pct is not None and spec.trigger_values:
                    entry["sl_trail_pct"] = float(spec.sl_trail_pct)
                    # For two-leg OCO the SL trigger sits at orders[1]
                    # (index 1). For single SL it's [0]. The trigger_values
                    # parallel-index orders.
                    _last_trig = float(spec.trigger_values[-1])
                    entry["current_trigger"] = _last_trig
                    # Sprint A fix (#6): persist the TP trigger too so
                    # the two-leg trail-stop modify_gtt call can pass
                    # BOTH trigger values [tp, new_sl]. Pre-fix the
                    # poller silently `continue`d on every two-leg
                    # entry because it had no way to construct the
                    # full triggers list — the trail effectively never
                    # ratcheted for OCO templates. single-leg specs
                    # leave `tp_trigger` absent (None) and the poller
                    # falls through to its single-trigger path.
                    if (
                        str(spec.trigger_type) == "two-leg"
                        and len(spec.trigger_values) >= 2
                    ):
                        entry["tp_trigger"] = float(spec.trigger_values[0])
                    entry["highest_ltp"]     = float(result.plan.parent_fill_price)
                    entry["lowest_ltp"]      = float(result.plan.parent_fill_price)
                    entry["parent_side"]     = str(result.plan.parent_side)
                    entry["parent_symbol"]   = str(result.plan.parent_symbol)
                    entry["parent_exchange"] = str(result.plan.parent_exchange)
                    entry["parent_account"]  = str(result.plan.parent_account)
                    entry["parent_qty"]      = int(result.plan.parent_qty)
                    entry["parent_product"]  = str(parent_product)
                    entry["trigger_type"]    = str(spec.trigger_type)
                attached.append(entry)
            if result.wing_order_id:
                attached.append({
                    "kind":  "wing",
                    "label": "Wing",
                    "id":    result.wing_order_id,
                })
    
            async with _async_s() as _s:
                _row = (await _s.execute(
                    _sel_t(_AO).where(_AO.id == parent_row_id)
                )).scalar_one_or_none()
                if _row is None:
                    return
                _row.attached_gtts_json = _json.dumps(attached) if attached else _json.dumps([])
                if result.errors:
                    _row.detail = ((_row.detail or "")[:200]
                                   + f" · template attach: "
                                   + "; ".join(result.errors[:2]))
                await _s.commit()
            logger.info(
                f"[TPL-ATTACH] parent #{parent_row_id} {parent_account} "
                f"{parent_side} {parent_symbol} fill={fill_price} → "
                f"{len(attached)} child placement(s), "
                f"{len(result.errors)} error(s)"
            )
        except Exception as e:
            logger.error(
                f"[TPL-ATTACH] failed for parent #{parent_row_id}: {e}"
            )


async def _arm_take_profit(
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    parent_side: str,      # "BUY" | "SELL"
    fill_price: float,
    target_pct: float,
    target_abs: float | None,
    parent_mode: str,      # "paper" | "live"
    parent_product: str = "NRML",
) -> None:
    """[LEGACY — Phase 2 deprecation marker] Arm a take-profit child
    order on fill via the v1 fractional target_pct path.

    Prefer attaching an OrderTemplate to the parent order (Phase 0+);
    the template pipeline (apply_template_to_order →
    _fire_template_attach_on_fill) supports rich TP / SL / Wing /
    MARKET-TP / wing-by-premium scan, broker-native OCO, and
    chained chase orders. This helper survives as the back-compat
    shim for Lab MCP scripts + legacy callers that still pass
    `data.target_pct`; both the postback handler and chase terminal
    handler keep firing both shim + template attach so neither path
    interferes with the other (each is idempotent against double
    fires).

    Called from the postback handler and _emit_chase_terminal when a parent
    order reaches FILLED.  Idempotent: skips if a child row already exists
    for this parent.

    Logic:
      - BUY parent  → SELL TP  @ fill_price × (1 + target_pct)
      - SELL parent → BUY  TP  @ fill_price × (1 - target_pct)
      If target_abs is also set, adds it on top of the pct delta.
    """
    if not fill_price or fill_price <= 0:
        return
    if not target_pct and not target_abs:
        return

    try:
        from sqlalchemy import select as _sel, func as _func
        from datetime import datetime, timezone
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrder as _AlgoOrder

        async with _async_session() as _s:
            # Idempotency — skip if a TP child already exists.
            existing = (await _s.execute(
                _sel(_func.count(_AlgoOrder.id)).where(
                    _AlgoOrder.parent_order_id == parent_row_id
                )
            )).scalar_one()
            if existing:
                return

            # Resolve parent row to get quantity (needed for the child).
            parent = (await _s.execute(
                _sel(_AlgoOrder).where(_AlgoOrder.id == parent_row_id)
            )).scalar_one_or_none()
            if parent is None:
                return

            qty = int(parent.quantity or 0)
            if not qty:
                return

            parent_side_u = (parent_side or "BUY").upper()
            tp_side = "SELL" if parent_side_u == "BUY" else "BUY"

            # Compute TP price.
            pct_delta = float(target_pct or 0.0)
            abs_delta = float(target_abs or 0.0)
            if tp_side == "SELL":
                tp_price = fill_price * (1.0 + pct_delta) + abs_delta
            else:
                tp_price = fill_price * (1.0 - pct_delta) - abs_delta
            tp_price = max(0.01, round(tp_price, 2))

            tp_detail = (
                f"TP +{pct_delta*100:.0f}% · parent #{parent_row_id} "
                f"fill ₹{fill_price:.2f} → limit ₹{tp_price:.2f}"
            )

            tp_row = _AlgoOrder(
                account=parent_account,
                symbol=parent_symbol,
                exchange=parent_exchange,
                transaction_type=tp_side,
                quantity=qty,
                initial_price=tp_price,
                status="OPEN",
                engine="target",
                mode=parent_mode,
                target_pct=target_pct,
                target_abs=target_abs,
                parent_order_id=parent_row_id,
                detail=tp_detail,
            )
            _s.add(tp_row)
            await _s.commit()
            tp_id = tp_row.id

        # Register with the paper engine so it chases the TP.
        if parent_mode == "paper":
            try:
                from backend.api.algo.paper import get_prod_paper_engine
                eng = get_prod_paper_engine()
                eng.register_open_order({
                    "algo_order_id": tp_id,
                    "account":       parent_account,
                    "symbol":        parent_symbol,
                    "side":          tp_side,
                    "qty":           qty,
                    "limit_price":   tp_price,
                    "initial_price": tp_price,
                    "exchange":      parent_exchange,
                    "agent_slug":    "auto-tp",
                    "action_type":   "place_order",
                    "chase_agg":     "low",
                })
            except Exception as _pe:
                logger.warning(f"[TP] paper engine register failed for tp #{tp_id}: {_pe}")

        elif parent_mode == "live":
            # Live TP — fire kite.place_order as a limit order.
            try:
                broker = _broker_for(parent_account)
                from backend.shared.brokers.kite import get_lot_size
                _ls = await get_lot_size(parent_exchange, parent_symbol)
                _kq = broker.translate_qty(parent_exchange, qty, _ls)
                kite_order_id = broker.place_order(
                    variety="regular",
                    exchange=parent_exchange,
                    tradingsymbol=parent_symbol,
                    transaction_type=tp_side,
                    quantity=_kq,
                    product=parent_product or "NRML",
                    order_type="LIMIT",
                    price=tp_price,
                    validity="DAY",
                    tag=f"rb-tp-{parent_row_id}",  # Kite tag cap: 20 chars
                )
                # Link the broker order id back to the child row.
                from backend.api.database import async_session as _async_session2
                from backend.api.models import AlgoOrder as _AO2
                async with _async_session2() as _s2:
                    tp_upd = (await _s2.execute(
                        _sel(_AO2).where(_AO2.id == tp_id)
                    )).scalar_one_or_none()
                    if tp_upd is not None:
                        tp_upd.broker_order_id = str(kite_order_id)
                    await _s2.commit()
                logger.info(f"[TP] live TP order placed: broker={kite_order_id} "
                            f"tp_id={tp_id} parent={parent_row_id}")
            except Exception as _le:
                logger.error(f"[TP] live TP placement failed for parent #{parent_row_id}: {_le}")

        logger.info(f"[TP] armed: tp_id={tp_id} parent={parent_row_id} "
                    f"side={tp_side} limit=₹{tp_price:.2f} mode={parent_mode}")

    except Exception as _e:
        logger.warning(f"[TP] _arm_take_profit failed for parent #{parent_row_id}: {_e}")


class OrdersController(Controller):
    path = "/api/orders"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_orders(self, request: Request) -> OrdersResponse:
        try:
            resp = await get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL)
            # Mask account codes for everyone who is NOT admin/designated.
            # Copy-not-mutate so the shared cache doesn't keep the masked
            # codes (was the demo→signin lag bug — first masked caller
            # poisoned the cached resp for subsequent admin requests).
            if not is_admin_request(request):
                import msgspec as _ms
                return _ms.structs.replace(
                    resp,
                    rows=[_ms.structs.replace(
                        r, account=mask_account(r.account)
                    ) for r in resp.rows],
                )
            return resp
        except Exception as e:
            logger.error(f"Orders API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @get("/algo/recent")
    async def list_algo_orders(self, request: Request, n: int = 100, mode: str = "all") -> list[AlgoOrderInfo]:
        """
        Recent agent-generated orders from the algo_orders table.

        `mode`:
          - "all"  (default) → every row, newest first. Order-log tab on the
            agents page uses this so operators see both real and simulated
            fires with a single fetch.
          - "live" → mode='live' only
          - "sim"  → mode='sim'  only

        Response includes `initial_price` (the LIMIT price = sim's LTP at
        trigger time, or the broker-submitted price in live mode), so the
        UI can show "SELL 50 NIFTY @ ₹175.50" inline.
        """
        from sqlalchemy import desc, select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        async with async_session() as s:
            q = sql_select(AlgoOrder).order_by(desc(AlgoOrder.id)).limit(max(1, min(n, 500)))
            if mode in ("live", "sim", "paper", "replay", "shadow"):
                q = q.where(AlgoOrder.mode == mode)
            rows = (await s.execute(q)).scalars().all()
            child_map = await _fetch_child_order_ids(s, [r.id for r in rows])
        # Mask account codes for everyone who is NOT admin/designated.
        # Partner JWTs see masked codes too (audit fix). Same masking
        # the /performance grids apply — turns ZG0790 into ZG####.
        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [
            AlgoOrderInfo(
                id=r.id, account=masked_acct(r.account), symbol=r.symbol, exchange=r.exchange,
                transaction_type=r.transaction_type, quantity=r.quantity,
                initial_price=(float(r.initial_price) if r.initial_price is not None else None),
                current_limit=(float(r.current_limit) if r.current_limit is not None else None),
                fill_price=(float(r.fill_price) if r.fill_price is not None else None),
                attempts=int(r.attempts or 0),
                status=r.status, engine=r.engine, mode=r.mode,
                detail=r.detail,
                created_at=r.created_at.isoformat() if r.created_at else "",
                target_pct=(float(r.target_pct) if r.target_pct is not None else None),
                target_abs=(float(r.target_abs) if r.target_abs is not None else None),
                parent_order_id=r.parent_order_id,
                basket_tag=r.basket_tag,
                template_id=r.template_id,
                attached_gtts_json=r.attached_gtts_json,
                filled_quantity=(int(r.filled_quantity) if r.filled_quantity is not None else None),
                child_order_ids=child_map.get(r.id, []),
            )
            for r in rows
        ]

    @get("/chases/active")
    async def list_active_chases(self, request: Request) -> list[AlgoOrderInfo]:
        """In-flight chase orders — algo_orders rows in OPEN state
        across paper / live / shadow. Sorted newest-first.

        Two cheap inline reconciles before returning so the card
        never surfaces an order that's already dead (operator: "how
        can an order present in chase in flight when they are no
        open orders"):
          1. paper rows whose id isn't in the paper engine's open
             set — marked UNFILLED in DB, dropped from response.
          2. live rows with no broker_order_id (placement never
             succeeded) — marked REJECTED in DB, dropped.

        Live rows that DO carry a broker_order_id are trusted to be
        legitimately in-flight; the operator-triggered reconcile
        endpoint does the broker round-trip when they want a
        thorough sweep.
        """
        from sqlalchemy import desc, select as sql_select
        from datetime import datetime, timezone
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        # Snapshot paper engine's open-order ids ONCE before the DB
        # query so we don't pay for the lock on every row.
        try:
            from backend.api.algo.paper import get_prod_paper_engine
            _pe = get_prod_paper_engine()
            _paper_open_ids = {
                o.get("algo_order_id") for o in _pe.open_order_details()
            }
        except Exception:
            _paper_open_ids = None  # engine unavailable → don't drop paper rows

        # Snapshot live broker orders (cached 15 s in get_or_fetch).
        # Operator: "once chase reconciled and order completed it should
        # not show in chase reconcile card. example GOLDM 146000 strike
        # completed but still listed." Postbacks can drop, leaving the
        # row stuck at OPEN; the panel polled /chases/active every 3 s
        # but never round-tripped to the broker, so a FILLED order kept
        # rendering until the operator clicked Reconcile (which also
        # had the KiteConnection bug — separate fix in this commit).
        # Inline broker lookup uses the existing /api/orders cache so we
        # pay 1 broker.orders() per account per 15 s, not 1 per 3 s poll.
        _LIVE_TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}
        _KITE_TO_ALGO = {
            "COMPLETE":  "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED":  "REJECTED",
            "EXPIRED":   "UNFILLED",
        }
        _broker_status_by_id: dict[str, dict] = {}
        try:
            _ord_resp = await get_or_fetch("orders", _fetch_orders,
                                           ttl_seconds=_ORDERS_TTL)
            for _o in (_ord_resp.rows or []):
                _bid = str(getattr(_o, "order_id", "") or "")
                if _bid:
                    _broker_status_by_id[_bid] = {
                        "status": str(getattr(_o, "status", "") or "").upper(),
                        "average_price": float(getattr(_o, "average_price", 0) or 0),
                    }
        except Exception as _oe:
            logger.debug(f"chases/active broker snapshot failed: {_oe}")

        async with async_session() as s:
            # Audit fix (H-1) — also include CANCEL_FAILED rows so the
            # operator sees orders where the Kill click hit a broker
            # failure. Pre-fix these rows fell off the chase grid
            # (status != OPEN) and the operator had no surface short of
            # browsing /orders. The order is still LIVE at the broker;
            # recovery is via Reconcile or another Kill attempt.
            rows = (await s.execute(
                sql_select(AlgoOrder)
                .where(AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"]))
                .order_by(desc(AlgoOrder.id))
                .limit(500)
            )).scalars().all()

            kept = []
            dropped_paper = 0
            dropped_live = 0
            reconciled_live = 0
            # Capture rows that flip to FILLED here so we can call
            # `_maybe_fire_template_attach_for_reconcile` after the
            # commit — pre-fix the polling reconcile path silently
            # dropped the template TP/SL attach because the function
            # was only invoked from `reconcile_algo_orders` /
            # `reconcile_single_order`, never from this in-line poll.
            _reconciled_filled: list = []
            for r in rows:
                mode = (r.mode or "").lower()
                if mode == "paper" and _paper_open_ids is not None:
                    if r.id not in _paper_open_ids:
                        # Guard: only flip to UNFILLED when the row is still
                        # OPEN in the DB.  The engine's concurrent step() may
                        # have already written FILLED; a plain assignment would
                        # overwrite that terminal status.  SQLAlchemy ORM
                        # doesn't expose UPDATE … WHERE clauses directly, so
                        # only mutate if the refreshed instance still shows
                        # OPEN (the SELECT above fetched it as OPEN, but a
                        # concurrent commit could have raced us).
                        if r.status == "OPEN":
                            r.status = "UNFILLED"
                            r.detail = ((r.detail or "")[:200]
                                        + " · paper engine no longer tracking")
                            dropped_paper += 1
                        continue
                elif mode == "live":
                    if not (r.broker_order_id or "").strip():
                        r.status = "REJECTED"
                        r.detail = ((r.detail or "")[:200]
                                    + " · live placement never returned broker_order_id")
                        dropped_live += 1
                        continue
                    # Broker reconciliation — if the cached order book
                    # shows this id at a terminal status, flip the row
                    # and drop it from the response.
                    _bo = _broker_status_by_id.get(str(r.broker_order_id))
                    if _bo and _bo["status"] in _LIVE_TERMINAL:
                        new_status = _KITE_TO_ALGO[_bo["status"]]
                        if r.status != new_status:
                            r.status = new_status
                            if new_status == "FILLED":
                                if _bo["average_price"]:
                                    try:
                                        r.fill_price = float(_bo["average_price"])
                                    except (TypeError, ValueError):
                                        pass
                                r.filled_at = datetime.now(timezone.utc)
                                # Queue for template-attach fire after commit.
                                _reconciled_filled.append(r)
                            r.detail = ((r.detail or "")[:200]
                                        + f" · broker says {_bo['status']}")
                            reconciled_live += 1
                        continue
                kept.append(r)
            if dropped_paper or dropped_live or reconciled_live:
                await s.commit()
                # After the commit lands, fire template-attach for
                # every FILLED row that surfaced here. Same hook the
                # postback handler + chase terminal use; idempotent
                # via the `attached_gtts_json IS NOT NULL` guard
                # inside `_fire_template_attach_on_fill`.
                for _filled_row in _reconciled_filled:
                    _maybe_fire_template_attach_for_reconcile(_filled_row)
            child_map = await _fetch_child_order_ids(s, [r.id for r in kept])

        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [
            AlgoOrderInfo(
                id=r.id, account=masked_acct(r.account), symbol=r.symbol,
                exchange=r.exchange,
                transaction_type=r.transaction_type, quantity=r.quantity,
                initial_price=(float(r.initial_price) if r.initial_price is not None else None),
                current_limit=(float(r.current_limit) if r.current_limit is not None else None),
                fill_price=(float(r.fill_price) if r.fill_price is not None else None),
                attempts=int(r.attempts or 0),
                status=r.status, engine=r.engine, mode=r.mode,
                detail=r.detail,
                created_at=r.created_at.isoformat() if r.created_at else "",
                target_pct=(float(r.target_pct) if r.target_pct is not None else None),
                target_abs=(float(r.target_abs) if r.target_abs is not None else None),
                parent_order_id=r.parent_order_id,
                basket_tag=r.basket_tag,
                template_id=r.template_id,
                attached_gtts_json=r.attached_gtts_json,
                filled_quantity=(int(r.filled_quantity) if r.filled_quantity is not None else None),
                child_order_ids=child_map.get(r.id, []),
            )
            for r in kept
        ]

    @post("/chases/{algo_order_id:int}/kill", guards=[admin_guard])
    async def kill_chase(self, algo_order_id: int, request: Request) -> dict:
        """Cancel an in-flight chase — best-effort across paper, live,
        and shadow modes. Admin-only.

        - Paper: delegate to engine.cancel_paper_order() which writes the
          canonical AlgoOrderEvent(kind='cancel') row and flips DB status
          via _safe_update_algo_order_cancel.
        - Live: call broker.cancel_order(variety, broker_order_id).
        - Shadow: just flip the row's status (nothing real to cancel).
        Sets AlgoOrder.status='CANCELLED' and writes a 'killed' event so
        the timeline reflects the operator action.
        """
        from sqlalchemy import select as _sql_select
        from datetime import datetime, timezone
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.api.algo.order_events import write_event as _write_event

        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == algo_order_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="order not found")
            if row.status != "OPEN":
                return {"ok": True, "already_terminal": True, "status": row.status}

            mode = (row.mode or "").lower()
            err_msg = ""

            if mode == "paper":
                try:
                    from backend.api.algo.paper import get_prod_paper_engine
                    eng = get_prod_paper_engine()
                    # Delegate to the engine's canonical cancel path — it writes
                    # the AlgoOrderEvent(kind='cancel') row and flips the DB row
                    # to CANCELLED via _safe_update_algo_order_cancel, preventing
                    # a race with the engine's concurrent step() tick.
                    cancelled = eng.cancel_paper_order(row.id)
                    if not cancelled:
                        err_msg = "paper engine no longer tracking"
                except Exception as e:
                    err_msg = f"paper cancel failed: {e}"

            elif mode == "live":
                try:
                    # Use the broker registry — Connections.conn[account]
                    # holds the raw KiteConnection (or vendor connection
                    # object) which does NOT expose cancel_order. The
                    # vendor-agnostic Broker adapter from get_broker()
                    # is what every other order-mutating path uses.
                    # Earlier this called Connections().conn.get(account)
                    # directly and silently failed every kill on prod with
                    # "'KiteConnection' object has no attribute 'cancel_order'".
                    from backend.shared.brokers import get_broker
                    if not str(row.account or "").strip():
                        err_msg = "no account on row"
                    elif not (row.broker_order_id or "").strip():
                        err_msg = "no broker_order_id on row"
                    else:
                        try:
                            broker = get_broker(str(row.account))
                        except Exception as _ge:
                            broker = None
                            err_msg = f"broker not loaded for {row.account}: {_ge}"
                        if broker is not None:
                            # Mark BEFORE the broker call so the chase
                            # loop sees the kill flag even if its 20-s
                            # poll lands between cancel and mark. The
                            # CANCELLED-handler in chase_order checks
                            # is_killed(current_order_id) and exits
                            # cleanly instead of re-placing.
                            try:
                                from backend.api.algo.chase import mark_killed
                                mark_killed(str(row.broker_order_id))
                            except Exception:
                                pass
                            # ChaseEngine + every other live-cancel path
                            # passes order_id positionally then variety
                            # kwarg — match that convention so adapters
                            # don't double-bind 'order_id'.
                            await asyncio.to_thread(
                                broker.cancel_order,
                                str(row.broker_order_id),
                                variety="regular",
                            )
                except Exception as e:
                    err_msg = f"broker cancel failed: {e}"

            # Audit fix: only commit CANCELLED to the DB when the
            # broker cancel actually succeeded. Pre-fix, a broker-side
            # failure (network blip, "order not found" race with a
            # post-postback delete, vendor-specific error) still
            # flipped the row to CANCELLED — operator's UI claimed the
            # order was killed while the exchange still carried the
            # live position. CANCEL_FAILED keeps the row visible in
            # the chase grid with a clear surface for the operator to
            # retry or reconcile.
            if err_msg:
                row.status = "CANCEL_FAILED"
                row.detail = ((row.detail or "")[:200]
                              + f" · cancel failed: {err_msg}")
            else:
                row.status = "CANCELLED"
                row.detail = ((row.detail or "")[:200]
                              + " · killed by operator")
            await s.commit()
            _row_id = row.id

        invalidate("orders")
        await _write_event(
            _row_id, "killed",
            f"Chase killed by operator{' (' + err_msg + ')' if err_msg else ''}",
            payload={"err": err_msg or None,
                     "ts": datetime.now(timezone.utc).isoformat()},
        )
        return {"ok": True, "err": err_msg or None}

    @post("/{algo_order_id:int}/retry-template", guards=[admin_guard])
    async def retry_template(self, algo_order_id: int, request: Request) -> dict:
        """Re-run apply_template_to_order against an already-filled
        parent. Useful when the original attach failed silently — e.g.
        a SELL option with wing_premium_pct=10 picked nothing because
        every chain candidate failed the OI gate (fixed forward in
        d826590e, but pre-existing positions need this to catch up).

        Idempotent: bails when `attached_gtts_json` is already set,
        when the order isn't FILLED, or when no template was attached
        in the first place. Operator gets a clear reason string back
        rather than a generic 4xx.
        """
        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder, OrderTemplate
        from backend.api.algo.template_attach import apply_template_to_order

        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == algo_order_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="order not found")
            if row.template_id is None:
                return {"ok": False, "reason": "no template attached to this order"}
            if row.attached_gtts_json:
                return {"ok": False, "reason": "template already attached — nothing to retry"}
            if (row.status or "").upper() != "FILLED":
                return {"ok": False, "reason": f"parent must be FILLED to attach (status={row.status})"}

            # Sanity-check the template row still exists before
            # dispatching — apply_template_to_order does its own load via
            # load_template_for_slug_or_id, but we want a clear "deleted"
            # reason for the operator rather than a "no template found"
            # surface from deep inside the attach pipeline.
            tpl = (await s.execute(
                _sql_select(OrderTemplate.id).where(OrderTemplate.id == row.template_id)
            )).scalar_one_or_none()
            if tpl is None:
                return {"ok": False, "reason": f"template #{row.template_id} no longer exists"}

            # Apply path mirrors the mode the parent was placed in. Paper
            # mode flows through the live attach path (real GTTs against
            # the broker — paper just refers to the parent's mode); sim
            # mode flows through SimDriver.
            apply_path = "sim" if (row.mode or "").lower() == "sim" else "live"
            # Re-use the per-submit overrides persisted on the row.
            _retry_overrides: dict = {}
            if row.template_overrides_json:
                try:
                    import json as _json2
                    _parsed = _json2.loads(row.template_overrides_json)
                    if isinstance(_parsed, dict):
                        _retry_overrides = _parsed
                except Exception:
                    pass
            result = await apply_template_to_order(
                template_id=row.template_id,
                template_slug=None,
                overrides=_retry_overrides,
                parent_account=row.account or "",
                parent_symbol=row.symbol or "",
                # Audit fix: AlgoOrder has no `.side` column; the
                # field is `transaction_type`. The previous `row.side`
                # raised AttributeError on every Re-attach click
                # before any broker call.
                parent_side=row.transaction_type or "BUY",
                # Audit fix: prefer filled_quantity over original quantity
                # when accumulated. Without this, a retry on a row that
                # had partially filled (e.g. 25 of 50 lots) sizes the exit
                # GTT at the original 50 — over-hedging on a SELL or
                # over-flattening on a BUY. Same fix-pattern as the
                # postback path.
                parent_qty=(int(row.filled_quantity)
                            if int(row.filled_quantity or 0) > 0
                            else int(row.quantity or 0)),
                parent_exchange=row.exchange or "NFO",
                parent_fill_price=float(row.fill_price or row.initial_price or 0),
                parent_product=row.product or "NRML",
                parent_order_id=row.id,
                apply_path=apply_path,
            )
            if result is None:
                return {"ok": False, "reason": "apply_template_to_order returned no result (template missing or empty plan)"}
            # Audit fix (H-7) + redo-audit (Sc.5a / 5b / 5c) — persist
            # attached_gtts_json on the row mirroring the EXACT shape
            # `_fire_template_attach_on_fill` writes. Pre-fix retry_template
            # (a) landed GTTs but reported `attached: false` so the next
            # retry doubled at broker, and (b) omitted `current_trigger`
            # + `sl_trail_pct` entries so the trail-stop poller silently
            # refused to ratchet retry-attached SLs forever.
            import json as _json_retry
            _attached_payload = []
            if result.plan and result.gtt_ids:
                for _spec, _gid in zip(result.plan.gtts, result.gtt_ids):
                    _entry = {
                        "kind":          "gtt",
                        "label":         _spec.label,
                        "id":            _gid,
                        "trigger_values": list(_spec.trigger_values or []),
                        "trigger_type":  str(_spec.trigger_type),
                    }
                    # Sc.5a / 5b — trail-stop scaffolding. The trail
                    # poller reads `sl_trail_pct` to decide whether to
                    # ratchet + `current_trigger` to know what to beat.
                    # For two-leg OCO the SL trigger sits at
                    # trigger_values[-1] (orders[1] index), single-SL is
                    # the only trigger ([0]). Mirrors lines 826-857 in
                    # the on-fill wrapper exactly.
                    if _spec.sl_trail_pct is not None and _spec.trigger_values:
                        _entry["sl_trail_pct"]    = float(_spec.sl_trail_pct)
                        _last_trig = float(_spec.trigger_values[-1])
                        _entry["current_trigger"] = _last_trig
                        if str(_spec.trigger_type) == "two-leg" \
                                and len(_spec.trigger_values) >= 2:
                            _entry["tp_trigger"]  = float(_spec.trigger_values[0])
                        _entry["highest_ltp"]     = float(result.plan.parent_fill_price)
                        _entry["lowest_ltp"]      = float(result.plan.parent_fill_price)
                    elif str(_spec.trigger_type) == "two-leg" \
                            and len(_spec.trigger_values) >= 2:
                        # Non-trail two-leg still wants tp_trigger for
                        # any modify_gtt round-trip the operator might
                        # trigger later (e.g. cancel + recreate flow).
                        _entry["tp_trigger"]      = float(_spec.trigger_values[0])
                    # Parent metadata — needed by both trail-stop poller
                    # (rebuild orders_payload) and OCO pair-watcher
                    # (sibling cancel routing). Always populated so the
                    # downstream consumers don't need to look up the
                    # parent row again.
                    _entry["parent_side"]     = str(result.plan.parent_side)
                    _entry["parent_symbol"]   = str(result.plan.parent_symbol)
                    _entry["parent_exchange"] = str(result.plan.parent_exchange)
                    _entry["parent_account"]  = str(result.plan.parent_account)
                    _entry["parent_qty"]      = int(result.plan.parent_qty)
                    _entry["parent_product"]  = str(row.product or "NRML")
                    _attached_payload.append(_entry)
            if result.wing_order_id:
                _attached_payload.append({
                    "kind":  "wing",
                    "label": "Wing",
                    "id":    result.wing_order_id,
                })
            if _attached_payload:
                row.attached_gtts_json = _json_retry.dumps(_attached_payload)
                if result.errors:
                    row.detail = ((row.detail or "")[:200]
                                  + " · retry attach: "
                                  + "; ".join(result.errors[:2]))[:240]
                await s.commit()
                await s.refresh(row)

        # Sc.5c — distinguish full success from silent no-op. When the
        # plan resolved but produced no GTTs and no wing (all branches
        # rejected by overrides or chain-scan), the response previously
        # claimed `ok: true, attached: false` which read like a success.
        # Treat the empty-payload case as a failure with a clear reason.
        _attached_now = row.attached_gtts_json is not None
        if not _attached_now and not (result.errors or []):
            return {
                "ok":            False,
                "reason":        "Template produced no GTTs and no wing — nothing to attach (check overrides + chain-scan filters)",
                "wing_order_id": result.wing_order_id,
                "gtt_ids":       result.gtt_ids,
                "notes":         result.plan.notes if result.plan else [],
                "errors":        result.errors,
                "attached":      False,
            }
        return {
            "ok": True,
            "wing_order_id": result.wing_order_id,
            "gtt_ids":       result.gtt_ids,
            "notes":         result.plan.notes if result.plan else [],
            "errors":        result.errors,
            "attached":      _attached_now,
        }

    @post("/algo/reconcile")
    async def reconcile_algo_orders(self, request: Request) -> dict:
        """Admin sweep — re-syncs stale OPEN algo_orders rows against the
        broker. Operator-triggered after observing rows stuck at OPEN
        when the broker has already moved the order to a terminal
        status (postback miss, network drop, etc.).

        Strategy: group OPEN live rows by account, fetch each
        account's order book once, then map Kite's per-order status
        through _KITE_STATUS_MAP. If the broker no longer carries the
        order_id at all, mark it UNFILLED (best-effort — operator can
        still re-fire manually).
        """
        if not is_admin_request(request):
            raise HTTPException(status_code=403, detail="admin only")

        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.shared.brokers import get_broker
        from datetime import datetime, timezone

        _KITE_TO_ALGO = {
            "COMPLETE":  "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED":  "REJECTED",
            "EXPIRED":   "UNFILLED",
        }

        # Pull every live OPEN algo_orders row.
        async with async_session() as s:
            rows = (await s.execute(
                _sql_select(AlgoOrder).where(
                    AlgoOrder.mode == "live",
                    AlgoOrder.status == "OPEN",
                )
            )).scalars().all()

            if not rows:
                return {"scanned": 0, "updated": 0, "missing": 0}

            # Group rows by account so each broker call is one lookup.
            by_acct: dict[str, list] = {}
            for r in rows:
                by_acct.setdefault(str(r.account or ""), []).append(r)

            updated = 0
            missing = 0
            # Audit fix — collect rows that need template attach AFTER
            # commit instead of firing inline. The attach pipeline opens
            # its own session and reads the row by id; if we call inside
            # this loop, the in-memory mutations (status=FILLED,
            # fill_price set) haven't committed yet so the attach reads
            # the pre-commit state. Now ordering is: mutate in-memory →
            # commit → fire attach → attach reads committed FILLED state.
            _attach_queue: list = []

            for acct, acct_rows in by_acct.items():
                # Route via the broker registry — Connections.conn.get()
                # returned the raw KiteConnection wrapper which has no
                # .orders() method, so every reconcile silently failed
                # at the broker and the operator's "Reconcile" click did
                # nothing (the GOLDM 146000CE stayed OPEN even after
                # filling at Kite). Same KiteConnection bug fixed in
                # kill_chase (commit 41133e16).
                try:
                    broker = get_broker(acct)
                except Exception as e:
                    logger.warning(f"reconcile: get_broker({acct}) failed: {e}")
                    continue
                try:
                    broker_orders = await asyncio.to_thread(broker.orders)
                except Exception as e:
                    logger.warning(f"reconcile: broker.orders() for {acct} failed: {e}")
                    continue
                by_id = {str(o.get("order_id")): o for o in (broker_orders or [])}

                for r in acct_rows:
                    bid = str(r.broker_order_id or "")
                    if not bid:
                        continue
                    bo = by_id.get(bid)
                    if bo is None:
                        # Broker doesn't carry this order any more.
                        r.status = "UNFILLED"
                        r.detail = (r.detail or "") + " [reconciled — broker no longer carries order_id]"
                        missing += 1
                        continue
                    kite_status = str(bo.get("status") or "").upper()
                    new_status = _KITE_TO_ALGO.get(kite_status)
                    if new_status and r.status != new_status:
                        r.status = new_status
                        if new_status == "FILLED":
                            try:
                                ap = bo.get("average_price") or bo.get("price")
                                if ap is not None:
                                    r.fill_price = float(ap)
                                r.filled_at = datetime.now(timezone.utc)
                            except (TypeError, ValueError):
                                pass
                            # Sprint A — missed postback recovered via
                            # reconcile must still fire the template
                            # attach. Pre-fix, a templated row reconciled
                            # to FILLED silently lost its TP/SL bracket.
                            # Audit fix — defer attach to AFTER commit.
                            _attach_queue.append(r)
                        updated += 1

            await s.commit()
            if updated or missing:
                invalidate("orders")
            # Fire template attach AFTER commit so the new session opened
            # inside `_fire_template_attach_on_fill` reads the committed
            # FILLED state, not the pre-commit OPEN state.
            for _r in _attach_queue:
                _maybe_fire_template_attach_for_reconcile(_r)

        return {"scanned": len(rows), "updated": updated, "missing": missing}

    @post("/{broker_order_id:str}/reconcile")
    async def reconcile_single_order(
        self,
        broker_order_id: str,
        request: Request,
        data: ReconcileSingleRequest,
    ) -> dict:
        """Per-card reconcile — re-sync ONE order against the broker.
        Looks the broker_order_id up in the broker's live order book,
        then maps any terminal status back onto the matching algo_orders
        row (when one exists). Operator-triggered from the OrderCard
        Reconcile button so a stuck row can be cleared without running
        the full sweep across every account.
        """
        if not is_admin_request(request):
            raise HTTPException(status_code=403, detail="admin only")

        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.shared.brokers import get_broker
        from datetime import datetime, timezone

        _KITE_TO_ALGO = {
            "COMPLETE":  "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED":  "REJECTED",
            "EXPIRED":   "UNFILLED",
        }

        acct = (data.account or "").strip()
        if not acct:
            raise HTTPException(status_code=400, detail="account required")

        # Same broker-registry routing as reconcile_algo_orders /
        # kill_chase — Connections().conn.get(acct) returns the raw
        # KiteConnection wrapper which has no .orders() method.
        try:
            broker = get_broker(acct)
        except Exception as e:
            raise HTTPException(status_code=404,
                                detail=f"account {acct} not loaded ({e})")

        try:
            broker_orders = await asyncio.to_thread(broker.orders)
        except Exception as e:
            logger.warning(f"reconcile {broker_order_id}: broker.orders() failed: {e}")
            raise HTTPException(status_code=502, detail=f"broker fetch failed: {e}")

        by_id = {str(o.get("order_id")): o for o in (broker_orders or [])}
        bo = by_id.get(str(broker_order_id))
        kite_status = str(bo.get("status") or "").upper() if bo else None
        target = _KITE_TO_ALGO.get(kite_status) if kite_status else None

        _attach_after_commit = False
        async with async_session() as s:
            r = (await s.execute(
                _sql_select(AlgoOrder).where(
                    AlgoOrder.broker_order_id == str(broker_order_id),
                )
            )).scalars().first()
            updated = False
            note = ""

            if r is None:
                if bo is None:
                    note = f"broker has no order {broker_order_id}"
                else:
                    note = f"broker status={kite_status} (no algo row to update)"
            elif bo is None:
                if r.status == "OPEN":
                    r.status = "UNFILLED"
                    r.detail = (r.detail or "") + " [reconciled — broker no longer carries order_id]"
                    updated = True
                    note = "broker no longer carries order_id"
                else:
                    note = "broker has no order and algo row already terminal"
            else:
                if target and r.status != target:
                    r.status = target
                    if target == "FILLED" and bo.get("average_price"):
                        try:
                            r.fill_price = float(bo["average_price"])
                            r.filled_at = datetime.now(timezone.utc)
                        except Exception:
                            pass
                        # Audit fix (C-4) — defer template attach to
                        # AFTER commit. Pre-fix the attach pipeline
                        # opened its own session and read the row by id
                        # WHILE the outer session still held the row at
                        # status=OPEN in committed state. GTT sizes were
                        # computed from pre-commit in-memory mutation;
                        # if the outer commit failed after GTTs landed
                        # at broker, no cleanup. reconcile_algo_orders
                        # was already fixed via _attach_queue (commit
                        # 257c1ed1); this single-order path was missed.
                        _attach_after_commit = True
                    r.detail = (r.detail or "") + f" [reconciled → {target}]"
                    updated = True
                    note = f"broker status={kite_status} → {target}"
                elif target and r.status == target:
                    note = f"already {target}"
                else:
                    note = f"broker status={kite_status} (no algo mapping)"

            if updated:
                await s.commit()
                invalidate("orders")
            # Fire template attach AFTER commit so the attach pipeline's
            # new session reads the committed FILLED state.
            if updated and r is not None and _attach_after_commit:
                _maybe_fire_template_attach_for_reconcile(r)

        return {
            "broker_order_id": str(broker_order_id),
            "broker_status":   kite_status,
            "algo_status":     (r.status if r is not None else None),
            "updated":         updated,
            "note":            note,
        }

    @get("/{order_id:int}/events")
    async def order_events(self, order_id: int, request: Request) -> list[AlgoOrderEventInfo]:
        """Per-order event timeline, oldest-first.

        Returns every row in algo_order_events for the given AlgoOrder id.
        account values inside payload_json are masked for non-admin (demo)
        callers so raw account codes are never exposed.
        """
        import re
        from sqlalchemy import asc, select as _sql_select
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrderEvent as _AlgoOrderEvent

        async with _async_session() as s:
            rows = (await s.execute(
                _sql_select(_AlgoOrderEvent)
                .where(_AlgoOrderEvent.order_id == order_id)
                .order_by(asc(_AlgoOrderEvent.ts))
            )).scalars().all()

        # Admin/designated only — partners + demo see masked codes.
        do_mask = not is_admin_request(request)

        def _mask_payload(raw: str | None) -> str | None:
            if raw is None or not do_mask:
                return raw
            # Replace bare account codes like ZG0790 → ZG#### in the JSON string.
            return re.sub(r'\b([A-Z]{2})\d{4,8}\b', r'\1####', raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask_payload(r.payload_json),
            )
            for r in rows
        ]

    @get("/events/recent")
    async def recent_order_events(
        self,
        request: Request,
        limit: int = 50,
        status: str = "open",
    ) -> list[AlgoOrderEventInfo]:
        """Recent events for orders whose current status matches the filter.

        Default: status=open — useful for the navbar chase chip to show what's
        happening on actively-chasing orders without listing the whole history.

        status='open'  → OPEN orders only (default)
        status='all'   → every order regardless of status
        status=<other> → exact case-insensitive status match
        """
        import re
        from sqlalchemy import asc, desc, select as _sql_select, and_
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrder as _AlgoOrder, AlgoOrderEvent as _AlgoOrderEvent

        limit = max(1, min(limit, 500))
        status_filter = (status or "open").strip().upper()

        async with _async_session() as s:
            if status_filter == "ALL":
                # All orders — pull latest N events across the full table.
                rows = (await s.execute(
                    _sql_select(_AlgoOrderEvent)
                    .order_by(desc(_AlgoOrderEvent.ts))
                    .limit(limit)
                )).scalars().all()
                # Return oldest-first within the window.
                rows = list(reversed(rows))
            else:
                # Fetch matching order ids first, then their events.
                kite_status = "OPEN" if status_filter == "OPEN" else status_filter
                order_ids_q = (await s.execute(
                    _sql_select(_AlgoOrder.id).where(
                        _AlgoOrder.status == kite_status
                    ).order_by(desc(_AlgoOrder.created_at)).limit(200)
                )).scalars().all()
                if not order_ids_q:
                    return []
                rows = (await s.execute(
                    _sql_select(_AlgoOrderEvent)
                    .where(_AlgoOrderEvent.order_id.in_(order_ids_q))
                    .order_by(asc(_AlgoOrderEvent.ts))
                    .limit(limit)
                )).scalars().all()

        # Admin/designated only — partners + demo see masked codes.
        do_mask = not is_admin_request(request)

        def _mask_payload(raw: str | None) -> str | None:
            if raw is None or not do_mask:
                return raw
            return re.sub(r'\b([A-Z]{2})\d{4,8}\b', r'\1####', raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask_payload(r.payload_json),
            )
            for r in rows
        ]

    @post("/preflight")
    async def preflight_order(self, request: Request) -> dict:
        """
        Pre-validate an order before any broker call.

        Runs four checks: ACCOUNT_UNKNOWN → SEGMENT_INACTIVE → QTY_FREEZE →
        MARGIN_SHORTFALL. Returns ok=true when all pass, or ok=false with a
        structured `blocked[]` list each carrying code / reason / fix / data.

        Gated by jwt_guard (admin) — demo callers see 401; this endpoint
        intentionally never proxies to broker on demo sessions.
        """
        import msgspec as _ms
        from backend.api.algo.actions import run_preflight

        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: preflight requires an authenticated session.")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        account       = str(body.get("account") or "").strip()
        exchange      = str(body.get("exchange") or "NFO").strip().upper()
        tradingsymbol = str(body.get("tradingsymbol") or "").strip().upper()
        quantity      = int(body.get("quantity") or 0)
        order_type    = str(body.get("order_type") or "LIMIT").strip().upper()
        product       = str(body.get("product") or "NRML").strip().upper()
        variety       = str(body.get("variety") or "regular").strip().lower()
        side          = str(body.get("side") or body.get("transaction_type") or "BUY").strip().upper()
        price         = float(body.get("price") or 0)
        trigger_price = float(body.get("trigger_price") or 0)
        # Optional paired legs — typically the template's auto-wing for
        # a SELL option. Factored into basket_order_margins so the
        # "Required" number reads as the bracketed-strategy margin,
        # not the naked-short margin. Frontend pipes this in only when
        # the preview plan resolved a wing successfully.
        paired_legs   = body.get("paired_legs") or []
        if not isinstance(paired_legs, list):
            paired_legs = []

        if not account:
            raise HTTPException(status_code=400, detail="account is required")
        if not tradingsymbol:
            raise HTTPException(status_code=400, detail="tradingsymbol is required")
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="quantity must be > 0")
        if exchange not in _EXCHANGES:
            raise HTTPException(status_code=400,
                detail=f"exchange must be one of {sorted(_EXCHANGES)}")
        if side not in _TXN_TYPES:
            raise HTTPException(status_code=400, detail="side must be BUY or SELL")

        result = await run_preflight(account, {
            "exchange":         exchange,
            "tradingsymbol":    tradingsymbol,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          product,
            "variety":          variety,
            "transaction_type": side,
            "price":            price,
            "trigger_price":    trigger_price,
        }, paired_orders=paired_legs)
        return result

    @post("/place")
    async def place_order(self, data: PlaceOrderRequest, request: Request) -> PlaceOrderResponse:
        """
        DEPRECATED — kept only for any external scripts that may still
        be hitting this endpoint. Every frontend surface now opens the
        shared <OrderTicket> and submits via POST /api/orders/ticket
        (TicketOrderRequest), which records an AlgoOrder row, supports
        chase / chase_aggressiveness, and routes by mode (paper vs
        live). Direct-broker placement here skips all that bookkeeping.
        New integrations should use /ticket instead.
        """
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: use OrderTicket → PAPER.")
        _validate_place(data)
        broker = _broker_for(data.account)
        masked = mask_account(data.account)
        # Surface a deprecation warning every time this path is used
        # so we can spot any lingering callers in the logs and migrate
        # them to /ticket.
        logger.warning(
            f"[deprecated] POST /api/orders/place hit by [{masked}] — "
            f"use POST /api/orders/ticket instead. "
            f"{data.transaction_type} {data.quantity} {data.tradingsymbol}"
        )
        try:
            from backend.shared.brokers.kite import get_lot_size
            _ls_dep = await get_lot_size(data.exchange, data.tradingsymbol.upper())
            _kq_dep = broker.translate_qty(data.exchange, data.quantity, _ls_dep)
            order_id = broker.place_order(
                variety=data.variety,
                exchange=data.exchange,
                tradingsymbol=data.tradingsymbol.upper(),
                transaction_type=data.transaction_type,
                quantity=_kq_dep,
                product=data.product,
                order_type=data.order_type,
                price=data.price,
                trigger_price=data.trigger_price,
                validity=data.validity,
                tag=data.tag or "ramboq",
            )
            invalidate("orders")  # force fresh fetch on next request
            logger.info(f"Order placed: {order_id} [{masked}] {data.transaction_type} "
                        f"{data.quantity} {data.tradingsymbol}")
            try:
                from backend.api.algo.agent_engine import record_manual_event
                asyncio.create_task(record_manual_event(
                    outcome="action_success", source="place",
                    account=data.account, symbol=data.tradingsymbol,
                    exchange=data.exchange, side=data.transaction_type,
                    qty=data.quantity, mode="live", order_id=str(order_id),
                ))
            except Exception:
                pass
            return PlaceOrderResponse(order_id=str(order_id), account=masked)
        except Exception as e:
            logger.error(f"Place order failed [{masked}]: {e}")
            try:
                from backend.shared.helpers.alert_utils import send_order_failure_alert
                send_order_failure_alert(
                    account=data.account, symbol=data.tradingsymbol,
                    exchange=data.exchange, side=data.transaction_type,
                    qty=data.quantity, mode="live", source="agent:manual:place",
                    error=str(e),
                )
            except Exception:
                pass
            try:
                from backend.api.algo.agent_engine import record_manual_event
                asyncio.create_task(record_manual_event(
                    outcome="action_failure", source="place",
                    account=data.account, symbol=data.tradingsymbol,
                    exchange=data.exchange, side=data.transaction_type,
                    qty=data.quantity, mode="live", error=str(e),
                ))
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=str(e))

    @post("/ticket/preview")
    async def ticket_preview(self, data: TicketPreviewRequest, request: Request) -> TicketPreviewResponse:
        """
        Preview what the ticket WILL place — resolves the chosen
        template + overrides into a concrete TemplatePlan WITHOUT
        making any broker / DB writes. OrderTicket calls this on
        every relevant field change so the operator sees the planned
        TP / SL / Wing artefacts inline before they hit Submit.

        Returns the plan as a plain dict — see template_attach.TemplatePlan
        for the shape. Empty plan (no gtts + no wing) when neither a
        template nor overrides were supplied.
        """
        from backend.api.algo.template_attach import apply_template_to_order

        result = await apply_template_to_order(
            template_id=data.template_id,
            template_slug=None,
            overrides=_ticket_overrides_dict(data),
            parent_account=(data.account or ""),
            parent_symbol=(data.tradingsymbol or "").upper(),
            parent_side=(data.side or "").upper(),
            parent_qty=int(data.quantity or 0),
            parent_exchange=(data.exchange or "NFO"),
            parent_fill_price=float(data.reference_price or 0.0),
            parent_product=(data.product or "NRML"),
            apply_path="preview",
        )
        if result is None:
            # No template selected + no overrides — return a stub plan
            # so the UI can still display "no exit attachments planned"
            # without special-casing None.
            empty = {
                "template_id":   None,
                "template_name": "(none)",
                "template_slug": None,
                "parent_account":    data.account,
                "parent_symbol":     (data.tradingsymbol or "").upper(),
                "parent_side":       (data.side or "").upper(),
                "parent_qty":        data.quantity,
                "parent_exchange":   data.exchange,
                "parent_fill_price": float(data.reference_price or 0.0),
                "gtts": [],
                "wing": None,
                "notes": [],
            }
            return TicketPreviewResponse(plan=empty)
        return TicketPreviewResponse(plan=result.plan.to_dict())

    @post("/ticket")
    async def ticket_order(self, data: TicketOrderRequest, request: Request) -> TicketOrderResponse:
        """
        Operator-initiated order from the reusable <OrderTicket> on
        any algo page. Routes by `mode`:
          - paper → AlgoOrder row + register_open_order on the prod
                    paper engine. The engine's 5-second tick runs
                    fill / modify / unfilled lifecycle off real bid/
                    ask via LiveQuoteSource. Same chase loop agent
                    fires use, just operator-triggered.
          - live  → phase 3 (real broker placement).

        Returns the AlgoOrder row id; UI tracks it via the existing
        `/api/orders/algo/recent?mode=paper` endpoint or the live
        Order tab in the LogPanel.
        """
        from datetime import datetime
        from backend.api.algo.paper import get_prod_paper_engine
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        if data.mode == "draft":
            raise HTTPException(status_code=400,
                detail="Drafts are client-side; the backend doesn't track them.")
        if data.mode not in ("paper", "live"):
            raise HTTPException(status_code=400,
                detail=f"unknown mode '{data.mode}'")

        # Demo mode chokepoint: reject up-front with a clear "demo
        # cannot place orders" message. Earlier we silently downgraded
        # LIVE → PAPER and let the request fall through to the account
        # check, which then 400-ed with "Account is required" because
        # anonymous demo sessions don't carry a broker account. The
        # operator-facing error read as a missing-field problem instead
        # of the actual gate.
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo mode — orders cannot be placed. Sign in to trade.")

        # Slice 7e — trader scoping enforcement on strategy_id.
        # When a trader places an order with strategy_id set, the
        # strategy MUST be in their `assigned_strategies` list.
        # Admin / risk / ops / observer / demo are firm-wide and
        # pass through. Empty assigned_strategies for a trader is
        # the fail-safe initial state (slice 5) — they can't trade
        # anything until admin grants explicit strategies.
        if data.strategy_id:
            from backend.api.rbac import (
                normalise_role, resolve_role_from_connection,
                user_scope_for_connection,
            )
            role = normalise_role(resolve_role_from_connection(request))
            if role == "trader":
                _, allowed_strategies = await user_scope_for_connection(request)
                if int(data.strategy_id) not in (allowed_strategies or []):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"Strategy {data.strategy_id} not in your "
                            f"assigned_strategies list. Ask an admin to "
                            f"grant access, or pick a strategy you own."
                        ),
                    )

        # Server-side enum validation — same set the regular /place
        # endpoint uses. Kite errors on invalid values look cryptic
        # ("Invalid input — 400"); reject early with a clear reason.
        side = (data.side or "").upper()
        if side not in _TXN_TYPES:
            raise HTTPException(status_code=400, detail="side must be BUY or SELL")
        sym = (data.tradingsymbol or "").upper().strip()
        qty = int(data.quantity or 0)
        if not sym or qty <= 0:
            raise HTTPException(status_code=400,
                detail="tradingsymbol and quantity > 0 are required")
        if data.exchange   and data.exchange   not in _EXCHANGES:
            raise HTTPException(status_code=400,
                detail=f"exchange must be one of {sorted(_EXCHANGES)}")
        if data.product    and data.product    not in _PRODUCTS:
            raise HTTPException(status_code=400,
                detail=f"product must be one of {sorted(_PRODUCTS)}")
        if data.order_type and data.order_type not in _ORDER_TYPES:
            raise HTTPException(status_code=400,
                detail=f"order_type must be one of {sorted(_ORDER_TYPES)}")
        if data.variety    and data.variety    not in _VARIETIES:
            raise HTTPException(status_code=400,
                detail=f"variety must be one of {sorted(_VARIETIES)}")

        # Validate input parameters first (cheap, deterministic — bad
        # inputs should fail with 400, not 409 from the operational
        # market-hours gate below).
        if data.order_type in ("LIMIT", "SL") and not data.price:
            raise HTTPException(status_code=400, detail="price is required for LIMIT/SL")
        if data.order_type in ("SL", "SL-M") and not data.trigger_price:
            raise HTTPException(status_code=400, detail="trigger_price is required for SL/SL-M")

        # Resolve + validate account BEFORE the market-hours gate. An
        # unknown account is a bad-input error (400), not an operational
        # market-hours violation (409). Tests exercise this ordering by
        # posting missing/unknown-account payloads outside market hours
        # and asserting 400.
        conns = Connections()
        account = (data.account or "").strip()
        if not account:
            raise HTTPException(status_code=400, detail="Account is required.")
        if account not in conns.conn:
            raise HTTPException(status_code=400, detail=f"Unknown account: {account}.")

        # Slice 7i — capacity guardrail. When the strategy has a
        # capacity_cap_inr ceiling set, refuse the order if it would
        # push the strategy's open notional over the cap. Skips when:
        #   - no strategy attached (data.strategy_id falsy)
        #   - strategy has no cap (capacity_cap_inr is NULL)
        #   - order is a CLOSE intent (existing opposite-side open
        #     lot exists for this strategy + sym → consumes it →
        #     reduces exposure → won't trip cap)
        # On breach: 403 with a clear breach amount so operator knows
        # exactly how much to trim qty or how much to raise the cap.
        if data.strategy_id:
            await _enforce_capacity_guard(
                strategy_id=int(data.strategy_id),
                account=account,
                tradingsymbol=sym,
                side_kite=side,
                quantity=qty,
                price_hint=(float(data.price)
                            if data.price is not None else None),
            )

        # Phase 23 — per-order exchange-open gate.
        # Block submission when the target exchange's market segment
        # is closed. Applies to BOTH paper and live (paper is meant to
        # mirror live; Kite itself would reject a 21:00 NSE order).
        # Sim mode (driven by SimDriver, not this route) bypasses
        # naturally — it doesn't call /ticket.
        from backend.api.algo.agent_engine import _symbol_exchange_open, _build_now_ctx
        target_exchange = data.exchange or "NFO"   # default matches struct
        if not _symbol_exchange_open(target_exchange, _build_now_ctx()):
            seg = (target_exchange or "").upper()
            raise HTTPException(status_code=409,
                detail=(f"Exchange {seg} is closed. Orders for {sym} "
                        f"can only be placed during {seg}'s market "
                        f"hours (IST holidays apply)."))

        # Tick-size sanitisation. Kite rejects orders whose price isn't
        # an exact tick multiple ("Exchange invalid price — entered
        # price is not as per ticker price"). Tick varies by
        # instrument: most NSE F&O / equity = ₹0.05; MCX commodities
        # like CRUDEOIL = ₹1; some bullion = ₹1; etc. The OrderTicket
        # frontend can't always know the correct tick, and the
        # previous hardcoded-₹0.05 path let MCX commodities through
        # with halves (e.g. ₹9961.50) that Kite then rejected.
        # _align_price_to_tick() reads the actual tick from the
        # instruments cache and rounds to the nearest valid multiple.
        _exch_for_snap = (data.exchange or "NFO")
        data.price         = await _align_price_to_tick(
            _exch_for_snap, sym, data.price)
        data.trigger_price = await _align_price_to_tick(
            _exch_for_snap, sym, data.trigger_price)

        # `account` / `conns` were resolved + validated above (before
        # the market-hours gate) so a bad account fails fast with 400
        # rather than getting swallowed by the 409 exchange-closed
        # response on off-hours requests.

        # ─── LIVE branch ─────────────────────────────────────────────
        # Two gates: branch + per-action setting flag. Both must be
        # truthy.
        #
        # Two paths within LIVE:
        #   chase=True + LIMIT → background chase loop (chase.py).
        #     The first place_order returns synchronously so the ticket
        #     gets an order_id to display; the loop keeps cancel-and-
        #     re-placing in the background per L/M/H aggressiveness
        #     until the order fills or the attempt cap is hit. The
        #     order_id mutates per attempt; the response carries the
        #     FIRST one. Operators see the current order via /api/orders.
        #   chase=False or non-LIMIT → single-shot kite.place_order
        #     (the existing direct-broker path; preserved for MARKET/
        #     SL-M and for operators who explicitly opted out).
        from backend.shared.helpers.utils import is_prod_branch, config
        from backend.shared.helpers.settings import get_bool

        # Diagnostic so we can see the resolved values when an operator
        # reports "I picked LIVE but orders are paper".
        _ptm_now = get_bool("execution.paper_trading_mode", False)
        _shadow_now = get_bool("execution.shadow_mode", False)
        logger.info(
            f"[ticket-mode] requested={data.mode!r} "
            f"paper_trading_mode={_ptm_now} shadow_mode={_shadow_now} "
            f"branch={config.get('deploy_branch','?')!r}"
        )

        if data.mode == "live":
            if not is_prod_branch():
                raise HTTPException(status_code=403,
                    detail="LIVE mode is disabled on non-prod branches; use PAPER on dev.")
            if _ptm_now:
                raise HTTPException(status_code=403,
                    detail="LIVE disabled — paper_trading_mode is ON. Toggle in /admin/execution (LIVE mode).")

            # ── Circuit breaker ───────────────────────────────────────
            # Block the operator from re-attempting the SAME live order
            # after 3+ rejections within the last hour. Surfaces a
            # 423 with a clear "breaker tripped" message + fires a
            # Telegram/email alert (cooldown'd) so the operator knows
            # to investigate the root cause (margin shortfall, API key
            # segment scope, etc.) before re-trying.
            _bk_key = _rejection_key(account, sym, side, qty)
            _bk_count = _rejection_count(_bk_key)
            if _bk_count >= _REJECTION_THRESHOLD:
                _maybe_send_breaker_alert(_bk_key, account, sym, side, qty,
                                           f"{_bk_count} rejections in last hour")
                logger.warning(
                    f"[BREAKER] BLOCKED ticket — {_bk_key} has "
                    f"{_bk_count} rejections in last hour"
                )
                raise HTTPException(
                    status_code=423,
                    detail=(f"Circuit breaker: {_REJECTION_THRESHOLD}+ rejections "
                            f"in the last hour for {sym} {side} {qty} on {account}. "
                            "Further attempts blocked until the breaker resets. "
                            "Check margin / segment scope, then wait or reset."),
                )

            # ── Preflight gate ────────────────────────────────────────
            # Run before any broker call; surface structured blockers to
            # the operator with specific fix hints. Skipped for PAPER /
            # SHADOW / SIM (paper engine uses basket_margin internally;
            # shadow validates its own way).
            from backend.api.algo.actions import run_preflight as _run_preflight
            try:
                _pf = await _run_preflight(account, {
                    "exchange":         (data.exchange or "NFO"),
                    "tradingsymbol":    sym,
                    "quantity":         qty,
                    "order_type":       (data.order_type or "LIMIT"),
                    "product":          (data.product or "NRML"),
                    "variety":          (data.variety or "regular"),
                    "transaction_type": side,
                    "price":            data.price or 0,
                    "trigger_price":    data.trigger_price or 0,
                })
            except Exception as _pf_err:
                # Preflight itself blew up (Kite hung / SDK error / etc).
                # Surface it explicitly so the operator sees something.
                logger.error(f"[LIVE-TICKET] preflight raised for {account} "
                             f"{sym}: {_pf_err}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Preflight check failed: {str(_pf_err)[:240]} — "
                           "broker may be unreachable. Try again.",
                ) from _pf_err
            logger.info(
                f"[LIVE-TICKET] preflight {('ok' if _pf['ok'] else 'BLOCKED')} "
                f"acct={account} {sym} {side} qty={qty}"
                + ("" if _pf["ok"]
                   else f" — {len(_pf['blocked'])} blocker(s): "
                        f"{', '.join(b.get('code','?') for b in _pf['blocked'])}")
            )
            if not _pf["ok"]:
                # Persist a REJECTED AlgoOrder + preflight_block event so
                # the operator's Log + Orders panels surface the rejection
                # (instead of silently 422-ing into the void).
                try:
                    from backend.api.algo.agent_engine import get_agent_id_by_slug as _g_aid
                    from backend.api.algo.order_events import write_event as _write_ev
                    _live_manual_aid: int | None = None
                    try:
                        _live_manual_aid = await _g_aid("manual")
                    except Exception:
                        pass
                    async with async_session() as _s:
                        _row = AlgoOrder(
                            account=account, symbol=sym,
                            exchange=(data.exchange or "NFO"),
                            transaction_type=side, quantity=qty,
                            initial_price=(float(data.price)
                                           if data.price is not None else None),
                            status="REJECTED", engine="live", mode="live",
                            agent_id=_live_manual_aid,
                            strategy_id=data.strategy_id,
                            detail=f"preflight blocked: "
                                   f"{', '.join(b.get('code','?') for b in _pf['blocked'])}",
                        )
                        _s.add(_row)
                        await _s.commit()
                        _algo_id = _row.id
                    await _write_ev(
                        _algo_id, "preflight_block",
                        f"{', '.join(b.get('reason','?') for b in _pf['blocked'])[:300]}",
                        payload={"blocked": _pf["blocked"],
                                 "diagnostics": _pf.get("diagnostics", {})},
                    )
                except Exception as _ev_err:
                    logger.warning(f"[LIVE-TICKET] preflight_block log failed: "
                                   f"{_ev_err}")
                # Tick the circuit breaker — N+1 rejection on this key.
                _new_count = _record_rejection(_bk_key)
                if _new_count >= _REJECTION_THRESHOLD:
                    _reason = ', '.join(b.get('code', '?') for b in _pf['blocked'])
                    _maybe_send_breaker_alert(_bk_key, account, sym, side, qty, _reason)
                raise HTTPException(
                    status_code=422,
                    detail={"blocked": _pf["blocked"],
                            "diagnostics": _pf.get("diagnostics", {})},
                )

            order_type = (data.order_type or "LIMIT")
            chase_eligible = (data.chase
                              and order_type == "LIMIT"
                              and data.price is not None
                              and data.price > 0)

            try:
                # Phase 0.5 — write the AlgoOrder row BEFORE the chase
                # spawns so we can plumb its id into chase_order. The
                # chase loop syncs broker_order_id on every re-place
                # and identifies the row by id in _emit_chase_terminal,
                # so chased orders flip to FILLED correctly even after
                # multiple cancel-and-replaces. Same row write is done
                # for the single-shot path so template attach works
                # uniformly. Resolved id is None on persist failure;
                # the chase still runs and the legacy broker_order_id
                # lookup remains as a fallback.
                _live_algo_id: int | None = None
                try:
                    from backend.api.algo.agent_engine import get_agent_id_by_slug as _g_aid
                    _live_manual_aid: int | None = None
                    try:
                        _live_manual_aid = await _g_aid("manual")
                    except Exception:
                        pass
                    _eff_target_pct = _resolve_target_pct(data.target_pct)
                    # Capture the middleware's request_id so /admin/history
                    # → /admin/audit drill-through works on this row.
                    _req_id = (request.scope.get("state") or {}).get("request_id")
                    async with async_session() as _s_pre:
                        _live_row = AlgoOrder(
                            account=account, symbol=sym,
                            exchange=(data.exchange or "NFO"),
                            transaction_type=side, quantity=qty,
                            initial_price=(float(data.price)
                                           if data.price is not None else None),
                            status="OPEN", engine="live", mode="live",
                            agent_id=_live_manual_aid,
                            strategy_id=data.strategy_id,
                            broker_order_id=None,
                            request_id=_req_id,
                            target_pct=(_eff_target_pct
                                        if _eff_target_pct > 0 else None),
                            template_id=data.template_id,
                            template_overrides_json=_build_overrides_json(data),
                            product=(data.product or "NRML"),
                            detail=f"[LIVE-TICKET] manual {side} {qty} {sym}"
                                   f"{' @₹' + str(data.price) if data.price else ''}",
                        )
                        _s_pre.add(_live_row)
                        await _s_pre.commit()
                        _live_algo_id = _live_row.id
                except Exception as _e_pre:
                    logger.warning(
                        f"[LIVE-TICKET] AlgoOrder pre-persist failed: {_e_pre}"
                    )

                if chase_eligible:
                    # Background chase loop — first place_order
                    # returns synchronously; loop keeps running. The
                    # algo_order_id propagates into chase_order so
                    # broker_order_id stays in sync per replace.
                    order_id = await _start_live_chase(
                        account=account,
                        symbol=sym,
                        exchange=(data.exchange or "NFO"),
                        transaction_type=side,
                        quantity=qty,
                        aggressiveness=(data.chase_aggressiveness or "low"),
                        algo_order_id=_live_algo_id,
                    )
                    chase_tag = f" CHASE[{(data.chase_aggressiveness or 'low').lower()}]"
                else:
                    # Single-shot — preserves the existing path for
                    # MARKET / SL-M and explicit chase=False tickets.
                    from backend.shared.brokers.kite import get_lot_size
                    _ls_ticket = await get_lot_size(data.exchange or "NFO", sym)
                    broker = _broker_for(account)
                    _kq_ticket = broker.translate_qty(data.exchange or "NFO", qty, _ls_ticket)
                    order_id = broker.place_order(
                        variety=(data.variety or "regular"),
                        exchange=(data.exchange or "NFO"),
                        tradingsymbol=sym,
                        transaction_type=side,
                        quantity=_kq_ticket,
                        product=(data.product or "NRML"),
                        order_type=order_type,
                        price=data.price,
                        trigger_price=data.trigger_price,
                        validity="DAY",
                        tag="ramboq-ticket",
                    )
                    chase_tag = ""

                # Seed the row's broker_order_id with the first place
                # result. For chased orders this gets overwritten on
                # every re-place via _sync_algo_order_id; for single-
                # shot orders this is the final value.
                if _live_algo_id is not None and order_id:
                    try:
                        from sqlalchemy import select as _sel_seed
                        async with async_session() as _s_seed:
                            _r = (await _s_seed.execute(
                                _sel_seed(AlgoOrder).where(
                                    AlgoOrder.id == _live_algo_id
                                )
                            )).scalar_one_or_none()
                            if _r is not None:
                                _r.broker_order_id = str(order_id)
                                await _s_seed.commit()
                    except Exception as _e_seed:
                        logger.debug(
                            f"[LIVE-TICKET] broker_order_id seed failed: {_e_seed}"
                        )

                invalidate("orders")    # refresh /api/orders cache
                masked = mask_account(account)
                logger.info(f"Ticket LIVE order: {order_id} [{masked}] "
                            f"{side} {qty} {sym}{chase_tag}")
                try:
                    from backend.api.algo.agent_engine import record_manual_event
                    asyncio.create_task(record_manual_event(
                        outcome="action_success", source=data.source or "ticket",
                        account=account, symbol=sym,
                        exchange=(data.exchange or "NFO"), side=side,
                        qty=qty, mode="live", order_id=str(order_id),
                    ))
                except Exception:
                    pass
                # Successful placement clears any prior rejection
                # history for this key — the breaker resets.
                _clear_rejections(_bk_key)
                return TicketOrderResponse(
                    order_id=str(order_id),
                    mode="live",
                    status="OPEN",
                    detail=(f"Live broker order #{order_id} placed at {account}"
                            + (f" — chasing [{(data.chase_aggressiveness or 'low').lower()}]"
                               if chase_eligible else "")
                            + "."),
                )
            except HTTPException:
                raise
            except Exception as e:
                # Enriched failure log: account / exchange / symbol / product /
                # side / qty / order_type / price + post-flight diagnosis via
                # basket_margin (Kite's "Insufficient permission for that call"
                # is overloaded — segment scope, account activation, OR margin
                # shortfall all surface as the same string).
                from backend.api.algo.actions import diagnose_live_failure
                masked_acct = mask_account(account)
                kite_msg = str(e)
                diag_order = {
                    "exchange": data.exchange or "NFO",
                    "symbol":   sym,
                    "side":     side,
                    "qty":      qty,
                    "order_type": order_type,
                    "product":  data.product or "NRML",
                    "price":    data.price or 0,
                    "variety":  data.variety or "regular",
                }
                try:
                    diag = await diagnose_live_failure(_broker_for(account), diag_order, kite_msg)
                except Exception:
                    diag = "diagnosis unavailable"
                logger.error(
                    f"[LIVE-TICKET] place_order failed for {masked_acct} "
                    f"{(data.exchange or 'NFO')}/{sym} {(data.product or 'NRML')} "
                    f"{side} {qty} {order_type}"
                    f"{f' @{data.price}' if data.price else ''}: "
                    f"{kite_msg} | diag: {diag}"
                )
                try:
                    from backend.shared.helpers.alert_utils import send_order_failure_alert
                    send_order_failure_alert(
                        account=account, symbol=sym,
                        exchange=(data.exchange or "NFO"), side=side,
                        qty=qty, mode="live", source="agent:manual:ticket",
                        error=kite_msg,
                    )
                except Exception:
                    pass
                try:
                    from backend.api.algo.agent_engine import record_manual_event
                    asyncio.create_task(record_manual_event(
                        outcome="action_failure", source=data.source or "ticket",
                        account=account, symbol=sym,
                        exchange=(data.exchange or "NFO"), side=side,
                        qty=qty, mode="live", error=kite_msg,
                    ))
                except Exception:
                    pass
                # Tick the circuit breaker — Kite rejected this order.
                # Same key the pre-preflight check uses; cumulative
                # with preflight blocks.
                _new_count = _record_rejection(_bk_key)
                if _new_count >= _REJECTION_THRESHOLD:
                    _maybe_send_breaker_alert(_bk_key, account, sym, side, qty,
                                               kite_msg[:200])
                raise HTTPException(
                    status_code=400,
                    detail=f"{kite_msg} ({diag})"[:400],
                )

        # Paper preflight gate — retired Jun 2026. Pre-fix this fired
        # the full run_preflight() chain (4 broker calls, ~800ms) on
        # every paper ticket; the paper engine itself already runs
        # basket_margin internally via its REJECTED-vs-OPEN gate
        # (see PaperTradeEngine.register_open_order), so the route-
        # level preflight was duplicate work that added ~800ms of
        # latency to every paper placement for zero additional
        # correctness. The paper engine's own margin check still
        # catches the same QTY_FREEZE / SEGMENT_INACTIVE /
        # MARGIN_SHORTFALL conditions; rejections surface in the
        # AlgoOrder row's .detail field within one tick.
        #
        # LIVE preflight (above) stays — it's the only chance to
        # block a real broker order before kite.place_order fires.

        # Persist AlgoOrder row first so the engine has an id to
        # reference back into.
        algo_order_id = None
        detail = (f"[PAPER-TICKET] manual {side} {qty} {sym} "
                  f"@₹{data.price:.2f}" if data.price is not None
                  else f"[PAPER-TICKET] manual {side} {qty} {sym} @MARKET")
        # Resolve manual agent id for audit attribution (best-effort — None
        # is safe; the order still writes, just without agent linkage).
        _manual_aid: int | None = None
        try:
            from backend.api.algo.agent_engine import get_agent_id_by_slug
            _manual_aid = await get_agent_id_by_slug("manual")
        except Exception:
            pass
        _eff_target_pct = _resolve_target_pct(data.target_pct)
        _req_id_paper = (request.scope.get("state") or {}).get("request_id")
        try:
            async with async_session() as s:
                row = AlgoOrder(
                    account=account, symbol=sym, exchange=(data.exchange or "NFO"),
                    transaction_type=side, quantity=qty,
                    initial_price=(float(data.price) if data.price is not None else None),
                    status="OPEN", engine="paper", mode="paper",
                    agent_id=_manual_aid,
                    strategy_id=data.strategy_id,
                    request_id=_req_id_paper,
                    target_pct=(_eff_target_pct if _eff_target_pct > 0 else None),
                    template_id=data.template_id,
                    template_overrides_json=_build_overrides_json(data),
                    product=(data.product or "NRML"),
                    detail=detail,
                )
                s.add(row)
                await s.commit()
                algo_order_id = row.id
        except Exception as e:
            logger.error(f"[PAPER-TICKET] DB write failed: {e}")
            raise HTTPException(status_code=500, detail=f"DB write failed: {e}")

        # Register with the paper engine so the chase loop picks
        # it up. Skip when no limit price (MARKET orders fill at
        # next bid/ask immediately on first tick) OR when the
        # operator explicitly opted out of chase via `chase=False`
        # (the order then sits OPEN at the initial limit until the
        # market crosses it naturally).
        if data.price is not None and qty > 0 and data.chase:
            try:
                # Validate + normalise aggressiveness so an out-of-
                # band value silently downgrades to 'low' (the
                # operator's standing default) rather than blowing
                # up the engine.
                agg = (data.chase_aggressiveness or "low").lower()
                if agg not in ("low", "med", "high"):
                    agg = "low"
                engine = get_prod_paper_engine()
                engine.register_open_order({
                    "algo_order_id": algo_order_id,
                    "account":       account,
                    "symbol":        sym,
                    "side":          side,
                    "qty":           qty,
                    "limit_price":   float(data.price),
                    "initial_price": float(data.price),
                    "exchange":      (data.exchange or "NFO"),
                    "agent_slug":    "manual-ticket",
                    "action_type":   "place_order",
                    "chase_agg":     agg,
                    # Slice 7a — pass strategy_id through so the
                    # paper engine's fill hook can write the per-
                    # strategy lot ledger entry. None / 0 = no
                    # attribution; ledger write skips.
                    "strategy_id":      data.strategy_id,
                    "is_close_intent":  False,  # ticket = open intent
                })
            except Exception as e:
                logger.warning(f"[PAPER-TICKET] engine register failed: {e}")
                # Row is persisted; engine can be restarted to re-pick-up.
        elif not data.chase:
            logger.info(f"[PAPER-TICKET] chase opted out — order #{algo_order_id} "
                        f"resting at limit ₹{data.price}")

        # Timeline: placed event for the ticket order.
        if algo_order_id:
            try:
                from backend.api.algo.order_events import write_event as _write_evt
                asyncio.create_task(_write_evt(
                    algo_order_id, "placed",
                    f"[PAPER-TICKET] manual {side} {qty} {sym} "
                    f"{'@₹' + f'{data.price:.2f}' if data.price is not None else '@MARKET'}",
                    payload={
                        "account": account, "price": data.price,
                        "exchange": data.exchange or "NFO",
                        "source": "ticket",
                    },
                ))
            except Exception:
                pass

        masked = mask_account(account)
        logger.info(f"Ticket paper order: {algo_order_id} [{masked}] {side} {qty} {sym}")
        if algo_order_id:
            try:
                from backend.api.algo.agent_engine import record_manual_event
                asyncio.create_task(record_manual_event(
                    outcome="action_success", source=data.source or "ticket",
                    account=account, symbol=sym,
                    exchange=(data.exchange or "NFO"), side=side,
                    qty=qty, mode="paper", order_id=str(algo_order_id),
                ))
            except Exception:
                pass

        # ── Template attachment ──────────────────────────────────────────
        # Apply TP/SL/Wing per the chosen template (or ad-hoc overrides /
        # legacy target_pct shim). When sim is active, GTTs land in
        # SimGttBook + wing fans into SimDriver._paper; otherwise the
        # apply call returns the plan structure but defers actual GTT
        # placement until live broker fill-postback wiring lands.
        attachment_dict = await _maybe_attach_template_to_ticket(data, account, sym, side, qty, algo_order_id)

        return TicketOrderResponse(
            order_id=str(algo_order_id),
            mode="paper",
            status="OPEN",
            detail=f"Paper order #{algo_order_id} placed — chase loop will fill it on the next bid/ask cross.",
            template_attachment=attachment_dict,
        )

    @put("/{order_id:str}")
    async def modify_order(self, order_id: str, data: ModifyOrderRequest, request: Request) -> ModifyOrderResponse:
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: cannot modify orders.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required to modify orders.")
        broker = _broker_for(data.account)
        masked = mask_account(data.account)
        # Note: MCX qty translation (to_kite_qty) is NOT applied here because
        # ModifyOrderRequest carries no exchange/tradingsymbol — the operator is
        # modifying an existing Kite order and should supply the quantity already
        # in Kite's convention (lots for MCX). This endpoint is legacy / rarely
        # used; the primary order path (/ticket + chase.py) handles the translation.
        kwargs = {k: v for k, v in {
            "quantity":      data.quantity,
            "price":         data.price,
            "order_type":    data.order_type,
            "trigger_price": data.trigger_price,
            "validity":      data.validity,
        }.items() if v is not None}
        try:
            broker.modify_order(order_id, variety=data.variety, **kwargs)
            invalidate("orders")
            logger.info(f"Order modified: {order_id} [{masked}]")
            return ModifyOrderResponse(order_id=order_id)
        except Exception as e:
            logger.error(f"Modify order failed [{masked}] {order_id}: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @post("/postback", guards=[])
    async def order_postback(self, request: Request) -> dict:
        """Kite postback — receives real-time order status updates.
        No JWT guard — Kite sends this directly. Verified via
        HMAC-SHA256 signature over order_id + order_timestamp + api_secret
        using the account's api_secret as the key (Kite postback protocol).
        """
        try:
            body = await request.json()
            order_id        = body.get("order_id", "")
            order_timestamp = body.get("order_timestamp", "")
            checksum        = body.get("checksum", "")
            account         = body.get("user_id", "")
            status          = body.get("status", "")
            tradingsymbol   = body.get("tradingsymbol", "")
            txn             = body.get("transaction_type", "")
            qty             = body.get("quantity", 0)
            price           = body.get("average_price") or body.get("price", 0)
            status_msg      = body.get("status_message") or ""
            masked          = mask_account(account)

            # ── HMAC verification ────────────────────────────────────
            # Kite signs each postback with:
            #   sha256(order_id + order_timestamp + api_secret)
            # Try the account's own api_secret first; fall back to
            # iterating every loaded account (Kite postback doesn't
            # always include a recognisable user_id). We have ≤5
            # accounts so the iteration is negligible.
            conns = Connections()
            # Skip non-Kite connections — postbacks only come from Kite,
            # and Dhan/Groww connections don't expose `api_secret` so
            # iterating them would AttributeError. KiteConnection is
            # the only class with the property today; check by class to
            # avoid hardcoding strings.
            from backend.shared.helpers.connections import KiteConnection
            kite_candidates: list[str] = [
                a for a, c in conns.conn.items() if isinstance(c, KiteConnection)
            ]
            candidates: list[str] = []
            if account and account in kite_candidates:
                # Put the claimed account first so the fast path hits.
                candidates = [account] + [a for a in kite_candidates if a != account]
            else:
                candidates = kite_candidates

            sig_valid = False
            for acct in candidates:
                api_secret = conns.conn[acct].api_secret
                msg = (str(order_id) + str(order_timestamp) + api_secret).encode()
                expected = hashlib.sha256(msg).hexdigest()
                if hmac.compare_digest(expected, str(checksum)):
                    sig_valid = True
                    break

            if not sig_valid:
                logger.warning(
                    "postback signature mismatch",
                    extra={"order_id": order_id},
                )
                raise HTTPException(
                    status_code=401,
                    detail="Invalid postback signature.",
                )
            # ─────────────────────────────────────────────────────────

            logger.info(f"Postback: {order_id} [{masked}] {status} {txn} {qty} "
                        f"{tradingsymbol} price={price} msg={status_msg}")

            # Audit trail — every broker fill / cancellation / rejection
            # lands in audit_log so /admin/audit shows the complete
            # order-lifecycle timeline alongside operator-initiated
            # actions. Fire-and-forget; zero latency impact on the
            # postback acknowledgement.
            try:
                from backend.api.audit import write_audit_event
                _status_u = str(status or "").upper()
                # Audit category mapping. Same shape as _process_broker_postback
                # — EXPIRED gets its own category instead of falling through
                # to "order.fill", and truly-unknown statuses bucket to the
                # generic "order". (Slice P3.)
                _cat = ("order.fill"     if _status_u == "COMPLETE"
                        else "order.cancel"  if _status_u == "CANCELLED"
                        else "order.reject"  if _status_u == "REJECTED"
                        else "order.expired" if _status_u == "EXPIRED"
                        else "order")
                write_audit_event(
                    category=_cat,
                    action=f"BROKER_{_status_u or 'EVENT'}",
                    actor_username="broker",
                    actor_role="system",
                    target_type="broker_order",
                    target_id=str(order_id) if order_id else None,
                    summary=(f"{_status_u} {txn} {qty} {tradingsymbol} "
                             f"@₹{price} acct={masked}"
                             + (f" msg={status_msg}" if status_msg else ""))[:1000],
                )
            except Exception as _exc:
                logger.debug(f"postback audit write skipped: {_exc}")

            # Timeline: write postback event to any matching AlgoOrder rows.
            # We match on broker_order_id (Kite's order_id string).  Best-effort —
            # never raises; the postback acknowledgement must still return quickly.
            try:
                from sqlalchemy import select as _sql_select
                from backend.api.database import async_session as _async_session
                from backend.api.models import AlgoOrder as _AlgoOrder
                from backend.api.algo.order_events import write_event as _write_event

                # Kite → AlgoOrder.status mapping. Operator reported
                # algo_orders rows stuck at OPEN even after Kite said the
                # order was COMPLETE/CANCELLED/REJECTED — the previous
                # postback handler only wrote a timeline event and never
                # synced the row's status field. Map terminal Kite
                # statuses and update the row in the same transaction
                # that records the event so the orders list stays in
                # lockstep with the broker.
                _KITE_STATUS_MAP = {
                    "COMPLETE":  "FILLED",
                    "CANCELLED": "CANCELLED",
                    "REJECTED":  "REJECTED",
                    "EXPIRED":   "UNFILLED",
                }
                _new_status = _KITE_STATUS_MAP.get(str(status or "").upper())

                async def _pb_event():
                    try:
                        _filled_rows = []
                        async with _async_session() as _s:
                            _rows = (await _s.execute(
                                _sql_select(_AlgoOrder).where(
                                    _AlgoOrder.broker_order_id == str(order_id)
                                )
                            )).scalars().all()
                            # Audit fix — postback-before-broker_id race.
                            # The live-ticket path commits the row with
                            # broker_order_id=NULL, then places the order,
                            # then commits the broker_order_id in a second
                            # session. A fast IOC/MARKET postback in that
                            # gap (~200-500ms) finds zero rows above —
                            # template attach silently misses. Fall back
                            # to a recent-NULL-id match by
                            # (account / symbol / qty / side) within the
                            # last 60s, seeding broker_order_id on the
                            # winner so subsequent postbacks for the same
                            # broker id find it directly. Idempotent:
                            # repeat postbacks land on the now-seeded row.
                            # Regression-audit fix: ALSO scope by account
                            # so two live accounts placing the same
                            # symbol+side within 60s can't cross-pollinate
                            # broker_order_id onto the wrong row.
                            if not _rows:
                                from datetime import datetime, timezone, timedelta
                                # 60s: long enough for the slowest IOC fill + DB
                                # commit race; short enough to avoid cross-pollinating
                                # broker_order_id with a newer unrelated order.
                                _cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
                                _fallback_where = [
                                    _AlgoOrder.broker_order_id.is_(None),
                                    _AlgoOrder.status == "OPEN",
                                    _AlgoOrder.mode == "live",
                                    _AlgoOrder.symbol == str(tradingsymbol or ""),
                                    _AlgoOrder.transaction_type == str(txn or "").upper(),
                                    _AlgoOrder.created_at >= _cutoff,
                                ]
                                _pb_account = str(account or "").strip() if account else ""
                                if _pb_account:
                                    _fallback_where.append(_AlgoOrder.account == _pb_account)
                                # Audit fix (C-3) — also scope by quantity. Pre-fix
                                # two LIVE orders for the same symbol+side+account
                                # within 60s (e.g. operator places BUY 50 then BUY 25
                                # on the same option) could cross-pollinate
                                # broker_order_id onto the wrong row — the latest by
                                # id.desc() would win regardless of which postback
                                # actually arrived first.
                                try:
                                    _pb_qty = int(qty or 0)
                                except (TypeError, ValueError):
                                    _pb_qty = 0
                                if _pb_qty > 0:
                                    _fallback_where.append(_AlgoOrder.quantity == _pb_qty)
                                _fallback = (await _s.execute(
                                    _sql_select(_AlgoOrder).where(*_fallback_where)
                                    .order_by(_AlgoOrder.id.desc()).limit(1)
                                )).scalars().first()
                                if _fallback is not None:
                                    _fallback.broker_order_id = str(order_id)
                                    _rows = [_fallback]
                                    logger.info(
                                        f"postback fallback matched row #{_fallback.id} "
                                        f"to broker_order_id={order_id} via account/symbol/side"
                                    )
                            for _r in _rows:
                                if _new_status and _r.status != _new_status:
                                    _r.status = _new_status
                                    if _new_status == "FILLED" and price:
                                        try:
                                            _r.fill_price = float(price)
                                        except (TypeError, ValueError):
                                            pass
                                        if _r.created_at:
                                            from datetime import datetime, timezone
                                            _r.filled_at = datetime.now(timezone.utc)
                                        _filled_rows.append(_r)
                            await _s.commit()
                            # Slice 7c — live postback fill hook. Write
                            # the per-strategy lot ledger entry for any
                            # row that just flipped to FILLED with a
                            # strategy_id. Auto-detects OPEN-vs-CLOSE
                            # intent via the existing-lots heuristic in
                            # lot_ledger.record_fill (SELL with an open
                            # long lot = close; BUY with an open short
                            # = close; otherwise open). Failure logs +
                            # drops — the row already carries broker
                            # pnl so the per-strategy rollup falls back
                            # to AlgoOrder.pnl SUM for un-ledgered fills.
                            from backend.api.algo.lot_ledger import record_fill as _record_ledger_fill
                            for _r in _filled_rows:
                                if _r.strategy_id and _r.fill_price and _r.quantity > 0:
                                    try:
                                        await _record_ledger_fill(
                                            _s,
                                            strategy_id=_r.strategy_id,
                                            algo_order_id=_r.id,
                                            account=str(_r.account or ""),
                                            symbol=str(_r.symbol or ""),
                                            exchange=str(_r.exchange or "NFO"),
                                            side_kite=str(_r.transaction_type or "BUY"),
                                            qty=int(_r.quantity or 0),
                                            fill_price=float(_r.fill_price or 0),
                                        )
                                    except Exception as _le:
                                        logger.warning(
                                            f"postback lot_ledger write failed for "
                                            f"order_id={_r.id} strategy={_r.strategy_id}: {_le}"
                                        )
                            await _s.commit()
                        for _r in _rows:
                            await _write_event(
                                _r.id, "postback",
                                f"Kite postback: {status} {txn} {qty} {tradingsymbol} "
                                f"@{price}",
                                payload={
                                    "broker_order_id": order_id,
                                    "status": status,
                                    "new_algo_status": _new_status,
                                    "tradingsymbol": tradingsymbol,
                                    "transaction_type": txn,
                                    "quantity": qty,
                                    "price": price,
                                    "status_message": status_msg,
                                },
                            )
                        # Auto TP — arm take-profit child on every fill that
                        # has a target_pct / target_abs set and is a parent
                        # (parent_order_id is NULL) to prevent TP-of-TP chains.
                        # Phase 3D #6 — gate on template_id IS NULL so a
                        # row with BOTH legacy target_pct AND a template
                        # picks template path only (template's GTT
                        # supersedes the v1 single-target child).
                        for _r in _filled_rows:
                            if ((_r.target_pct or _r.target_abs)
                                    and _r.parent_order_id is None
                                    and _r.template_id is None
                                    and _r.fill_price):
                                asyncio.create_task(_arm_take_profit(
                                    parent_row_id=_r.id,
                                    parent_account=str(_r.account or ""),
                                    parent_symbol=str(_r.symbol or ""),
                                    parent_exchange=str(_r.exchange or "NFO"),
                                    parent_side=str(_r.transaction_type or "BUY"),
                                    fill_price=float(_r.fill_price),
                                    target_pct=float(_r.target_pct or 0.0),
                                    target_abs=(_r.target_abs
                                                and float(_r.target_abs)),
                                    parent_mode=str(_r.mode or "live"),
                                ))
                        # Phase 0 — template attachment on real broker fill.
                        # Fires when the parent has template_id set, is a
                        # parent (parent_order_id NULL → no TP-of-TP), is a
                        # real broker order (mode='live'), and is now
                        # FILLED with a known fill_price. Without this hook,
                        # the LIVE path persisted the template_id but never
                        # actually placed the TP/SL GTT + wing on the
                        # broker, so the operator saw "Default" picked in
                        # OrderTicket but the broker had no brackets.
                        for _r in _filled_rows:
                            if (_r.template_id
                                    and _r.parent_order_id is None
                                    and _r.mode == "live"
                                    and _r.fill_price):
                                # Sprint B (#4) — size exit GTTs against
                                # the actual filled qty when partials
                                # occurred (chase took the order in
                                # pieces). filled_quantity is the truth-
                                # of-record; quantity is the original ask.
                                _attach_qty = (
                                    int(_r.filled_quantity)
                                    if int(_r.filled_quantity or 0) > 0
                                    else int(_r.quantity or 0)
                                )
                                asyncio.create_task(
                                    _fire_template_attach_on_fill(
                                        parent_row_id=int(_r.id),
                                        parent_account=str(_r.account or ""),
                                        parent_symbol=str(_r.symbol or ""),
                                        parent_exchange=str(_r.exchange or "NFO"),
                                        parent_side=str(_r.transaction_type or "BUY"),
                                        parent_qty=_attach_qty,
                                        fill_price=float(_r.fill_price),
                                        template_id=int(_r.template_id),
                                        parent_product=str(_r.product or "NRML"),
                                    )
                                )
                    except Exception as _pe:
                        logger.debug(f"postback event write failed: {_pe}")

                asyncio.create_task(_pb_event())
            except Exception:
                pass

            # Invalidate the orders cache on EVERY postback so next
            # fetch gets fresh data regardless of terminal vs partial.
            invalidate("orders")

            # On a terminal status (COMPLETE / CANCELLED / REJECTED /
            # EXPIRED) the book itself changes — fan out invalidation
            # to every dependent cache so the next refetch is
            # consistent. Without this, /api/positions returns stale
            # data for up to its TTL (30s), and the snapshot grid
            # takes a second poll cycle to settle. Operator's report:
            # "snapshot grid updated two iterations" — root cause was
            # positions cache lagging by one tick.
            _terminal = str(status or "").upper() in (
                "COMPLETE", "CANCELLED", "REJECTED", "EXPIRED",
            )
            if _terminal:
                for _key in ("positions", "holdings"):
                    try:
                        invalidate(_key)
                    except Exception:
                        pass

            # Push real-time update to all connected WebSocket clients
            broadcast(json.dumps({
                "event": "order_update",
                "order_id": order_id,
                "account": masked,
                "status": status,
                "tradingsymbol": tradingsymbol,
                "transaction_type": txn,
                "quantity": qty,
                "price": price,
                "status_message": status_msg,
            }))

            # On a FILL (Kite status='COMPLETE') broadcast a separate
            # `position_filled` event with the signed qty delta. The
            # frontend patches its local sum_positions table immediately
            # — no waiting for the 5-min performance poll to roll over.
            # The poll remains the source of truth: if a postback is ever
            # dropped (Kite delivery is best-effort), the next refresh
            # reconciles. Optimistic-add is additive and self-correcting.
            if status == "COMPLETE":
                try:
                    _qty_int = int(qty or 0)
                    if _qty_int > 0:
                        _side_sign = 1 if (txn or "").upper() == "BUY" else -1
                        broadcast(json.dumps({
                            "event": "position_filled",
                            "account": masked,
                            "exchange": body.get("exchange", ""),
                            "tradingsymbol": tradingsymbol,
                            "qty": _qty_int * _side_sign,
                            "fill_price": float(price or 0),
                            "ts": int(_time.time() * 1000),
                            "order_id": order_id,
                        }))
                except Exception as _pe:
                    # Never let a malformed delta payload break the
                    # postback ACK — Kite retries on a non-2xx response.
                    logger.debug(f"position_filled broadcast skipped: {_pe}")

            # Single coordinated `book_changed` broadcast for every
            # terminal status. Frontend pages subscribe once and
            # refetch their primary loader (positions / holdings /
            # strategy analytics / payoff curve) in one synchronized
            # pass — replaces the prior "wait for next poll tick" UX
            # where the snapshot grid took 2+ iterations to settle.
            #
            # Payload carries the changed (account, symbol, exchange)
            # tuple so a future surface can do scoped refetch
            # instead of book-wide. v1 frontend ignores the scope
            # fields and refetches the visible bucket; v2 can target.
            if _terminal:
                try:
                    broadcast(json.dumps({
                        "event": "book_changed",
                        "account": masked,
                        "exchange": body.get("exchange", ""),
                        "tradingsymbol": tradingsymbol,
                        "reason": status,
                        "ts": int(_time.time() * 1000),
                    }))
                except Exception as _bce:
                    logger.debug(f"book_changed broadcast skipped: {_bce}")

            return {"status": "ok"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Postback error: {e}")
            return {"status": "error", "detail": str(e)}

    @post("/dhan_postback", guards=[])
    async def order_postback_dhan(self, request: Request) -> dict:
        """Dhan order-status webhook. Same role as the Kite postback
        but parses Dhan's payload shape. Scaffold ship — caches the
        full payload to the API log on first hit so the operator can
        forward Dhan's actual structure; the parser then maps known
        fields. Operator must configure the webhook URL inside the
        Dhan partner dashboard for this account.

        Best-effort: never 5xx (Dhan retries on non-2xx and will
        rapidly back-pressure us). Always returns 200 OK; failures
        log + drop. No HMAC validation yet — Dhan's signature scheme
        differs from Kite's and the operator hasn't surfaced their
        test payload yet.
        """
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"dhan postback non-JSON body: {e}")
            return {"status": "ok"}
        logger.info(f"dhan postback raw payload: {body!r}")

        # Best-effort field extraction. Dhan v2 ships:
        #   dhanClientId, orderId, exchangeOrderId, orderStatus,
        #   transactionType, exchangeSegment, tradingSymbol,
        #   quantity, filledQuantity, price, triggerPrice,
        #   averageTradedPrice, postOnly, orderType, …
        order_id = str(body.get("orderId") or body.get("order_id") or "")
        status   = str(body.get("orderStatus") or body.get("status") or "").upper()
        symbol   = body.get("tradingSymbol") or body.get("tradingsymbol") or ""
        txn      = body.get("transactionType") or body.get("transaction_type") or ""
        qty      = body.get("filledQuantity") or body.get("quantity") or 0
        price    = body.get("averageTradedPrice") or body.get("price") or 0
        account  = body.get("dhanClientId") or body.get("account") or ""

        # Map Dhan status → Kite-canonical via the existing
        # _DHAN_STATUS_TO_KITE table inside the adapter.
        try:
            from backend.shared.brokers.dhan import _DHAN_STATUS_TO_KITE
            kite_status = _DHAN_STATUS_TO_KITE.get(status, status)
        except Exception:
            kite_status = status

        await _process_broker_postback(
            broker_id="dhan",
            order_id=order_id,
            status=kite_status,
            account=str(account),
            symbol=str(symbol),
            txn=str(txn),
            qty=qty,
            price=price,
            exchange=str(body.get("exchangeSegment") or ""),
            status_message=str(body.get("statusMessage") or ""),
        )
        return {"status": "ok"}

    @post("/groww_postback", guards=[])
    async def order_postback_groww(self, request: Request) -> dict:
        """Groww order-status webhook. Same shape as the Dhan
        scaffold — log raw payload, best-effort parse known fields,
        delegate to the shared `_process_broker_postback` so the
        Kite-canonical mapping + invalidation + book_changed
        broadcast all reuse the same code path.

        Groww's postback support is uncertain (per the audit
        broker-API parity matrix). Route exists so we capture
        whatever Groww sends if/when the webhook is configured.
        """
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"groww postback non-JSON body: {e}")
            return {"status": "ok"}
        logger.info(f"groww postback raw payload: {body!r}")

        order_id = str(body.get("groww_order_id") or body.get("order_id") or "")
        status   = str(body.get("order_status") or body.get("status") or "").upper()
        symbol   = body.get("trading_symbol") or body.get("symbol") or ""
        txn      = body.get("transaction_type") or ""
        qty      = body.get("filled_quantity") or body.get("quantity") or 0
        price    = body.get("average_price") or body.get("price") or 0

        try:
            from backend.shared.brokers.groww import _GROWW_STATUS_TO_KITE
            kite_status = _GROWW_STATUS_TO_KITE.get(status, status)
        except Exception:
            kite_status = status

        await _process_broker_postback(
            broker_id="groww",
            order_id=order_id,
            status=kite_status,
            account="",
            symbol=str(symbol),
            txn=str(txn),
            qty=qty,
            price=price,
            exchange=str(body.get("exchange") or body.get("segment") or ""),
            status_message=str(body.get("status_message") or ""),
        )
        return {"status": "ok"}

    @post("/basket/margin")
    async def basket_margin(self, data: BasketOrderRequest, request: Request) -> BasketMarginResponse:
        """
        Compute the offset-aware margin for a basket of orders WITHOUT placing them.

        Calls kite.basket_order_margins(orders) per account in parallel.
        This is the true Kite basket benefit: spreads and hedges reduce
        the required margin vs. summing per-leg margin individually.

        Demo sessions → 403. Non-admin → 403.
        """
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403, detail="Demo: basket margin requires sign-in.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403, detail="Admin access required.")

        async def _margin_for_group(grp: BasketGroup) -> BasketMarginGroupResult:
            account = (grp.account or "").strip()
            if not account:
                return BasketMarginGroupResult(
                    account=account, required=None, available=None,
                    shortfall=None, error="account is required",
                )
            try:
                broker = _broker_for(account)
                orders_payload = [
                    {
                        "exchange":         leg.exchange.upper(),
                        "tradingsymbol":    leg.tradingsymbol.upper(),
                        "transaction_type": leg.transaction_type.upper(),
                        "variety":          leg.variety or "regular",
                        "product":          leg.product or "NRML",
                        "order_type":       leg.order_type or "LIMIT",
                        "quantity":         leg.quantity,
                        "price":            float(leg.price or 0),
                        "trigger_price":    float(leg.trigger_price or 0),
                    }
                    for leg in grp.legs
                ]
                result = await asyncio.to_thread(broker.basket_order_margins, orders_payload)
                # Kite returns {"initial": {...}, "final": {...}}; we surface
                # "final" as the post-hedge required margin.
                final  = (result or {}).get("final", {}) or {}
                avail  = (result or {}).get("initial", {}).get("available", {}) or {}
                required  = float(final.get("total") or 0)
                available = float(avail.get("cash") or 0)
                shortfall = max(0.0, required - available)
                return BasketMarginGroupResult(
                    account=account,
                    required=required,
                    available=available,
                    shortfall=shortfall,
                )
            except HTTPException:
                raise
            except Exception as _e:
                logger.warning(f"[BASKET-MARGIN] account={account} error={_e}")
                return BasketMarginGroupResult(
                    account=account, required=None, available=None,
                    shortfall=None, error=str(_e)[:200],
                )

        results = await asyncio.gather(*[_margin_for_group(g) for g in data.groups])
        return BasketMarginResponse(groups=list(results))

    @post("/basket")
    async def basket_order(self, data: BasketOrderRequest, request: Request) -> BasketOrderResponse:
        """
        True multi-account basket order endpoint.

        Legs are grouped by account.  Per group:
          - LIVE mode: each leg is dispatched via kite.place_order with a
            shared `tag="ramboq-basket-<uuid>"`.  Groups run concurrently
            via asyncio.gather; legs within a group run in sequence (Kite
            expects sequential placement for a basket — the tag links them
            server-side).
          - PAPER mode: each leg is persisted as an AlgoOrder and registered
            with the prod paper engine.
          - SHADOW mode: logs the Kite payload + computes basket_margin
            without executing.

        Demo → 403 (consistent with /ticket).
        Non-prod branch + mode=live → 403.
        paper_trading_mode=ON + mode=live → 403.
        """
        import uuid as _uuid
        from datetime import datetime, timezone
        from backend.api.database import async_session as _async_session2
        from backend.api.models import AlgoOrder as _AlgoOrder2
        from backend.shared.helpers.utils import is_prod_branch
        from backend.shared.helpers.settings import get_bool as _get_bool

        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: basket orders require sign-in.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required for basket orders.")

        # Resolve effective mode — same gate as /ticket.
        _ptm = _get_bool("execution.paper_trading_mode", False)
        _shadow = _get_bool("execution.shadow_mode", False)
        _shadow_on = _shadow and is_prod_branch()
        # Determine mode: shadow > paper_trading_mode > LIVE.
        if _shadow_on:
            eff_mode = "shadow"
        elif not is_prod_branch() or _ptm:
            eff_mode = "paper"
        else:
            eff_mode = "live"

        eff_target_pct = _resolve_target_pct(data.target_pct)

        async def _dispatch_group(grp: BasketGroup) -> BasketGroupResult:
            account = (grp.account or "").strip()
            if not account:
                return BasketGroupResult(
                    account=account,
                    basket_id="",
                    results=[BasketLegResult(leg_index=i, order_id=None,
                                            status="error",
                                            error="account is required")
                             for i in range(len(grp.legs))],
                )

            conns = Connections()
            if account not in conns.conn:
                return BasketGroupResult(
                    account=account,
                    basket_id="",
                    results=[BasketLegResult(leg_index=i, order_id=None,
                                            status="error",
                                            error=f"unknown account: {account}")
                             for i in range(len(grp.legs))],
                )

            # Kite's `tag` field is hard-capped at 20 chars (the broker
            # rejects anything longer with "invalid tags - maximum
            # allowed length is 20"). `rb-bk-` + 12 hex = 18 chars,
            # leaves 2 chars of headroom for any future suffix.
            basket_id = f"rb-bk-{_uuid.uuid4().hex[:12]}"
            leg_results: list[BasketLegResult] = []

            for i, leg in enumerate(grp.legs):
                sym = leg.tradingsymbol.upper().strip()
                side = leg.transaction_type.upper()
                qty = int(leg.quantity or 0)
                exch = (leg.exchange or "NFO").upper()

                # Basic validation per leg.
                if not sym or qty <= 0 or side not in _TXN_TYPES or exch not in _EXCHANGES:
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="error",
                        error=f"invalid leg: sym={sym} qty={qty} side={side} exch={exch}",
                    ))
                    continue

                if eff_mode == "live":
                    try:
                        broker = _broker_for(account)
                        from backend.shared.brokers.kite import get_lot_size
                        _ls = await get_lot_size(exch, sym)
                        _kq = broker.translate_qty(exch, qty, _ls)
                        kite_oid = await asyncio.to_thread(
                            broker.place_order,
                            variety=leg.variety or "regular",
                            exchange=exch,
                            tradingsymbol=sym,
                            transaction_type=side,
                            quantity=_kq,
                            product=leg.product or "NRML",
                            order_type=leg.order_type or "LIMIT",
                            price=float(leg.price or 0),
                            trigger_price=float(leg.trigger_price or 0),
                            validity="DAY",
                            tag=basket_id,
                        )
                        # Persist AlgoOrder row so the order book tracks it.
                        async with _async_session2() as _s:
                            _r = _AlgoOrder2(
                                account=account, symbol=sym, exchange=exch,
                                transaction_type=side, quantity=qty,
                                initial_price=float(leg.price or 0) or None,
                                broker_order_id=str(kite_oid),
                                status="OPEN", engine="live", mode="live",
                                basket_tag=basket_id,
                                target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                                template_id=leg.template_id,
                                template_overrides_json=_build_overrides_json(leg),
                                product=(leg.product or "NRML"),
                            )
                            _s.add(_r)
                            await _s.commit()
                            _live_aid = _r.id
                        invalidate("orders")
                        await _attach_basket_leg_template(
                            algo_order_id=_live_aid,
                            template_id=leg.template_id,
                            account=account, sym=sym, side=side, qty=qty,
                            exch=exch,
                            price=float(leg.price or 0),
                            product=(leg.product or "NRML"),
                        )
                        leg_results.append(BasketLegResult(
                            leg_index=i, order_id=str(kite_oid), status="OPEN",
                        ))
                    except Exception as _e:
                        leg_results.append(BasketLegResult(
                            leg_index=i, order_id=None, status="error",
                            error=str(_e)[:200],
                        ))

                elif eff_mode == "shadow":
                    # Log payload; compute basket margin for the group but
                    # don't place any orders.
                    logger.info(
                        f"[SHADOW-BASKET] {account} leg {i}: {side} {qty} {sym} "
                        f"@{leg.price} tag={basket_id}"
                    )
                    async with _async_session2() as _s:
                        _r = _AlgoOrder2(
                            account=account, symbol=sym, exchange=exch,
                            transaction_type=side, quantity=qty,
                            initial_price=float(leg.price or 0) or None,
                            status="OPEN", engine="shadow", mode="shadow",
                            basket_tag=basket_id,
                            target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                            template_id=leg.template_id,
                            template_overrides_json=_build_overrides_json(leg),
                            product=(leg.product or "NRML"),
                            detail=f"[SHADOW-BASKET] leg {i} tag={basket_id}",
                        )
                        _s.add(_r)
                        await _s.commit()
                        _shadow_aid = _r.id
                    await _attach_basket_leg_template(
                        algo_order_id=_shadow_aid,
                        template_id=leg.template_id,
                        account=account, sym=sym, side=side, qty=qty,
                        exch=exch,
                        price=float(leg.price or 0),
                        product=(leg.product or "NRML"),
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="SHADOW",
                    ))

                else:
                    # Paper mode.
                    from backend.api.algo.paper import get_prod_paper_engine
                    _manual_aid2: int | None = None
                    try:
                        from backend.api.algo.agent_engine import get_agent_id_by_slug as _ga
                        _manual_aid2 = await _ga("manual")
                    except Exception:
                        pass
                    _detail = (f"[PAPER-BASKET] {side} {qty} {sym} "
                               f"tag={basket_id}")
                    async with _async_session2() as _s:
                        _r = _AlgoOrder2(
                            account=account, symbol=sym, exchange=exch,
                            transaction_type=side, quantity=qty,
                            initial_price=float(leg.price or 0) or None,
                            status="OPEN", engine="paper", mode="paper",
                            agent_id=_manual_aid2,
                            basket_tag=basket_id,
                            target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                            template_id=leg.template_id,
                            template_overrides_json=_build_overrides_json(leg),
                            product=(leg.product or "NRML"),
                            detail=_detail,
                        )
                        _s.add(_r)
                        await _s.commit()
                        _paper_id = _r.id

                    if leg.price and qty > 0:
                        try:
                            eng = get_prod_paper_engine()
                            eng.register_open_order({
                                "algo_order_id": _paper_id,
                                "account":       account,
                                "symbol":        sym,
                                "side":          side,
                                "qty":           qty,
                                "limit_price":   float(leg.price),
                                "initial_price": float(leg.price),
                                "exchange":      exch,
                                "agent_slug":    "basket-ticket",
                                "action_type":   "place_order",
                                "chase_agg":     "low",
                            })
                        except Exception as _pe:
                            logger.warning(
                                f"[PAPER-BASKET] engine register failed for "
                                f"#{_paper_id}: {_pe}"
                            )

                    await _attach_basket_leg_template(
                        algo_order_id=_paper_id,
                        template_id=leg.template_id,
                        account=account, sym=sym, side=side, qty=qty,
                        exch=exch,
                        price=float(leg.price or 0),
                        product=(leg.product or "NRML"),
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=str(_paper_id), status="PAPER",
                    ))

            # Compute offset-aware margin for the group (best-effort; not a
            # gate — operators already see it before submitting via /basket/margin).
            margin_required: float | None = None
            margin_available: float | None = None
            try:
                broker = _broker_for(account)
                orders_payload = [
                    {
                        "exchange":         (leg.exchange or "NFO").upper(),
                        "tradingsymbol":    leg.tradingsymbol.upper(),
                        "transaction_type": leg.transaction_type.upper(),
                        "variety":          leg.variety or "regular",
                        "product":          leg.product or "NRML",
                        "order_type":       leg.order_type or "LIMIT",
                        "quantity":         leg.quantity,
                        "price":            float(leg.price or 0),
                        "trigger_price":    float(leg.trigger_price or 0),
                    }
                    for leg in grp.legs
                ]
                mr = await asyncio.to_thread(broker.basket_order_margins, orders_payload)
                final_m = (mr or {}).get("final", {}) or {}
                avail_m = (mr or {}).get("initial", {}).get("available", {}) or {}
                margin_required  = float(final_m.get("total") or 0)
                margin_available = float(avail_m.get("cash") or 0)
            except Exception as _me:
                logger.debug(f"[BASKET] margin lookup failed for {account}: {_me}")

            return BasketGroupResult(
                account=account,
                basket_id=basket_id,
                results=leg_results,
                margin_required=margin_required,
                margin_available=margin_available,
            )

        group_results = await asyncio.gather(*[_dispatch_group(g) for g in data.groups])
        return BasketOrderResponse(groups=list(group_results))

    @delete("/{order_id:str}", status_code=HTTP_200_OK)
    async def cancel_order(
        self,
        order_id: str,
        request:  Request,
        account:  str = Parameter(query="account"),
        variety:  str = Parameter(query="variety", default="regular"),
    ) -> CancelOrderResponse:
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: cannot cancel orders.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required to cancel orders.")
        broker = _broker_for(account)
        masked = mask_account(account)
        try:
            # Slice Q — look up exchange from the persisted AlgoOrder row
            # so Groww's segment resolver gets the right segment instead
            # of silently routing MCX/NFO cancels to the CASH segment.
            _exchange = ""
            try:
                from sqlalchemy import select as _sel
                from backend.api.models import AlgoOrder as _AO
                async with async_session() as _s:
                    _row = (await _s.execute(
                        _sel(_AO).where(_AO.broker_order_id == order_id)
                    )).scalar_one_or_none()
                if _row and _row.exchange:
                    _exchange = _row.exchange
            except Exception:
                pass
            if _exchange:
                broker.cancel_order(order_id, variety=variety, exchange=_exchange)
            else:
                broker.cancel_order(order_id, variety=variety)
            invalidate("orders")
            logger.info(f"Order cancelled: {order_id} [{masked}]")
            return CancelOrderResponse(order_id=order_id)
        except Exception as e:
            logger.error(f"Cancel order failed [{masked}] {order_id}: {e}")
            raise HTTPException(status_code=400, detail=str(e))


class AccountsController(Controller):
    path = "/api/accounts"
    guards = [jwt_guard]

    @get("/")
    async def list_accounts(self, request: Request) -> AccountsResponse:
        # Raw account codes gated to admin/designated only. Partner JWTs
        # get masked codes (ZG####), symmetric with mask_column() in
        # row endpoints (positions/holdings/funds). Demo never reaches
        # this endpoint — controller guard is jwt_guard.
        conn = Connections().conn
        do_mask = not is_admin_request(request)
        accounts = [
            AccountInfo(
                account_id=account,
                display=(mask_account(account) if do_mask else account),
            )
            for account in conn
        ]
        # Default broker account is operator-configurable from
        # /admin/settings (orders.default_account). When set + the
        # account is loaded, the frontend SymbolPanel pre-selects it.
        # Masked partners get an empty string back — the raw default
        # code would leak the unmasked ID through this surface.
        from backend.shared.helpers.settings import get_string
        default_acct = "" if do_mask else get_string("orders.default_account", "ZG0790")
        # Only return the default if it's actually in the loaded set —
        # otherwise the frontend would try to pre-select an account
        # that doesn't exist (silent fail; operator sees the dropdown
        # stay empty). Stale settings shouldn't break the UI.
        if default_acct and default_acct not in conn:
            default_acct = ""
        # orders.default_symbol setting retired — modals now use the
        # operator's recent symbol (localStorage on the FE) or open
        # with empty context. Always return blank so legacy FE code
        # paths don't latch onto a non-existent symbol.
        return AccountsResponse(
            accounts=accounts,
            default_account=default_acct,
            default_symbol="",
        )
