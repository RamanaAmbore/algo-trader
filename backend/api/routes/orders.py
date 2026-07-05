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
    ReconcileSingleRequest,
    TicketOrderRequest,
    TicketOrderResponse,
    TicketPreviewRequest,
    TicketPreviewResponse,
)
from backend.brokers.connections import Connections
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_account_in_text, mask_column, secrets

logger = get_logger(__name__)

# ── Re-exports from split sub-modules ─────────────────────────────────────────
# Symbols below were extracted in the RED-zone split (orders.py 4322→<1500 LOC).
# They are imported here so that:
#   1. All existing callers of `from backend.api.routes.orders import X` keep
#      working without change (backward-compat shim).
#   2. The controller methods that reference these names in their bodies find
#      them in the module namespace as before.
#
# DO NOT remove these re-exports until all external callers have been updated.
from backend.api.routes.orders_helpers import (  # noqa: E402
    _VARIETIES,
    _ORDER_TYPES,
    _PRODUCTS,
    _TXN_TYPES,
    _EXCHANGES,
    _VALIDITIES,
    _ORDERS_TTL,
    _REJECTION_TRACKER,
    _REJECTION_WINDOW_S,
    _REJECTION_THRESHOLD,
    _BREAKER_ALERT_COOLDOWN_S,
    _BREAKER_LAST_ALERT,
    _rejection_key,
    _prune_rejection_window,
    _rejection_count,
    _record_rejection,
    _clear_rejections,
    _maybe_send_breaker_alert,
    _TICK_INDEX,
    _TICK_INDEX_STAMP,
    _rebuild_tick_index,
    _align_price_to_tick,
    _broker_for,
    _live_chase_config,
    _start_live_chase,
    _row_from_dict,
    _fetch_orders,
    AlgoOrderEventInfo,
    AlgoOrderInfo,
    _fetch_child_order_ids,
    _resolve_target_pct,
    _ticket_overrides_dict,
    _build_overrides_json,
)
from backend.api.routes.orders_place import (  # noqa: E402
    _enforce_capacity_guard,
    _TEMPLATE_ATTACH_LOCKS,
    _TEMPLATE_ATTACH_META_LOCK,
    _TPL_LOCK_TTL_S,
    _get_template_attach_lock,
    _maybe_fire_template_attach_for_reconcile,
    _fire_template_attach_on_fill,
    _maybe_attach_template_to_ticket,
    _attach_basket_leg_template,
    _arm_take_profit,
)
from backend.api.routes.orders_postback import (  # noqa: E402
    _process_broker_postback,
)

import time as _time  # used by _postback_broadcast_fanout for ts fields


def _postback_broadcast_fanout(
    *,
    status: str,
    order_id,
    account: str,
    masked: str,
    symbol: str,
    txn: str,
    qty,
    price,
    exchange: str = "",
    status_message: str = "",
) -> None:
    """Cache invalidation + WS broadcast trio shared by every broker
    postback handler (Kite inline, Dhan/Groww via _process_broker_postback).

    Steps:
      1. `invalidate("orders")` always
      2. On terminal status: also invalidate `positions`, `holdings`,
         and the raw-DataFrame caches (positions/holdings/margins).
      3. Broadcast `order_update` with the postback payload.
      4. On COMPLETE: broadcast `position_filled` with signed qty
         delta so the frontend can patch its local positions
         table immediately (no waiting for the 5-min poll).
      5. On terminal: broadcast `book_changed` so subscribers can
         refetch their primary loaders in one coordinated pass.

    Best-effort — every step wrapped in try/except so a single
    broadcast failure can't break the postback ACK. Kite's webhook
    will retry on a non-2xx response, so swallowing fan-out errors
    here is safer than propagating.
    """
    _terminal = str(status or "").upper() in (
        "COMPLETE", "CANCELLED", "REJECTED", "EXPIRED",
    )

    try:
        invalidate("orders")
        if _terminal:
            for _key in ("positions", "holdings"):
                try:
                    invalidate(_key)
                except Exception:
                    pass
            # Also drop the raw-DataFrame cache in broker_apis so
            # compute_firm_nav + investor slice see fresh broker
            # state on the next call. Without this, NavCard /
            # /performance NAV row could lag by up to _RAW_TTL_S
            # (30 s) after a fill.
            try:
                from backend.brokers.broker_apis import _raw_cache_invalidate
                _raw_cache_invalidate("positions")
                _raw_cache_invalidate("holdings")
                _raw_cache_invalidate("margins")
            except Exception:
                pass

        broadcast(json.dumps({
            "event": "order_update",
            "order_id": order_id, "account": masked, "status": status,
            "tradingsymbol": symbol, "transaction_type": txn,
            "quantity": qty, "price": price, "status_message": status_message,
        }))

        if str(status).upper() == "COMPLETE":
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
        logger.warning(f"postback fan-out failed: {_be}")


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
                    from backend.brokers import get_broker
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
        from backend.brokers import get_broker
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
        from backend.brokers import get_broker
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
        _mask = mask_account_in_text if do_mask else (lambda raw: raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask(r.payload_json),
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
        _mask = mask_account_in_text if do_mask else (lambda raw: raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask(r.payload_json),
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
        """Ticket order placement. Delegates full logic to orders_place.ticket_order_handler."""
        from backend.api.routes.orders_place import ticket_order_handler
        return await ticket_order_handler(data, request)

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
        """Kite postback. Delegates to orders_postback.kite_postback_handler."""
        from backend.api.routes.orders_postback import kite_postback_handler
        return await kite_postback_handler(request)
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
        # Also translate the raw Dhan symbol (CRUDEOIL-16JUL2026-8500-CE) to
        # Kite format and map the exchangeSegment (NSE_FNO, MCX_COMM) to the
        # Kite canonical exchange string (NFO, MCX) so WS broadcasts + DB rows
        # carry values that frontend consumers and the chase loop can match.
        raw_seg = str(body.get("exchangeSegment") or "")
        try:
            from backend.brokers.adapters.dhan import (
                _DHAN_STATUS_TO_KITE,
                _DHAN_SEGMENT_TO_EXCHANGE,
                _dhan_to_kite_symbol,
            )
            kite_status = _DHAN_STATUS_TO_KITE.get(status, status)
            kite_symbol = _dhan_to_kite_symbol(str(symbol)) if symbol else str(symbol)
            kite_exchange = _DHAN_SEGMENT_TO_EXCHANGE.get(raw_seg, raw_seg)
        except Exception:
            kite_status = status
            kite_symbol = str(symbol)
            kite_exchange = raw_seg

        await _process_broker_postback(
            broker_id="dhan",
            order_id=order_id,
            status=kite_status,
            account=str(account),
            symbol=kite_symbol,
            txn=str(txn),
            qty=qty,
            price=price,
            exchange=kite_exchange,
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
            from backend.brokers.adapters.groww import _GROWW_STATUS_TO_KITE
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
        """Compute offset-aware margin for a basket WITHOUT placing. Delegates to orders_basket."""
        from backend.api.routes.orders_basket import basket_margin_handler
        return await basket_margin_handler(data, request)

    @post("/basket")
    async def basket_order(self, data: BasketOrderRequest, request: Request) -> BasketOrderResponse:
        """Multi-account basket order dispatch. Delegates to orders_basket."""
        from backend.api.routes.orders_basket import basket_order_handler
        return await basket_order_handler(data, request)

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
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_accounts(self, request: Request) -> AccountsResponse:
        # Raw account codes gated to admin/designated only. Partner JWTs
        # and demo (anonymous) sessions get masked codes (ZG####, D1####, …),
        # symmetric with mask_column() in row endpoints (positions/holdings/funds).
        # auth_or_demo_guard allows anonymous sessions on prod so the demo UI
        # can populate the account dropdown with all broker accounts (incl.
        # Dhan / Groww) even when those brokers have zero positions/holdings.
        conn = Connections().conn
        # Cutover branch — when conn_service owns the sessions, local
        # Connections.conn is empty. Fetch the canonical account list
        # from /internal/accounts so order modals get a populated
        # dropdown. Without this, every order surface (derivatives
        # ticket, /orders ticket, /pulse modal) saw an empty list.
        loaded_accounts: list[str] = list(conn.keys())
        if not loaded_accounts:
            from backend.brokers.client import is_cutover_on
            if is_cutover_on():
                from backend.brokers.client.remote_broker import list_remote_accounts
                loaded_accounts = [
                    r["account"] for r in list_remote_accounts() if r.get("account")
                ]
        do_mask = not is_admin_request(request)
        accounts = [
            AccountInfo(
                # Non-admin (demo / partner) callers get masked codes in BOTH
                # fields so the raw broker ID never surfaces in JS memory.
                # account_id == display for masked callers; downstream order-
                # placement guards (auth_or_demo_guard + broker checks) prevent
                # the masked code from being submitted as a real order target.
                account_id=(mask_account(account) if do_mask else account),
                display=(mask_account(account) if do_mask else account),
            )
            for account in loaded_accounts
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
        if default_acct and default_acct not in loaded_accounts:
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
