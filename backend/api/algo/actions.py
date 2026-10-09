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

import asyncio
import importlib
import sys
from typing import NamedTuple

from backend.shared.helpers.ramboq_logger import get_logger
from backend.api.algo.expr_eval import eval_expr, ExprError
from backend.api.algo.grammar import get_action_params_schema

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


class _HandlerRef(NamedTuple):
    """Deferred (module, attr) reference to an action handler.

    Registries store NAMES, not function objects, and `_resolve_handler`
    re-resolves the attribute off the live module on every single call
    (never cached). This is deliberate: every existing dispatch test
    patches a handler via `unittest.mock.patch("<module>.<attr>", ...)`
    — the same pattern the original per-call
    `from <module> import <attr>` lazy-import satisfied. Binding the
    function object into the registry once at build time would capture
    a stale pre-patch reference that `patch()` could no longer reach,
    silently breaking those tests (and any future one written the same
    way). `swallow_errors` is only meaningful for `_NOOP_ACTION_HANDLERS`
    entries — see `_al_run_noop_handler`.
    """
    module: str
    attr: str
    swallow_errors: bool = False


def _resolve_handler(ref: _HandlerRef):
    """Resolve a `_HandlerRef` to its current callable, read fresh each call."""
    mod = sys.modules.get(ref.module) or importlib.import_module(ref.module)
    return getattr(mod, ref.attr)


# action_type → live (mode-3) broker handler. `chase_close` and
# `chase_close_positions` are a deliberate alias — both route to the
# same positions-sweep handler.
_LIVE_ACTION_HANDLERS: dict[str, _HandlerRef] = {
    "place_order":           _HandlerRef("backend.api.algo.actions_live", "_action_place_order"),
    "close_position":        _HandlerRef("backend.api.algo.actions_live", "_action_live_close_position"),
    "modify_order":          _HandlerRef("backend.api.algo.actions_live", "_action_live_modify_order"),
    "cancel_order":          _HandlerRef("backend.api.algo.actions_live", "_action_live_cancel_order"),
    "cancel_all_orders":     _HandlerRef("backend.api.algo.actions_live", "_action_live_cancel_all_orders"),
    "chase_close":           _HandlerRef("backend.api.algo.actions_live", "_action_live_chase_close_positions"),
    "chase_close_positions": _HandlerRef("backend.api.algo.actions_live", "_action_live_chase_close_positions"),
    "expiry_auto_close":     _HandlerRef("backend.api.algo.actions_live", "_action_live_expiry_auto_close"),
}


# action_type → noop (non-broker) handler. `swallow_errors=False` for
# `send_summary` preserves its EXISTING un-wrapped behavior (an
# exception propagates out of `_al_run_noop_handler` to `execute()`'s
# outer try/except → `_al_action_failed_audit`).
# `swallow_errors=True` for the other four preserves their existing
# wrapped behavior (exception is logged + swallowed, handler returns
# False, `execute()` continues without logging success or failure).
# `chase_close` was REMOVED from this dict (2026-10 cleanup) — it was
# dead code: `chase_close` is in `BROKER_ACTIONS` (above), so
# `_resolve_mode` always routes it to `_LIVE_ACTION_HANDLERS` first;
# this noop entry's own comment admitted reaching it meant
# BROKER_ACTIONS was already misconfigured. Its target function,
# `_action_chase_close` in actions_live.py, was deleted alongside it.
_NOOP_ACTION_HANDLERS: dict[str, _HandlerRef] = {
    "send_summary":     _HandlerRef("backend.api.algo.actions_live", "_action_send_summary", False),
    "monitor_order":    _HandlerRef(__name__, "monitor_order", True),
    "deactivate_agent": _HandlerRef(__name__, "deactivate_agent", True),
    "set_flag":         _HandlerRef(__name__, "set_flag", True),
    "emit_log":         _HandlerRef(__name__, "emit_log", True),
}


async def _dispatch_live_action(agent, action_type: str, params: dict, context: dict) -> None:
    """Route a live-mode action to its broker handler via `_LIVE_ACTION_HANDLERS`.

    Handler names are resolved fresh off their module on every call (see
    `_resolve_handler`), preserving the original lazy-import-per-call
    semantics. Exceptions bubble to the caller (execute's outer
    try/except).
    """
    ref = _LIVE_ACTION_HANDLERS.get(action_type)
    if ref is None:
        logger.warning(f"Agent [{agent.slug}]: live action '{action_type}' has no wired handler")
        return
    handler = _resolve_handler(ref)
    await handler(agent, context, params)


async def _al_run_noop_handler(
    agent, action_type: str, params: dict, context: dict,
) -> bool:
    """Execute a single noop (non-broker) action handler via `_NOOP_ACTION_HANDLERS`.

    Returns True on success, False on failure. `send_summary`/`chase_close`
    (ref.swallow_errors=False) are NOT wrapped — an exception propagates
    to the caller, matching their original un-wrapped behavior. The
    remaining handlers (swallow_errors=True) wrap individually, logging
    and returning False on failure, so the outer execute() loop can
    `continue`.
    """
    ref = _NOOP_ACTION_HANDLERS.get(action_type)
    if ref is None:
        logger.warning(f"Agent [{agent.slug}]: unknown action type '{action_type}'")
        return False
    handler = _resolve_handler(ref)
    if not ref.swallow_errors:
        await handler(context, params)
        return True
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


# Fields that represent a COUNT (lots/contracts), not a continuous price or
# percentage. `order_fields.yaml`'s shared catalog types every numeric
# field identically as plain `"number"` — `qty` and `price` share the
# exact same shape — so this int-vs-float distinction cannot be read off
# the schema itself. Hand-maintained here instead: these are the two field
# names (across place_order/modify_order's params_schema) that represent a
# literal order quantity. A non-integer expression result landing on one
# of these is a real defect (half a lot/contract is meaningless), so it is
# rejected rather than silently truncated — see `resolve_action_params`.
_INTEGER_ONLY_PARAM_KEYS = frozenset({"qty", "new_qty"})


def resolve_action_params(action_type: str, params: dict, context: dict) -> dict:
    """
    Resolve any expression-string param values for this action into real
    numbers/booleans via `expr_eval.eval_expr`, before the action is
    dispatched to its sim/paper/live/noop handler.

    Activation rule (zero-sigil, zero behavior change for every existing
    agent): a param is expression-evaluated **iff all three** hold:
      (a) the action's params_schema entry for that key has
          `token_ref_ok: true`
      (b) that entry's `type` is `"number"` or `"boolean"` (never
          `"string"`/`"enum"` — keeps fields like `account`/`symbol` safe
          even if one of them ever carries a stale `token_ref_ok` flag)
      (c) the actual value in `params` for that key is a Python `str`

    Any value that is already a native JSON number/bool is left
    COMPLETELY UNTOUCHED — every currently-seeded agent stores native JSON
    numbers/bools, never expression strings, so this is a true no-op for
    all existing agents.

    Namespace scope (deliberately narrow): only sibling keys already
    present as literal `int`/`float`/`bool` values in the SAME action's
    own `params` dict, in the dict's natural iteration order.
    Cross-action / condition-match-value binding is explicitly out of
    scope for this phase.

    Returns a NEW dict (`dict(params)` with only the resolved keys
    overwritten) — never mutates `params` in place, since it may be the
    same object backing the agent's ORM row.
    """
    if not isinstance(params, dict) or not params:
        return params

    schema = get_action_params_schema(action_type)
    if not schema:
        return params

    resolved = dict(params)
    for key, spec in schema.items():
        if not isinstance(spec, dict) or not spec.get("token_ref_ok"):
            continue
        field_type = spec.get("type")
        if field_type not in ("number", "boolean"):
            continue
        value = params.get(key)
        if not isinstance(value, str):
            continue  # native JSON number/bool (or absent) — untouched

        namespace = {
            k: v for k, v in params.items()
            if k != key and isinstance(v, (int, float, bool))
        }
        try:
            result = eval_expr(value, namespace)
        except ExprError:
            raise
        except Exception as e:  # defense in depth — eval_expr itself only
            # ever raises ExprError, but never let anything else escape.
            raise ExprError(f"error resolving {action_type}.{key}: {e}") from e

        # `bool` is an `int` subclass in Python — check it BEFORE the
        # numeric check so a relational/logical expression (`"a==b"`)
        # landing on a numeric field never silently becomes qty=1/qty=0.
        if field_type == "boolean":
            if not isinstance(result, bool):
                raise ExprError(
                    f"{action_type}.{key}: expression must evaluate to a "
                    f"boolean, got {type(result).__name__}"
                )
        else:  # field_type == "number"
            if isinstance(result, bool):
                raise ExprError(
                    f"{action_type}.{key}: expression evaluated to a "
                    f"boolean, not a number — refusing to coerce to 0/1"
                )
            if not isinstance(result, (int, float)):
                raise ExprError(
                    f"{action_type}.{key}: expression must evaluate to a "
                    f"number, got {type(result).__name__}"
                )
            if key in _INTEGER_ONLY_PARAM_KEYS and isinstance(result, float):
                if not result.is_integer():
                    raise ExprError(
                        f"{action_type}.{key}: expression must evaluate to "
                        f"a whole number, got {result}"
                    )
                result = int(result)

        resolved[key] = result

    return resolved


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
            params = resolve_action_params(action_type, params, context)
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
        from backend.api.routes.orders_place import _get_template_attach_lock
        # Same per-row lock _fire_template_attach_on_fill uses — now that
        # the live apply_plan_live chain runs off the event loop
        # (asyncio.to_thread), an agent-fired place_order's own template
        # attach can genuinely interleave with a concurrent postback/chase-
        # terminal attach on the same row and place a duplicate live GTT
        # without this.
        _row_lock = await _get_template_attach_lock(algo_order_id)
        async with _row_lock:
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
                parent_agent_id=getattr(agent, "id", None),
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
            row_id = row.id
        try:
            from backend.api.algo.order_events import write_event
            asyncio.create_task(write_event(
                row_id, "agent_trigger",
                f"{getattr(agent, 'slug', '?')}: {action_type}",
            ))
        except Exception:
            pass
        return row_id
    except Exception as e:
        logger.error(f"[LIVE] AlgoOrder write failed for {action_type}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Non-broker noop action handlers (monitor_order / deactivate_agent /
#  set_flag / emit_log)
#
#  These are genuinely dispatched via _NOOP_ACTION_HANDLERS in
#  _al_run_noop_handler() above, AND are the registry resolver targets for
#  these 4 tokens in agent_grammar.yaml (GrammarRegistry.actions[token]["fn"]
#  resolves to these same functions — harmless duplication, not a competing
#  path, since nothing calls REGISTRY.action()).
#  (2026-10-08: the 7 dead broker-action grammar-resolver stubs that used
#  to live here — place_order/modify_order/cancel_order/cancel_all_orders/
#  chase_close_positions/expiry_auto_close/close_position — were deleted;
#  real broker dispatch has always run through _LIVE_ACTION_HANDLERS /
#  _dispatch_live_action() above, not through this section. See CLAUDE.md
#  "Agent action-dispatch SSOT" note.)
# ═══════════════════════════════════════════════════════════════════════════

def _log_invoke(action: str, params: dict) -> dict:
    logger.info(f"Agent action invoked: {action} params={params}")
    return {"action": action, "status": "logged", "params": params}


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
