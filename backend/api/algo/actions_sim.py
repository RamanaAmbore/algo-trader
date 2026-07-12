"""
Simulator (mode-1) and replay (mode-4) paper-trade writers for agent actions.

Extracted from actions.py. `_maybe_attach_template_from_action` lives in
actions.py (called from both sim and paper paths) and is imported lazily
here to avoid a circular import.
"""

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _sim_prices_for(account: str, symbol: str) -> tuple[float | None, float | None, float | None, int | None]:
    """
    Look up simulated (last_price, bid, ask, signed quantity) for
    (account, symbol) from the SimDriver's per-symbol state. Paper-trade
    writers use `bid` / `ask` to pick the correct side of the book for
    the initial limit price (SELL@bid, BUY@ask), which is exactly what
    the live chase engine does against real broker quotes.

    Returns (None, None, None, None) when the symbol isn't in the sim
    state — the writer then falls back to the price param or leaves
    the price column null.
    """
    try:
        from backend.api.algo.sim.driver import get_driver
        drv = get_driver()
        for row in getattr(drv, "_positions_rows", []):
            if str(row.get("account")) == str(account) and \
               str(row.get("tradingsymbol")) == str(symbol):
                lp  = row.get("last_price")
                bid = row.get("bid")
                ask = row.get("ask")
                qty = row.get("quantity")
                return (float(lp)  if lp  is not None else None,
                        float(bid) if bid is not None else None,
                        float(ask) if ask is not None else None,
                        int(qty)   if qty is not None else None)
    except Exception:
        pass
    return None, None, None, None


def _sim_ltp_for(account: str, symbol: str) -> tuple[float | None, int | None]:
    """Back-compat shim — existing call sites want (LTP, qty) only."""
    lp, _bid, _ask, qty = _sim_prices_for(account, symbol)
    return lp, qty


def _sim_positions_in_scope(params: dict) -> list[dict]:
    """
    Return the per-symbol position rows that a scope-level action like
    `chase_close_positions` would hit in real life. Used when a
    scope-only action fires in sim — we expand it into one paper-trade
    per actual position so the Order / Simulator logs show real
    account / symbol / qty / LTP instead of a placeholder.

    `params.scope`: 'total' (default) → every position in the sim
                    'account'         → positions filtered by params.account
    """
    scope = (params.get("scope") or "total").lower()
    acct_filter = str(params.get("account") or "") if scope == "account" else None
    try:
        from backend.api.algo.sim.driver import get_driver
        drv = get_driver()
        rows = getattr(drv, "_positions_rows", []) or []
        if acct_filter:
            rows = [r for r in rows if str(r.get("account")) == acct_filter]
        return list(rows)
    except Exception:
        return []


async def _write_sim_order(agent, action_type: str, resolved: dict):
    """
    Write ONE AlgoOrder row (mode='sim'), push a 'kind=order' entry to
    the sim driver's tick log, AND register the order with the sim
    driver's chase engine. The driver's chase loop (`_chase_open_orders`)
    then adjusts the limit price on each subsequent tick and marks the
    order FILLED once the bid/ask crosses.

    `resolved` must contain real account / symbol / side / qty / price
    — callers resolve these from either the action params
    (close_position, place_order) or from the sim's per-symbol state
    (chase_close_positions expands per-position).
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    account = resolved["account"]
    symbol  = resolved["symbol"]
    side    = resolved["side"]
    qty     = int(resolved["qty"] or 0)
    price   = resolved.get("price")
    exchange = resolved.get("exchange") or "NFO"

    # Human-readable print-style line — shows up as logger.warning AND as
    # the AlgoOrder.detail column AND inside the tick_log entry so the
    # operator sees the same sentence in all three places.
    price_str = f"@₹{price:,.2f}" if price is not None else "@MARKET"
    pretty = (f"[SIM] {agent.slug} → {action_type}: {side} {qty} "
              f"{symbol} {price_str} · acct={account}")
    logger.warning(pretty)

    algo_order_id = None
    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty,
                initial_price=(float(price) if price is not None else None),
                status="OPEN", engine="sim", mode="sim",
                agent_id=getattr(agent, "id", None),
                detail=pretty,
            )
            s.add(row)
            await s.commit()
            algo_order_id = row.id
    except Exception as e:
        logger.error(f"[SIM] paper-trade write failed: {e}")
        return

    # Timeline: placed event (fire-and-forget — never raises).
    if algo_order_id:
        try:
            from backend.api.algo.order_events import write_event
            await write_event(
                algo_order_id, "placed",
                f"[SIM] {agent.slug} → {action_type}: {side} {qty} {symbol} "
                f"{'@₹' + f'{price:,.2f}' if price is not None else '@MARKET'}",
                payload={"account": account, "price": price, "exchange": exchange},
            )
        except Exception:
            pass

    try:
        from backend.api.algo.sim.driver import get_driver
        drv = get_driver()
        drv._tick_log.append({
            "ts":         __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "tick_index": drv.tick_index,
            "scenario":   drv.scenario_slug,
            "kind":       "order",
            "moves":      [],
            "changes":    [],
            "note":       pretty,
            "order": {
                "account": account, "symbol": symbol, "side": side, "qty": qty,
                "price":   (float(price) if price is not None else None),
                "agent":   agent.slug, "action": action_type,
                "algo_order_id": algo_order_id,
            },
        })
        # Hand the order to the driver's chase engine. If qty is zero (no
        # position to close — scope matched nothing) skip registration so
        # the chase loop doesn't carry an empty entry.
        if qty > 0 and price is not None:
            drv.register_open_order({
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
    except Exception as e:
        logger.debug(f"[SIM] could not record order in tick_log: {e}")

    return algo_order_id


async def _sim_paper_trade(agent, action_type: str, params: dict, context: dict):
    """
    Paper-trade dispatcher for sim-mode action fires.

    - `close_position` / `place_order` — params already specify
      account + symbol; write ONE AlgoOrder using those params, with
      the LIMIT price = sim's current LTP for that symbol.
    - `chase_close_positions` / `chase_close` — scope-level actions.
      Expand into ONE paper-trade per open position in scope, each
      carrying the real account / symbol / qty / LTP. Operators see a
      realistic picture of what the chase engine would have tried to
      close.
    - Non-order actions (emit_log / set_flag / monitor_order /
      deactivate_agent / cancel_* / send_summary) — no paper row,
      just the log_event that execute() already writes.
    """
    if action_type in {"chase_close", "chase_close_positions"}:
        positions = _sim_positions_in_scope(params)
        if not positions:
            # Scope matched nothing — still record one row so the fire is
            # visible in the logs, but make it obvious nothing closed.
            logger.warning(f"[SIM] {agent.slug} → {action_type}: scope matched 0 positions")
            await _write_sim_order(agent, action_type, {
                "account": str(params.get("account") or "TOTAL"),
                "symbol":  "(no positions in scope)",
                "side":    "SELL", "qty": 0, "price": None,
                "exchange": "NFO",
            })
            return
        for p in positions:
            qty_held = int(p.get("quantity") or 0)
            side = "SELL" if qty_held > 0 else "BUY"
            # SELL hits the bid, BUY lifts the ask — matches what the live
            # chase engine does on Kite. Fall back to LTP when the spread
            # helper isn't populated yet.
            price = (p.get("bid") if side == "SELL" else p.get("ask")) \
                    or p.get("last_price")
            await _write_sim_order(agent, action_type, {
                "account":  str(p.get("account", "SIM")),
                "symbol":   str(p.get("tradingsymbol", "")),
                "side":     side,
                "qty":      abs(qty_held),
                "price":    price,
                "exchange": str(p.get("exchange") or "NFO"),
            })
        return

    if action_type == "expiry_auto_close":
        # Sim-mode dry-run for the expiry agents. The synthesizer +
        # market_state preset (expiry_day) can drive the condition,
        # so the operator can verify the agent fires; the action then
        # closes whatever's in the sim book on the matching exchange.
        # Hedging filter is not applied in sim mode — the operator's
        # validating the timing + condition path, not the live
        # ExpiryEngine grouping logic.
        exch = (params.get("exchange") or "NFO").upper()
        all_pos = _sim_positions_in_scope({"scope": "total"})
        targets = [p for p in all_pos
                   if (p.get("exchange") or "").upper() == exch]
        if not targets:
            logger.info(f"[SIM] expiry_auto_close: no {exch} positions in sim book "
                        f"for {agent.slug}")
            await _write_sim_order(agent, action_type, {
                "account":  "TOTAL",
                "symbol":   f"(no {exch} positions in sim book)",
                "side":     "SELL", "qty": 0, "price": None,
                "exchange": exch,
            })
            return
        for p in targets:
            qty_held = int(p.get("quantity") or 0)
            if qty_held == 0:
                continue
            side = "SELL" if qty_held > 0 else "BUY"
            price = (p.get("bid") if side == "SELL" else p.get("ask")) \
                    or p.get("last_price")
            await _write_sim_order(agent, action_type, {
                "account":  str(p.get("account", "SIM")),
                "symbol":   str(p.get("tradingsymbol", "")),
                "side":     side,
                "qty":      abs(qty_held),
                "price":    price,
                "exchange": exch,
            })
        return

    if action_type in {"place_order", "close_position"}:
        account = str(params.get("account") or "SIM")
        symbol  = str(params.get("symbol")  or f"{agent.slug}-{action_type}")
        ltp, bid, ask, qty_held = _sim_prices_for(account, symbol)
        if params.get("side") in ("BUY", "SELL"):
            side = params.get("side")
        elif params.get("transaction_type") in ("BUY", "SELL"):
            side = params.get("transaction_type")
        elif qty_held is not None:
            side = "SELL" if qty_held > 0 else "BUY"
        else:
            side = "SELL"
        if params.get("quantity") is not None:
            qty = int(params.get("quantity") or 0)
        elif qty_held is not None:
            qty = abs(int(qty_held))
        else:
            qty = 0
        side_price = bid if side == "SELL" else ask
        price = side_price if side_price is not None else (
            ltp if ltp is not None else params.get("price")
        )
        algo_order_id = await _write_sim_order(agent, action_type, {
            "account":  account,
            "symbol":   symbol,
            "side":     side,
            "qty":      qty,
            "price":    price,
            "exchange": str(params.get("exchange") or "NFO"),
        })
        # Template attach — when the action's params carry template_id /
        # template_slug / *_override fields, fan out TP/SL GTTs + wing
        # into SimGttBook + SimDriver._paper. Mirrors the OrderTicket flow.
        # Lazy import to avoid circular dependency (actions.py imports us).
        from backend.api.algo.actions import _maybe_attach_template_from_action
        await _maybe_attach_template_from_action(
            agent, action_type, params,
            algo_order_id=algo_order_id,
            parent_account=account, parent_symbol=symbol,
            parent_side=side, parent_qty=qty,
            parent_exchange=str(params.get("exchange") or "NFO"),
            parent_price=float(price) if price is not None else 0.0,
            apply_path="sim",
        )
        return

    # Non-order action — no paper row. The log_event call in execute()
    # already captures the action_success event.


# ═══════════════════════════════════════════════════════════════════════════
#  Mode-4 replay paper trade — informational order log for backtest results
# ═══════════════════════════════════════════════════════════════════════════

async def _replay_paper_trade(agent, action_type: str, params: dict, context: dict):
    """
    Replay-mode dispatcher — writes AlgoOrder(mode='replay') rows as a
    record of what the agent would have done at each historical tick.
    No fill lifecycle — replay orders are purely informational.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    account = str(params.get("account") or "REPLAY")
    symbol = str(params.get("symbol") or f"{agent.slug}-{action_type}")
    side = params.get("side") or params.get("transaction_type") or "SELL"
    qty = int(params.get("quantity") or 0)
    price = params.get("price")

    price_str = f"@₹{price:,.2f}" if price is not None else "@MARKET"
    pretty = (f"[REPLAY] {agent.slug} → {action_type}: {side} {qty} "
              f"{symbol} {price_str}")
    logger.warning(pretty)

    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol,
                exchange=str(params.get("exchange") or "NFO"),
                transaction_type=side, quantity=qty,
                initial_price=(float(price) if price is not None else None),
                # Replay orders are deterministic fills against historical
                # candles — there's no fill lifecycle, so they land FILLED
                # immediately. OPEN would incorrectly imply a pending chase.
                status="FILLED", engine="replay", mode="replay",
                agent_id=getattr(agent, "id", None),
                detail=pretty,
            )
            s.add(row)
            await s.commit()
    except Exception as e:
        logger.error(f"[REPLAY] paper-trade write failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  Mode-5 shadow trade — logs exact Kite payload, validates via basket_margin
# ═══════════════════════════════════════════════════════════════════════════

async def _shadow_trade(agent, action_type: str, params: dict, context: dict):
    """
    Shadow-mode dispatcher — captures the exact Kite payload and validates
    via basket_margin, but never calls the broker.
    """
    from backend.api.algo.shadow import get_shadow_engine

    account = str(params.get("account") or "")
    symbol = str(params.get("symbol") or f"{agent.slug}-{action_type}")
    side = params.get("side") or params.get("transaction_type") or "SELL"
    qty = int(params.get("quantity") or 0)
    price = params.get("price")

    result = await get_shadow_engine().capture_order(
        agent=agent,
        action_type=action_type,
        resolved={
            "account": account,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "exchange": str(params.get("exchange") or "NFO"),
            "product": str(params.get("product") or "NRML"),
        },
    )
    if not result.get("ok"):
        logger.warning(f"[SHADOW] {agent.slug}: basket_margin rejected — {result.get('margin_info')}")
