"""
Live-broker (mode-3) action handlers for agent actions.

Extracted from actions.py. `_write_live_order` is defined in actions.py
(not here) so that test patches on `backend.api.algo.actions._write_live_order`
intercept calls made by `_action_live_close_position` and
`_action_live_chase_close_positions`. Those functions import it lazily
from actions.py at call time.

`run_preflight` and `diagnose_live_failure` are imported from
actions_preflight.py at module top (no cycle).
"""

from backend.shared.helpers.ramboq_logger import get_logger
from backend.api.algo.actions_preflight import run_preflight, diagnose_live_failure

logger = get_logger(__name__)


async def _action_chase_close(context: dict, params: dict):
    """Close positions using the adaptive chase engine."""
    from backend.api.algo.expiry import ExpiryEngine

    engine = ExpiryEngine()
    to_close = engine.scan_positions()
    if to_close:
        await engine.close_positions(to_close)


async def _action_send_summary(context: dict, params: dict):
    """Send portfolio summary via existing send_summary."""
    from backend.shared.helpers.alert_utils import send_summary
    import asyncio

    segments = params.get("segments", ["equity", "commodity"])
    summary_type = params.get("summary_type", "open")

    sum_holdings = context.get("sum_holdings")
    sum_positions = context.get("sum_positions")
    df_margins = context.get("df_margins")
    ist_display = context.get("ist_display", "")

    if sum_holdings is None:
        return

    for seg_name in segments:
        label = seg_name.capitalize()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: send_summary(sum_holdings, sum_positions, ist_display,
                                 summary_type, label=label, df_margins=df_margins),
        )


async def _fetch_ltp(
    broker,
    exchange: str,
    symbol: str,
    loop,
    context: str = "ltp_fetch",
) -> "float | None":
    """Fetch LTP for (exchange, symbol) from a broker. Returns None on failure.

    Wraps the sync ``broker.ltp(...)`` call in ``loop.run_in_executor`` and
    defensively returns None on any exception (broker session down, symbol not
    found, etc.).

    Args:
        broker:   A broker adapter (Kite / Dhan / Groww).
        exchange: Exchange string, e.g. ``"NFO"``.
        symbol:   Trading symbol string.
        loop:     Running asyncio event loop (from ``asyncio.get_running_loop()``).
        context:  Short label that appears in the warning log on failure, so
                  callers can distinguish ``'place_order'`` from ``'close_position'``.
    """
    key = f"{exchange}:{symbol}"
    try:
        ltp_data = await loop.run_in_executor(None, broker.ltp, [key])
        return float((ltp_data.get(key) or {}).get("last_price") or 0) or None
    except Exception as e:
        logger.warning(
            f"[LIVE] {context} LTP fetch failed, proceeding with None price: {e}"
        )
        return None


async def _place_order_preflight_block(
    pf: dict, agent_shim, context: dict,
    account: str, symbol: str, exchange: str, side: str, qty: int, price,
) -> None:
    """Handle a preflight-blocked place_order: write REJECTED row, fire alert."""
    from backend.api.algo.actions import _write_live_order

    reasons = "; ".join(b["reason"] for b in pf["blocked"])
    logger.warning(f"[LIVE] place_order BLOCKED for {account} {exchange}/{symbol} "
                   f"{side} {qty}: {reasons}")
    fake_order_id = await _write_live_order(
        agent_shim, "place_order",
        {"account": account, "symbol": symbol, "exchange": exchange,
         "side": side, "qty": qty, "price": price},
        status="REJECTED",
        detail_suffix=f"PREFLIGHT BLOCKED: {reasons[:200]}",
    )
    try:
        if fake_order_id:
            from backend.api.algo.order_events import write_event as _write_ev
            import asyncio as _aio
            _aio.create_task(_write_ev(
                fake_order_id, "preflight_block",
                f"Preflight blocked: {reasons[:300]}",
                payload={"blocked": pf["blocked"], "diagnostics": pf["diagnostics"]},
            ))
    except Exception:
        pass
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{context.get('agent_slug', 'place_order')}",
            error=f"preflight blocked: {reasons[:200]}",
        )
    except Exception as _e:
        logger.warning(f"Preflight-block notification failed: {_e}")


async def _place_order_write_intent(agent_shim, pf: dict,
                                    account: str, symbol: str, exchange: str,
                                    side: str, qty: int, price) -> None:
    """Write OPEN AlgoOrder row and fire preflight_ok event (best-effort)."""
    from backend.api.algo.actions import _write_live_order

    try:
        intent_id = await _write_live_order(
            agent_shim, "place_order",
            {"account": account, "symbol": symbol, "exchange": exchange,
             "side": side, "qty": qty, "price": price},
            status="OPEN",
        )
        if intent_id:
            from backend.api.algo.order_events import write_event as _write_ev_ok
            import asyncio as _aio
            _aio.create_task(_write_ev_ok(
                intent_id, "preflight_ok",
                "Preflight passed",
                payload={"diagnostics": pf["diagnostics"]},
            ))
    except Exception:
        pass


async def _place_order_on_failure(
    e: Exception, context: dict,
    account: str, symbol: str, exchange: str,
    side: str, qty: int, price, product: str,
) -> None:
    """Diagnose and alert on a place_order chase failure."""
    from backend.brokers import get_broker

    diag_order = {
        "exchange": exchange, "symbol": symbol, "side": side, "qty": qty,
        "order_type": "LIMIT", "product": product,
        "price": price or 0, "variety": "regular",
    }
    try:
        broker = get_broker(account)
        diag = await diagnose_live_failure(broker, diag_order, str(e))
    except Exception:
        diag = "diagnosis unavailable"
    logger.error(f"[LIVE] place_order failed for {account} {exchange}/{symbol} "
                 f"{side} {qty}: {e} | diag: {diag}")
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{context.get('agent_slug', 'place_order')}",
            error=f"{e} | {diag}",
        )
    except Exception:
        pass


def _al_place_resolve_params(
    context: dict, params: dict
) -> "tuple[object, str, str, str, str, int, object, str]":
    """Resolve _action_place_order params and build the _AgentShim sentinel.

    Returns (shim, account, symbol, exchange, side, qty, price, product).
    """
    class _AgentShim:
        slug = context.get("agent_slug", "place_order")

    return (
        _AgentShim(),
        str(params.get("account") or ""),
        str(params.get("symbol") or ""),
        str(params.get("exchange") or "NFO"),
        str(params.get("transaction_type") or params.get("side") or "SELL"),
        int(params.get("quantity") or 0),
        params.get("price"),
        str(params.get("product") or "NRML"),
    )


async def _action_place_order(context: dict, params: dict):
    """
    Place an order using the chase engine (live mode).

    Mirrors the pattern in `_action_live_close_position`:
      1. Resolve params.
      2. Persist AlgoOrder(mode='live', status='OPEN') BEFORE the broker call
         so the order row exists even if the service dies mid-chase.
      3. Call chase_order(); on failure run basket_margin diagnosis and re-raise
         so execute() writes an action_failed event.
    """
    import asyncio
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.brokers import get_broker

    _shim, account, symbol, exchange, side, qty, price, product = (
        _al_place_resolve_params(context, params)
    )

    # Fetch LTP as the initial limit price (best-effort).
    if price is None:
        try:
            broker = get_broker(account)
            loop = asyncio.get_running_loop()
            price = await _fetch_ltp(broker, exchange, symbol, loop, context="place_order")
        except Exception as ltp_e:
            logger.warning(f"[LIVE] place_order LTP fetch failed, proceeding with None price: {ltp_e}")

    # ── Preflight ─────────────────────────────────────────────────────────
    # Run before persisting the intent row so a blocked order never
    # creates an OPEN row that the chase loop would try to re-quote.
    pf = await run_preflight(account, {
        "exchange": exchange, "tradingsymbol": symbol, "side": side,
        "quantity": qty, "order_type": "LIMIT", "product": product,
        "price": price or 0, "variety": "regular",
    })
    if not pf["ok"]:
        await _place_order_preflight_block(
            pf, _shim, context, account, symbol, exchange, side, qty, price,
        )
        return  # abort without placing

    # Emit preflight_ok event (fire-and-forget).
    await _place_order_write_intent(_shim, pf, account, symbol, exchange, side, qty, price)

    cfg = ChaseConfig(exchange=exchange, product=product)
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
        )
    except Exception as e:
        await _place_order_on_failure(e, context, account, symbol, exchange, side, qty, price, product)
        raise


async def _close_position_preflight_block(
    pf: dict, agent,
    account: str, symbol: str, exchange: str, side: str, qty: int, price,
) -> None:
    """Handle a preflight-blocked close_position: write REJECTED row, fire alert."""
    from backend.api.algo.actions import _write_live_order

    reasons = "; ".join(b["reason"] for b in pf["blocked"])
    codes   = ", ".join(b["code"] for b in pf["blocked"])
    logger.error(
        f"[LIVE] close_position BLOCKED for {account} {exchange}/{symbol} "
        f"{side} {qty}: [{codes}] {reasons}"
    )
    await _write_live_order(
        agent, "close_position",
        {"account": account, "symbol": symbol, "exchange": exchange,
         "side": side, "qty": qty, "price": price},
        status="REJECTED",
        detail_suffix=f"PREFLIGHT BLOCKED: {reasons[:200]}",
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent, 'slug', 'close_position')}",
            error=f"PREFLIGHT BLOCKED [{codes}]: {reasons}",
        )
    except Exception:
        pass


async def _close_position_on_failure(
    e: Exception, agent, broker,
    account: str, symbol: str, exchange: str,
    side: str, qty: int, price, product: str,
) -> None:
    """Diagnose and alert on a close_position chase failure."""
    diag_order = {
        "exchange": exchange, "symbol": symbol, "side": side, "qty": qty,
        "order_type": "LIMIT", "product": product,
        "price": price or 0, "variety": "regular",
    }
    try:
        if broker is not None:
            diag = await diagnose_live_failure(broker, diag_order, str(e))
        else:
            diag = "broker resolve failed — no diagnosis available"
    except Exception:
        diag = "diagnosis unavailable"
    logger.error(f"[LIVE] close_position failed for {account} {exchange}/{symbol} "
                 f"{side} {qty}: {e} | diag: {diag}")
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent, 'slug', 'close_position')}",
            error=f"{e} | {diag}",
        )
    except Exception:
        pass


def _al_close_resolve_params(
    params: dict,
) -> "tuple[str, str, str, int, str, str]":
    """Extract and validate close_position params from the action dict.

    Returns (account, symbol, exchange, qty, side, product).
    Raises ValueError when required fields are absent.
    """
    account  = str(params.get("account") or "")
    symbol   = str(params.get("symbol") or params.get("tradingsymbol") or "")
    exchange = str(params.get("exchange") or "NFO")
    qty      = int(params.get("quantity") or params.get("qty") or 0)
    side     = (params.get("side") or params.get("transaction_type") or "SELL").upper()
    product  = str(params.get("product") or "NRML")
    if not account or not symbol or qty <= 0:
        raise ValueError(
            f"close_position: missing required params (account={account!r}, "
            f"symbol={symbol!r}, qty={qty})"
        )
    return account, symbol, exchange, qty, side, product


async def _al_close_fetch_broker_ltp(
    account: str, exchange: str, symbol: str
) -> "tuple[object | None, float | None]":
    """Resolve broker and fetch LTP for a close_position call.

    Returns (broker, price); both may be None on failure — caller proceeds
    with None price and lets the chase engine re-quote on first attempt.
    """
    import asyncio
    from backend.brokers import get_broker

    broker = None
    price  = None
    try:
        broker = get_broker(account)
        loop   = asyncio.get_running_loop()
        price  = await _fetch_ltp(broker, exchange, symbol, loop,
                                  context="close_position")
    except Exception as e:
        logger.warning(
            f"[LIVE] close_position LTP fetch failed, proceeding with None price: {e}"
        )
    return broker, price


async def _action_live_close_position(agent, context: dict, params: dict):
    """
    Close a single position via the adaptive chase engine.

    Resolves account / symbol / qty / side from params, fetches LTP from
    the broker as the initial limit price, then delegates to chase_order()
    which handles cancel-and-re-place until filled or attempt-cap.

    An AlgoOrder(mode='live') row is written before the first placement.
    The chase engine drives the actual Kite calls; any placement or fill
    event is logged by chase.py itself.
    """
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.api.algo.actions import _write_live_order

    account, symbol, exchange, qty, side, product = _al_close_resolve_params(params)
    broker, price = await _al_close_fetch_broker_ltp(account, exchange, symbol)

    # ── Preflight (G1 lot-multiple; G2 5-lot cap bypassed for closes) ────
    pf = await run_preflight(account, {
        "exchange": exchange, "tradingsymbol": symbol,
        "quantity": qty, "order_type": "LIMIT",
        "product": product,
        "price": price or 0, "variety": "regular",
        "intent": "close",   # signals G2 bypass inside run_preflight
    })
    if not pf["ok"]:
        await _close_position_preflight_block(
            pf, agent, account, symbol, exchange, side, qty, price,
        )
        return  # abort — do not reach chase_order

    # Persist the intent row before touching the broker.
    await _write_live_order(agent, "close_position", {
        "account": account, "symbol": symbol, "exchange": exchange,
        "side": side, "qty": qty, "price": price,
    }, status="OPEN")

    cfg = ChaseConfig(exchange=exchange, product=product, intent="close")
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
        )
    except Exception as e:
        await _close_position_on_failure(
            e, agent, broker, account, symbol, exchange, side, qty, price, product,
        )
        raise


def _al_modify_build_kwargs(params: dict) -> dict:
    """Build the kwargs dict for modify_order from action params.

    Iterates recognised field names and includes only values that are not None.
    """
    kwargs: dict = {}
    for field in ("quantity", "price", "trigger_price", "order_type", "validity"):
        v = params.get(field)
        if v is not None:
            kwargs[field] = v
    return kwargs


async def _al_modify_fetch_exchange(order_id: str) -> "str | None":
    """Fetch the exchange stored on the AlgoOrder row for a given broker_order_id.

    Returns None when the row does not exist or the DB call fails.
    """
    try:
        from sqlalchemy import select as _select
        from backend.api.database import async_session as _as
        from backend.api.models import AlgoOrder as _AO
        async with _as() as _s:
            _row = (await _s.execute(
                _select(_AO).where(_AO.broker_order_id == order_id)
            )).scalar_one_or_none()
        if _row and _row.exchange:
            return _row.exchange
    except Exception:
        pass
    return None


async def _al_modify_write_reject(order_id: str, e: Exception) -> None:
    """Mark an AlgoOrder row REJECTED after a modify_order broker failure."""
    try:
        from sqlalchemy import update as sql_update
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        async with async_session() as s:
            await s.execute(
                sql_update(AlgoOrder)
                .where(AlgoOrder.broker_order_id == order_id)
                .values(status="REJECTED", detail=str(e)[:240])
            )
            await s.commit()
    except Exception:
        pass


async def _action_live_modify_order(agent, context: dict, params: dict):
    """
    Modify an open broker order.  Wraps kite.modify_order in run_in_executor.
    Updates the matching AlgoOrder row on success.
    """
    import asyncio
    from backend.brokers import get_broker

    account  = str(params.get("account") or "")
    order_id = str(params.get("order_id") or "")
    variety  = str(params.get("variety") or "regular")

    if not account or not order_id:
        raise ValueError(f"modify_order: account and order_id are required")

    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    # Note: MCX qty translation (to_kite_qty) is NOT applied here because
    # modify_order references a live Kite order_id — any quantity in params
    # should already be in Kite's convention (lots for MCX). Agent actions
    # that modify orders are expected to supply the correct Kite qty.
    kwargs = _al_modify_build_kwargs(params)

    # Slice Q — pass exchange from persisted AlgoOrder row so Groww's
    # segment resolver doesn't raise ValueError on empty exchange.
    if "exchange" not in kwargs:
        exch = await _al_modify_fetch_exchange(order_id)
        if exch:
            kwargs["exchange"] = exch

    try:
        await loop.run_in_executor(
            None,
            lambda: broker.modify_order(order_id, variety=variety, **kwargs)
        )
    except Exception as e:
        # Update the AlgoOrder row to REJECTED so the operator can see it.
        await _al_modify_write_reject(order_id, e)
        raise


async def _action_live_cancel_order(agent, context: dict, params: dict):
    """
    Cancel a single open broker order.  Wraps kite.cancel_order.
    Marks the matching AlgoOrder row CANCELLED on success.
    """
    import asyncio
    from backend.brokers import get_broker
    from sqlalchemy import update as sql_update
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    account  = str(params.get("account") or "")
    order_id = str(params.get("order_id") or "")
    variety  = str(params.get("variety") or "regular")

    if not account or not order_id:
        raise ValueError(f"cancel_order: account and order_id are required")

    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(
            None, lambda: broker.cancel_order(order_id, variety=variety)
        )
    except Exception as e:
        raise

    # Mark CANCELLED in our order log.
    try:
        async with async_session() as s:
            await s.execute(
                sql_update(AlgoOrder)
                .where(AlgoOrder.broker_order_id == order_id)
                .values(status="CANCELLED",
                        detail=f"Cancelled by agent {agent.slug}")
            )
            await s.commit()
    except Exception as db_e:
        logger.warning(f"[LIVE] cancel_order DB update failed: {db_e}")


async def _al_cancel_broker_orders(
    broker, scope_account: str, loop
) -> "tuple[int, int]":
    """Cancel all open orders for a single broker account.

    Skips the broker entirely when scope_account is set and does not match.
    Returns (cancelled_count, error_count).
    """
    acct = broker.account
    if scope_account and acct != scope_account:
        return 0, 0

    cancelled = 0
    errors    = 0
    try:
        orders = await loop.run_in_executor(None, broker.orders)
        open_orders = [
            o for o in (orders or [])
            if str(o.get("status", "")).upper()
            in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")
        ]
        for o in open_orders:
            oid     = str(o.get("order_id", ""))
            variety = str(o.get("variety") or "regular")
            if not oid:
                continue
            try:
                await loop.run_in_executor(
                    None,
                    lambda _oid=oid, _v=variety:
                        broker.cancel_order(_oid, variety=_v),
                )
                cancelled += 1
                logger.info(f"[LIVE] cancel_all_orders: cancelled {oid} [{acct}]")
            except Exception as e:
                errors += 1
                logger.warning(
                    f"[LIVE] cancel_all_orders: failed to cancel {oid} [{acct}]: {e}"
                )
    except Exception as e:
        logger.error(f"[LIVE] cancel_all_orders: order list failed for [{acct}]: {e}")

    return cancelled, errors


async def _action_live_cancel_all_orders(agent, context: dict, params: dict):
    """
    Cancel every open order across all accounts (or a scoped account).

    Routes through the Broker registry — broker.orders() lists the
    account's open orders, broker.cancel_order() fires the cancel. All
    calls are wrapped in run_in_executor since the underlying SDKs are
    synchronous.  Returns aggregate cancelled count via log.
    """
    import asyncio
    from backend.brokers.registry import all_brokers

    loop = asyncio.get_running_loop()
    scope_account = str(params.get("account") or "")

    total_cancelled = 0
    total_errors = 0

    for broker in all_brokers():
        cancelled, errors = await _al_cancel_broker_orders(broker, scope_account, loop)
        total_cancelled += cancelled
        total_errors    += errors

    logger.info(f"[LIVE] cancel_all_orders complete: {total_cancelled} cancelled, "
                f"{total_errors} errors (agent={agent.slug})")


def _al_positions_to_rows(df) -> "list[dict]":
    """Convert a DataFrame of positions to a list of dicts.

    Returns an empty list when df is None, empty, or conversion fails.
    """
    if df is None or (hasattr(df, "empty") and df.empty):
        return []
    try:
        return df.to_dict(orient="records")
    except Exception as e:
        logger.error(
            f"[LIVE] chase_close_positions: could not read df_positions: {e}"
        )
        return []


def _al_positions_filter(rows: list, scope_acct: "str | None") -> list:
    """Apply account scope filter and drop zero-qty rows."""
    if scope_acct:
        rows = [r for r in rows if str(r.get("account")) == scope_acct]
    return [r for r in rows if int(r.get("quantity") or 0) != 0]


def _chase_resolve_positions(context: dict, params: dict) -> list[dict]:
    """Read df_positions from context, apply scope filter, drop zero-qty rows.

    Returns an empty list when the DataFrame is missing, empty, or cannot
    be converted — caller checks and returns early.
    """
    scope      = (params.get("scope") or "total").lower()
    scope_acct = str(params.get("account") or "") if scope == "account" else None

    rows = _al_positions_to_rows(context.get("df_positions"))
    return _al_positions_filter(rows, scope_acct)


async def _al_chase_handle_blocked(
    agent, acct: str, symbol: str, exchange: str,
    side: str, qty: int, price, pf: dict,
) -> None:
    """Write REJECTED AlgoOrder and fire alert for a preflight-blocked chase position."""
    from backend.api.algo.actions import _write_live_order

    reasons = "; ".join(b["reason"] for b in pf["blocked"])
    codes   = ", ".join(b["code"] for b in pf["blocked"])
    logger.error(
        f"[LIVE] chase_close_positions BLOCKED for {acct} "
        f"{exchange}/{symbol} {side} {qty}: [{codes}] {reasons}"
    )
    await _write_live_order(
        agent, "chase_close_positions",
        {"account": acct, "symbol": symbol, "exchange": exchange,
         "side": side, "qty": qty, "price": price},
        status="REJECTED",
        detail_suffix=f"PREFLIGHT BLOCKED: {reasons[:200]}",
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=acct, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent, 'slug', 'chase_close_positions')}",
            error=f"PREFLIGHT BLOCKED [{codes}]: {reasons}",
        )
    except Exception:
        pass


async def _chase_build_tasks(
    agent, rows: list[dict]
) -> "tuple[list, list[dict]]":
    """Run preflight for each position row; build chase task list.

    For each row:
      - Run run_preflight; on failure write REJECTED AlgoOrder + alert + skip.
      - On success write OPEN AlgoOrder + append chase_order task.

    Returns (chase_tasks, task_rows) where task_rows[i] matches chase_tasks[i].
    """
    import asyncio
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.api.algo.actions import _write_live_order

    chase_tasks: list = []
    task_rows:   list[dict] = []

    for p in rows:
        acct     = str(p.get("account", ""))
        symbol   = str(p.get("tradingsymbol", ""))
        exchange = str(p.get("exchange") or "NFO")
        qty_held = int(p.get("quantity") or 0)
        side     = "SELL" if qty_held > 0 else "BUY"
        qty      = abs(qty_held)

        # Best effort initial limit price from LTP in context row.
        ltp   = p.get("last_price") or p.get("close_price")
        price = float(ltp) if ltp is not None else None

        # ── Preflight (G1 lot-multiple; G2 5-lot cap bypassed for closes) ─
        pf = await run_preflight(acct, {
            "exchange": exchange, "tradingsymbol": symbol,
            "quantity": qty, "order_type": "LIMIT",
            "product": "NRML",
            "price": price or 0, "variety": "regular",
            "intent": "close",   # signals G2 bypass inside run_preflight
        })
        if not pf["ok"]:
            await _al_chase_handle_blocked(
                agent, acct, symbol, exchange, side, qty, price, pf
            )
            continue  # skip this position; other positions in the loop proceed

        # Persist intent row before broker call.
        await _write_live_order(agent, "chase_close_positions", {
            "account": acct, "symbol": symbol, "exchange": exchange,
            "side": side, "qty": qty, "price": price,
        }, status="OPEN")

        cfg = ChaseConfig(exchange=exchange, product="NRML", intent="close")
        chase_tasks.append(
            asyncio.create_task(
                chase_order(account=acct, symbol=symbol,
                            transaction_type=side, quantity=qty, cfg=cfg)
            )
        )
        task_rows.append(p)
        logger.info(f"[LIVE] chase_close_positions: queued {side} {qty} {symbol} [{acct}]")

    return chase_tasks, task_rows


def _al_parse_failure_row(
    p: dict,
) -> "tuple[str, str, str, int, str, float]":
    """Extract chase-failure fields from a position row dict.

    Returns (acct, symbol, exchange, qty, side, ltp).
    """
    acct     = str(p.get("account", ""))
    symbol   = str(p.get("tradingsymbol", ""))
    exchange = str(p.get("exchange") or "NFO")
    qty_raw  = int(p.get("quantity") or 0)
    qty      = abs(qty_raw)
    side     = "SELL" if qty_raw > 0 else "BUY"
    ltp      = float(p.get("last_price") or p.get("close_price") or 0)
    return acct, symbol, exchange, qty, side, ltp


async def _al_diagnose_chase_result(acct: str, diag_order: dict) -> str:
    """Run diagnose_live_failure for one chase task; returns the diagnosis string."""
    from backend.brokers import get_broker

    try:
        broker = get_broker(acct)
        return await diagnose_live_failure(broker, diag_order, "")
    except Exception:
        return "diagnosis unavailable"


async def _chase_handle_results(
    results: list, task_rows: list[dict], agent
) -> None:
    """Diagnose and log failed chase tasks.

    Iterates gather() results; for each Exception entry, fetches a
    basket_margin diagnosis and fires a failure alert.  Must be async
    because diagnose_live_failure is awaited.

    task_rows[i] must correspond to chase_tasks[i] (only preflight-passed
    rows are included — blocked positions are excluded from both lists).
    """
    for i, res in enumerate(results):
        if not isinstance(res, Exception):
            continue
        p = task_rows[i]
        acct, symbol, exchange, qty, side, ltp = _al_parse_failure_row(p)
        diag_order = {
            "exchange": exchange, "symbol": symbol, "side": side,
            "qty": qty, "order_type": "LIMIT", "product": "NRML",
            "price": ltp, "variety": "regular",
        }
        diag = await _al_diagnose_chase_result(acct, diag_order)
        logger.error(f"[LIVE] chase_close_positions task {i} failed for "
                     f"{acct} {exchange}/{symbol} {side} {qty}: "
                     f"{res} | diag: {diag}")
        try:
            from backend.shared.helpers.alert_utils import send_order_failure_alert
            send_order_failure_alert(
                account=acct, symbol=symbol, exchange=exchange,
                side=side, qty=qty, mode="live",
                source=f"agent:{getattr(agent, 'slug', 'chase_close_positions')}",
                error=f"{res} | {diag}",
            )
        except Exception:
            pass


async def _action_live_chase_close_positions(agent, context: dict, params: dict):
    """
    Close every open position in scope using the adaptive chase engine.

    Scope resolution — params.scope:
      'total'   (default) — every position across all accounts
      'account'           — positions for params.account only

    For each non-zero position, derives the closing side (SELL for long,
    BUY for short), fetches LTP for the initial limit, writes an
    AlgoOrder(mode='live') row, then fires chase_order() as an asyncio
    task so multiple positions close concurrently (same pattern as the
    expiry engine).

    We deliberately do NOT use ExpiryEngine.scan_positions() here because
    that scanner applies expiry-day ITM/NTM filters that are irrelevant
    for a generic loss-agent close.  Instead we read directly from
    context['df_positions'] (the live Kite snapshot already in context)
    which is a pandas DataFrame with columns: account, tradingsymbol,
    exchange, quantity, last_price, close_price, …
    """
    import asyncio

    scope = (params.get("scope") or "total").lower()

    rows = _chase_resolve_positions(context, params)
    if not rows:
        logger.warning(f"[LIVE] chase_close_positions: no positions in context "
                       f"(agent={agent.slug}, scope={scope})")
        return

    chase_tasks, task_rows = await _chase_build_tasks(agent, rows)
    if not chase_tasks:
        return

    # Await all chase tasks concurrently — each manages its own retry loop.
    results = await asyncio.gather(*chase_tasks, return_exceptions=True)
    await _chase_handle_results(results, task_rows, agent)


async def _action_live_expiry_auto_close(agent, context: dict, params: dict):
    """
    Expiry-day surgical close restricted to ONE exchange.

    Wraps the legacy ExpiryEngine so the agent path inherits the
    battle-tested rules:
      - NFO: close ALL ITM + NTM expiring today (no hedging exception
        — Indian equity F&O is settled per-leg, no broker netting).
      - MCX: close only UNHEDGED ITM + NTM (CE/PE pairs whose net qty
        across the underlying+expiry sum to zero are skipped — the
        broker nets them at settlement, no operator action needed).

    Reads NTM buffer + chase config from `algo.expiry_*` settings (same
    knobs the bg task uses), so a single /admin/settings change tunes
    both paths.
    """
    from backend.api.algo.expiry import ExpiryEngine

    exch = (params.get("exchange") or "").upper()
    if exch not in ("NFO", "MCX"):
        logger.error(f"[LIVE] expiry_auto_close: invalid exchange param {exch!r} for agent {agent.slug}")
        return

    engine = ExpiryEngine()
    try:
        to_close = engine.scan_positions()
    except Exception as e:
        logger.error(f"[LIVE] expiry_auto_close: scan failed for agent {agent.slug}: {e}")
        return

    targets = [p for p in to_close if (p.exchange or "").upper() == exch]
    if not targets:
        logger.info(f"[LIVE] expiry_auto_close: no {exch} positions need closing "
                    f"(agent={agent.slug}, scanned={len(to_close)})")
        return

    logger.info(f"[LIVE] expiry_auto_close: agent {agent.slug} closing "
                f"{len(targets)} {exch} positions")
    await engine.close_positions(targets)
    logger.info(f"[LIVE] expiry_auto_close: agent {agent.slug} done — "
                f"closed={len(engine.state.closed)} failed={len(engine.state.failed)} "
                f"slippage=₹{engine.state.total_slippage:.2f}")
