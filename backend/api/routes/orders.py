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

import hashlib
import hmac
import json

import msgspec
import pandas as pd
from litestar import Controller, Request, delete, get, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK

from backend.api.auth_guard import jwt_guard, auth_or_demo_guard, is_admin_request, is_authenticated_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.routes.ws import broadcast
from backend.api.schemas import (
    AccountInfo,
    AccountsResponse,
    CancelOrderResponse,
    ModifyOrderRequest,
    ModifyOrderResponse,
    OrderRow,
    OrdersResponse,
    PlaceOrderRequest,
    PlaceOrderResponse,
    TicketOrderRequest,
    TicketOrderResponse,
)
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_column, secrets

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


async def _align_price_to_tick(exchange: str, symbol: str,
                                price: float | None) -> float | None:
    """Snap *price* to the nearest valid tick for the instrument.

    Kite rejects orders whose LIMIT / trigger price isn't a multiple
    of the contract's `tick_size` with "Exchange invalid price — the
    entered price is not as per ticker price". Operators routinely
    enter ₹9961.50 for a MCX commodity that ticks at ₹1 (whole
    rupees); we round to the nearest valid tick before sending.

    Reads tick_size from the in-process instruments cache (no broker
    round-trip). Returns the input unchanged when:
      - price is None or 0
      - the instrument isn't in the cache (let Kite reject explicitly)
      - tick_size resolves to a non-positive value (defensive)

    Rounding policy: half-up to the nearest tick, then clamped to the
    same tick grid. For options with tick=0.05, 12.37 → 12.35;
    for commodities with tick=1, 9961.50 → 9962.
    """
    if price is None or price == 0:
        return price
    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_TTL_SECONDS)
        items = resp.items if resp else []
    except Exception:
        return price
    sym_u = (symbol or "").upper()
    ex_u  = (exchange or "").upper()
    tick = None
    for inst in items:
        if inst.s == sym_u and inst.e == ex_u:
            tick = float(inst.ts or 0)
            break
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
                            aggressiveness: str) -> str:
    """Place + chase a LIVE order in the background.

    Spawns `chase_order()` as an asyncio task and synchronously
    returns the first broker order_id (so the ticket can confirm
    placement to the operator immediately). The chase task keeps
    running after this returns — re-quoting the limit per the
    aggressiveness config until the order fills or the attempt
    cap is hit.

    Note: the chase loop CANCELS the current order and PLACES a
    NEW one each attempt, so the order_id mutates over the chase
    lifetime. The id we return here is the FIRST one. Operators
    can poll /api/orders/ to see the currently-live order_id.
    Future work could surface chase progress via WebSocket.
    """
    import asyncio
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
    from backend.shared.brokers.registry import all_brokers
    rows: list[OrderRow] = []
    for broker in all_brokers():
        account = broker.account
        try:
            for o in reversed(broker.orders() or []):
                rows.append(_row_from_dict(o, account))
        except Exception as e:
            logger.error(f"Orders list failed for {account}: {e}")
    return OrdersResponse(rows=rows, refreshed_at=timestamp_display())


class AlgoOrderEventInfo(msgspec.Struct):
    """One row from the per-order timeline."""
    id: int
    order_id: int
    ts: str          # ISO-8601 UTC timestamp
    kind: str
    message: str
    payload_json: str | None


class AlgoOrderInfo(msgspec.Struct):
    """Shape exposed to the frontend Order-log tab. Thin wrapper over the
    AlgoOrder row — adds a display-ready price string would be nice but
    the frontend formats it for locale anyway."""
    id: int
    account: str
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    initial_price: float | None
    fill_price: float | None
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


class OrdersController(Controller):
    path = "/api/orders"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_orders(self, request: Request) -> OrdersResponse:
        try:
            resp = await get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL)
            # Mask account codes for anonymous callers (demo / public).
            if not is_authenticated_request(request):
                for r in resp.rows:
                    r.account = mask_column(pd.Series([r.account]))[0]
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
        # Mask account codes for anonymous callers (demo + public).
        # Same masking the /performance grids apply — turns ZG0790
        # into ZG####.
        do_mask = not is_authenticated_request(request)
        masked_acct = (
            (lambda a: mask_column(pd.Series([a]))[0]) if do_mask else (lambda a: a)
        )
        return [
            AlgoOrderInfo(
                id=r.id, account=masked_acct(r.account), symbol=r.symbol, exchange=r.exchange,
                transaction_type=r.transaction_type, quantity=r.quantity,
                initial_price=(float(r.initial_price) if r.initial_price is not None else None),
                fill_price=(float(r.fill_price) if r.fill_price is not None else None),
                attempts=int(r.attempts or 0),
                status=r.status, engine=r.engine, mode=r.mode,
                detail=r.detail,
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in rows
        ]

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

        do_mask = not is_authenticated_request(request)

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

        do_mask = not is_authenticated_request(request)

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
        })
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
        masked = mask_column(pd.Series([data.account]))[0]
        # Surface a deprecation warning every time this path is used
        # so we can spot any lingering callers in the logs and migrate
        # them to /ticket.
        logger.warning(
            f"[deprecated] POST /api/orders/place hit by [{masked}] — "
            f"use POST /api/orders/ticket instead. "
            f"{data.transaction_type} {data.quantity} {data.tradingsymbol}"
        )
        try:
            from backend.shared.brokers.kite import to_kite_qty, get_lot_size
            _ls_dep = await get_lot_size(data.exchange, data.tradingsymbol.upper())
            _kq_dep = to_kite_qty(data.exchange, data.quantity, _ls_dep)
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
                import asyncio as _aio
                from backend.api.algo.agent_engine import record_manual_event
                _aio.create_task(record_manual_event(
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
                import asyncio as _aio
                from backend.api.algo.agent_engine import record_manual_event
                _aio.create_task(record_manual_event(
                    outcome="action_failure", source="place",
                    account=data.account, symbol=data.tradingsymbol,
                    exchange=data.exchange, side=data.transaction_type,
                    qty=data.quantity, mode="live", error=str(e),
                ))
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=str(e))

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

        # Demo mode chokepoint: silently downgrade LIVE → PAPER. Rather
        # than 403-ing, we let the visitor's order land as paper so the
        # "click Submit, see something happen" flow keeps working — the
        # ticket UI still warns this is a real trade with the LIVE
        # confirmation dialog, but the backend won't actually let the
        # order touch a broker.
        if getattr(request.state, "is_demo", False):
            data = msgspec.structs.replace(data, mode="paper")

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
        # LIMIT/SL need a price; MARKET/SL-M must NOT carry one (Kite
        # rejects price on MARKET). SL/SL-M need a trigger.
        if data.order_type in ("LIMIT", "SL") and not data.price:
            raise HTTPException(status_code=400, detail="price is required for LIMIT/SL")
        if data.order_type in ("SL", "SL-M") and not data.trigger_price:
            raise HTTPException(status_code=400, detail="trigger_price is required for SL/SL-M")

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

        # Resolve account — caller must supply one explicitly. Silently
        # falling back to the first available account would route an
        # operator's order to a different account than intended.
        conns = Connections()
        account = (data.account or "").strip()
        if not account:
            raise HTTPException(status_code=400, detail="Account is required.")
        if account not in conns.conn:
            raise HTTPException(status_code=400, detail=f"Unknown account: {account}.")

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
                if chase_eligible:
                    # Background chase loop — first place_order
                    # returns synchronously; loop keeps running.
                    order_id = await _start_live_chase(
                        account=account,
                        symbol=sym,
                        exchange=(data.exchange or "NFO"),
                        transaction_type=side,
                        quantity=qty,
                        aggressiveness=(data.chase_aggressiveness or "low"),
                    )
                    chase_tag = f" CHASE[{(data.chase_aggressiveness or 'low').lower()}]"
                else:
                    # Single-shot — preserves the existing path for
                    # MARKET / SL-M and explicit chase=False tickets.
                    from backend.shared.brokers.kite import to_kite_qty, get_lot_size
                    _ls_ticket = await get_lot_size(data.exchange or "NFO", sym)
                    _kq_ticket = to_kite_qty(data.exchange or "NFO", qty, _ls_ticket)
                    broker = _broker_for(account)
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

                invalidate("orders")    # refresh /api/orders cache
                masked = mask_column(pd.Series([account]))[0]
                logger.info(f"Ticket LIVE order: {order_id} [{masked}] "
                            f"{side} {qty} {sym}{chase_tag}")
                try:
                    import asyncio as _aio
                    from backend.api.algo.agent_engine import record_manual_event
                    _aio.create_task(record_manual_event(
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
                masked_acct = mask_column(pd.Series([account]))[0]
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
                    import asyncio as _aio
                    from backend.api.algo.agent_engine import record_manual_event
                    _aio.create_task(record_manual_event(
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

        # ── Paper preflight gate ──────────────────────────────────
        # Catch obvious blockers (qty freeze, segment inactive)
        # before the engine churns on an order that would never fill.
        from backend.api.algo.actions import run_preflight as _run_pf_paper
        _pfp = await _run_pf_paper(account, {
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
        if not _pfp["ok"]:
            raise HTTPException(
                status_code=422,
                detail={"blocked": _pfp["blocked"],
                        "diagnostics": _pfp.get("diagnostics", {})},
            )

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
        try:
            async with async_session() as s:
                row = AlgoOrder(
                    account=account, symbol=sym, exchange=(data.exchange or "NFO"),
                    transaction_type=side, quantity=qty,
                    initial_price=(float(data.price) if data.price is not None else None),
                    status="OPEN", engine="paper", mode="paper",
                    agent_id=_manual_aid,
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
                import asyncio as _aio
                _aio.create_task(_write_evt(
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

        masked = mask_column(pd.Series([account]))[0]
        logger.info(f"Ticket paper order: {algo_order_id} [{masked}] {side} {qty} {sym}")
        if algo_order_id:
            try:
                import asyncio as _aio
                from backend.api.algo.agent_engine import record_manual_event
                _aio.create_task(record_manual_event(
                    outcome="action_success", source=data.source or "ticket",
                    account=account, symbol=sym,
                    exchange=(data.exchange or "NFO"), side=side,
                    qty=qty, mode="paper", order_id=str(algo_order_id),
                ))
            except Exception:
                pass
        return TicketOrderResponse(
            order_id=str(algo_order_id),
            mode="paper",
            status="OPEN",
            detail=f"Paper order #{algo_order_id} placed — chase loop will fill it on the next bid/ask cross.",
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
        masked = mask_column(pd.Series([data.account]))[0]
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
            masked          = mask_column(pd.Series([account]))[0] if account else ""

            # ── HMAC verification ────────────────────────────────────
            # Kite signs each postback with:
            #   sha256(order_id + order_timestamp + api_secret)
            # Try the account's own api_secret first; fall back to
            # iterating every loaded account (Kite postback doesn't
            # always include a recognisable user_id). We have ≤5
            # accounts so the iteration is negligible.
            conns = Connections()
            candidates: list[str] = []
            if account and account in conns.conn:
                # Put the claimed account first so the fast path hits.
                candidates = [account] + [a for a in conns.conn if a != account]
            else:
                candidates = list(conns.conn.keys())

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

            # Timeline: write postback event to any matching AlgoOrder rows.
            # We match on broker_order_id (Kite's order_id string).  Best-effort —
            # never raises; the postback acknowledgement must still return quickly.
            try:
                from sqlalchemy import select as _sql_select
                from backend.api.database import async_session as _async_session
                from backend.api.models import AlgoOrder as _AlgoOrder
                from backend.api.algo.order_events import write_event as _write_event
                import asyncio as _asyncio

                async def _pb_event():
                    try:
                        async with _async_session() as _s:
                            _rows = (await _s.execute(
                                _sql_select(_AlgoOrder).where(
                                    _AlgoOrder.broker_order_id == str(order_id)
                                )
                            )).scalars().all()
                        for _r in _rows:
                            await _write_event(
                                _r.id, "postback",
                                f"Kite postback: {status} {txn} {qty} {tradingsymbol} "
                                f"@{price}",
                                payload={
                                    "broker_order_id": order_id,
                                    "status": status,
                                    "tradingsymbol": tradingsymbol,
                                    "transaction_type": txn,
                                    "quantity": qty,
                                    "price": price,
                                    "status_message": status_msg,
                                },
                            )
                    except Exception as _pe:
                        logger.debug(f"postback event write failed: {_pe}")

                _asyncio.create_task(_pb_event())
            except Exception:
                pass

            # Invalidate orders cache so next fetch gets fresh data
            invalidate("orders")

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

            return {"status": "ok"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Postback error: {e}")
            return {"status": "error", "detail": str(e)}

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
        masked = mask_column(pd.Series([account]))[0]
        try:
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
        # display = real account_id for any authenticated caller,
        # masked (ZG####) for anonymous (demo / public). Symmetric
        # with mask_column() in row endpoints (positions/holdings/funds).
        conn = Connections().conn
        do_mask = not is_authenticated_request(request)
        accounts = [
            AccountInfo(
                account_id=account,
                display=(mask_column(pd.Series([account]))[0] if do_mask else account),
            )
            for account in conn
        ]
        return AccountsResponse(accounts=accounts)
