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


def _resolve_mode(_action_type: str, agent, context: dict) -> str:
    """
    Decide how this action should be executed:
      * 'sim'    — agent was fired by the simulator → route to the sim
                   paper-trade writer (SimDriver owns the lifecycle)
      * 'replay' — agent was fired by the replay engine → route to replay
                   paper-trade writer (informational only)
      * 'shadow' — real data, log-only: captures exact Kite payload +
                   basket_margin validation without executing. Prod only.
      * 'paper'  — mode 2: real data, paper order. Reached on dev for
                   every action, on prod when execution.paper_trading_mode
                   is True (master kill-switch) OR agent.trade_mode='paper'.
      * 'live'   — mode 3: real data, real order. Reached on prod when
                   execution.paper_trading_mode is False AND
                   agent.trade_mode='live'.
      * 'noop'   — non-broker action (no gate); the existing handler
                   (send_summary, emit_log, …) runs as-is

    Precedence:
      sim > replay > (prod-branch check) > shadow >
      execution.paper_trading_mode (master kill-switch) > agent.trade_mode
    """
    if context.get("sim_mode"):
        return "sim"
    if context.get("replay_mode"):
        return "replay"
    if _action_type not in BROKER_ACTIONS:
        return "noop"
    from backend.shared.helpers.utils    import is_prod_branch
    from backend.shared.helpers.settings import get_bool
    if not is_prod_branch():
        return "paper"                         # dev never hits broker
    if get_bool("execution.shadow_mode", False):
        return "shadow"
    # Master kill-switch wins over per-agent — operator can force every
    # agent to paper from /admin/live regardless of per-agent settings.
    if get_bool("execution.paper_trading_mode", True):
        return "paper"
    # Manual one-shot triggers (agent fire / Test Fire) set this so the
    # single fire stays paper regardless of the agent's trade_mode.
    if context.get("force_paper"):
        return "paper"
    # Per-agent decides: 'live' goes to the broker, anything else paper.
    return "live" if getattr(agent, "trade_mode", "paper") == "live" else "paper"


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
        mode = _resolve_mode(action_type, agent, context)
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
                # Real broker path. Only reached on main AND with
                # execution.paper_trading_mode = False in /admin/live.
                if action_type in ("chase_close", "chase_close_positions"):
                    await _action_live_chase_close_positions(agent, context, params)
                elif action_type == "place_order":
                    await _action_place_order(context, params)
                elif action_type == "close_position":
                    await _action_live_close_position(agent, context, params)
                elif action_type == "modify_order":
                    await _action_live_modify_order(agent, context, params)
                elif action_type == "cancel_order":
                    await _action_live_cancel_order(agent, context, params)
                elif action_type == "cancel_all_orders":
                    await _action_live_cancel_all_orders(agent, context, params)
                else:
                    logger.warning(f"Agent [{agent.slug}]: live action '{action_type}' has no wired handler")
            else:  # 'noop' — non-broker action
                if action_type == "send_summary":
                    await _action_send_summary(context, params)
                elif action_type == "chase_close":
                    # chase_close is in BROKER_ACTIONS so we'd only get
                    # here if BROKER_ACTIONS is misconfigured — safety net
                    await _action_chase_close(context, params)
                elif action_type == "monitor_order":
                    try:
                        await monitor_order(context, params)
                    except Exception as e:
                        logger.error(f"Agent [{agent.slug}]: monitor_order failed: {e}")
                        continue
                elif action_type == "deactivate_agent":
                    try:
                        await deactivate_agent(context, params)
                    except Exception as e:
                        logger.error(f"Agent [{agent.slug}]: deactivate_agent failed: {e}")
                        continue
                elif action_type == "set_flag":
                    try:
                        await set_flag(context, params)
                    except Exception as e:
                        logger.error(f"Agent [{agent.slug}]: set_flag failed: {e}")
                        continue
                elif action_type == "emit_log":
                    try:
                        await emit_log(context, params)
                    except Exception as e:
                        logger.error(f"Agent [{agent.slug}]: emit_log failed: {e}")
                        continue
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
                # Replay orders are deterministic fills against historical
                # candles — there's no fill lifecycle, so they land FILLED
                # immediately. OPEN would incorrectly imply a pending chase.
                status="FILLED", engine="replay", mode="replay",
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
        # broker.kite.basket_order_margins is a synchronous requests call; run it
        # in a thread executor so the event loop is not blocked.
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, broker.kite.basket_order_margins, [basket_order])
        return True, "basket_margin OK"
    except Exception as e:
        return False, str(e)[:240]


async def run_preflight(account: str, order: dict) -> dict:
    """
    Pre-validate an order before any broker placement.

    Runs four checks in order:
      1. ACCOUNT_UNKNOWN  — account not in Connections map.
      2. SEGMENT_INACTIVE — exchange not in kite.profile()['exchanges'].
      3. QTY_FREEZE       — quantity exceeds the instrument's freeze_qty
                           from the Kite instruments dump.
      4. MARGIN_SHORTFALL — kite.basket_order_margins reports required > available.

    Returns a dict:
      {
        "ok": bool,
        "blocked": [{"code", "reason", "fix", "data"}, ...],
        "diagnostics": {
          "basket_margin_used": float | None,
          "available_margin":   float | None,
          "margin_shortfall":   float | None,
        }
      }

    Never raises — any broker call failure surfaced as a blocker or
    skipped gracefully.
    """
    import asyncio
    import math
    from backend.shared.helpers.connections import Connections

    blocked: list[dict] = []
    diagnostics: dict = {
        "basket_margin_used": None,
        "available_margin":   None,
        "margin_shortfall":   None,
    }

    # ── 1. ACCOUNT_UNKNOWN ────────────────────────────────────────────────
    conns = Connections()
    if account not in conns.conn:
        from backend.shared.helpers.utils import mask_column
        import pandas as pd
        masked = mask_column(pd.Series([account]))[0] if account else account
        blocked.append({
            "code":   "ACCOUNT_UNKNOWN",
            "reason": f"Account {masked} not loaded in broker connections",
            "fix":    "Add the account in /admin/brokers and verify it shows LOADED",
            "data":   {},
        })
        return {"ok": False, "blocked": blocked, "diagnostics": diagnostics}

    kite_conn = conns.conn[account]
    kite = kite_conn.get_kite_conn()
    loop = asyncio.get_running_loop()

    exchange  = str(order.get("exchange", "NFO"))
    symbol    = str(order.get("tradingsymbol") or order.get("symbol", ""))
    qty       = int(order.get("quantity") or order.get("qty") or 0)
    side      = str(order.get("transaction_type") or order.get("side", "BUY"))
    price     = order.get("price") or 0
    product   = str(order.get("product", "NRML"))
    order_type = str(order.get("order_type", "LIMIT"))
    variety   = str(order.get("variety", "regular"))

    # ── 2. SEGMENT_INACTIVE ───────────────────────────────────────────────
    try:
        profile = await loop.run_in_executor(None, kite.profile)
        enabled_exchanges = set(profile.get("exchanges") or [])
        if exchange not in enabled_exchanges:
            blocked.append({
                "code":   "SEGMENT_INACTIVE",
                "reason": f"{exchange} segment not activated on this account",
                "fix":    (f"Activate the {exchange} segment in the Kite developer "
                           "console for this account, then re-test"),
                "data":   {"enabled_exchanges": sorted(enabled_exchanges)},
            })
    except Exception as e:
        logger.debug(f"[PREFLIGHT] profile fetch failed for {account}: {e}")

    # ── 3. QTY_FREEZE ─────────────────────────────────────────────────────
    if exchange in ("NFO", "BFO", "MCX", "CDS") and qty > 0:
        try:
            raw_instruments = await loop.run_in_executor(
                None, kite.instruments, exchange
            )
            freeze_qty: int | None = None
            lot_size: int = 1
            for inst in raw_instruments:
                if inst.get("tradingsymbol") == symbol:
                    freeze_qty = inst.get("freeze_qty") or None
                    lot_size   = int(inst.get("lot_size") or 1)
                    break
            if freeze_qty is not None and qty > int(freeze_qty):
                # How many lots fit in the freeze limit?
                max_qty = int(freeze_qty)
                max_lots = max(1, max_qty // lot_size) if lot_size > 0 else max_qty
                blocked.append({
                    "code":   "QTY_FREEZE",
                    "reason": (f"Quantity {qty} exceeds {symbol} freeze qty "
                               f"{freeze_qty}"),
                    "fix":    (f"Reduce qty to {max_qty:,} "
                               f"({max_lots:,} lot{'s' if max_lots != 1 else ''}) "
                               "or split into multiple orders"),
                    "data":   {
                        "freeze_qty": int(freeze_qty),
                        "lot_size":   lot_size,
                        "requested":  qty,
                    },
                })
        except Exception as e:
            logger.debug(f"[PREFLIGHT] instruments fetch failed for {account}/{exchange}: {e}")

    # ── 4. MARGIN_SHORTFALL ───────────────────────────────────────────────
    basket_order = {
        "exchange":         exchange,
        "tradingsymbol":    symbol,
        "transaction_type": side,
        "quantity":         qty,
        "order_type":       order_type,
        "product":          product,
        "price":            float(price) if price else 0.0,
        "variety":          variety,
    }
    try:
        bm_result = await loop.run_in_executor(
            None, kite.basket_order_margins, [basket_order]
        )
        # Kite returns a list; take the first element.
        if isinstance(bm_result, list) and bm_result:
            bm_result = bm_result[0]
        required  = float((bm_result or {}).get("initial", {}).get("total") or
                          (bm_result or {}).get("required") or 0)

        # Available margin is NOT in the basket_order_margins response —
        # that endpoint only returns the REQUIRED margin breakdown
        # (span, exposure, premium, total). Available lives in a separate
        # call: kite.margins(segment).
        #
        # Segment routing: MCX/NCO → commodity wallet, everything else
        # (NSE/BSE/NFO/BFO/CDS/BCD) → equity wallet. Zerodha keeps the
        # two wallets separate; an equity-only operator hitting a MCX
        # order legitimately can't see commodity margin.
        segment = "commodity" if exchange in ("MCX", "NCO") else "equity"
        available = None
        m_enabled = None
        try:
            # Use the un-segmented kite.margins() call which returns both
            # equity + commodity. Counter-intuitively, kite.margins("equity")
            # with the segment arg returns enabled=False for accounts that
            # the un-segmented call shows as enabled=True with real net.
            # API quirk; documented by Kite as the segment arg requiring
            # a separate API scope. Fall back to segmented if all-call
            # throws.
            try:
                m_all = await loop.run_in_executor(None, kite.margins)
                m = (m_all or {}).get(segment, {})
            except TypeError:
                # Some kite SDK versions don't accept zero args.
                m = await loop.run_in_executor(None, kite.margins, segment)
            m_enabled = bool(m.get("enabled"))
            net = m.get("net")
            if isinstance(net, (int, float)) and not math.isnan(float(net)):
                available = float(net)
        except Exception as e:
            logger.warning(f"[PREFLIGHT] margins({segment}) failed for {account}: {e}")

        diagnostics["basket_margin_used"] = required
        diagnostics["available_margin"]   = available
        diagnostics["margin_shortfall"]   = None

        # Three states for the margin gate:
        #   1. enabled=False (or margins call failed): API key likely
        #      lacks the "Read Margin" permission, or the segment is
        #      not subscribed. We can't reliably know available — DO
        #      NOT block. Let Kite's place_order reject if needed. This
        #      mirrors what happens when the operator places the same
        #      order through kite.zerodha.com directly (Kite's web app
        #      uses cookie auth which has full permissions and lets
        #      the order through).
        #   2. enabled=True + net < required: real shortfall — block.
        #   3. enabled=True + net >= required: pass.
        shortfall = 0.0
        if m_enabled and available is not None:
            shortfall = max(0.0, required - available)
            diagnostics["margin_shortfall"] = shortfall if shortfall > 0 else None
        elif required > 0:
            # Segment-permission gap: log + skip the block.
            logger.info(
                f"[PREFLIGHT] margin check SKIPPED for {account} "
                f"(segment={segment} enabled={m_enabled} available={available}); "
                f"required={required:.2f} — relying on Kite place_order for the real verdict"
            )

        if shortfall > 0 and not math.isnan(shortfall):
            # Estimate the reduced qty that would fit within available margin.
            if required > 0 and available > 0:
                # Infer per-unit margin and solve for max fillable qty aligned
                # to lot_size.
                per_unit = required / qty if qty > 0 else 0
                if per_unit > 0:
                    max_qty_fit = int(available / per_unit)
                    # Round down to nearest lot boundary (if we know lot_size).
                    try:
                        from backend.api.cache import get_or_fetch
                        lot_size_hint = 1
                        instr_cache = None
                        instr_cache_raw = None
                        try:
                            from backend.api.routes.instruments import _fetch_instruments
                            instr_cache = await loop.run_in_executor(
                                None, lambda: None  # avoid re-fetching; use cached
                            )
                        except Exception:
                            pass
                        if lot_size_hint > 0:
                            max_qty_fit = (max_qty_fit // lot_size_hint) * lot_size_hint
                        max_qty_fit = max(0, max_qty_fit)
                        fix_qty = (f" or reduce qty to {max_qty_fit:,}" if max_qty_fit > 0
                                   else "")
                    except Exception:
                        fix_qty = ""
                else:
                    fix_qty = ""
            else:
                fix_qty = ""

            blocked.append({
                "code":   "MARGIN_SHORTFALL",
                "reason": (f"Required margin ₹{required:,.0f} exceeds available "
                           f"₹{available:,.0f} (shortfall ₹{shortfall:,.0f})"),
                "fix":    (f"Add ₹{shortfall:,.0f} more margin to the account"
                           + fix_qty),
                "data":   {
                    "required":   required,
                    "available":  available,
                    "shortfall":  shortfall,
                },
            })
    except Exception as e:
        bm_msg = str(e).lower()
        # basket_margin raised — interpret the error signal.
        if any(k in bm_msg for k in ("margin", "fund", "shortfall", "balance")):
            blocked.append({
                "code":   "MARGIN_SHORTFALL",
                "reason": f"Margin check failed: {str(e)[:160]}",
                "fix":    "Add margin to the account or reduce quantity",
                "data":   {"broker_error": str(e)[:240]},
            })
        else:
            logger.debug(f"[PREFLIGHT] basket_margin raised for {account}: {e}")

    return {
        "ok":          len(blocked) == 0,
        "blocked":     blocked,
        "diagnostics": diagnostics,
    }


async def diagnose_live_failure(kite_or_broker, order: dict, kite_error: str) -> str:
    """
    When kite.place_order raises, run basket_margin to distinguish the
    likely cause. Kite returns "Insufficient permission for that call"
    for several distinct conditions (segment scope, account activation,
    margin shortfall) — basket_margin gives us a second signal:

      - basket_margin succeeds  →  margin OK; place_order failure is
                                   likely a segment-permission issue
      - basket_margin fails with margin/fund/shortfall keywords → margin
      - basket_margin fails with the same generic error              → unclear

    `kite_or_broker` may be either a raw KiteConnect instance or a
    `Broker` adapter (which exposes `.kite`). Returns a one-line
    diagnostic suitable for both the log line and the operator-facing
    HTTP detail.
    """
    import asyncio
    kite = getattr(kite_or_broker, "kite", kite_or_broker)
    basket_order = {
        "exchange":         order.get("exchange", "NFO"),
        "tradingsymbol":    order.get("symbol") or order.get("tradingsymbol"),
        "transaction_type": order.get("side") or order.get("transaction_type"),
        "quantity":         order.get("qty") or order.get("quantity"),
        "order_type":       order.get("order_type", "LIMIT"),
        "product":          order.get("product", "NRML"),
        "price":            order.get("price") or 0,
        "variety":          order.get("variety", "regular"),
    }
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, kite.basket_order_margins, [basket_order])
        return ("margin OK via basket_margin — likely segment permission "
                "(check Account → Segments + API key exchange scope at "
                "developers.kite.trade)")
    except Exception as bm_e:
        bm_msg = str(bm_e)
        low = bm_msg.lower()
        if any(k in low for k in ("margin", "fund", "shortfall", "balance")):
            return f"margin shortfall (basket_margin: {bm_msg[:160]})"
        return f"basket_margin also failed ({bm_msg[:160]}); cause unclear"


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
    from backend.shared.brokers import get_broker

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
            ltp_data = await loop.run_in_executor(
                None, broker.kite.ltp, [f"{exchange}:{symbol}"]
            )
            key = f"{exchange}:{symbol}"
            price = float((ltp_data.get(key) or {}).get("last_price") or 0) or None
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
            # Send a Telegram + email so the operator sees a LIVE agent-
            # initiated order that Kite's basket_margin would have rejected.
            # The earlier path tried `asyncio.create_task(_dispatch(...))`
            # but _dispatch is sync and the arg count was wrong; the outer
            # try/except swallowed both errors silently.
            from backend.shared.helpers.alert_utils       import _dispatch
            from backend.shared.helpers.date_time_utils   import timestamp_display
            lines = [f"• [{b['code']}] {b['reason']} — {b['fix']}"
                     for b in pf["blocked"]]
            header = f"🚫 Order preflight BLOCKED: {side} {qty} {symbol} [{account}]"
            tg_body    = f"<code>{header}\n" + "\n".join(lines) + "</code>"
            email_body = (
                f"<pre style='font-family:monospace;color:#c0392b'>"
                f"{header}\n" + "\n".join(lines) + "</pre>"
            )
            _dispatch(
                'alert', timestamp_display(), tg_body, email_body,
                f"Order blocked — {symbol}",
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
        raise


# ═══════════════════════════════════════════════════════════════════════════
#  LIVE broker action handlers (mode 3)
#
#  Each handler calls the broker via run_in_executor (Kite SDK is sync).
#  On any failure: write AlgoOrder.status='REJECTED' + return error dict
#  so execute() writes an action_failed event rather than bubbling the
#  exception to the agent engine.
# ═══════════════════════════════════════════════════════════════════════════

async def _write_live_order(agent, action_type: str, resolved: dict,
                            broker_order_id: str | None = None,
                            status: str = "OPEN",
                            detail_suffix: str = "") -> int | None:
    """
    Persist one AlgoOrder(mode='live') row.  Returns the row id.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    account  = str(resolved.get("account", ""))
    symbol   = str(resolved.get("symbol", ""))
    side     = str(resolved.get("side", "SELL"))
    qty      = int(resolved.get("qty") or 0)
    price    = resolved.get("price")
    exchange = str(resolved.get("exchange") or "NFO")

    price_str = f"@₹{price:,.2f}" if price is not None else "@MARKET"
    detail = (f"{agent.slug} → {action_type}: {side} {qty} "
              f"{symbol} {price_str} · acct={account}"
              + (f" · {detail_suffix}" if detail_suffix else ""))
    logger.warning(f"[LIVE] {detail}")

    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty,
                initial_price=(float(price) if price is not None else None),
                status=status, engine="live", mode="live",
                broker_order_id=broker_order_id or "",
                detail=detail,
            )
            s.add(row)
            await s.commit()
            return row.id
    except Exception as e:
        logger.error(f"[LIVE] AlgoOrder write failed for {action_type}: {e}")
        return None


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
    from backend.shared.brokers import get_broker

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
    try:
        broker = get_broker(account)
        loop = asyncio.get_running_loop()
        ltp_data = await loop.run_in_executor(
            None, broker.kite.ltp, [f"{exchange}:{symbol}"]
        )
        key = f"{exchange}:{symbol}"
        price = float((ltp_data.get(key) or {}).get("last_price") or 0) or None
    except Exception as e:
        logger.warning(f"[LIVE] close_position LTP fetch failed, proceeding with None price: {e}")

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
            diag = await diagnose_live_failure(broker, diag_order, str(e))
        except Exception:
            diag = "diagnosis unavailable"
        logger.error(f"[LIVE] close_position failed for {account} {exchange}/{symbol} "
                     f"{side} {qty}: {e} | diag: {diag}")
        raise


async def _action_live_modify_order(agent, context: dict, params: dict):
    """
    Modify an open broker order.  Wraps kite.modify_order in run_in_executor.
    Updates the matching AlgoOrder row on success.
    """
    import asyncio
    from backend.shared.brokers import get_broker

    account  = str(params.get("account") or "")
    order_id = str(params.get("order_id") or "")
    variety  = str(params.get("variety") or "regular")

    if not account or not order_id:
        raise ValueError(f"modify_order: account and order_id are required")

    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    kwargs: dict = {}
    for field in ("quantity", "price", "trigger_price", "order_type", "validity"):
        v = params.get(field)
        if v is not None:
            kwargs[field] = v

    try:
        await loop.run_in_executor(
            None,
            lambda: broker.kite.modify_order(variety=variety, order_id=order_id, **kwargs)
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
    from backend.shared.brokers import get_broker
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
            None, broker.kite.cancel_order, variety, order_id
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

    Iterates Connections().conn, calls kite.orders() to get the open
    order list, then kite.cancel_order() for each.  All broker calls are
    wrapped in run_in_executor.  Returns aggregate cancelled count via log.
    """
    import asyncio
    from backend.shared.helpers.connections import Connections

    loop = asyncio.get_running_loop()
    scope_account = str(params.get("account") or "")
    conns = Connections().conn

    total_cancelled = 0
    total_errors = 0

    for acct, kite_conn in conns.items():
        if scope_account and acct != scope_account:
            continue
        try:
            kite = kite_conn.get_kite_conn()
            orders = await loop.run_in_executor(None, kite.orders)
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
                        None, kite.cancel_order, variety, oid
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
    import pandas as pd
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.shared.brokers import get_broker

    scope        = (params.get("scope") or "total").lower()
    scope_acct   = str(params.get("account") or "") if scope == "account" else None
    loop         = asyncio.get_running_loop()

    # Read live positions from context (already fetched by _task_performance).
    df = context.get("df_positions")
    if df is None or (hasattr(df, "empty") and df.empty):
        logger.warning(f"[LIVE] chase_close_positions: no positions in context for agent {agent.slug}")
        return

    try:
        rows: list[dict] = df.to_dict(orient="records")
    except Exception as e:
        logger.error(f"[LIVE] chase_close_positions: could not read df_positions: {e}")
        return

    if scope_acct:
        rows = [r for r in rows if str(r.get("account")) == scope_acct]

    # Filter to non-zero positions only.
    rows = [r for r in rows if int(r.get("quantity") or 0) != 0]

    if not rows:
        logger.warning(f"[LIVE] chase_close_positions: scope matched 0 positions "
                       f"(agent={agent.slug}, scope={scope})")
        return

    chase_tasks = []
    for p in rows:
        acct     = str(p.get("account", ""))
        symbol   = str(p.get("tradingsymbol", ""))
        exchange = str(p.get("exchange") or "NFO")
        qty_held = int(p.get("quantity") or 0)
        side     = "SELL" if qty_held > 0 else "BUY"
        qty      = abs(qty_held)

        # Best effort initial limit price from LTP in context row.
        ltp = p.get("last_price") or p.get("close_price")
        price = float(ltp) if ltp is not None else None

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
        logger.info(f"[LIVE] chase_close_positions: queued {side} {qty} {symbol} [{acct}]")

    # Await all chase tasks concurrently — each manages its own retry loop.
    if chase_tasks:
        results = await asyncio.gather(*chase_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                # Diagnose via basket_margin so the log distinguishes
                # margin-shortfall from segment-permission for this leg.
                p = rows[i]
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


# ═══════════════════════════════════════════════════════════════════════════
#  Grammar-token action handlers (dotted-path resolvers from grammar.py)
#
#  These are the public entry points that GrammarRegistry resolves via
#  dotted path.  They delegate to the _action_* helpers above for live
#  mode; paper/sim/shadow routing is done upstream in execute().
# ═══════════════════════════════════════════════════════════════════════════

def _log_invoke(action: str, params: dict) -> dict:
    logger.info(f"Agent action invoked: {action} params={params}")
    return {"action": action, "status": "logged", "params": params}


async def place_order(ctx, params: dict) -> dict:
    """Place a new broker order."""
    return _log_invoke("place_order", params)


async def modify_order(ctx, params: dict) -> dict:
    return _log_invoke("modify_order", params)


async def cancel_order(ctx, params: dict) -> dict:
    return _log_invoke("cancel_order", params)


async def cancel_all_orders(ctx, params: dict) -> dict:
    return _log_invoke("cancel_all_orders", params)


async def chase_close_positions(ctx, params: dict) -> dict:
    """Close every open position in scope via the adaptive chase engine."""
    return _log_invoke("chase_close_positions", params)


async def close_position(ctx, params: dict) -> dict:
    """
    One-shot close of a single position with a LIMIT order at current LTP.

    Sim / paper / shadow modes are dispatched upstream by execute() before
    this function is reached.  This grammar-token resolver is the LIVE path
    only — actual broker wiring lives in _action_live_close_position above.
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
