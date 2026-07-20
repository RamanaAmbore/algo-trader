"""
orders_postback.py — Shared broker-postback processing pipeline.

Extracted from orders.py (4322 LOC → split) as Commit 2 of the RED-zone split.

Exports:
  _process_broker_postback   — shared fan-out used by Dhan + Groww handlers.
  kite_postback_handler      — full Kite postback logic (HMAC + row-sync + fan-out).

`_postback_broadcast_fanout` intentionally stays in orders.py because:
  - test_postback_fanout_ssot.py reads orders.py source directly and asserts
    that `def _postback_broadcast_fanout(` is defined there.
  - test_paper_fanout.py asserts paper.py imports from backend.api.routes.orders.

This module imports `_postback_broadcast_fanout` lazily (inside the function
body) to avoid the circular import that would arise from a module-level import
of orders.py here while orders.py imports from this module.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)


_BROKER_STATUS_MAP = {
    "COMPLETE":  "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED":  "REJECTED",
    "EXPIRED":   "UNFILLED",
}

# Audit category mapping — pre-fix EXPIRED + unknown statuses fell
# through to "order.fill" which mislabelled them in /admin/audit.
_STATUS_AUDIT_CATEGORY: dict[str, str] = {
    "COMPLETE":  "order.fill",
    "CANCELLED": "order.cancel",
    "REJECTED":  "order.reject",
    "EXPIRED":   "order.expired",
}


def _pb_audit_category(status: str) -> str:
    """Map a broker status string to its audit log category."""
    return _STATUS_AUDIT_CATEGORY.get(str(status or "").upper(), "order")


async def _create_postback_orphan_row(
    s,
    *,
    broker_id: str,
    order_id: str,
    account: str,
    symbol: str,
    txn: str,
    qty,
    price,
    new_status: str | None,
    AlgoOrder_cls,
) -> None:
    """Create an orphan AlgoOrder row when no direct or fuzzy-match succeeds.

    Ensures postback events are never silently dropped. Logs a warning before
    attempting the insert; logs success or a warning on failure.
    """
    try:
        _orphan = AlgoOrder_cls(
            account=account or broker_id,
            symbol=symbol or "UNKNOWN",
            exchange="",
            transaction_type=(txn or "BUY").upper(),
            quantity=int(qty or 0),
            broker_order_id=str(order_id),
            status=(new_status or "OPEN"),
            engine="live",
            mode="live",
            detail=(
                f"orphan postback from {broker_id}: {new_status or 'UNKNOWN'} "
                + (f"@{price}" if price else "")
            )[:500],
        )
        if new_status == "FILLED" and price:
            try:
                _orphan.fill_price = float(price)
            except (TypeError, ValueError):
                pass
            _orphan.filled_at = datetime.now(timezone.utc)
        s.add(_orphan)
        await s.commit()
        logger.info(
            "[%s-POSTBACK] orphan AlgoOrder #%s created for %s.",
            broker_id.upper(), _orphan.id, order_id,
        )
    except Exception as _orp_err:
        logger.warning(
            "[%s-POSTBACK] orphan creation failed for %s: %s",
            broker_id.upper(), order_id, _orp_err,
        )


def _sync_apply_row_status(
    _r, *, new_status: str, price, broker_id: str, status: str, status_message: str,
) -> bool:
    """Update a single AlgoOrder row in-place; return True if the row transitioned to FILLED."""
    if not new_status or _r.status == new_status:
        return False
    _r.status = new_status
    if new_status == "FILLED":
        try:
            _r.fill_price = float(price) if price else _r.fill_price
        except (TypeError, ValueError):
            pass
        _r.filled_at = datetime.now(timezone.utc)
    _r.detail = (
        (_r.detail or "")[:200]
        + f" · {broker_id} postback {status}"
        + (f": {status_message}" if status_message else "")
    )
    return new_status == "FILLED"


async def _sync_algo_order_rows(
    *,
    broker_id: str,
    order_id: str,
    status: str,
    price,
    status_message: str,
    qty,
    account: str = "",
    symbol: str = "",
    txn: str = "",
) -> list:
    """Update AlgoOrder rows for broker_order_id, write order events, return filled rows.

    Handles the DB session block of _process_broker_postback: status update,
    fill_price + filled_at assignment, detail annotation, commit, and
    order-event writes. Returns a list of rows that transitioned to FILLED
    so the caller can fire template-attach for each.

    M2(a): When no rows are found by broker_order_id, attempts a fuzzy-match
    fallback (same logic as the Kite path) using account/symbol/txn/qty to
    bind the postback to an OPEN row created in the last 60 s.

    M2(b): Logs CRITICAL on fuzzy-match success so the race is auditable.

    M2(c): Creates an orphan AlgoOrder when both direct and fallback lookups
    produce zero matches so the postback is never silently dropped.
    """
    from sqlalchemy import select as _sql_select
    from backend.api.database import async_session as _async_s
    from backend.api.models import AlgoOrder as _AO
    from backend.api.algo.order_events import write_event as _write_event

    _new_status = _BROKER_STATUS_MAP.get(status)
    _filled_rows: list = []

    async with _async_s() as _s:
        _rows = (await _s.execute(
            _sql_select(_AO).where(_AO.broker_order_id == str(order_id))
        )).scalars().all()

        # M2(a): fallback lookup when no direct broker_order_id match.
        if not _rows and symbol and txn:
            _fallback = await _pb_fallback_lookup_row(
                _s,
                order_id=order_id,
                tradingsymbol=symbol,
                txn=txn,
                qty=qty,
                account=account,
            )
            if _fallback is not None:
                # M2(b): CRITICAL so the race condition is surfaced in logs.
                logger.critical(
                    "[%s-POSTBACK] fuzzy-matched broker_order_id=%s to "
                    "AlgoOrder #%s via account/symbol/side (race: "
                    "postback arrived before seed). acct=%s sym=%s",
                    broker_id.upper(), order_id, _fallback.id, account, symbol,
                )
                _rows = [_fallback]

        # M2(c): create an orphan row so the postback is never silently dropped.
        if not _rows:
            logger.warning(
                "[%s-POSTBACK] no AlgoOrder match for broker_order_id=%s "
                "(acct=%s sym=%s txn=%s) — creating orphan row.",
                broker_id.upper(), order_id, account, symbol, txn,
            )
            await _create_postback_orphan_row(
                _s, broker_id=broker_id, order_id=order_id,
                account=account, symbol=symbol, txn=txn, qty=qty,
                price=price, new_status=_new_status, AlgoOrder_cls=_AO,
            )
            return _filled_rows

        for _r in _rows:
            if _sync_apply_row_status(
                _r, new_status=_new_status, price=price,
                broker_id=broker_id, status=status, status_message=status_message,
            ):
                _filled_rows.append(_r)
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

    return _filled_rows


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
    masked = mask_account(account)

    logger.info(
        f"{broker_id} postback: {order_id} [{masked}] {status} {txn} "
        f"{qty} {symbol} price={price} msg={status_message}"
    )

    _terminal = status in ("COMPLETE", "CANCELLED", "REJECTED", "EXPIRED")

    # Sync AlgoOrder row + record event.
    try:
        _filled_rows = await _sync_algo_order_rows(
            broker_id=broker_id, order_id=order_id, status=status,
            price=price, status_message=status_message, qty=qty,
            account=account, symbol=symbol, txn=txn,
        )
        for _r in _filled_rows:
            try:
                from backend.api.routes.orders_place import (
                    _maybe_fire_template_attach_for_reconcile,
                )
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
        write_audit_event(
            category=_pb_audit_category(status),
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

    # Cache invalidation + WS broadcasts — delegated to the shared
    # `_postback_broadcast_fanout` helper. Imported lazily to avoid a
    # circular import at module load time (orders.py imports this
    # module; this module would circularly depend on orders.py at the
    # module level if we imported at the top).
    from backend.api.routes.orders import _postback_broadcast_fanout  # noqa: PLC0415
    _postback_broadcast_fanout(
        status=status, order_id=order_id, account=account, masked=masked,
        symbol=symbol, txn=txn, qty=qty, price=price,
        exchange=exchange, status_message=status_message,
    )


# ── Kite postback handler ─────────────────────────────────────────────────────

_KITE_STATUS_MAP = {
    "COMPLETE":  "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED":  "REJECTED",
    "EXPIRED":   "UNFILLED",
}


async def _pb_fallback_lookup_row(
    _s, *, order_id: str, tradingsymbol: str, txn: str, qty, account: str
):
    """Broker webhook may fire before we recorded broker_order_id.

    Attempt an account/symbol/side/qty match on rows created in the
    last 60s that are still OPEN with no broker_order_id yet. Sets
    broker_order_id on the match so subsequent postbacks resolve
    directly.
    """
    from sqlalchemy import select as _sql_select
    from datetime import datetime, timezone, timedelta
    from backend.api.models import AlgoOrder as _AlgoOrder

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
        logger.info(
            f"postback fallback matched row #{_fallback.id} "
            f"to broker_order_id={order_id} via account/symbol/side"
        )
    return _fallback


def _pb_apply_status_to_row(_r, *, new_status: str | None, price) -> bool:
    """Mutate an AlgoOrder row per postback status.

    Returns True iff the row transitioned to FILLED (caller uses the
    list to drive ledger / take-profit / template-attach fan-outs).
    """
    if not new_status or _r.status == new_status:
        return False
    _r.status = new_status
    if new_status != "FILLED" or not price:
        return False
    try:
        _r.fill_price = float(price)
    except (TypeError, ValueError):
        pass
    if _r.created_at:
        from datetime import datetime, timezone
        _r.filled_at = datetime.now(timezone.utc)
    return True


def _pb_ledger_fill_args(_r) -> dict:
    """Extract record_fill kwargs from a filled AlgoOrder row."""
    return {
        "strategy_id": _r.strategy_id,
        "algo_order_id": _r.id,
        "account": str(_r.account) if _r.account else "",
        "symbol": str(_r.symbol) if _r.symbol else "",
        "exchange": str(_r.exchange) if _r.exchange else "NFO",
        "side_kite": str(_r.transaction_type) if _r.transaction_type else "BUY",
        "qty": int(_r.quantity) if _r.quantity else 0,
        "fill_price": float(_r.fill_price) if _r.fill_price else 0.0,
    }


async def _pb_ledger_fill_row(_s, _r, record_fn) -> None:
    """Record a single FIFO ledger fill for one filled row; skip if guard fails."""
    if not (_r.strategy_id and _r.fill_price and _r.quantity > 0):
        return
    try:
        await record_fn(_s, **_pb_ledger_fill_args(_r))
    except Exception as _le:
        logger.warning(
            f"postback lot_ledger write failed for "
            f"order_id={_r.id} strategy={_r.strategy_id}: {_le}"
        )


async def _pb_write_ledger_fills(_s, filled_rows: list) -> None:
    """Record FIFO ledger entries for every FILLED row with a strategy."""
    from backend.api.algo.lot_ledger import record_fill as _record_ledger_fill
    for _r in filled_rows:
        await _pb_ledger_fill_row(_s, _r, _record_ledger_fill)


async def _pb_write_timeline_events(
    rows: list, *,
    order_id: str, tradingsymbol: str, txn: str, qty, price,
    status: str, status_msg: str, new_status: str | None,
) -> None:
    """One timeline event per row so the operator UI sees the postback."""
    from backend.api.algo.order_events import write_event as _write_event

    payload = {
        "broker_order_id": order_id,
        "status": status,
        "new_algo_status": new_status,
        "tradingsymbol": tradingsymbol,
        "transaction_type": txn,
        "quantity": qty,
        "price": price,
        "status_message": status_msg,
    }
    for _r in rows:
        await _write_event(
            _r.id, "postback",
            f"Kite postback: {status} {txn} {qty} {tradingsymbol} @{price}",
            payload=payload,
        )


def _pb_wants_take_profit_arm(_r) -> bool:
    """Parent fill with a TP target and no template attached wants
    the take-profit arm dispatched.
    """
    return bool(
        (_r.target_pct or _r.target_abs)
        and _r.parent_order_id is None
        and _r.template_id is None
        and _r.fill_price
    )


def _pb_wants_template_attach(_r) -> bool:
    """Live parent fill with a template_id wants the template attach
    fan-out dispatched.
    """
    return bool(
        _r.template_id
        and _r.parent_order_id is None
        and _r.mode == "live"
        and _r.fill_price
    )


def _pb_dispatch_take_profit_arm(_r) -> None:
    from backend.api.routes.orders_place import _arm_take_profit
    asyncio.create_task(_arm_take_profit(
        parent_row_id=_r.id,
        parent_account=str(_r.account or ""),
        parent_symbol=str(_r.symbol or ""),
        parent_exchange=str(_r.exchange or "NFO"),
        parent_side=str(_r.transaction_type or "BUY"),
        fill_price=float(_r.fill_price),
        target_pct=float(_r.target_pct or 0.0),
        target_abs=(_r.target_abs and float(_r.target_abs)),
        parent_mode=str(_r.mode or "live"),
    ))


def _pb_dispatch_template_attach(_r) -> None:
    from backend.api.routes.orders_place import _fire_template_attach_on_fill

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


def _pb_dispatch_take_profit_and_template(filled_rows: list) -> None:
    """Arm TP on parent fills without a template, else fire template
    attach. Both fan-outs run as detached tasks so postback ack isn't
    blocked.
    """
    for _r in filled_rows:
        if _pb_wants_take_profit_arm(_r):
            _pb_dispatch_take_profit_arm(_r)

    for _r in filled_rows:
        if _pb_wants_template_attach(_r):
            _pb_dispatch_template_attach(_r)


async def _pb_event_kite(
    *,
    order_id: str,
    tradingsymbol: str,
    txn: str,
    qty,
    price,
    status: str,
    status_msg: str,
    account: str,
) -> None:
    """Timeline + row-sync for the Kite postback.

    Runs inside an asyncio.create_task so failures log + drop
    without blocking the postback acknowledgement.
    """
    from sqlalchemy import select as _sql_select
    from backend.api.database import async_session as _async_session
    from backend.api.models import AlgoOrder as _AlgoOrder

    _new_status = _KITE_STATUS_MAP.get(str(status or "").upper())

    try:
        _filled_rows: list = []
        async with _async_session() as _s:
            _rows = (await _s.execute(
                _sql_select(_AlgoOrder).where(
                    _AlgoOrder.broker_order_id == str(order_id)
                )
            )).scalars().all()

            if not _rows:
                _fallback = await _pb_fallback_lookup_row(
                    _s,
                    order_id=order_id, tradingsymbol=tradingsymbol,
                    txn=txn, qty=qty, account=account,
                )
                if _fallback is not None:
                    _rows = [_fallback]

            for _r in _rows:
                if _pb_apply_status_to_row(_r, new_status=_new_status, price=price):
                    _filled_rows.append(_r)
            await _s.commit()

            await _pb_write_ledger_fills(_s, _filled_rows)
            await _s.commit()

        await _pb_write_timeline_events(
            _rows,
            order_id=order_id, tradingsymbol=tradingsymbol, txn=txn,
            qty=qty, price=price, status=status, status_msg=status_msg,
            new_status=_new_status,
        )
        _pb_dispatch_take_profit_and_template(_filled_rows)
    except Exception as _pe:
        logger.debug(f"postback event write failed: {_pe}")


def _pb_ordered_candidates(account: str, kite_candidates: list[str]) -> list[str]:
    """Return candidates list with the asserted account promoted to front."""
    if account and account in kite_candidates:
        return [account] + [a for a in kite_candidates if a != account]
    return list(kite_candidates)


def _pb_verify_via_conn_svc(
    order_id: str, order_timestamp: str, checksum: str, account: str,
) -> bool:
    """Signature verification path when RAMBOQ_USE_CONN_SERVICE=1."""
    from backend.brokers.client.remote_broker import list_remote_accounts, verify_postback
    kite_candidates = [
        r["account"] for r in list_remote_accounts()
        if r.get("broker_id") in ("zerodha_kite", "kite")
    ]
    for acct in _pb_ordered_candidates(account, kite_candidates):
        if verify_postback(acct, order_id=order_id,
                           order_timestamp=order_timestamp, checksum=checksum):
            return True
    return False


def _pb_verify_direct(
    order_id: str, order_timestamp: str, checksum: str, account: str,
) -> bool:
    """Signature verification path for direct-Connections (no conn_service)."""
    import hashlib
    import hmac as _hmac
    from backend.brokers.connections import Connections, KiteConnection
    conns = Connections()
    kite_candidates: list[str] = [
        a for a, c in conns.conn.items() if isinstance(c, KiteConnection)
    ]
    for acct in _pb_ordered_candidates(account, kite_candidates):
        api_secret = conns.conn[acct].api_secret
        msg = (str(order_id) + str(order_timestamp) + api_secret).encode()
        expected = hashlib.sha256(msg).hexdigest()
        if _hmac.compare_digest(expected, str(checksum)):
            return True
    return False


async def _pb_verify_signature(
    order_id: str,
    order_timestamp: str,
    checksum: str,
    account: str,
) -> bool:
    """Verify the Kite postback HMAC signature.

    Handles both the conn_service path (delegates to the remote
    ``verify_postback`` RPC) and the direct-Connections path
    (local SHA-256 / hmac comparison). Returns ``True`` if any
    candidate account produces a matching signature, ``False``
    otherwise. Never raises.
    """
    import os as _os
    _use_conn_svc = _os.environ.get(
        "RAMBOQ_USE_CONN_SERVICE", "",
    ).strip().lower() in ("1", "true", "yes", "on")

    if _use_conn_svc:
        return _pb_verify_via_conn_svc(order_id, order_timestamp, checksum, account)
    return _pb_verify_direct(order_id, order_timestamp, checksum, account)


def _pb_write_audit(
    status: str,
    txn: str,
    qty,
    tradingsymbol: str,
    price,
    order_id: str,
    masked: str,
    status_msg: str,
) -> None:
    """Write the audit-log entry for a Kite postback event.

    Best-effort — logs and swallows on failure so the postback
    acknowledgement is never blocked by an audit write error.
    """
    try:
        from backend.api.audit import write_audit_event
        _status_u = str(status or "").upper()
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


async def kite_postback_handler(request) -> dict:
    """Full Kite postback logic — HMAC verification + row sync + broadcast.

    Extracted from OrdersController.order_postback. Delegates to
    _pb_event_kite for async timeline work, then calls _postback_broadcast_fanout
    (lazily imported from orders.py to avoid circular import).
    """
    from litestar.exceptions import HTTPException
    from backend.shared.helpers.utils import mask_account

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

        sig_valid = await _pb_verify_signature(order_id, order_timestamp, checksum, account)
        if not sig_valid:
            logger.warning(
                "postback signature mismatch",
                extra={"order_id": order_id},
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid postback signature.",
            )

        logger.info(f"Postback: {order_id} [{masked}] {status} {txn} {qty} "
                    f"{tradingsymbol} price={price} msg={status_msg}")

        _pb_write_audit(status, txn, qty, tradingsymbol, price, order_id, masked, status_msg)

        try:
            asyncio.create_task(_pb_event_kite(
                order_id=order_id,
                tradingsymbol=tradingsymbol,
                txn=txn,
                qty=qty,
                price=price,
                status=status,
                status_msg=status_msg,
                account=account,
            ))
        except Exception:
            pass

        from backend.api.routes.orders import _postback_broadcast_fanout  # noqa: PLC0415
        _postback_broadcast_fanout(
            status=status, order_id=order_id, account=account,
            masked=masked, symbol=tradingsymbol, txn=txn,
            qty=qty, price=price,
            exchange=body.get("exchange", ""),
            status_message=status_msg,
        )

        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Postback error: {e}")
        return {"status": "error", "detail": str(e)}
