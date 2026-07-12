"""
Mode-2 paper-trade writer and dispatcher for agent actions.

Extracted from actions.py. Imports `_maybe_attach_template_from_action`
lazily from actions.py to avoid a circular import. Imports
`_basket_margin_validate` and `_live_positions_in_scope` from
actions_preflight.py at module top (no cycle).
"""

from backend.shared.helpers.ramboq_logger import get_logger
from backend.api.algo.actions_preflight import (
    _basket_margin_validate,
    _live_positions_in_scope,
)

logger = get_logger(__name__)


async def _write_paper_order(agent, action_type: str, resolved: dict, context: dict):
    """
    Write ONE AlgoOrder(mode='paper') row after a dry-run via Kite's
    basket_margin. If the dry-run fails, the row is persisted as
    REJECTED with Kite's error text — so the operator sees exactly the
    same rejections they'd see from a real place_order.

    On success, the order is registered with the prod PaperTradeEngine
    so its fill / modify / unfilled lifecycle plays out against real
    Kite quotes on the 5 s chase tick.
    """
    import uuid
    from backend.api.database        import async_session
    from backend.api.models          import AlgoOrder
    from backend.brokers      import get_broker
    from backend.api.algo.paper      import get_prod_paper_engine

    account  = str(resolved["account"])
    symbol   = str(resolved["symbol"])
    side     = str(resolved["side"])
    qty      = int(resolved["qty"] or 0)
    price    = resolved.get("price")
    exchange = resolved.get("exchange") or "NFO"

    # Basket-margin validation — Kite checks instrument / lot / tick /
    # segment / circuit-limit rules and returns required margin. If it
    # raises the error flows into the AlgoOrder.detail column so the
    # operator can see exactly why a real placement would have been
    # rejected.
    broker = None
    ok, reason = True, "paper"
    try:
        broker = get_broker(account)
        if qty > 0 and price is not None and symbol and exchange:
            ok, reason = await _basket_margin_validate(broker, {
                "account": account, "symbol": symbol, "side": side,
                "qty": qty, "price": price, "exchange": exchange,
            })
    except Exception as e:
        ok, reason = False, f"broker lookup failed: {e}"

    status = "OPEN" if ok else "REJECTED"
    fake_order_id = "PAPER-" + uuid.uuid4().hex[:12]

    price_str = f"@₹{price:,.2f}" if price is not None else "@MARKET"
    pretty = (f"[PAPER] {agent.slug} → {action_type}: {side} {qty} "
              f"{symbol} {price_str} · acct={account}"
              + ("" if ok else f" · REJECTED ({reason})"))
    logger.warning(pretty)

    algo_order_id = None
    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty,
                initial_price=(float(price) if price is not None else None),
                status=status, engine="paper", mode="paper",
                agent_id=getattr(agent, "id", None),
                broker_order_id=fake_order_id,
                detail=pretty,
            )
            s.add(row)
            await s.commit()
            algo_order_id = row.id
    except Exception as e:
        logger.error(f"[PAPER] write failed: {e}")
        return

    # Timeline events — placed or rejected.
    if algo_order_id:
        try:
            from backend.api.algo.order_events import write_event
            if ok:
                await write_event(
                    algo_order_id, "placed",
                    f"[PAPER] {agent.slug} → {action_type}: {side} {qty} {symbol} "
                    f"{'@₹' + f'{price:,.2f}' if price is not None else '@MARKET'} — preflight OK",
                    payload={"account": account, "price": price, "exchange": exchange,
                             "margin_check": reason},
                )
            else:
                await write_event(
                    algo_order_id, "reject",
                    f"[PAPER] {agent.slug} → {action_type}: REJECTED ({reason[:200]})",
                    payload={"account": account, "price": price, "broker_response": reason},
                )
        except Exception:
            pass

    if not ok:
        # Rejected by basket_margin — nothing to chase. The REJECTED
        # row on the Orders log tells the story.
        return

    # Register with the prod paper engine. Its 5 s tick loop will ask
    # LiveQuoteSource for real bid/ask and run the same fill / modify /
    # unfilled lifecycle the simulator uses.
    if qty > 0 and price is not None:
        engine = get_prod_paper_engine()
        engine.register_open_order({
            "algo_order_id": algo_order_id,
            "account":       account,
            "symbol":        symbol,
            "side":          side,
            "qty":           qty,
            "limit_price":   float(price),
            "initial_price": float(price),
            "exchange":      exchange,
            "agent_slug":    agent.slug,
            "action_type":   action_type,
        })


async def _paper_chase_close(
    agent, action_type: str, params: dict, context: dict
) -> None:
    """Expand a paper-mode chase_close / chase_close_positions across live positions."""
    positions = _live_positions_in_scope(context, params)
    if not positions:
        logger.warning(f"[PAPER] {agent.slug} → {action_type}: scope matched 0 positions")
        await _write_paper_order(agent, action_type, {
            "account":  str(params.get("account") or "TOTAL"),
            "symbol":   "(no positions in scope)",
            "side":     "SELL", "qty": 0, "price": None,
            "exchange": "NFO",
        }, context)
        return
    for p in positions:
        qty_held = int(p.get("quantity") or 0)
        if qty_held == 0:
            continue
        side = "SELL" if qty_held > 0 else "BUY"
        # For the initial limit: use LTP ± half spread so the mode-2
        # path mirrors what the chase engine does on Kite. Real
        # bid/ask will come from LiveQuoteSource on the first tick.
        ltp = p.get("last_price") or p.get("close_price")
        price = float(ltp) if ltp is not None else None
        await _write_paper_order(agent, action_type, {
            "account":  str(p.get("account", "")),
            "symbol":   str(p.get("tradingsymbol", "")),
            "side":     side,
            "qty":      abs(qty_held),
            "price":    price,
            "exchange": str(p.get("exchange") or "NFO"),
        }, context)


async def _paper_expiry_close(
    agent, action_type: str, params: dict, context: dict
) -> None:
    """Paper-mode dry-run for expiry_auto_close.

    Reads live positions, filters by exchange + DTE ≤ 1.5, writes a paper
    order per matched row. The MCX hedging-net filter is NOT applied in
    paper mode — operators reviewing the paper book want to see EVERY
    ITM/NTM expiring leg; the live ExpiryEngine path is where the hedging
    check actually fires.
    """
    from backend.api.algo.derivatives import parse_tradingsymbol, days_to_expiry

    exch = (params.get("exchange") or "").upper()
    if exch not in ("NFO", "MCX"):
        logger.warning(f"[PAPER] expiry_auto_close: invalid exchange {exch!r} for {agent.slug}")
        return
    all_positions = _live_positions_in_scope(context, {"scope": "total"})
    targets = []
    for p in all_positions:
        if (p.get("exchange") or "").upper() != exch:
            continue
        qty_held = int(p.get("quantity") or 0)
        if qty_held == 0:
            continue
        parsed = parse_tradingsymbol(p.get("tradingsymbol") or "")
        if not parsed or not parsed.get("expiry"):
            continue
        try:
            close_time = (23, 30) if exch == "MCX" else (15, 30)
            d = float(days_to_expiry(parsed["expiry"], ref=context.get("now"),
                                     close_time=close_time))
        except Exception:
            continue
        if d > 1.5:
            continue
        targets.append(p)
    if not targets:
        logger.info(f"[PAPER] expiry_auto_close: no {exch} expiring positions "
                    f"for {agent.slug}")
        await _write_paper_order(agent, action_type, {
            "account":  "TOTAL",
            "symbol":   f"(no {exch} expiring positions)",
            "side":     "SELL", "qty": 0, "price": None,
            "exchange": exch,
        }, context)
        return
    for p in targets:
        qty_held = int(p.get("quantity") or 0)
        side = "SELL" if qty_held > 0 else "BUY"
        ltp = p.get("last_price") or p.get("close_price")
        price = float(ltp) if ltp is not None else None
        await _write_paper_order(agent, action_type, {
            "account":  str(p.get("account", "")),
            "symbol":   str(p.get("tradingsymbol", "")),
            "side":     side,
            "qty":      abs(qty_held),
            "price":    price,
            "exchange": exch,
        }, context)


async def _paper_place_or_close(
    agent, action_type: str, params: dict, context: dict
) -> None:
    """Handle place_order / close_position / modify_order / cancel_order / cancel_all_orders in paper mode."""
    account = str(params.get("account") or "")
    symbol  = str(params.get("symbol")  or f"{agent.slug}-{action_type}")
    if params.get("side") in ("BUY", "SELL"):
        side = params.get("side")
    elif params.get("transaction_type") in ("BUY", "SELL"):
        side = params.get("transaction_type")
    else:
        side = "SELL"
    qty   = int(params.get("quantity") or 0)
    price = params.get("price")
    await _write_paper_order(agent, action_type, {
        "account":  account,
        "symbol":   symbol,
        "side":     side,
        "qty":      qty,
        "price":    price,
        "exchange": str(params.get("exchange") or "NFO"),
    }, context)


async def _paper_trade(agent, action_type: str, params: dict, context: dict):
    """
    Mode-2 dispatcher — mirrors `_sim_paper_trade` but:
      - Writes AlgoOrder.mode='paper' instead of 'sim'
      - Validates via Kite basket_margin before marking OPEN
      - Registers with the prod PaperTradeEngine (LiveQuoteSource)
    """
    if action_type in {"chase_close", "chase_close_positions"}:
        await _paper_chase_close(agent, action_type, params, context)
        return

    if action_type == "expiry_auto_close":
        await _paper_expiry_close(agent, action_type, params, context)
        return

    if action_type in {"place_order", "close_position", "modify_order",
                       "cancel_order", "cancel_all_orders"}:
        await _paper_place_or_close(agent, action_type, params, context)
        return
