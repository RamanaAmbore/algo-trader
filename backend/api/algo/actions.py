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
    "expiry_auto_close",
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
    # agent to paper from the navbar mode dropdown regardless of
    # per-agent settings.
    if get_bool("execution.paper_trading_mode", False):
        return "paper"
    # Manual one-shot triggers (agent fire / Test Fire) set this so the
    # single fire stays paper regardless of the agent's trade_mode.
    if context.get("force_paper"):
        return "paper"
    # Per-agent decides: 'live' goes to the broker, anything else paper.
    return "live" if getattr(agent, "trade_mode", "paper") == "live" else "paper"


def _action_target_exchanges(action_type: str, params: dict, context: dict) -> list[str]:
    """Phase 23 — return the exchange(s) an action would touch.

    Returns the target exchange for SINGLE-symbol actions that need
    to be gated at the agent layer:
      place_order / modify_order / cancel_order / close_position
        → params.exchange (default NFO matches TicketOrderRequest).

    Returns [] for actions that target multiple symbols at once
    (chase_close_positions / chase_close). Those go through to the
    handler, which iterates positions and lets Kite reject closed-
    exchange ones individually — partial-close behaviour without
    invasive per-position pre-filtering here.

    Returns uppercased exchange codes (or [] when not applicable)."""
    at = (action_type or "").lower()
    if at in ("place_order", "modify_order", "cancel_order", "close_position"):
        ex = (params.get("exchange") or "").upper().strip()
        if not ex:
            # Default mirrors TicketOrderRequest's default: NFO.
            ex = "NFO"
        return [ex]
    if at == "expiry_auto_close":
        # Single-exchange action — gate it so a misconfigured agent
        # (e.g. NFO agent retimed to fire after 15:30) gets a clean
        # "exchange closed" skip rather than a Kite reject.
        ex = (params.get("exchange") or "").upper().strip()
        return [ex] if ex else []
    # chase_close / chase_close_positions: skip gate. Handler iterates
    # positions; each broker call gets per-symbol exchange validation
    # from Kite itself. Partial close falls out naturally.
    return []


def _exchange_gate_passes(action_type: str, params: dict, context: dict) -> tuple[bool, str]:
    """Phase 23 — return (allowed, reason).

    `allowed=True` when EVERY exchange this action targets is open
    (or the gate is bypassed entirely). `reason` is empty on allow,
    a short human-readable explanation on block.

    Bypasses:
      - sim mode (the simulator drives its own clock + has its own
        market_state_preset override)
      - replay mode (historical bars are only available for trading
        hours by definition; gate is a no-op)
      - non-broker actions (returns allowed=True with no checks)
      - empty target list (action_type doesn't touch a broker)

    Per-position partial close (chase_close_positions): when SOME
    positions are on a closed exchange, returns allowed=True but
    annotates `reason` with the skipped count. The caller separately
    filters params before dispatching to the live/paper handler.
    """
    mode = context.get("sim_mode") and "sim" or context.get("replay_mode") and "replay" or None
    if mode in ("sim", "replay"):
        return True, ""

    targets = _action_target_exchanges(action_type, params, context)
    if not targets:
        return True, ""

    from backend.api.algo.agent_engine import _symbol_exchange_open
    # context carries flat nse_open / mcx_open flags from _build_context,
    # not a nested 'segments' list. Pass the whole context dict.
    closed = [e for e in targets if not _symbol_exchange_open(e, context)]
    if not closed:
        return True, ""
    return False, (
        f"exchange{'es' if len(closed) > 1 else ''} closed: "
        f"{', '.join(sorted(set(closed)))}"
    )


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

        # ── Phase 23 — per-order exchange-open gate ────────────────
        # Skip broker-touching actions when the target symbol's
        # exchange segment is closed. Applies to BOTH paper and live
        # (paper is meant to mirror live; Kite would reject anyway).
        # Sim/replay bypass — they drive their own clock.
        allowed, reason = _exchange_gate_passes(action_type, params, context)
        if not allowed:
            logger.info(
                f"{tag}Agent [{agent.slug}]: skipping action '{action_type}' "
                f"— {reason}"
            )
            try:
                from backend.api.algo.events import log_event
                await log_event(
                    agent, "action_skipped",
                    f"{tag}Action {action_type} skipped: {reason}",
                    {"action_type": action_type, "params": params,
                     "skip_reason": reason},
                    sim_mode=sim_mode,
                )
            except Exception as e:
                logger.warning(f"action_skipped log_event failed: {e}")
            continue

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
                # execution.paper_trading_mode = False (navbar LIVE).
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
                elif action_type == "expiry_auto_close":
                    await _action_live_expiry_auto_close(agent, context, params)
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
            # Audit trail for every agent-triggered action that
            # mutates state. Skipped when sim — sim_mode actions are
            # already isolated in their own logs + don't touch real
            # broker / DB state beyond agent_events.
            if not sim_mode:
                try:
                    from backend.api.audit import write_audit_event
                    _sym = (params.get("symbol") or params.get("tradingsymbol") or "")
                    _acct = (params.get("account") or "")
                    write_audit_event(
                        category="agent.action",
                        action=f"AGENT_{action_type.upper()}",
                        actor_username=f"agent:{agent.slug}",
                        actor_role="system",
                        target_type="agent",
                        target_id=str(agent.id) if getattr(agent, "id", None) else None,
                        summary=(f"{tag}{action_type} {_sym} acct={_acct}".strip())[:1000],
                        status_code=200,
                    )
                except Exception as _aud_e:
                    logger.debug(f"agent action audit skipped: {_aud_e}")

        except Exception as e:
            logger.error(f"{tag}Agent [{agent.slug}]: action '{action_type}' failed: {e}")
            from backend.api.algo.events import log_event
            await log_event(agent, "action_failed",
                            f"{tag}Action: {action_type} — {e}",
                            params, sim_mode=sim_mode)
            if not sim_mode:
                try:
                    from backend.api.audit import write_audit_event
                    write_audit_event(
                        category="agent.action",
                        action=f"AGENT_{action_type.upper()}_FAILED",
                        actor_username=f"agent:{agent.slug}",
                        actor_role="system",
                        target_type="agent",
                        target_id=str(agent.id) if getattr(agent, "id", None) else None,
                        summary=f"{tag}{action_type} failed: {e}"[:1000],
                        status_code=500,
                    )
                except Exception:
                    pass


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


async def _maybe_attach_template_from_action(
    agent, action_type: str, params: dict,
    *, algo_order_id: int | None,
    parent_account: str, parent_symbol: str, parent_side: str,
    parent_qty: int, parent_exchange: str, parent_price: float,
    apply_path: str = "auto",
) -> dict | None:
    """Run the unified template-attach pipeline for an agent action.
    Mirrors the OrderTicket path so OrderTicket-driven and agent-driven
    placements behave identically.

    Action params can carry:
      template_id            int  | null
      template_slug          str  | null    (e.g. "default-bull")
      tp_pct_override        float | null
      sl_pct_override        float | null
      wing_premium_pct_override   float | null
      wing_strike_offset_override int   | null

    Backward compat: when `target_pct` (legacy v1 fractional) is set and
    no tp_pct_override is, we map it to tp_pct (% units).

    Returns the AttachResult dict for the agent_events.detail line, or
    None when neither a template nor an override was supplied.
    """
    if algo_order_id is None:
        return None

    overrides = {
        "tp_pct":             params.get("tp_pct_override"),
        "sl_pct":             params.get("sl_pct_override"),
        "wing_premium_pct":   params.get("wing_premium_pct_override"),
        "wing_strike_offset": params.get("wing_strike_offset_override"),
    }
    if overrides["tp_pct"] is None and params.get("target_pct") is not None:
        try:
            overrides["tp_pct"] = float(params["target_pct"]) * 100.0
        except (TypeError, ValueError):
            pass

    template_id   = params.get("template_id")
    template_slug = params.get("template_slug")

    if template_id is None and not template_slug and not any(
        v is not None for v in overrides.values()
    ):
        return None

    try:
        from backend.api.algo.template_attach import apply_template_to_order
        result = await apply_template_to_order(
            template_id=int(template_id) if template_id is not None else None,
            template_slug=str(template_slug) if template_slug else None,
            overrides=overrides,
            parent_account=parent_account,
            parent_symbol=parent_symbol,
            parent_side=parent_side,
            parent_qty=parent_qty,
            parent_exchange=parent_exchange,
            parent_fill_price=parent_price,
            parent_product=str(params.get("product") or "NRML"),
            parent_order_id=algo_order_id,
            apply_path=apply_path,
        )
    except Exception as e:
        logger.error(
            f"[ACTION-TEMPLATE] attach failed for agent={agent.slug} "
            f"order=#{algo_order_id}: {e}"
        )
        return None

    if result is None:
        return None
    return result.to_dict()


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
        from backend.brokers.adapters.kite import to_kite_qty, get_lot_size
        exchange  = order.get("exchange", "NFO")
        symbol    = order.get("symbol") or ""
        raw_qty   = int(order.get("qty") or 0)
        lot_size  = await get_lot_size(exchange, symbol)
        kite_qty  = to_kite_qty(exchange, raw_qty, lot_size)
        basket_order = {
            "exchange":         exchange,
            "tradingsymbol":    symbol,
            "transaction_type": order.get("side"),
            "quantity":         kite_qty,
            "order_type":       "LIMIT",
            "product":          order.get("product", "NRML"),
            "price":            order.get("price"),
            "variety":          order.get("variety", "regular"),
        }
        # basket_order_margins lives on the Broker ABC — every adapter
        # validates orders without placing them. Sync HTTP under the
        # hood, so offload to a thread to keep the event loop free.
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, broker.basket_order_margins, [basket_order])
        return True, "basket_margin OK"
    except Exception as e:
        return False, str(e)[:240]


async def run_preflight(
    account: str,
    order: dict,
    paired_orders: list[dict] | None = None,
) -> dict:
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
    from backend.brokers.connections import Connections

    blocked: list[dict] = []
    diagnostics: dict = {
        "basket_margin_used": None,
        "available_margin":   None,
        "margin_shortfall":   None,
    }

    # ── 0. QTY↔LOT SAFETY GUARDS (G1 multiple + G2 5-lot cap) ────────────
    # Operator 2026-07-01: "the code by mistake ordered 100 lots instead
    # of 1 lot ... happened multiple times." /ticket + /basket enforce
    # the same guards; agent-driven place_order / close_position paths
    # ALSO run this preflight, so the guards fire there too. Only F&O.
    _exch = str(order.get("exchange") or "").upper()
    _sym  = str(order.get("tradingsymbol") or order.get("symbol") or "").upper()
    try:
        _qty_check = int(order.get("quantity") or order.get("qty") or 0)
    except Exception:
        _qty_check = 0
    if _exch in ("NFO", "MCX", "CDS", "BFO") and _qty_check > 0 and _sym:
        try:
            from backend.brokers.adapters.kite import get_lot_size as _pf_get_lot_size
            _pf_lot = int(await _pf_get_lot_size(_exch, _sym) or 0)
        except Exception:
            _pf_lot = 0
        if _pf_lot > 1:
            if _qty_check % _pf_lot != 0:
                blocked.append({
                    "code": "LOT_MULTIPLE",
                    "reason": (
                        f"qty={_qty_check} is not a multiple of "
                        f"lot_size={_pf_lot} (would be "
                        f"{_qty_check / _pf_lot:.2f} lots)"
                    ),
                    "fix": (
                        f"send qty={_pf_lot} for 1 lot, or N × {_pf_lot} for N lots"
                    ),
                    "data": {"qty": _qty_check, "lot_size": _pf_lot},
                })
            else:
                _pf_lots = _qty_check // _pf_lot
                if _pf_lots > 5:
                    blocked.append({
                        "code": "FAT_FINGER_5_LOT_CAP",
                        "reason": (
                            f"{_pf_lots} lots exceeds the 5-lot safety cap "
                            f"(qty={_qty_check}, lot_size={_pf_lot})"
                        ),
                        "fix": (
                            "split into ≤5-lot orders or contact ops to raise the cap"
                        ),
                        "data": {"qty": _qty_check, "lot_size": _pf_lot,
                                 "lots": _pf_lots, "cap": 5},
                    })
        elif _pf_lot == 0 and _exch in ("MCX", "NCO"):
            # MCX/NCO cache miss — deny (no real MCX contract has lot_size ≤ 1).
            blocked.append({
                "code": "LOT_SIZE_UNKNOWN",
                "reason": (
                    f"lot_size unknown for {_exch}/{_sym} — instruments cache "
                    f"missed. Refusing to send raw qty as lots."
                ),
                "fix": "retry after the instruments cache warms (≤5 s)",
                "data": {"qty": _qty_check, "exchange": _exch, "symbol": _sym},
            })
    # If any guard tripped, short-circuit remaining checks — clean 400 up top.
    if blocked:
        return {"ok": False, "blocked": blocked, "diagnostics": diagnostics}

    # ── 1. ACCOUNT_UNKNOWN ────────────────────────────────────────────────
    conns = Connections()
    loaded_accounts: set[str] = set(conns.conn.keys())
    # Cutover branch — local Connections is empty when conn_service owns
    # the sessions, so consult /internal/accounts for the canonical list.
    from backend.brokers.client import is_cutover_on
    if is_cutover_on() and not loaded_accounts:
        from backend.brokers.client.remote_broker import list_remote_accounts
        loaded_accounts = {r["account"] for r in list_remote_accounts() if r.get("account")}
    if account not in loaded_accounts:
        from backend.shared.helpers.utils import mask_account
        masked = mask_account(account) if account else account
        blocked.append({
            "code":   "ACCOUNT_UNKNOWN",
            "reason": f"Account {masked} not loaded in broker connections",
            "fix":    "Add the account in /admin/brokers and verify it shows LOADED",
            "data":   {},
        })
        return {"ok": False, "blocked": blocked, "diagnostics": diagnostics}

    # Resolve via the Broker registry — every method below is on the
    # Broker ABC (profile / instruments / basket_order_margins / margins),
    # so this function is broker-agnostic. When a Groww or Dhan account
    # lands, no further change here is needed.
    from backend.brokers.registry import get_broker
    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    exchange  = str(order.get("exchange", "NFO"))
    symbol    = str(order.get("tradingsymbol") or order.get("symbol", ""))
    qty       = int(order.get("quantity") or order.get("qty") or 0)
    side      = str(order.get("transaction_type") or order.get("side", "BUY"))
    price     = order.get("price") or 0
    product   = str(order.get("product", "NRML"))
    order_type = str(order.get("order_type", "LIMIT"))
    variety   = str(order.get("variety", "regular"))

    # ── Stage 1: build inputs (cheap, synchronous) ────────────────────────
    # Done before the broker-call fan-out so basket_orders is ready when
    # the parallel gather fires. get_lot_size + normalise_qty are
    # cache-hits; no broker network.
    from backend.brokers.adapters.kite import get_lot_size
    _lot_size = await get_lot_size(exchange, symbol)
    _broker_qty = broker.normalise_qty(exchange, qty, _lot_size)
    basket_order = {
        "exchange":         exchange,
        "tradingsymbol":    symbol,
        "transaction_type": side,
        "quantity":         _broker_qty,
        "order_type":       order_type,
        "product":          product,
        "price":            float(price) if price else 0.0,
        "variety":          variety,
    }
    # Paired legs (typically the template's wing) factored into the
    # basket. Kite's basket_order_margins returns the NET margin across
    # every leg, so a short option + protective long wing reads as the
    # capped spread margin instead of the naked SPAN. Operator sees the
    # actual margin they'll be charged, not a scarier naked-short
    # number.
    basket_orders = [basket_order]
    for pl in paired_orders or []:
        try:
            _pl_exchange = str(pl.get("exchange") or exchange)
            _pl_symbol   = str(pl.get("tradingsymbol") or pl.get("symbol") or "")
            if not _pl_symbol:
                continue
            _pl_qty = int(pl.get("quantity") or 0)
            if _pl_qty <= 0:
                continue
            _pl_lot = await get_lot_size(_pl_exchange, _pl_symbol)
            basket_orders.append({
                "exchange":         _pl_exchange,
                "tradingsymbol":    _pl_symbol,
                "transaction_type": str(pl.get("transaction_type") or pl.get("side") or "BUY"),
                "quantity":         broker.normalise_qty(_pl_exchange, _pl_qty, _pl_lot),
                "order_type":       str(pl.get("order_type") or "MARKET"),
                "product":          str(pl.get("product") or product),
                "price":            float(pl.get("price") or 0),
                "variety":          str(pl.get("variety") or "regular"),
            })
        except Exception as _e:
            logger.debug(f"[PREFLIGHT] paired leg skipped: {_e}")

    # ── Stage 2: fan out 4 independent broker calls in parallel ──────────
    # All four are orthogonal — no data dependency between them. Pre-fix
    # this section ran sequentially via four `await run_in_executor`
    # calls, costing ~800-1200ms on Kite (each round-trip ~200-300ms).
    # Now they're gathered; total time = max(individual call), typically
    # ~300ms. Operator's reported "order placement deteriorated" pain
    # tracks back to this section accumulating across recent slices.
    segment = "commodity" if exchange in ("MCX", "NCO") else "equity"

    async def _fetch_profile():
        if broker.broker_id != "zerodha_kite":
            return None
        try:
            return await loop.run_in_executor(None, broker.profile)
        except Exception as e:
            logger.debug(f"[PREFLIGHT] profile fetch failed for {account}: {e}")
            return None

    async def _fetch_instruments():
        if exchange not in ("NFO", "BFO", "MCX", "CDS") or qty <= 0:
            return None
        try:
            return await loop.run_in_executor(None, broker.instruments, exchange)
        except Exception as e:
            logger.debug(f"[PREFLIGHT] instruments fetch failed for {account}/{exchange}: {e}")
            return None

    async def _fetch_basket_margin():
        # Surface the exception so the existing MARGIN_SHORTFALL
        # handler downstream can produce its diagnostic. We return
        # the exception object on failure (the caller branches on
        # isinstance(result, Exception)).
        try:
            return await loop.run_in_executor(
                None, broker.basket_order_margins, basket_orders
            )
        except Exception as e:
            return e

    async def _fetch_account_margins():
        try:
            # Un-segmented call first (returns both wallets; some
            # accounts report enabled=True there but False on the
            # segmented call due to a Kite scope quirk).
            try:
                m_all = await loop.run_in_executor(None, broker.margins)
                return (m_all or {}).get(segment, {}), None
            except TypeError:
                return await loop.run_in_executor(
                    None, broker.margins, segment), None
        except Exception as e:
            return None, str(e)

    profile_res, instruments_res, bm_res, margins_res = await asyncio.gather(
        _fetch_profile(),
        _fetch_instruments(),
        _fetch_basket_margin(),
        _fetch_account_margins(),
    )

    # ── Apply segment-inactive gate from profile result ──────────────────
    if profile_res is not None:
        enabled_exchanges = set(profile_res.get("exchanges") or [])
        if enabled_exchanges and exchange not in enabled_exchanges:
            blocked.append({
                "code":   "SEGMENT_INACTIVE",
                "reason": f"{exchange} segment not activated on this account",
                "fix":    (f"Activate the {exchange} segment in the Kite developer "
                           "console for this account, then re-test"),
                "data":   {"enabled_exchanges": sorted(enabled_exchanges)},
            })

    # ── Apply qty-freeze gate from instruments result ────────────────────
    if instruments_res is not None:
        freeze_qty: int | None = None
        lot_size: int = 1
        for inst in instruments_res:
            if inst.get("tradingsymbol") == symbol:
                freeze_qty = inst.get("freeze_qty") or None
                lot_size   = int(inst.get("lot_size") or 1)
                break
        if freeze_qty is not None and qty > int(freeze_qty):
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

    # ── Margin-shortfall gate (basket_order_margins + account margins) ───
    if isinstance(bm_res, Exception):
        # Existing fallback below the try-block handles broker
        # unreachable / SDK error — preserve that path by raising
        # through the original except branch.
        bm_exception = bm_res
    else:
        bm_exception = None
        bm_result = bm_res

    try:
        if bm_exception is not None:
            raise bm_exception
        # Kite's /margins/basket returns one entry per input leg. Each
        # has BOTH `initial.total` (bare margin for this leg in
        # isolation) AND `final.total` (per-leg margin after the basket
        # hedge offset). Audit fix: summing `initial.total` overstated
        # paired SELL+wing margin by ignoring the spread offset —
        # operator was seeing the naked-short number even with the
        # protective wing factored in. Prefer `final.total` when the
        # broker ships it; fall back to `initial.total` otherwise.
        def _leg_required(entry: dict) -> float:
            if not isinstance(entry, dict):
                return 0.0
            for branch in ("final", "initial"):
                slot = (entry.get(branch) or {}).get("total")
                if slot is not None:
                    try:
                        return float(slot)
                    except (TypeError, ValueError):
                        pass
            try:
                return float(entry.get("required") or 0)
            except (TypeError, ValueError):
                return 0.0

        if isinstance(bm_result, list) and bm_result:
            if len(bm_result) > 1:
                required = float(sum(_leg_required(r) for r in bm_result))
                bm_result = {"required": required, "_legs": bm_result}
            else:
                bm_result = bm_result[0]
                required  = _leg_required(bm_result)
        else:
            required = _leg_required(bm_result if isinstance(bm_result, dict) else {})

        # Initialise locals used across both the negative-margin path and
        # the normal path below so the `if shortfall > 0` block at the
        # end always has valid bindings even when we take the anomaly branch.
        available: float | None = None
        shortfall: float = 0.0

        # ── Negative-margin sanity check ────────────────────────────────
        # Kite's basket_order_margins can return a negative `required` value
        # when existing positions on the account NET with the new leg to
        # release margin (deep-OTM short option positions carrying "credit
        # margin" at the basket level). That's a LEGITIMATE outcome — the
        # operator receives premium and the basket's total margin drops.
        #
        # The original safety-blocker landed on 2026-06-30 to catch the
        # "qty sent in lots instead of contracts" bug that produced grossly
        # over-sized negative margins (up to -₹8.5cr on a 1-lot order).
        # That bug is now prevented at the source by `translate_qty` (same
        # commit 29f3ef58), so this branch no longer needs to block — the
        # qty unit is guaranteed correct before the broker call.
        #
        # We keep the WARNING log + diagnostic surfacing so the operator
        # sees any unusual value; Kite's own place_order remains the
        # ultimate gate for insufficient funds.
        if required < 0:
            logger.warning(
                f"[PREFLIGHT] negative basket margin for {account}/{symbol}: "
                f"required={required:.2f} — treating as legitimate netting "
                f"credit (qty={_broker_qty} contracts is unit-safe via "
                f"translate_qty). Kite's place_order will reject if the "
                f"actual SPAN check fails."
            )
            diagnostics["basket_margin_used"]  = required
            diagnostics["available_margin"]    = None
            diagnostics["margin_shortfall"]    = None
            diagnostics["negative_margin_note"] = (
                "Broker returned a credit basket margin — usually indicates "
                "existing positions net with the new leg. Broker will still "
                "verify at order-placement time."
            )

        else:
            # Available margin came from `_fetch_account_margins` in the
            # earlier gather. `margins_res` is `(seg_dict, err_str_or_None)`.
            # Segment routing: MCX/NCO → commodity wallet, everything else
            # (NSE/BSE/NFO/BFO/CDS/BCD) → equity wallet. Zerodha keeps the
            # two wallets separate.
            m, _m_err = (margins_res or (None, None))
            m = m or {}
            available = None
            m_enabled = None
            if _m_err:
                logger.warning(f"[PREFLIGHT] margins({segment}) failed for {account}: {_m_err}")
            else:
                m_enabled = bool(m.get("enabled"))
                net = m.get("net")
                if isinstance(net, (int, float)) and not math.isnan(float(net)):
                    available = float(net)

            diagnostics["basket_margin_used"] = required
            diagnostics["available_margin"]   = available
            diagnostics["margin_shortfall"]   = None

            # ── Available-is-zero gate ────────────────────────────────────
            # When available=0 AND required>0 AND segment is enabled, the
            # order has zero chance of going through — block immediately.
            # Pre-fix: shortfall = max(0, required − 0) = required > 0 would
            # have blocked. This path is a belt-and-suspenders guard for the
            # edge case where `net` parses as 0.0 when the real balance is
            # non-zero due to a Kite API quirk (unreachable margins endpoint
            # returns {} → available stays None, not 0). Explicit 0.0 check
            # only fires when we DID get a numeric 0, not when we skipped.
            if m_enabled and available == 0.0 and required > 0:
                logger.warning(
                    f"[PREFLIGHT] available_margin=0 with required={required:.2f} "
                    f"for {account} — blocking as INSUFFICIENT_FUNDS."
                )
                blocked.append({
                    "code":   "INSUFFICIENT_FUNDS",
                    "reason": (
                        f"Available margin is ₹0 but order requires "
                        f"₹{required:,.0f}. Account has no usable balance "
                        f"in the {segment} segment."
                    ),
                    "fix": (
                        f"Add at least ₹{required:,.0f} to the {segment} wallet "
                        f"before placing this order."
                    ),
                    "data": {"required": required, "available": 0.0},
                })
                diagnostics["margin_shortfall"] = required

            # Four states for the margin gate:
            #   1. enabled=False (or margins call failed): API key likely
            #      lacks the "Read Margin" permission, or the segment is
            #      not subscribed. We can't reliably know available — DO
            #      NOT block. Let Kite's place_order reject if needed. This
            #      mirrors what happens when the operator places the same
            #      order through kite.zerodha.com directly (Kite's web app
            #      uses cookie auth which has full permissions and lets
            #      the order through).
            #   2. enabled=True + net = 0: blocked above as INSUFFICIENT_FUNDS.
            #   3. enabled=True + net < required: real shortfall — block.
            #   4. enabled=True + net >= required: pass.
            # (shortfall initialised to 0.0 before the if/else above)
            if m_enabled and available is not None and available != 0.0:
                shortfall = max(0.0, required - available)
                diagnostics["margin_shortfall"] = shortfall if shortfall > 0 else None
            elif required > 0 and not (m_enabled and available == 0.0):
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


async def diagnose_live_failure(broker, order: dict, kite_error: str) -> str:
    """
    When a place_order call raises, re-run basket_order_margins to
    distinguish the likely root cause:

      - basket_margin succeeds  →  margin OK; the failure was likely a
                                   segment-permission issue
      - basket_margin fails with margin/fund/shortfall keywords → margin
      - basket_margin fails with the same generic error → unclear

    `broker` is a `Broker` adapter from the registry. For backwards
    compatibility we still accept a raw SDK handle and reach for its
    `basket_order_margins` method — callers should pass the adapter.
    """
    import asyncio
    from backend.brokers.adapters.kite import get_lot_size
    # Accept either a Broker adapter or a legacy SDK handle.
    basket_margin_fn = (
        broker.basket_order_margins
        if hasattr(broker, "basket_order_margins")
        else getattr(broker, "kite", broker).basket_order_margins
    )
    normalise = (
        broker.normalise_qty
        if hasattr(broker, "normalise_qty")
        else (lambda _exch, _qty, _ls: _qty)
    )
    _exch   = order.get("exchange", "NFO")
    _sym    = order.get("symbol") or order.get("tradingsymbol") or ""
    _raw_q  = int(order.get("qty") or order.get("quantity") or 0)
    _ls     = await get_lot_size(_exch, _sym)
    _bq     = normalise(_exch, _raw_q, _ls)
    basket_order = {
        "exchange":         _exch,
        "tradingsymbol":    _sym,
        "transaction_type": order.get("side") or order.get("transaction_type"),
        "quantity":         _bq,
        "order_type":       order.get("order_type", "LIMIT"),
        "product":          order.get("product", "NRML"),
        "price":            order.get("price") or 0,
        "variety":          order.get("variety", "regular"),
    }
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, basket_margin_fn, [basket_order])
        return ("margin OK via basket_margin — likely segment permission "
                "(check Account → Segments + API key exchange scope at "
                "the broker's developer console)")
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

    if action_type == "expiry_auto_close":
        # Paper-mode dry-run: read live positions, filter by exchange,
        # filter to F&O contracts expiring today, and write a paper
        # order per matched row. The MCX hedging-net filter is NOT
        # applied in paper mode — operators reviewing the paper book
        # want to see EVERY ITM/NTM expiring leg the live path would
        # consider; the live ExpiryEngine path is where the hedging
        # check actually fires.
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
    from backend.brokers import get_broker

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
                None, broker.ltp, [f"{exchange}:{symbol}"]
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
                agent_id=getattr(agent, "id", None),
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
    from backend.brokers import get_broker

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
            None, broker.ltp, [f"{exchange}:{symbol}"]
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
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.brokers import get_broker

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


async def expiry_auto_close(ctx, params: dict) -> dict:
    """Run ExpiryEngine scan + close restricted to one exchange.

    Grammar-token stub — the real wiring lives in
    `_action_live_expiry_auto_close` (live path) and the paper/sim
    paths in `_paper_trade` / `_sim_paper_trade`. execute() dispatches
    by mode before reaching this resolver.
    """
    return _log_invoke("expiry_auto_close", params)


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
