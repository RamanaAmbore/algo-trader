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
    # Lazy import — _write_live_order lives in actions.py; patch path
    # "backend.api.algo.actions._write_live_order" must remain valid.
    from backend.api.algo.actions import _write_live_order

    # A sentinel agent object for _write_live_order which expects agent.slug.
    # _action_place_order is called from execute() where `agent` is in scope,
    # but this function only receives context/params.  Build a minimal shim.
    class _AgentShim:
        slug = context.get("agent_slug", "place_order")

    _shim = _AgentShim()

    account  = str(params.get("account") or "")
    symbol   = str(params.get("symbol") or "")
    exchange = str(params.get("exchange") or "NFO")
    side     = str(params.get("transaction_type") or params.get("side") or "SELL")
    qty      = int(params.get("quantity") or 0)
    price    = params.get("price")
    product  = str(params.get("product") or "NRML")

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
        reasons = "; ".join(b["reason"] for b in pf["blocked"])
        logger.warning(f"[LIVE] place_order BLOCKED for {account} {exchange}/{symbol} "
                       f"{side} {qty}: {reasons}")
        # Write a synthetic AlgoOrder row so the block is visible in the
        # Orders log alongside real fires.
        _fake_order_id = await _write_live_order(
            _shim, "place_order",
            {"account": account, "symbol": symbol, "exchange": exchange,
             "side": side, "qty": qty, "price": price},
            status="REJECTED",
            detail_suffix=f"PREFLIGHT BLOCKED: {reasons[:200]}",
        )
        # Fire-and-forget event + Telegram alert (best-effort).
        try:
            if _fake_order_id:
                from backend.api.algo.order_events import write_event as _write_ev
                import asyncio as _aio
                _aio.create_task(_write_ev(
                    _fake_order_id, "preflight_block",
                    f"Preflight blocked: {reasons[:300]}",
                    payload={"blocked": pf["blocked"],
                             "diagnostics": pf["diagnostics"]},
                ))
        except Exception:
            pass
        try:
            # Route through send_order_failure_alert so the preflight-block
            # notification benefits from the same cooldown dedup + market-hours
            # gate as real broker failures (fixes cooldown bypass on preflight).
            from backend.shared.helpers.alert_utils import send_order_failure_alert
            send_order_failure_alert(
                account=account,
                symbol=symbol,
                exchange=exchange,
                side=side,
                qty=qty,
                mode="live",
                source=f"agent:{context.get('agent_slug', 'place_order')}",
                error=f"preflight blocked: {reasons[:200]}",
            )
        except Exception as _e:
            logger.warning(f"Preflight-block notification failed: {_e}")
        return  # abort without placing

    # Emit preflight_ok event (fire-and-forget).
    try:
        _intent_id = await _write_live_order(
            _shim, "place_order",
            {"account": account, "symbol": symbol, "exchange": exchange,
             "side": side, "qty": qty, "price": price},
            status="OPEN",
        )
        if _intent_id:
            from backend.api.algo.order_events import write_event as _write_ev_ok
            import asyncio as _aio
            _aio.create_task(_write_ev_ok(
                _intent_id, "preflight_ok",
                "Preflight passed",
                payload={"diagnostics": pf["diagnostics"]},
            ))
    except Exception:
        pass

    cfg = ChaseConfig(exchange=exchange, product=product)
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
        )
    except Exception as e:
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
        raise


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
    import asyncio
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.brokers import get_broker
    # Lazy import — _write_live_order lives in actions.py; patch path
    # "backend.api.algo.actions._write_live_order" must remain valid.
    from backend.api.algo.actions import _write_live_order

    account  = str(params.get("account") or "")
    symbol   = str(params.get("symbol") or params.get("tradingsymbol") or "")
    exchange = str(params.get("exchange") or "NFO")
    qty      = int(params.get("quantity") or params.get("qty") or 0)
    side     = (params.get("side") or params.get("transaction_type") or "SELL").upper()

    if not account or not symbol or qty <= 0:
        raise ValueError(f"close_position: missing required params (account={account!r}, "
                         f"symbol={symbol!r}, qty={qty})")

    # Fetch LTP as the initial limit price — the chase engine re-quotes
    # on every subsequent attempt so this is just the first bid.
    price = None
    broker = None
    try:
        broker = get_broker(account)
        loop = asyncio.get_running_loop()
        price = await _fetch_ltp(broker, exchange, symbol, loop, context="close_position")
    except Exception as e:
        logger.warning(f"[LIVE] close_position LTP fetch failed, proceeding with None price: {e}")

    # ── Preflight (G1 lot-multiple; G2 5-lot cap bypassed for closes) ────
    pf = await run_preflight(account, {
        "exchange": exchange, "tradingsymbol": symbol,
        "quantity": qty, "order_type": "LIMIT",
        "product": str(params.get("product") or "NRML"),
        "price": price or 0, "variety": "regular",
        "intent": "close",   # signals G2 bypass inside run_preflight
    })
    if not pf["ok"]:
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
        return  # abort — do not reach chase_order

    # Persist the intent row before touching the broker.
    await _write_live_order(agent, "close_position", {
        "account": account, "symbol": symbol, "exchange": exchange,
        "side": side, "qty": qty, "price": price,
    }, status="OPEN")

    cfg = ChaseConfig(exchange=exchange, product=str(params.get("product") or "NRML"))
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
        )
    except Exception as e:
        # Run basket_margin diagnosis so an agent-fired close failure logs
        # whether the cause was margin or permission, same enrichment the
        # /ticket route applies on operator-typed orders.
        diag_order = {
            "exchange": exchange, "symbol": symbol, "side": side, "qty": qty,
            "order_type": "LIMIT", "product": str(params.get("product") or "NRML"),
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
        raise


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
    kwargs: dict = {}
    for field in ("quantity", "price", "trigger_price", "order_type", "validity"):
        v = params.get(field)
        if v is not None:
            kwargs[field] = v

    # Slice Q — pass exchange from persisted AlgoOrder row so Groww's
    # segment resolver doesn't raise ValueError on empty exchange.
    if "exchange" not in kwargs:
        try:
            from sqlalchemy import select as _select
            from backend.api.database import async_session as _as
            from backend.api.models import AlgoOrder as _AO
            async with _as() as _s:
                _row = (await _s.execute(
                    _select(_AO).where(_AO.broker_order_id == order_id)
                )).scalar_one_or_none()
            if _row and _row.exchange:
                kwargs["exchange"] = _row.exchange
        except Exception:
            pass

    try:
        await loop.run_in_executor(
            None,
            lambda: broker.modify_order(order_id, variety=variety, **kwargs)
        )
    except Exception as e:
        # Update the AlgoOrder row to REJECTED so the operator can see it.
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
        acct = broker.account
        if scope_account and acct != scope_account:
            continue
        try:
            orders = await loop.run_in_executor(None, broker.orders)
            open_orders = [o for o in (orders or [])
                           if str(o.get("status", "")).upper() in
                           ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")]
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
                    total_cancelled += 1
                    logger.info(f"[LIVE] cancel_all_orders: cancelled {oid} [{acct}]")
                except Exception as e:
                    total_errors += 1
                    logger.warning(f"[LIVE] cancel_all_orders: failed to cancel {oid} [{acct}]: {e}")
        except Exception as e:
            logger.error(f"[LIVE] cancel_all_orders: order list failed for [{acct}]: {e}")

    logger.info(f"[LIVE] cancel_all_orders complete: {total_cancelled} cancelled, "
                f"{total_errors} errors (agent={agent.slug})")


def _chase_resolve_positions(context: dict, params: dict) -> list[dict]:
    """Read df_positions from context, apply scope filter, drop zero-qty rows.

    Returns an empty list when the DataFrame is missing, empty, or cannot
    be converted — caller checks and returns early.
    """
    scope      = (params.get("scope") or "total").lower()
    scope_acct = str(params.get("account") or "") if scope == "account" else None

    df = context.get("df_positions")
    if df is None or (hasattr(df, "empty") and df.empty):
        return []

    try:
        rows: list[dict] = df.to_dict(orient="records")
    except Exception as e:
        logger.error(f"[LIVE] chase_close_positions: could not read df_positions: {e}")
        return []

    if scope_acct:
        rows = [r for r in rows if str(r.get("account")) == scope_acct]

    # Filter to non-zero positions only.
    rows = [r for r in rows if int(r.get("quantity") or 0) != 0]
    return rows


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
            continue  # skip this position; other positions in the loop proceed

        # Persist intent row before broker call.
        await _write_live_order(agent, "chase_close_positions", {
            "account": acct, "symbol": symbol, "exchange": exchange,
            "side": side, "qty": qty, "price": price,
        }, status="OPEN")

        cfg = ChaseConfig(exchange=exchange, product="NRML")
        chase_tasks.append(
            asyncio.create_task(
                chase_order(account=acct, symbol=symbol,
                            transaction_type=side, quantity=qty, cfg=cfg)
            )
        )
        task_rows.append(p)
        logger.info(f"[LIVE] chase_close_positions: queued {side} {qty} {symbol} [{acct}]")

    return chase_tasks, task_rows


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
    from backend.brokers import get_broker

    for i, res in enumerate(results):
        if not isinstance(res, Exception):
            continue
        # Diagnose via basket_margin so the log distinguishes
        # margin-shortfall from segment-permission for this leg.
        p        = task_rows[i]
        acct     = str(p.get("account", ""))
        symbol   = str(p.get("tradingsymbol", ""))
        exchange = str(p.get("exchange") or "NFO")
        qty      = abs(int(p.get("quantity") or 0))
        side     = "SELL" if int(p.get("quantity") or 0) > 0 else "BUY"
        ltp      = p.get("last_price") or p.get("close_price") or 0
        diag_order = {
            "exchange": exchange, "symbol": symbol, "side": side,
            "qty": qty, "order_type": "LIMIT", "product": "NRML",
            "price": float(ltp) if ltp else 0, "variety": "regular",
        }
        try:
            broker = get_broker(acct)
            diag = await diagnose_live_failure(broker, diag_order, str(res))
        except Exception:
            diag = "diagnosis unavailable"
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
