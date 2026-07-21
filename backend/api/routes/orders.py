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
from backend.shared.helpers.utils import mask_account, mask_account_in_text, secrets

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


# ── Chase-reconcile helpers (extracted from list_active_chases, reconcile_algo_orders, retry_template) ─
_CHASE_LIVE_TERMINAL = frozenset({"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"})
_CHASE_KITE_TO_ALGO = {
    "COMPLETE":  "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED":  "REJECTED",
    "EXPIRED":   "UNFILLED",
}


def _chase_snapshot_paper_open_ids() -> Optional[set]:
    """Snapshot paper engine's in-flight AlgoOrder ids, once per request.
    Returns None when the engine can't be reached — callers use that
    sentinel to skip the paper-row drop path.
    """
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        _pe = get_prod_paper_engine()
        return {o.get("algo_order_id") for o in _pe.open_order_details()}
    except Exception:
        return None


async def _chase_snapshot_broker_status_by_id() -> dict[str, dict]:
    """Snapshot the cached live broker order book keyed by order_id.
    Values are `{"status": <UPPER>, "average_price": <float>}`. On
    cache miss returns an empty dict — callers treat that as "broker
    snapshot unavailable, keep row as OPEN".
    """
    out: dict[str, dict] = {}
    try:
        _ord_resp = await get_or_fetch("orders", _fetch_orders,
                                       ttl_seconds=_ORDERS_TTL)
        for _o in (_ord_resp.rows or []):
            _bid = str(getattr(_o, "order_id", "") or "")
            if _bid:
                out[_bid] = {
                    "status": str(getattr(_o, "status", "") or "").upper(),
                    "average_price": float(getattr(_o, "average_price", 0) or 0),
                }
    except Exception as _oe:
        logger.debug(f"chases/active broker snapshot failed: {_oe}")
    return out


def _chase_process_paper_row(r, _paper_open_ids: set) -> tuple[bool, int]:
    """Handle one paper-mode row. Returns `(drop, dropped_paper_delta)`.
    Only counts as `dropped_paper` when this call actually flipped the
    row from OPEN → UNFILLED (concurrent step() may have raced us to a
    terminal state — do NOT overcount those).
    """
    if r.id in _paper_open_ids:
        return False, 0
    if r.status == "OPEN":
        r.status = "UNFILLED"
        r.detail = ((r.detail or "")[:200]
                    + " · paper engine no longer tracking")
        return True, 1
    return True, 0


def _rco_apply_fill_price(r, bo: dict) -> None:
    """Stamp fill_price + filled_at on a row that just transitioned to FILLED.
    Silently skips when average_price is absent or unconvertible.
    """
    if bo.get("average_price"):
        try:
            r.fill_price = float(bo["average_price"])
        except (TypeError, ValueError):
            pass
    r.filled_at = datetime.now(timezone.utc)


def _chase_process_live_row(
    r,
    _broker_status_by_id: dict[str, dict],
    _reconciled_filled: list,
) -> tuple[bool, int, int]:
    """Handle one live-mode row. Returns
    `(drop, dropped_live_delta, reconciled_live_delta)`. Appends to
    `_reconciled_filled` when a row flips to FILLED so the caller can
    fire template-attach post-commit.
    """
    if not (r.broker_order_id or "").strip():
        r.status = "REJECTED"
        r.detail = ((r.detail or "")[:200]
                    + " · live placement never returned broker_order_id")
        return True, 1, 0

    _bo = _broker_status_by_id.get(str(r.broker_order_id))
    if not _bo or _bo["status"] not in _CHASE_LIVE_TERMINAL:
        return False, 0, 0

    new_status = _CHASE_KITE_TO_ALGO[_bo["status"]]
    if r.status != new_status:
        r.status = new_status
        if new_status == "FILLED":
            _rco_apply_fill_price(r, _bo)
            _reconciled_filled.append(r)
        r.detail = ((r.detail or "")[:200]
                    + f" · broker says {_bo['status']}")
        return True, 0, 1
    return True, 0, 0


def _retry_parse_overrides(overrides_json) -> dict:
    """Best-effort parse of the row's persisted per-submit overrides.
    Returns `{}` on any failure — the retry pipeline just re-runs
    without the overrides in that case.
    """
    if not overrides_json:
        return {}
    try:
        import json as _json_parse
        _parsed = _json_parse.loads(overrides_json)
        return _parsed if isinstance(_parsed, dict) else {}
    except Exception:
        return {}


def _retry_build_gtt_entry(spec, gid: str, plan, product: str) -> dict:
    """Build one `attached_gtts_json` GTT entry mirroring the exact
    shape `_fire_template_attach_on_fill` writes. Split out so both
    surfaces can't drift.

    Populates the trail-stop scaffolding (`sl_trail_pct`,
    `current_trigger`, `highest_ltp`, `lowest_ltp`, `tp_trigger`) and
    the parent metadata block downstream pollers (`trail-stop poller`,
    OCO pair-watcher) rely on.
    """
    _entry: dict = {
        "kind":           "gtt",
        "label":          spec.label,
        "id":             gid,
        "trigger_values": list(spec.trigger_values or []),
        "trigger_type":   str(spec.trigger_type),
    }
    _is_two_leg = (str(spec.trigger_type) == "two-leg"
                   and len(spec.trigger_values or []) >= 2)
    if spec.sl_trail_pct is not None and spec.trigger_values:
        _entry["sl_trail_pct"]    = float(spec.sl_trail_pct)
        _entry["current_trigger"] = float(spec.trigger_values[-1])
        if _is_two_leg:
            _entry["tp_trigger"]  = float(spec.trigger_values[0])
        _entry["highest_ltp"]     = float(plan.parent_fill_price)
        _entry["lowest_ltp"]      = float(plan.parent_fill_price)
    elif _is_two_leg:
        # Non-trail two-leg still wants tp_trigger for any modify_gtt
        # round-trip the operator might trigger later.
        _entry["tp_trigger"]      = float(spec.trigger_values[0])
    # Parent metadata — needed by both trail-stop poller
    # (rebuild orders_payload) and OCO pair-watcher (sibling
    # cancel routing).
    _entry["parent_side"]     = str(plan.parent_side)
    _entry["parent_symbol"]   = str(plan.parent_symbol)
    _entry["parent_exchange"] = str(plan.parent_exchange)
    _entry["parent_account"]  = str(plan.parent_account)
    _entry["parent_qty"]      = int(plan.parent_qty)
    _entry["parent_product"]  = str(product or "NRML")
    return _entry


def _retry_build_attached_payload(result, product: str) -> list:
    """Build the full `attached_gtts_json` list from an attach result.
    One dict per GTT (via `_retry_build_gtt_entry`) plus a `wing`
    entry when the plan issued a wing order.
    """
    payload: list = []
    if result.plan and result.gtt_ids:
        for _spec, _gid in zip(result.plan.gtts, result.gtt_ids):
            payload.append(_retry_build_gtt_entry(
                _spec, _gid, result.plan, product,
            ))
    if result.wing_order_id:
        payload.append({
            "kind":  "wing",
            "label": "Wing",
            "id":    result.wing_order_id,
        })
    return payload


async def _retry_effective_parent_qty(row) -> int:
    """Prefer accumulated `filled_quantity` when non-zero so a partial
    fill doesn't oversize the exit GTT (mirrors the postback path).
    Falls back to original quantity when no partial fill was captured.

    M7: re-fetch `filled_quantity` from the DB rather than relying on
    the in-memory row snapshot, which may be stale if the postback
    arrived after the reconcile loaded the row but before it applied
    the fill count. The extra SELECT is cheap (PK lookup) and prevents
    an exit GTT sized off an unupdated row.
    """
    try:
        from sqlalchemy import select as _sel_rq
        from backend.api.database import async_session as _ars
        from backend.api.models import AlgoOrder as _AO_rq
        async with _ars() as _s_rq:
            _fresh_filled = (await _s_rq.execute(
                _sel_rq(_AO_rq.filled_quantity).where(_AO_rq.id == row.id)
            )).scalar_one_or_none()
        if _fresh_filled is not None and int(_fresh_filled or 0) > 0:
            return int(_fresh_filled)
    except Exception as _rq_err:
        logger.debug(
            "[RETRY-QTY] DB re-fetch failed for #%s, falling back "
            "to in-memory row: %s", getattr(row, "id", None), _rq_err,
        )
    # Fall back to the in-memory row value.
    filled = int(row.filled_quantity or 0)
    if filled > 0:
        return filled
    return int(row.quantity or 0)


def _retry_build_result_response(result, attached_now: bool) -> dict:
    """Materialise the retry outcome into the operator-facing dict.
    When the plan resolved cleanly but produced neither GTTs nor a wing,
    treat that as a failure with a clear reason (Sc.5c) rather than a
    silent "ok: true, attached: false".
    """
    _notes = result.plan.notes if result.plan else []
    _errors = result.errors or []
    if not attached_now and not _errors:
        return {
            "ok":            False,
            "reason":        "Template produced no GTTs and no wing — nothing to attach (check overrides + chain-scan filters)",
            "wing_order_id": result.wing_order_id,
            "gtt_ids":       result.gtt_ids,
            "notes":         _notes,
            "errors":        result.errors,
            "attached":      False,
        }
    return {
        "ok":            True,
        "wing_order_id": result.wing_order_id,
        "gtt_ids":       result.gtt_ids,
        "notes":         _notes,
        "errors":        result.errors,
        "attached":      attached_now,
    }


def _retry_precheck_row(row) -> Optional[dict]:
    """Return an early-exit response dict when the row shouldn't be
    retried; `None` when the retry may proceed.
    """
    if row.template_id is None:
        return {"ok": False, "reason": "no template attached to this order"}
    if row.attached_gtts_json:
        return {"ok": False, "reason":
                "template already attached — nothing to retry"}
    if (row.status or "").upper() != "FILLED":
        return {"ok": False,
                "reason": f"parent must be FILLED to attach (status={row.status})"}
    return None


def _chase_row_to_info(r, masked_acct, child_map: dict) -> "AlgoOrderInfo":
    """Materialise an AlgoOrder ORM row into the API response Struct.
    Extracted so the list_active_chases + list_orders response builders
    can share the same mapping.
    """
    return AlgoOrderInfo(
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
        interval_seconds=(int(r.interval_seconds) if getattr(r, "interval_seconds", None) is not None else None),
        last_attempt_at=(float(r.last_attempt_at) if getattr(r, "last_attempt_at", None) is not None else None),
        next_attempt_at=(float(r.next_attempt_at) if getattr(r, "next_attempt_at", None) is not None else None),
    )


def _rco_invalidate_terminal_caches(status: str) -> None:
    """Invalidate API + raw-DF caches on terminal order status.

    positions/holdings caches are only busted on COMPLETE — they don't
    change on CANCELLED/REJECTED/EXPIRED, so busting them there just causes
    a cold-cache broker round-trip on the next NavStrip poll.

    funds/margins are busted on every terminal status: a fill releases margin
    and a cancel may release reserved funds.
    """
    _is_complete = str(status).upper() == "COMPLETE"
    if _is_complete:
        for _key in ("positions", "holdings"):
            try:
                invalidate(_key)
            except Exception:
                pass
    try:
        invalidate("funds")
    except Exception:
        pass
    try:
        from backend.brokers.broker_apis import _raw_cache_invalidate
        if _is_complete:
            _raw_cache_invalidate("positions")
            _raw_cache_invalidate("holdings")
        _raw_cache_invalidate("margins")
    except Exception:
        pass


def _rco_broadcast_position_filled(
    masked: str, exchange: str, symbol: str, txn: str, qty, price, order_id
) -> None:
    """Broadcast position_filled WS event on COMPLETE. No-op when qty is zero."""
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


async def _positions_refresh_after_fill(
    account: str, tradingsymbol: str, qty_delta: int
) -> None:
    """Fire-and-forget: poll broker positions for up to 5 s after a fill,
    then invalidate the raw cache + broadcast positions_refreshed once the
    symbol shows up (or qty changes).  The initial cache bust in
    _rco_invalidate_terminal_caches() clears stale data immediately; this
    task handles the broker-propagation lag so the UI eventually sees the
    new position without waiting for the 5-min performance poll.
    """
    try:
        import asyncio as _aio
        from backend.brokers.broker_apis import fetch_positions, _raw_cache_invalidate
        initial_qty: int | None = None
        # Give the broker 2 s to propagate the fill before the first poll so
        # that (a) we don't read pre-fill data and set initial_qty incorrectly,
        # and (b) a fully-closed position (rows=[]) is not confused with "no
        # position ever existed".
        await _aio.sleep(2)
        for _attempt in range(5):
            await _aio.sleep(1)
            try:
                dfs = await _aio.to_thread(fetch_positions)
                rows = [
                    r for df in (dfs or [])
                    for r in df.to_dict(orient="records")
                    if r.get("tradingsymbol") == tradingsymbol
                ]
                if initial_qty is None:
                    initial_qty = sum(int(r.get("quantity", 0)) for r in rows)

                cur_qty = sum(int(r.get("quantity", 0)) for r in rows)
                # Fire only when quantity has actually changed, or when the
                # position disappeared entirely (close fill → empty rows) after
                # we already had a non-empty baseline.  The old
                # `qty_delta > 0 and cur_qty > 0` arm fired on the very first
                # poll for any existing BUY position, sending pre-fill data.
                changed = (cur_qty != initial_qty) or (not rows and initial_qty is not None)
                if changed:
                    _raw_cache_invalidate("positions")
                    broadcast(json.dumps({
                        "event": "positions_refreshed",
                        "tradingsymbol": tradingsymbol,
                        "account": account,
                        "ts": int(_time.time() * 1000),
                    }))
                    logger.debug(
                        "[FILL-POLL] positions_refreshed after %d attempt(s) "
                        "symbol=%s qty_delta=%+d",
                        _attempt + 1, tradingsymbol, qty_delta,
                    )
                    return
            except Exception as _poll_err:
                logger.debug("[FILL-POLL] attempt %d error: %s", _attempt + 1, _poll_err)
        logger.warning(
            "[FILL-POLL] positions_refreshed timed out after 5 attempts "
            "symbol=%s qty_delta=%+d",
            tradingsymbol, qty_delta,
        )
    except Exception as _outer:
        logger.debug("[FILL-POLL] outer error: %s", _outer)


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
            _rco_invalidate_terminal_caches(status)

        broadcast(json.dumps({
            "event": "order_update",
            "order_id": order_id, "account": masked, "status": status,
            "tradingsymbol": symbol, "transaction_type": txn,
            "quantity": qty, "price": price, "status_message": status_message,
        }))

        if str(status).upper() == "COMPLETE":
            _rco_broadcast_position_filled(masked, exchange, symbol, txn, qty, price, order_id)
            try:
                _qty_int = int(qty or 0)
                _side_sign = 1 if (txn or "").upper() == "BUY" else -1
                asyncio.create_task(
                    _positions_refresh_after_fill(account, symbol, _qty_int * _side_sign)
                )
            except Exception:
                pass

        if _terminal:
            broadcast(json.dumps({
                "event": "book_changed",
                "account": masked, "exchange": exchange,
                "tradingsymbol": symbol, "reason": status,
                "ts": int(_time.time() * 1000),
            }))
    except Exception as _be:
        logger.warning(f"postback fan-out failed: {_be}")


# ── Controller-level helpers (_rco_*) ─────────────────────────────────────────
# Extracted from D-grade controller methods to reduce cyclomatic complexity.

async def _rco_kill_paper_mode(row) -> str:
    """Cancel a paper-mode chase row via the engine's canonical cancel path.
    Returns an error string on failure, empty string on success."""
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        eng = get_prod_paper_engine()
        cancelled = eng.cancel_paper_order(row.id)
        if not cancelled:
            return "paper engine no longer tracking"
        return ""
    except Exception as e:
        return f"paper cancel failed: {e}"


async def _rco_kill_live_mode(row) -> str:
    """Cancel a live-mode chase row at the broker. Returns error string on
    failure, empty string on success. Marks the broker_order_id as killed
    before the broker call so the chase loop sees the flag even if a poll
    lands between cancel and DB mark."""
    from backend.brokers import get_broker
    if not str(row.account or "").strip():
        return "no account on row"
    if not (row.broker_order_id or "").strip():
        return "no broker_order_id on row"
    try:
        broker = get_broker(str(row.account))
    except Exception as _ge:
        return f"broker not loaded for {row.account}: {_ge}"
    try:
        from backend.api.algo.chase import mark_killed
        mark_killed(str(row.broker_order_id))
    except Exception:
        pass
    try:
        await asyncio.to_thread(
            broker.cancel_order,
            str(row.broker_order_id),
            variety="regular",
        )
        return ""
    except Exception as e:
        return f"broker cancel failed: {e}"


def _rco_reconcile_active_rows(
    rows: list,
    paper_open_ids: "set | None",
    broker_status_by_id: dict,
) -> "tuple[list, list, bool]":
    """Scan OPEN/CANCEL_FAILED rows and inline-reconcile paper + live modes.

    Returns (kept, reconciled_filled, needs_commit).
    `reconciled_filled` is the list of rows that just transitioned to FILLED
    so the caller can fire template-attach after the DB commit.
    """
    kept: list = []
    reconciled_filled: list = []
    dropped_paper = dropped_live = reconciled_live = 0
    for r in rows:
        mode = (r.mode or "").lower()
        if mode == "paper" and paper_open_ids is not None:
            drop, d_delta = _chase_process_paper_row(r, paper_open_ids)
            dropped_paper += d_delta
            if drop:
                continue
        elif mode == "live":
            drop, d_delta, r_delta = _chase_process_live_row(r, broker_status_by_id, reconciled_filled)
            dropped_live += d_delta
            reconciled_live += r_delta
            if drop:
                continue
        kept.append(r)
    needs_commit = bool(dropped_paper or dropped_live or reconciled_live)
    return kept, reconciled_filled, needs_commit


_PREVIEW_FO_EXCHANGES = frozenset({"NFO", "MCX", "CDS", "BFO", "BCD", "NCO"})


async def _rco_preview_resolve_qty(exch: str, sym: str, input_qty: int) -> int:
    """Convert LOTS to contracts for F&O exchanges in ticket_preview.
    Returns contracts when lot_size is available, raw input_qty otherwise.
    """
    if exch not in _PREVIEW_FO_EXCHANGES or input_qty <= 0:
        return input_qty
    from backend.brokers.adapters.kite import get_lot_size as _prev_lot
    try:
        _lot = int(await _prev_lot(exch, sym) or 0)
    except Exception:
        _lot = 0
    return input_qty * _lot if _lot > 0 else input_qty


def _rco_preview_empty_plan(data) -> dict:
    """Build a stub plan dict when apply_template_to_order returns None."""
    return {
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


def _rco_parse_dhan_postback_body(body: dict) -> "tuple[str, str, str, str, str, object, object, str, str]":
    """Extract canonical (order_id, account, kite_status, kite_symbol, txn,
    qty, price, kite_exchange, status_message) from a Dhan postback payload.
    """
    order_id = str(body.get("orderId") or body.get("order_id") or "")
    status   = str(body.get("orderStatus") or body.get("status") or "").upper()
    symbol   = body.get("tradingSymbol") or body.get("tradingsymbol") or ""
    txn      = str(body.get("transactionType") or body.get("transaction_type") or "")
    qty      = body.get("filledQuantity") or body.get("quantity") or 0
    price    = body.get("averageTradedPrice") or body.get("price") or 0
    account  = str(body.get("dhanClientId") or body.get("account") or "")
    raw_seg  = str(body.get("exchangeSegment") or "")
    status_msg = str(body.get("statusMessage") or "")
    try:
        from backend.brokers.adapters.dhan import (
            _DHAN_STATUS_TO_KITE,
            _DHAN_SEGMENT_TO_EXCHANGE,
            _dhan_to_kite_symbol,
        )
        kite_status   = _DHAN_STATUS_TO_KITE.get(status, status)
        kite_symbol   = _dhan_to_kite_symbol(str(symbol)) if symbol else str(symbol)
        kite_exchange = _DHAN_SEGMENT_TO_EXCHANGE.get(raw_seg, raw_seg)
    except Exception:
        kite_status   = status
        kite_symbol   = str(symbol)
        kite_exchange = raw_seg
    return order_id, account, kite_status, kite_symbol, txn, qty, price, kite_exchange, status_msg


def _rco_parse_groww_postback_body(body: dict) -> "tuple[str, str, str, str, object, object, str, str]":
    """Extract canonical (order_id, kite_status, symbol, txn, qty, price,
    exchange, status_message) from a Groww postback payload.
    """
    order_id = str(body.get("groww_order_id") or body.get("order_id") or "")
    status   = str(body.get("order_status") or body.get("status") or "").upper()
    symbol   = str(body.get("trading_symbol") or body.get("symbol") or "")
    txn      = str(body.get("transaction_type") or "")
    qty      = body.get("filled_quantity") or body.get("quantity") or 0
    price    = body.get("average_price") or body.get("price") or 0
    exchange = str(body.get("exchange") or body.get("segment") or "")
    status_msg = str(body.get("status_message") or "")
    try:
        from backend.brokers.adapters.groww import _GROWW_STATUS_TO_KITE
        kite_status = _GROWW_STATUS_TO_KITE.get(status, status)
    except Exception:
        kite_status = status
    return order_id, kite_status, symbol, txn, qty, price, exchange, status_msg


async def _rco_run_template_attach(row) -> "tuple | None":
    """Run apply_template_to_order for *row* and persist attached_gtts_json.

    Returns (result, payload_persisted) where payload_persisted is True
    when attached_gtts_json was written to the row. Returns None when
    apply_template_to_order returned no result.

    Must be called within an active SQLAlchemy session context (row is
    session-attached). The caller must commit after this returns.
    """
    from backend.api.algo.template_attach import apply_template_to_order

    apply_path = "sim" if (row.mode or "").lower() == "sim" else "live"
    _retry_overrides = _retry_parse_overrides(row.template_overrides_json)
    result = await apply_template_to_order(
        template_id=row.template_id,
        template_slug=None,
        overrides=_retry_overrides,
        parent_account=row.account or "",
        parent_symbol=row.symbol or "",
        parent_side=row.transaction_type or "BUY",
        parent_qty=await _retry_effective_parent_qty(row),
        parent_exchange=row.exchange or "NFO",
        parent_fill_price=float(row.fill_price or row.initial_price or 0),
        parent_product=row.product or "NRML",
        parent_order_id=row.id,
        apply_path=apply_path,
    )
    return result


def _rco_persist_attach_payload(row, result, session) -> bool:
    """Write the attached_gtts_json payload onto *row* from *result*.
    Returns True when a payload was written, False when empty.
    Assumes the session's commit will be called by the caller.
    """
    _attached_payload = _retry_build_attached_payload(result, row.product or "NRML")
    if _attached_payload:
        import json as _json_retry
        row.attached_gtts_json = _json_retry.dumps(_attached_payload)
        if result.errors:
            row.detail = ((row.detail or "")[:200]
                          + " · retry attach: "
                          + "; ".join(result.errors[:2]))[:240]
        return True
    return False


async def _rco_fetch_broker_order(acct: str, broker_order_id: str) -> "tuple[dict | None, str | None, str | None]":
    """Fetch broker order book for *acct* and look up *broker_order_id*.

    Returns (bo, kite_status, target) where:
    - bo is the matching order dict or None when not found
    - kite_status is the UPPER-cased Kite status string or None
    - target is the mapped algo status ("FILLED", "CANCELLED", …) or None

    Raises HTTPException on auth / network failure.
    """
    from backend.brokers import get_broker

    try:
        broker = get_broker(acct)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"account {acct} not loaded ({e})")
    try:
        broker_orders = await asyncio.to_thread(broker.orders)
    except Exception as e:
        logger.warning(f"reconcile {broker_order_id}: broker.orders() failed: {e}")
        raise HTTPException(status_code=502, detail=f"broker fetch failed: {e}")

    by_id = {str(o.get("order_id")): o for o in (broker_orders or [])}
    bo = by_id.get(str(broker_order_id))
    kite_status = str(bo.get("status") or "").upper() if bo else None
    target = _RECONCILE_KITE_TO_ALGO.get(kite_status) if kite_status else None
    return bo, kite_status, target


async def _rco_dispatch_kill(row) -> str:
    """Dispatch the mode-appropriate cancel for a chase row.
    Returns an error string on failure, empty string on success.
    """
    mode = (row.mode or "").lower()
    if mode == "paper":
        return await _rco_kill_paper_mode(row)
    if mode == "live":
        return await _rco_kill_live_mode(row)
    return ""


def _rco_apply_kill_status(row, err_msg: str) -> None:
    """Stamp CANCELLED or CANCEL_FAILED status + detail onto the row."""
    if err_msg:
        row.status = "CANCEL_FAILED"
        row.detail = ((row.detail or "")[:200] + f" · cancel failed: {err_msg}")
    else:
        row.status = "CANCELLED"
        row.detail = ((row.detail or "")[:200] + " · killed by operator")


_RECONCILE_KITE_TO_ALGO = {
    "COMPLETE":  "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED":  "REJECTED",
    "EXPIRED":   "UNFILLED",
}


def _rco_stamp_fill_price(r, bo: dict) -> None:
    """Write fill_price + filled_at from broker order dict onto AlgoOrder row.
    Uses average_price, falls back to price; silently skips on bad value.
    """
    from datetime import datetime, timezone
    try:
        ap = bo.get("average_price") or bo.get("price")
        if ap is not None:
            r.fill_price = float(ap)
        r.filled_at = datetime.now(timezone.utc)
    except (TypeError, ValueError):
        pass


def _rco_reconcile_one_row(r, by_id: dict, _attach_queue: list) -> tuple[int, int]:
    """Reconcile one AlgoOrder row against the broker snapshot dict.
    Returns (updated_delta, missing_delta). Appends to _attach_queue on FILLED.
    """
    bid = str(r.broker_order_id or "")
    if not bid:
        return 0, 0
    bo = by_id.get(bid)
    if bo is None:
        r.status = "UNFILLED"
        r.detail = (r.detail or "") + " [reconciled — broker no longer carries order_id]"
        return 0, 1
    kite_status = str(bo.get("status") or "").upper()
    new_status = _RECONCILE_KITE_TO_ALGO.get(kite_status)
    if new_status and r.status != new_status:
        r.status = new_status
        if new_status == "FILLED":
            _rco_stamp_fill_price(r, bo)
            _attach_queue.append(r)
        return 1, 0
    return 0, 0


async def _rco_reconcile_account(
    acct: str, acct_rows: list, _attach_queue: list,
) -> tuple[int, int]:
    """Reconcile one account's OPEN live rows against the broker order book.
    Returns (updated, missing) delta counts. Appends FILLED rows to
    _attach_queue for post-commit template-attach dispatch."""
    from backend.brokers import get_broker

    try:
        broker = get_broker(acct)
    except Exception as e:
        logger.warning(f"reconcile: get_broker({acct}) failed: {e}")
        return 0, 0
    try:
        broker_orders = await asyncio.to_thread(broker.orders)
    except Exception as e:
        logger.warning(f"reconcile: broker.orders() for {acct} failed: {e}")
        return 0, 0

    by_id = {str(o.get("order_id")): o for o in (broker_orders or [])}
    updated = 0
    missing = 0
    for r in acct_rows:
        upd, mis = _rco_reconcile_one_row(r, by_id, _attach_queue)
        updated += upd
        missing += mis
    return updated, missing


def _rco_parse_preflight_identity(body: dict) -> tuple:
    """Extract account / exchange / symbol / qty / order classification fields."""
    account       = str(body.get("account") or "").strip()
    exchange      = str(body.get("exchange") or "NFO").strip().upper()
    tradingsymbol = str(body.get("tradingsymbol") or "").strip().upper()
    quantity      = int(body.get("quantity") or 0)
    order_type    = str(body.get("order_type") or "LIMIT").strip().upper()
    product       = str(body.get("product") or "NRML").strip().upper()
    return account, exchange, tradingsymbol, quantity, order_type, product


def _rco_parse_preflight_scalars(body: dict) -> tuple:
    """Extract and coerce scalar order fields from the preflight JSON body.
    Returns (account, exchange, tradingsymbol, quantity, order_type,
    product, variety, side, price, trigger_price, intent).
    """
    account, exchange, tradingsymbol, quantity, order_type, product = _rco_parse_preflight_identity(body)
    variety       = str(body.get("variety") or "regular").strip().lower()
    side          = str(body.get("side") or body.get("transaction_type") or "BUY").strip().upper()
    price         = float(body.get("price") or 0)
    trigger_price = float(body.get("trigger_price") or 0)
    intent        = body.get("intent") or None
    return account, exchange, tradingsymbol, quantity, order_type, product, variety, side, price, trigger_price, intent


def _rco_parse_preflight_body(body: dict) -> tuple:
    """Parse and coerce the raw JSON body for `preflight_order`.
    Returns (account, exchange, tradingsymbol, quantity, order_type,
    product, variety, side, price, trigger_price, intent, paired_legs)."""
    scalars = _rco_parse_preflight_scalars(body)
    paired_legs = body.get("paired_legs") or []
    if not isinstance(paired_legs, list):
        paired_legs = []
    return scalars + (paired_legs,)


def _rco_validate_preflight_params(
    account: str, tradingsymbol: str, quantity: int, exchange: str, side: str,
) -> None:
    """Validate required fields for preflight_order. Raises HTTPException on failure."""
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


def _rco_reconcile_apply_target(r, bo: Optional[dict], kite_status: Optional[str], target: str) -> tuple[bool, str, bool]:
    """Apply a known target status onto an AlgoOrder row (both row and broker order present).
    Returns (updated, note, attach_after_commit).
    """
    from datetime import datetime, timezone

    if r.status != target:
        r.status = target
        if target == "FILLED" and bo and bo.get("average_price"):
            try:
                r.fill_price = float(bo["average_price"])
                r.filled_at = datetime.now(timezone.utc)
            except Exception:
                pass
            return True, f"broker status={kite_status} → {target}", True
        r.detail = (r.detail or "") + f" [reconciled → {target}]"
        return True, f"broker status={kite_status} → {target}", False
    return False, f"already {target}", False


def _rco_apply_reconcile_status(
    r,
    bo: Optional[dict],
    kite_status: Optional[str],
    target: Optional[str],
) -> tuple[bool, str, bool]:
    """Apply the broker status onto a single AlgoOrder row.

    Returns (updated, note, attach_after_commit).
    `attach_after_commit` is True when the row was just flipped to FILLED
    and has a template that should be fired post-commit.
    """
    if r is None:
        if bo is None:
            return False, "broker has no order (no algo row to update)", False
        return False, f"broker status={kite_status} (no algo row to update)", False

    if bo is None:
        if r.status == "OPEN":
            r.status = "UNFILLED"
            r.detail = (r.detail or "") + " [reconciled — broker no longer carries order_id]"
            return True, "broker no longer carries order_id", False
        return False, "broker has no order and algo row already terminal", False

    if target:
        return _rco_reconcile_apply_target(r, bo, kite_status, target)
    return False, f"broker status={kite_status} (no algo mapping)", False


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
        return [_chase_row_to_info(r, masked_acct, child_map) for r in rows]

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
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        # Snapshot paper engine's open-order ids ONCE before the DB
        # query so we don't pay for the lock on every row.
        _paper_open_ids = _chase_snapshot_paper_open_ids()

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
        _broker_status_by_id = await _chase_snapshot_broker_status_by_id()

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

            kept, _reconciled_filled, _needs_commit = _rco_reconcile_active_rows(
                rows, _paper_open_ids, _broker_status_by_id,
            )
            if _needs_commit:
                await s.commit()
                for _filled_row in _reconciled_filled:
                    _maybe_fire_template_attach_for_reconcile(_filled_row)
            child_map = await _fetch_child_order_ids(s, [r.id for r in kept])

        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [_chase_row_to_info(r, masked_acct, child_map) for r in kept]

    @post("/chases/{algo_order_id:int}/kill", guards=[admin_guard])
    async def kill_chase(self, algo_order_id: int, request: Request) -> dict:
        """Cancel an in-flight chase — best-effort across paper, live,
        and shadow modes. Admin-only.

        - Paper: delegate to engine.cancel_paper_order() which writes the
          canonical AlgoOrderEvent(kind='cancel') row and flips DB status
          via _safe_update_algo_order_cancel.
        - Live: call broker.cancel_order(variety, broker_order_id).
        - Shadow: just flip the row's status (nothing real to cancel).
        Sets AlgoOrder.status='CANCELLED' (or CANCEL_FAILED on error) and
        writes a 'killed' event so the timeline reflects the operator action.
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

            err_msg = await _rco_dispatch_kill(row)
            _rco_apply_kill_status(row, err_msg)
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

        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == algo_order_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="order not found")
            _precheck = _retry_precheck_row(row)
            if _precheck is not None:
                return _precheck

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

            result = await _rco_run_template_attach(row)
            if result is None:
                return {"ok": False, "reason": "apply_template_to_order returned no result (template missing or empty plan)"}
            if _rco_persist_attach_payload(row, result, s):
                await s.commit()
                await s.refresh(row)

        # Sc.5c — distinguish full success from silent no-op. When the
        # plan resolved but produced no GTTs and no wing (all branches
        # rejected by overrides or chain-scan), the response previously
        # claimed `ok: true, attached: false` which read like a success.
        # Treat the empty-payload case as a failure with a clear reason.
        return _retry_build_result_response(
            result, row.attached_gtts_json is not None,
        )

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
            # commit so the attach pipeline reads the committed FILLED
            # state rather than the in-memory pre-commit mutation.
            _attach_queue: list = []

            for acct, acct_rows in by_acct.items():
                _upd, _miss = await _rco_reconcile_account(
                    acct, acct_rows, _attach_queue,
                )
                updated += _upd
                missing += _miss

            await s.commit()
            if updated or missing:
                invalidate("orders")
            # Fire template attach AFTER commit so the new session reads
            # the committed FILLED state.
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

        acct = (data.account or "").strip()
        if not acct:
            raise HTTPException(status_code=400, detail="account required")

        bo, kite_status, target = await _rco_fetch_broker_order(acct, broker_order_id)

        async with async_session() as s:
            r = (await s.execute(
                _sql_select(AlgoOrder).where(
                    AlgoOrder.broker_order_id == str(broker_order_id),
                )
            )).scalars().first()

            updated, note, _attach_after_commit = _rco_apply_reconcile_status(
                r, bo, kite_status, target,
            )

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
        from backend.api.algo.actions import run_preflight

        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: preflight requires an authenticated session.")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        (account, exchange, tradingsymbol, quantity, order_type,
         product, variety, side, price, trigger_price, intent,
         paired_legs) = _rco_parse_preflight_body(body)

        _rco_validate_preflight_params(account, tradingsymbol, quantity, exchange, side)

        result = await run_preflight(account, {
            "exchange":         exchange,
            "tradingsymbol":    tradingsymbol,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          product,
            "variety":          variety,
            "transaction_type": side,
            "intent":           intent,
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

        # v2 API (2026-07-08): `data.quantity` is LOTS for F&O and shares
        # for equity. Convert to contracts here so the template resolver
        # sizes exit GTTs against the same unit convention it always has.
        _sym = (data.tradingsymbol or "").upper()
        _exch = (data.exchange or "NFO")
        _parent_qty = await _rco_preview_resolve_qty(_exch, _sym, int(data.quantity or 0))

        result = await apply_template_to_order(
            template_id=data.template_id,
            template_slug=None,
            overrides=_ticket_overrides_dict(data),
            parent_account=(data.account or ""),
            parent_symbol=_sym,
            parent_side=(data.side or "").upper(),
            parent_qty=_parent_qty,
            parent_exchange=_exch,
            parent_fill_price=float(data.reference_price or 0.0),
            parent_product=(data.product or "NRML"),
            apply_path="preview",
        )
        if result is None:
            return TicketPreviewResponse(plan=_rco_preview_empty_plan(data))
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

        order_id, account, kite_status, kite_symbol, txn, qty, price, kite_exchange, status_msg = (
            _rco_parse_dhan_postback_body(body)
        )
        # M9: empty account means Dhan didn't send dhanClientId — we can't
        # route the postback to an AlgoOrder. Log CRITICAL for visibility
        # but return 200 OK so Dhan doesn't retry indefinitely.
        if not account:
            logger.critical(
                "[DHAN-POSTBACK] missing account (dhanClientId) in payload "
                "for order_id=%s status=%s sym=%s — postback cannot be "
                "attributed; raw payload logged above.",
                order_id, kite_status, kite_symbol,
            )
            return {"status": "ok"}
        await _process_broker_postback(
            broker_id="dhan",
            order_id=order_id,
            status=kite_status,
            account=account,
            symbol=kite_symbol,
            txn=txn,
            qty=qty,
            price=price,
            exchange=kite_exchange,
            status_message=status_msg,
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
