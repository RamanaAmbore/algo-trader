"""
Agent action executor — runs automated responses when an agent triggers.

Actions are stored in Agent.actions as a JSON list:
  [{"type": "chase_close", "params": {"exchange": "NFO"}}]

Empty list means alert-only (no action taken).

Module layout (split from the original 2580-line file):
  actions.py          — coordinator: execute(), _resolve_mode(), gate helpers,
                        _maybe_attach_template_from_action(), _write_live_order(),
                        grammar stubs, re-exports of all sub-module symbols.
  actions_preflight.py — preflight helpers + run_preflight() + diagnose_live_failure()
  actions_sim.py       — sim (mode-1), replay (mode-4), shadow (mode-5) writers
  actions_paper.py     — paper-trade writer + dispatcher (mode-2)
  actions_live.py      — live broker handlers (mode-3)
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


def _al_sim_replay_bypass(context: dict) -> bool:
    """Return True when the action should skip the exchange gate (sim/replay)."""
    return bool(context.get("sim_mode") or context.get("replay_mode"))


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
    if _al_sim_replay_bypass(context):
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


async def _dispatch_live_action(agent, action_type: str, params: dict, context: dict) -> None:
    """Route a live-mode action to its broker handler.

    All imports are lazy (inside function body) to preserve the existing
    circular-import avoidance pattern from the original execute() body.
    Exceptions bubble to the caller (execute's outer try/except).
    """
    from backend.api.algo.actions_live import (
        _action_place_order, _action_live_close_position,
        _action_live_modify_order, _action_live_cancel_order,
        _action_live_cancel_all_orders, _action_live_chase_close_positions,
        _action_live_expiry_auto_close,
    )
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


async def _al_run_noop_handler(
    agent, action_type: str, params: dict, context: dict,
) -> bool:
    """Execute a single noop (non-broker) action handler.

    Returns True on success, False on failure.  Swallows exceptions and
    logs them so the outer execute() loop can `continue` on False.
    """
    from backend.api.algo.actions_live import (
        _action_send_summary, _action_chase_close,
    )
    # Non-raising handlers
    if action_type == "send_summary":
        await _action_send_summary(context, params)
        return True
    if action_type == "chase_close":
        # Safety net — chase_close is in BROKER_ACTIONS; reaching here
        # means BROKER_ACTIONS is misconfigured.
        await _action_chase_close(context, params)
        return True

    # Handlers that may raise — wrap individually
    _raising: dict[str, object] = {
        "monitor_order":    monitor_order,
        "deactivate_agent": deactivate_agent,
        "set_flag":         set_flag,
        "emit_log":         emit_log,
    }
    handler = _raising.get(action_type)
    if handler is None:
        logger.warning(f"Agent [{agent.slug}]: unknown action type '{action_type}'")
        return False
    try:
        await handler(context, params)
        return True
    except Exception as e:
        logger.error(f"Agent [{agent.slug}]: {action_type} failed: {e}")
        return False


async def _dispatch_noop_action(agent, action_type: str, params: dict, context: dict) -> bool:
    """Route a noop-mode (non-broker) action to its handler.

    Returns True when the action completed successfully and the caller
    should proceed to log action_success.  Returns False when the handler
    raised an internally-swallowed exception — in that case the caller
    must `continue` (skip success logging) to match the original semantics
    where each inner try/except did `continue` on failure.

    All imports are lazy to preserve the existing circular-import pattern.
    """
    return await _al_run_noop_handler(agent, action_type, params, context)


async def _log_action_success(
    agent, action_type: str, params: dict, tag: str, sim_mode: bool
) -> None:
    """Log a successful action dispatch: logger line + log_event + optional audit."""
    logger.info(f"{tag}Agent [{agent.slug}]: action '{action_type}' completed")
    from backend.api.algo.events import log_event
    await log_event(agent, "action_success", f"{tag}Action: {action_type}",
                    params, sim_mode=sim_mode)
    # Audit trail for every agent-triggered action that mutates state.
    # Skipped when sim — sim_mode actions are already isolated in their own
    # logs + don't touch real broker / DB state beyond agent_events.
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


async def _al_action_failed_audit(
    agent, action_type: str, params: dict, tag: str, sim_mode: bool, exc: Exception,
) -> None:
    """Log action_failed event + optional audit trail (fire-and-forget)."""
    logger.error(f"{tag}Agent [{agent.slug}]: action '{action_type}' failed: {exc}")
    from backend.api.algo.events import log_event
    await log_event(agent, "action_failed",
                    f"{tag}Action: {action_type} — {exc}",
                    params, sim_mode=sim_mode)
    if sim_mode:
        return
    try:
        from backend.api.audit import write_audit_event
        write_audit_event(
            category="agent.action",
            action=f"AGENT_{action_type.upper()}_FAILED",
            actor_username=f"agent:{agent.slug}",
            actor_role="system",
            target_type="agent",
            target_id=str(agent.id) if getattr(agent, "id", None) else None,
            summary=f"{tag}{action_type} failed: {exc}"[:1000],
            status_code=500,
        )
    except Exception:
        pass


async def _al_dispatch_by_mode(
    agent, mode: str, action_type: str, params: dict, context: dict,
) -> bool:
    """Dispatch one action to the appropriate mode handler.

    Returns True when the action succeeded and action_success should be
    logged.  Returns False when a noop handler failed and the loop should
    `continue` (success NOT logged).  Raises on hard errors so the
    outer try/except can log action_failed.
    """
    from backend.api.algo.actions_sim import (
        _sim_paper_trade, _replay_paper_trade, _shadow_trade,
    )
    from backend.api.algo.actions_paper import _paper_trade

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
        await _dispatch_live_action(agent, action_type, params, context)
    else:  # 'noop' — non-broker action
        return await _dispatch_noop_action(agent, action_type, params, context)
    return True


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
            ok = await _al_dispatch_by_mode(agent, mode, action_type, params, context)
            if not ok:
                continue
            await _log_action_success(agent, action_type, params, tag, sim_mode)
        except Exception as e:
            await _al_action_failed_audit(agent, action_type, params, tag, sim_mode, e)


def _build_template_overrides(params: dict) -> dict:
    """Build the override dict from action params (with legacy target_pct mapping)."""
    overrides = {
        "tp_pct":             params.get("tp_pct_override"),
        "sl_pct":             params.get("sl_pct_override"),
        "wing_premium_pct":   params.get("wing_premium_pct_override"),
        "wing_strike_offset": params.get("wing_strike_offset_override"),
    }
    # Backward compat: target_pct (legacy v1 fractional) → tp_pct (% units)
    if overrides["tp_pct"] is None and params.get("target_pct") is not None:
        try:
            overrides["tp_pct"] = float(params["target_pct"]) * 100.0
        except (TypeError, ValueError):
            pass
    return overrides


async def _al_apply_template(
    agent, algo_order_id: int, template_id, template_slug,
    overrides: dict, params: dict,
    parent_account: str, parent_symbol: str, parent_side: str,
    parent_qty: int, parent_exchange: str, parent_price: float,
    apply_path: str,
) -> "dict | None":
    """Call apply_template_to_order and return the result dict (or None on error)."""
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
    return result.to_dict() if result is not None else None


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

    overrides     = _build_template_overrides(params)
    template_id   = params.get("template_id")
    template_slug = params.get("template_slug")

    if template_id is None and not template_slug and not any(
        v is not None for v in overrides.values()
    ):
        return None

    return await _al_apply_template(
        agent, algo_order_id, template_id, template_slug, overrides, params,
        parent_account, parent_symbol, parent_side, parent_qty,
        parent_exchange, parent_price, apply_path,
    )


async def _write_live_order(agent, action_type: str, resolved: dict,
                            broker_order_id: str | None = None,
                            status: str = "OPEN",
                            detail_suffix: str = "") -> int | None:
    """
    Persist one AlgoOrder(mode='live') row.  Returns the row id.

    Defined here (not in actions_live.py) so that test patches on
    ``backend.api.algo.actions._write_live_order`` intercept calls
    originating from _action_live_close_position and
    _action_live_chase_close_positions, which import this function
    lazily from this module at call time.
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


# ═══════════════════════════════════════════════════════════════════════════
#  Re-exports for backwards-compatible imports and patch() paths
#
#  Any external module doing:
#      from backend.api.algo.actions import run_preflight
#  or patching:
#      patch("backend.api.algo.actions.run_preflight", ...)
#  will resolve correctly via these re-exports.
# ═══════════════════════════════════════════════════════════════════════════

from backend.api.algo.actions_preflight import (  # noqa: E402
    run_preflight,
    diagnose_live_failure,
    _live_positions_in_scope,
    _basket_margin_validate,
    _preflight_validate_lots,
    _preflight_validate_account,
    _preflight_build_basket_orders,
    _preflight_leg_required,
    _preflight_parse_basket_margin,
    _preflight_check_segment,
    _preflight_check_qty_freeze,
    _preflight_resolve_available_margin,
    _preflight_margin_shortfall_fix_qty,
    _preflight_handle_positive_margin,
    _preflight_check_margin,
)

from backend.api.algo.actions_sim import (  # noqa: E402
    _sim_prices_for,
    _sim_positions_in_scope,
    _write_sim_order,
    _sim_paper_trade,
    _replay_paper_trade,
    _shadow_trade,
)

from backend.api.algo.actions_paper import (  # noqa: E402
    _write_paper_order,
    _paper_trade,
)

from backend.api.algo.actions_live import (  # noqa: E402
    _action_chase_close,
    _action_send_summary,
    _fetch_ltp,
    _action_place_order,
    _action_live_close_position,
    _action_live_modify_order,
    _action_live_cancel_order,
    _action_live_cancel_all_orders,
    _action_live_chase_close_positions,
    _action_live_expiry_auto_close,
)
