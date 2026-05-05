"""
Agent action executor — runs automated responses when an agent triggers.

Actions are stored in Agent.actions as a JSON list:
  [{"type": "chase_close", "params": {"exchange": "NFO"}}]

Empty list means alert-only (no action taken).
"""

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Broker-hitting actions — the three-way gate (sim / paper / live) only
# applies to these. Non-broker actions (emit_log, set_flag,
# monitor_order, deactivate_agent, send_summary) run uniformly regardless
# of mode.
BROKER_ACTIONS = {
    "place_order", "modify_order",
    "cancel_order", "cancel_all_orders",
    "close_position",
    "chase_close", "chase_close_positions",
}


def _resolve_mode(action_type: str, context: dict) -> str:
    """
    Decide how this action should be executed:
      * 'sim'    — agent was fired by the simulator → route to the sim
                   paper-trade writer (SimDriver owns the lifecycle)
      * 'replay' — agent was fired by the replay engine → route to replay
                   paper-trade writer (informational only)
      * 'shadow' — real data, log-only: captures exact Kite payload +
                   basket_margin validation without executing. Prod only.
      * 'paper'  — mode 2: real data, paper order. On non-main (dev) this
                   is the only path for broker actions. On main (prod),
                   it's the default when shadow is off and the per-action
                   `execution.live.<action>` DB flag is still False
      * 'live'   — mode 3: real data, real order. Only reachable on
                   main AND the per-action flag is True
      * 'noop'   — non-broker action (no gate); the existing handler
                   (send_summary, emit_log, …) runs as-is

    Precedence: sim > replay > (prod branch check) > shadow > live > paper
    """
    if context.get("sim_mode"):
        return "sim"
    if context.get("replay_mode"):
        return "replay"
    if action_type not in BROKER_ACTIONS:
        return "noop"
    from backend.shared.helpers.utils    import is_prod_branch
    from backend.shared.helpers.settings import get_bool
    if not is_prod_branch():
        return "paper"                         # dev never hits broker
    if get_bool("execution.shadow_mode", False):
        return "shadow"
    if get_bool(f"execution.live.{action_type}", False):
        return "live"
    return "paper"


async def execute(agent, actions: list, context: dict):
    """
    Execute action chain sequentially. Every broker-hitting action
    routes through `_resolve_mode` to pick sim / paper / live; the
    non-broker actions (send_summary, emit_log, set_flag, …) run
    as-is regardless of mode.

    Args:
        agent: Agent DB row
        actions: list of action dicts from agent.actions
        context: market data context (sim_mode flag routes to sim path;
                 df_positions used by paper-mode chase expansion)
    """
    sim_mode = bool(context.get("sim_mode"))
    for action in actions:
        action_type = action.get("type", "")
        params = action.get("params", {})
        mode = _resolve_mode(action_type, context)
        tag  = {"sim": "[SIM] ", "replay": "[REPLAY] ", "shadow": "[SHADOW] ",
                "paper": "[PAPER] ", "live": "", "noop": ""}.get(mode, "")

        try:
            if mode == "sim":
                await _sim_paper_trade(agent, action_type, params, context)
            elif mode == "replay":
                await _replay_paper_trade(agent, action_type, params, context)
            elif mode == "shadow":
                await _shadow_trade(agent, action_type, params, context)
            elif mode == "paper":
                await _paper_trade(agent, action_type, params, context)
            elif mode == "live":
                # Real broker path. Only reached on main AND with the
                # per-action flag flipped to True in /admin/settings.
                if action_type == "chase_close":
                    await _action_chase_close(context, params)
                elif action_type == "place_order":
                    await _action_place_order(context, params)
                elif action_type == "close_position":
                    await close_position(context, params)
                # modify_order / cancel_order / cancel_all_orders /
                # chase_close_positions land in the _log_invoke stubs at
                # the bottom of this file for now — they'll hit the
                # broker once their real wiring lands.
                else:
                    logger.warning(f"Agent [{agent.slug}]: live action '{action_type}' has no wired handler yet")
            else:  # 'noop' — non-broker action
                if action_type == "send_summary":
                    await _action_send_summary(context, params)
                elif action_type == "chase_close":
                    # chase_close is in BROKER_ACTIONS so we'd only get
                    # here if BROKER_ACTIONS is misconfigured — safety net
                    await _action_chase_close(context, params)
                else:
                    logger.warning(f"Agent [{agent.slug}]: unknown action type '{action_type}'")
                    continue

            logger.info(f"{tag}Agent [{agent.slug}]: action '{action_type}' completed")
            from backend.api.algo.events import log_event
            await log_event(agent, "action_success", f"{tag}Action: {action_type}",
                            params, sim_mode=sim_mode)

        except Exception as e:
            logger.error(f"{tag}Agent [{agent.slug}]: action '{action_type}' failed: {e}")
            from backend.api.algo.events import log_event
            await log_event(agent, "action_failed",
                            f"{tag}Action: {action_type} — {e}",
                            params, sim_mode=sim_mode)


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
                detail=pretty,
            )
            s.add(row)
            await s.commit()
            algo_order_id = row.id
    except Exception as e:
        logger.error(f"[SIM] paper-trade write failed: {e}")
        return

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
        await _write_sim_order(agent, action_type, {
            "account":  account,
            "symbol":   symbol,
            "side":     side,
            "qty":      qty,
            "price":    price,
            "exchange": str(params.get("exchange") or "NFO"),
        })
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
                status="REPLAY", engine="replay", mode="replay",
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


# ═══════════════════════════════════════════════════════════════════════════
#  Mode-2 paper trade (real data + paper) — feeds the prod PaperTradeEngine
# ═══════════════════════════════════════════════════════════════════════════

def _live_positions_in_scope(context: dict, params: dict) -> list[dict]:
    """
    Mirror of `_sim_positions_in_scope` for the real-data paper path.
    Pulls rows from `context['df_positions']` (the live Kite snapshot
    threaded through by `_task_performance`) filtered by scope.
    """
    scope = (params.get("scope") or "total").lower()
    acct_filter = str(params.get("account") or "") if scope == "account" else None
    df = context.get("df_positions")
    if df is None or getattr(df, "empty", True):
        return []
    try:
        rows = df.to_dict(orient="records")
    except Exception:
        return []
    if acct_filter:
        rows = [r for r in rows if str(r.get("account")) == acct_filter]
    return rows


async def _basket_margin_validate(broker, order: dict) -> tuple[bool, str]:
    """
    Ask Kite to dry-run the order via `basket_margin`. Returns
    (ok, detail). On `ok=False` the detail is Kite's error message —
    mirror of what `place_order` would have rejected with.
    """
    try:
        basket_order = {
            "exchange":         order.get("exchange", "NFO"),
            "tradingsymbol":    order.get("symbol"),
            "transaction_type": order.get("side"),
            "quantity":         order.get("qty"),
            "order_type":       "LIMIT",
            "product":          order.get("product", "NRML"),
            "price":            order.get("price"),
            "variety":          order.get("variety", "regular"),
        }
        # KiteConnect exposes `basket_margin` which validates a list of
        # orders without placing them. Raises on malformed parameters.
        # broker.kite.basket_margin is a synchronous requests call; run it
        # in a thread executor so the event loop is not blocked.
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, broker.kite.basket_margin, [basket_order])
        return True, "basket_margin OK"
    except Exception as e:
        return False, str(e)[:240]


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
    from backend.shared.brokers      import get_broker
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
                broker_order_id=fake_order_id,
                detail=pretty,
            )
            s.add(row)
            await s.commit()
            algo_order_id = row.id
    except Exception as e:
        logger.error(f"[PAPER] write failed: {e}")
        return

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


async def _paper_trade(agent, action_type: str, params: dict, context: dict):
    """
    Mode-2 dispatcher — mirrors `_sim_paper_trade` but:
      - Writes AlgoOrder.mode='paper' instead of 'sim'
      - Validates via Kite basket_margin before marking OPEN
      - Registers with the prod PaperTradeEngine (LiveQuoteSource)
    """
    if action_type in {"chase_close", "chase_close_positions"}:
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
        return

    if action_type in {"place_order", "close_position", "modify_order",
                       "cancel_order", "cancel_all_orders"}:
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
        return


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


async def _action_place_order(context: dict, params: dict):
    """Place an order using the chase engine."""
    from backend.api.algo.chase import chase_order, ChaseConfig

    cfg = ChaseConfig(
        exchange=params.get("exchange", "NFO"),
        product=params.get("product", "NRML"),
    )
    await chase_order(
        account=params.get("account", ""),
        symbol=params.get("symbol", ""),
        transaction_type=params.get("transaction_type", "SELL"),
        quantity=params.get("quantity", 0),
        cfg=cfg,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  NEW-GRAMMAR ACTION HANDLERS
#
#  Referenced by dotted path from the grammar_tokens seed (see grammar.py).
#  Each handler has the signature (ctx, params) → result dict.
#  Stubs at this phase — they log the invocation so the pipeline can be
#  exercised end-to-end before real broker calls land.
# ═══════════════════════════════════════════════════════════════════════════

def _log_invoke(action: str, params: dict) -> dict:
    logger.info(f"Agent action invoked: {action} params={params}")
    return {"action": action, "status": "logged", "params": params}


async def place_order(ctx, params: dict) -> dict:
    """Place a new broker order — wiring pending full action-runner landing."""
    return _log_invoke("place_order", params)


async def modify_order(ctx, params: dict) -> dict:
    return _log_invoke("modify_order", params)


async def cancel_order(ctx, params: dict) -> dict:
    return _log_invoke("cancel_order", params)


async def cancel_all_orders(ctx, params: dict) -> dict:
    return _log_invoke("cancel_all_orders", params)


async def chase_close_positions(ctx, params: dict) -> dict:
    """
    Close every open position in scope via the adaptive chase engine.
    Delegates to ExpiryEngine primitives once the action runner lands.
    """
    return _log_invoke("chase_close_positions", params)


async def close_position(ctx, params: dict) -> dict:
    """
    One-shot close of a single position with a LIMIT order at current LTP.

    Live mode: wiring pending the action runner landing — logs the
    invocation so the pipeline is exercised end-to-end without touching
    the broker.

    Sim mode: dispatched through `_sim_paper_trade` upstream (execute() in
    this module routes on ctx['sim_mode']), which records an AlgoOrder
    with initial_price = sim's current LTP for the symbol. So this
    handler is the LIVE path only.
    """
    return _log_invoke("close_position", params)


async def monitor_order(ctx, params: dict) -> dict:
    return _log_invoke("monitor_order", params)


async def deactivate_agent(ctx, params: dict) -> dict:
    return _log_invoke("deactivate_agent", params)


async def set_flag(ctx, params: dict) -> dict:
    return _log_invoke("set_flag", params)


async def emit_log(ctx, params: dict) -> dict:
    level   = (params.get("level") or "info").lower()
    message = params.get("message", "")
    getattr(logger, level, logger.info)(f"Agent emit_log: {message}")
    return {"action": "emit_log", "status": "logged", "level": level, "message": message}
