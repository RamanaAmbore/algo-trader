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

import asyncio

from backend.shared.helpers.ramboq_logger import get_logger
from backend.api.algo.actions_preflight import run_preflight, diagnose_live_failure

logger = get_logger(__name__)


# `_action_send_summary` and `_action_live_send_market_status` (plus its
# private, exclusive helpers `_market_status_for_segment` /
# `_holiday_reason` / `_format_market_status_message`) RELOCATED to
# backend/api/algo/grammar_tokens/ (Wave 1 of the DSL-token-unification
# plan, Part A — actions). Re-imported immediately below via the module's
# own backward-compatible re-export, so `_NOOP_ACTION_HANDLERS`'
# `_HandlerRef("backend.api.algo.actions_live", "_action_send_summary", ...)`
# entries, and every `patch("backend.api.algo.actions_live._holiday_reason",
# ...)` call site in test_market_status_alert.py, keep resolving unchanged.
# See grammar_tokens/action_send_summary.py, action_send_market_status.py.
from backend.api.algo.grammar_tokens.action_send_summary import _action_send_summary  # noqa: E402
from backend.api.algo.grammar_tokens.action_send_market_status import (  # noqa: E402
    _market_status_for_segment,
    _holiday_reason,
    _format_market_status_message,
    _action_live_send_market_status,
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
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{context.get('agent_slug', 'place_order')}",
            error=f"preflight blocked: {reasons[:200]}",
        )
    except Exception as _e:
        logger.warning(f"Preflight-block notification failed: {_e}")


async def _on_algo_order_write_failure(
    agent_shim, action_type: str,
    account: str, symbol: str, exchange: str, side: str, qty: int, price,
) -> None:
    """Fail closed when the AlgoOrder row write failed (DB exception).

    `_write_live_order` / `_place_order_write_intent` return None on any DB
    exception (constraint violation, rollback, timeout). Proceeding to call
    `chase_order()` in that case would place a REAL broker order with no
    AlgoOrder row behind it — no reconcile, no template attach, the
    repeated-rejection hold can never fire, and the operator has zero
    visibility into it. This is the exact same incident class as AlgoOrder
    #1088 (a live order placed, chased for ~12 minutes, never visible,
    never reconciled) — already fixed for the manual-ticket path
    (`orders_place.py:ticket_order_handler` returns HTTP 503 on this exact
    DB failure) but never fixed for the agent-action path until now.

    Callers MUST NOT call `chase_order()` after this returns — log + alert
    only; the caller decides whether to `raise` (single-position actions,
    so `execute()` logs `action_failed` instead of a false
    `action_success`) or `continue` to the next position (the
    multi-position chase_close_positions loop, matching the existing
    preflight-blocked-skip-this-position pattern in the same loop).
    """
    logger.error(
        f"[LIVE] {action_type} ABORTED for {account} {exchange}/{symbol} "
        f"{side} {qty}: AlgoOrder row write failed — refusing to place an "
        f"untracked live order"
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent_shim, 'slug', action_type)}",
            error="AlgoOrder DB write failed — order NOT placed (fail-closed)",
        )
    except Exception as _e:
        logger.warning(f"AlgoOrder-write-failure notification failed: {_e}")


async def _place_order_write_intent(agent_shim, pf: dict,
                                    account: str, symbol: str, exchange: str,
                                    side: str, qty: int, price,
                                    product: str = "NRML",
                                    action_type: str = "place_order") -> "int | None":
    """Write OPEN AlgoOrder row and fire preflight_ok event (best-effort).

    Returns the AlgoOrder row id so callers can pass algo_order_id to chase_order.

    `product` is applied via `_place_order_set_product_template` AFTER
    `_write_live_order` returns — that shared constructor (used by
    close_position/chase_close_positions too) is never touched. The
    helper swallows its own exceptions, so a failure there can never
    turn a real `intent_id` into None here.

    `template_id`/`template_slug`/`overrides` params removed (Bracket/
    Template-GTT removal, T3.6, 2026-11) — they fed
    `_place_order_set_product_template`'s now-removed write to
    `AlgoOrder.template_id`/`template_overrides_json`, both dropped
    columns.

    `action_type` (Part A, hedge leg): `_write_live_order`'s own
    `detail` string embeds it (`"{slug} → {action_type}: ..."`), so
    the hedge leg's own AlgoOrder row reads distinctly from its
    parent's in logs/UI when `_al_prepare_hedge_leg` passes
    `"place_order_hedge"` here — it is still a real `mode='live'`
    order row, not a different DSL action.
    """
    from backend.api.algo.actions import _write_live_order

    try:
        intent_id = await _write_live_order(
            agent_shim, action_type,
            {"account": account, "symbol": symbol, "exchange": exchange,
             "side": side, "qty": qty, "price": price},
            status="OPEN",
        )
        if intent_id:
            await _place_order_set_product_template(intent_id, product)
            from backend.api.algo.order_events import write_event as _write_ev_ok
            import asyncio as _aio
            _aio.create_task(_write_ev_ok(
                intent_id, "preflight_ok",
                "Preflight passed",
                payload={"diagnostics": pf["diagnostics"]},
            ))
        return intent_id
    except Exception:
        return None


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
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{context.get('agent_slug', 'place_order')}",
            error=f"{e} | {diag}",
        )
    except Exception:
        pass


def _al_place_resolve_params(
    agent, context: dict, params: dict
) -> "tuple[object, str, str, str, str, int, object, str]":
    """Resolve _action_place_order params and build the _AgentShim sentinel.

    `agent` is the real Agent DB row the caller (`_dispatch_live_action`)
    already has — captured onto plain class attributes (not held as a
    live reference) so the shim is safe to read across the async
    boundaries below even if `agent` is a detached ORM instance.
    Pre-fix, this shim only ever carried `slug` (and only the context
    fallback string "place_order", since `context["agent_slug"]` is
    never actually populated by the caller) — `_write_live_order`'s
    `agent_id=getattr(agent, "id", None)` therefore always wrote NULL
    for every live agent-placed order. Fixed 2026-10 (Sprint 1a).

    Quantity accepts either `quantity` or `qty` (checked in that order) —
    `quantity` is what the params_schema's resolved field name has always
    been read as here, but the Automation page's "+ place_order" quick-add
    skeleton (`frontend/.../automation/+page.svelte`) ships `qty` with no
    structured quantity field to correct it, so an operator using that UI
    unmodified fired a live order with quantity silently resolved to 0
    before this fallback was added. Mirrors `_al_close_resolve_params`'s
    already-shipped `quantity`/`qty` dual-key read for close_position.
    Fixed 2026-10.

    `template_id`/`template_slug` resolution removed (Bracket/
    Template-GTT removal, T3.6, 2026-11) — the `place_order`/`order`
    action params_schema (agent_grammar.yaml) stopped declaring either
    param in T3.2, so no grammar-validated agent action can carry them
    anymore; the dead `params.get(...)` reads here fed only
    `_place_order_set_product_template`'s now-removed writes.

    Returns (shim, account, symbol, exchange, side, qty, price, product).
    """
    class _AgentShim:
        slug = getattr(agent, "slug", None) or context.get("agent_slug", "place_order")
        id   = getattr(agent, "id", None)

    return (
        _AgentShim(),
        str(params.get("account") or ""),
        str(params.get("symbol") or ""),
        str(params.get("exchange") or "NFO"),
        str(params.get("transaction_type") or params.get("side") or "SELL"),
        int(params.get("quantity") or params.get("qty") or 0),
        params.get("price"),
        str(params.get("product") or "NRML"),
    )


# `_place_order_resolve_template_slug`/`_place_order_overrides_json`
# removed (Bracket/Template-GTT removal, T3.1, 2026-11) — the former
# called `template_attach.load_template_for_slug_or_id`, deleted along
# with `backend/api/algo/template_attach.py`; the latter served only
# `_place_order_set_product_template`'s now-removed `template_overrides_json`
# write (below). Confirmed zero active/inactive agents reference
# `template_id`/`template_slug` in their actions or preamble (prod + dev
# DB check).
#
# `_al_place_build_overrides` removed (Bracket/Template-GTT removal,
# T3.6, 2026-11) — its only caller was `_action_place_order`'s call into
# `_place_order_write_intent`'s now-removed `overrides` param, which fed
# `_place_order_set_product_template`'s now-removed write to
# `AlgoOrder.template_overrides_json`, a dropped column. It reused
# `actions._build_template_overrides`, also removed in the same commit.


async def _place_order_set_product_template(row_id: int, product: str) -> None:
    """Set `product` on a freshly-written place_order AlgoOrder row.

    Sprint 1a (docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md §2.2): the
    shared `_write_live_order` constructor (actions.py) is also used by
    `close_position` and `chase_close_positions` — neither of which sets
    this field today — so this is deliberately NOT added there. Instead
    it's a narrow, place_order-only follow-up UPDATE scoped to the
    single row just created, run immediately after `_write_live_order`
    returns.

    `template_id`/`template_slug`/`overrides` params removed (Bracket/
    Template-GTT removal, T3.6, 2026-11) — they used to be silently
    ignored (per T3.1) and fed no write even then; the columns they
    would have written (`AlgoOrder.template_id`/`template_overrides_json`)
    are now dropped entirely. Swallows all exceptions (logs + returns)
    so a failure here can never take down the caller's `intent_id`.
    """
    if not product:
        return
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import update as sa_update

    try:
        async with async_session() as s:
            await s.execute(
                sa_update(AlgoOrder).where(AlgoOrder.id == row_id)
                .values(product=str(product))
            )
            await s.commit()
    except Exception as e:
        logger.warning(
            f"[LIVE] place_order product update failed for row {row_id}: {e}"
        )


# ── Hedge leg (Part A, 2026-11) ─────────────────────────────────────────────
#
# DSL equivalent of the Chain tab's own Hedge checkbox
# (`frontend/src/lib/order/OptionChainTab.svelte`). Deliberately a
# separate implementation from that JS code, and from Part C's MCX
# settlement netting (`expiry.py:_best_opt_partner`/`_best_fut_partner`)
# — the latter is a backward-looking theta-priority match among
# currently HELD positions for settlement risk; this is a
# forward-looking, premium-percent match among currently QUOTED
# strikes at order-entry time. Never merge the two.

def _al_hedge_precheck_symbol(exchange: str, symbol: str, hedge_pct) -> "dict | None":
    """Cheap, synchronous gate before any broker/DB call: `hedge_pct`
    unset, a non-NFO exchange, or a symbol that doesn't parse as an
    option all mean "no hedge possible here" — no reason to pay for a
    position fetch or a quote call first. Returns the parsed symbol
    dict (`parse_tradingsymbol()`'s own shape) on success, else None.
    """
    if not hedge_pct:
        return None
    if str(exchange or "").upper() != "NFO":
        return None
    from backend.api.algo.derivatives import parse_tradingsymbol
    parsed = parse_tradingsymbol(symbol)
    if not parsed or parsed.get("kind") != "opt":
        return None
    return parsed


async def _al_hedge_account_signal(account: str) -> "tuple[list | None, list]":
    """One `fetch_positions()` pass. Returns `(dfs, signal)`, or
    `(None, [])` on a fetch exception. `signal` is `positions.py`'s own
    `_extract_per_account_signal()` shape — reused rather than
    re-derived, so this reads the IDENTICAL per-account health/outage
    classification `/api/positions` itself relies on (see CLAUDE.md,
    "Per-account failures degrade one account, never the batch")."""
    try:
        from backend.brokers.broker_apis import fetch_positions as _fp
        dfs = await asyncio.to_thread(_fp)
    except Exception:
        return None, []
    from backend.api.routes.positions import _extract_per_account_signal
    return dfs, _extract_per_account_signal(dfs)


def _al_hedge_account_is_degraded(dfs: "list | None", signal: list, account: str) -> bool:
    """True when THIS specific account's own fetch cannot be trusted —
    never inferred from whether some OTHER account in the same batch
    happened to come back healthy.

    Two signals, both scoped to `account`:
      1. A full-batch outage (`positions._is_positions_outage`) — every
         account failed, or the whole per-account fetch came back empty
         while accounts are genuinely configured. No per-account signal
         can be trusted when the whole fetch is down.
      2. This account's OWN entry in `signal` is tagged `stale` or
         `fetch_failed` — set by `_stale_substitute_frame`/
         `_lkg_or_fallback` on every degraded path, always alongside an
         explicit `account` tag (`broker_apis.py`). A genuinely healthy
         EMPTY account frame carries NO account tag at all (its
         `"account"` row/column is only populated on a non-empty
         frame) — so an account with no matching, explicitly-bad entry
         here is a confirmed-flat, confirmed-healthy account, not an
         untrustworthy one. The full-batch check above is what catches
         the case where this account is missing from the signal
         because the ENTIRE fetch failed, rather than because it is
         genuinely flat.
    """
    from backend.api.routes.positions import _is_positions_outage
    if _is_positions_outage(dfs or []):
        return True
    account_upper = str(account or "").upper()
    for s in signal:
        acct = s.get("account")
        if acct and str(acct).upper() == account_upper and (s.get("stale") or s.get("fetch_failed")):
            return True
    return False


async def _al_hedge_close_guard(
    symbol: str, exchange: str, side: str, account: str, qty: int,
) -> bool:
    """True only when it is genuinely safe to resolve a hedge for this
    order. False means "skip the hedge", on any of: the position fetch
    itself can't be trusted FOR THIS ACCOUNT (broker exception, a
    full-batch outage, or this account's own frame explicitly tagged
    stale/fetch_failed — never inferred from a DIFFERENT account's
    frame being healthy), or a held position's sign is offset by
    `side` — at ANY magnitude, not only when it covers the full order
    quantity. A partial close and a flip both still mean "this order
    is at least partly closing", which is exactly the case Part A's
    design says must never stage a hedge.

    Deliberately NOT `_verify_close_intent()` (`orders_place.py`) —
    that function is tuned for a cap-BYPASS decision (G2/ceiling), so
    it fails CLOSED (treats "can't verify" as "not a close", i.e. the
    opposite of what a hedge-fire gate needs) and only returns True
    when the order qty doesn't exceed the held qty, which would let an
    oversized offsetting order (a flip) read as "not closing" here and
    wrongly fire a hedge on it.
    """
    dfs, signal = await _al_hedge_account_signal(account)
    if dfs is None or _al_hedge_account_is_degraded(dfs, signal, account):
        return False
    try:
        from backend.api.routes.orders_place import _fetch_net_position_qty
        net_qty = await _fetch_net_position_qty(symbol, exchange, account)
    except Exception:
        return False
    if net_qty is None:
        return True  # this account's own fetch is confirmed healthy, genuinely no matching row
    side_upper = str(side).upper()
    if side_upper == "BUY" and net_qty < 0:
        return False
    if side_upper == "SELL" and net_qty > 0:
        return False
    return True


def _al_hedge_build_leg_dict(
    candidates: dict, result: dict, opt_type: str, side: str,
    exchange: str, qty: int, account: str, symbol: str,
) -> "dict | None":
    """Resolve the winning strike's own tradingsymbol out of the same
    candidates book the search ran over, and flip the side. Returns
    None (log + skip) on the pathological case of a winning strike
    whose own quote entry carries no symbol."""
    hedge_strike = result["strike"]
    hedge_sym = ((candidates.get(hedge_strike) or {}).get(opt_type.lower()) or {}).get("sym")
    if not hedge_sym:
        from backend.shared.helpers.utils import mask_account
        logger.warning(
            f"[LIVE] place_order hedge leg resolved a strike with no "
            f"tradingsymbol for {mask_account(account)} {exchange}/{symbol} "
            f"— proceeding unhedged"
        )
        return None
    hedge_side = "SELL" if str(side).upper() == "BUY" else "BUY"
    return {
        "symbol": hedge_sym,
        "exchange": exchange,
        "side": hedge_side,
        "qty": qty,
        "price": result["price"],
    }


async def _al_hedge_resolve_leg(
    account: str, symbol: str, exchange: str, side: str, qty: int,
    price, hedge_pct, parsed: dict,
) -> "dict | None":
    """Quote fetch + strike search + leg assembly, once the cheap
    precheck and the close-intent guard have both already passed."""
    from backend.api.algo.hedge_strike import (
        fetch_hedge_quotes, resolve_hedge_strike, resolve_parent_real_expiry,
        candidates_match_parent_symbol,
    )
    from backend.shared.helpers.utils import mask_account

    underlying = str(parsed.get("root") or "")
    parent_expiry = parsed.get("expiry")
    fallback_iso = parent_expiry.isoformat() if parent_expiry else ""
    expiry_iso = await resolve_parent_real_expiry(symbol, fallback_iso)
    if not expiry_iso:
        return None

    candidates = await fetch_hedge_quotes(underlying, expiry_iso, account)
    opt_type = str(parsed.get("opt_type") or "")
    parent_strike = float(parsed.get("strike") or 0)

    if not candidates_match_parent_symbol(candidates, parent_strike, opt_type, symbol):
        logger.warning(
            f"[LIVE] place_order hedge leg skipped for {mask_account(account)} "
            f"{exchange}/{symbol}: parent symbol not found in the resolved "
            f"option chain for expiry {expiry_iso} (possible expiry-"
            f"approximation mismatch) — proceeding unhedged"
        )
        return None

    result = resolve_hedge_strike(
        parent_opt_type=opt_type,
        parent_strike=parent_strike,
        parent_side=side,
        parent_premium=float(price or 0),
        target_pct=float(hedge_pct),
        candidates=candidates,
    )
    if not result.get("ok"):
        logger.warning(
            f"[LIVE] place_order hedge leg not resolved for "
            f"{mask_account(account)} {exchange}/{symbol}: "
            f"{result.get('reason')} — proceeding unhedged"
        )
        return None

    return _al_hedge_build_leg_dict(candidates, result, opt_type, side, exchange, qty, account, symbol)


async def _al_resolve_hedge_plan(
    agent, context: dict, account: str, symbol: str, exchange: str,
    side: str, qty: int, price, hedge_pct,
) -> "dict | None":
    """Resolve a hedge leg for a just-pre-persisted parent order, or None.

    None means "no hedge" — the parent proceeds exactly as it would
    with `hedge_pct` unset. This function must NEVER cause the parent
    order to be blocked or delayed beyond its own resolution cost, so
    its entire body runs inside one broad try/except: any exception
    anywhere in here (including inside the close-intent guard's own
    position fetch) degrades to "no hedge", same as a genuine
    not-found or a non-option symbol.

    Returns {"symbol", "exchange", "side", "qty", "price"} for the
    hedge leg's own order on a match — always a 1:1 qty ratio with the
    parent (no ratio knob in v1, same as the Chain-tab feature).
    """
    try:
        parsed = _al_hedge_precheck_symbol(exchange, symbol, hedge_pct)
        if parsed is None:
            return None

        safe = await _al_hedge_close_guard(symbol, exchange, side, account, qty)
        if not safe:
            return None

        return await _al_hedge_resolve_leg(account, symbol, exchange, side, qty, price, hedge_pct, parsed)
    except Exception as e:
        from backend.shared.helpers.utils import mask_account
        logger.warning(
            f"[LIVE] place_order hedge leg resolution raised for "
            f"{mask_account(account)} {exchange}/{symbol}: {e} — proceeding unhedged"
        )
        return None


async def _al_hedge_prepare_failure_alert(
    agent_shim, account: str, exchange: str, hedge_ctx: dict, reason: str,
) -> None:
    """Fire the operator-facing failure alert for a hedge leg that
    never reached the broker at all (preflight exception/block, or a
    DB-write failure before `chase_order()` is even called). The
    `self_alerted=True` WARNING these three callers also log would
    otherwise be silent to the operator — the position they asked to
    be hedged is naked, which is exactly the gap this alert, and the
    separate unhedged-short-option alert (Part B), both exist to
    surface. Never raises."""
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=hedge_ctx["symbol"], exchange=exchange,
            side=hedge_ctx["side"], qty=hedge_ctx["qty"], mode="live",
            source=f"agent:{getattr(agent_shim, 'slug', 'place_order_hedge')}",
            error=f"hedge leg never placed: {reason}",
            agent_id=getattr(agent_shim, "id", None),
        )
    except Exception:
        pass


async def _al_prepare_hedge_leg(
    agent_shim, account: str, exchange: str, hedge_ctx: dict,
    product: str, aggressiveness: str,
) -> "tuple[int, object] | None":
    """Preflight + persist the hedge leg's own AlgoOrder row.

    Mirrors the parent's own preflight-then-persist sequence in
    `_action_place_order` so a hedge leg gets the exact same G1/G2/
    margin safety net as any other live order — this is still a real
    broker order, placed with real money, not a cosmetic second leg.
    Returns None on any preflight block or DB-write failure; callers
    MUST treat None as "skip the hedge", never as a reason to touch
    the parent's own placement.
    """
    from backend.shared.helpers.utils import mask_account
    symbol = hedge_ctx["symbol"]
    side = hedge_ctx["side"]
    qty = hedge_ctx["qty"]
    price = hedge_ctx["price"]

    try:
        pf = await run_preflight(account, {
            "exchange": exchange, "tradingsymbol": symbol, "side": side,
            "quantity": qty, "order_type": "LIMIT", "product": product,
            "price": price or 0, "variety": "regular",
        })
    except Exception as e:
        logger.warning(
            f"[LIVE] place_order hedge leg preflight raised for "
            f"{mask_account(account)} {exchange}/{symbol}: {e} — "
            f"proceeding with parent order unhedged",
            extra={"self_alerted": True},
        )
        await _al_hedge_prepare_failure_alert(agent_shim, account, exchange, hedge_ctx, f"preflight raised: {e}")
        return None
    if not pf["ok"]:
        reasons = "; ".join(b["reason"] for b in pf["blocked"])
        logger.warning(
            f"[LIVE] place_order hedge leg BLOCKED for "
            f"{mask_account(account)} {exchange}/{symbol} {side} {qty}: "
            f"{reasons} — proceeding with parent order unhedged",
            extra={"self_alerted": True},
        )
        await _al_hedge_prepare_failure_alert(agent_shim, account, exchange, hedge_ctx, f"preflight blocked: {reasons}")
        return None

    oid = await _place_order_write_intent(
        agent_shim, pf, account, symbol, exchange, side, qty, price,
        product=product, action_type="place_order_hedge",
    )
    if oid is None:
        logger.warning(
            f"[LIVE] place_order hedge leg AlgoOrder write failed for "
            f"{mask_account(account)} {exchange}/{symbol} — proceeding "
            f"with parent order unhedged",
            extra={"self_alerted": True},
        )
        await _al_hedge_prepare_failure_alert(agent_shim, account, exchange, hedge_ctx, "AlgoOrder DB write failed")
        return None

    from backend.api.routes.orders_helpers import _live_chase_config
    hedge_cfg = _live_chase_config(aggressiveness, product=product)
    hedge_cfg.exchange = exchange or "NFO"
    return oid, hedge_cfg


def _al_chase_status_str(outcome) -> str:
    """Normalize a `chase_order()` outcome (a real `ChaseResult`, a
    test double, or a raised exception) to an uppercase status string.
    `status` may be a real `ChaseStatus` enum member or a bare string
    — both compare the same way via `.value` when present. An
    exception has no status at all; it reads as the sentinel
    `"EXCEPTION"`, never mistaken for a real `ChaseStatus` value."""
    if isinstance(outcome, Exception):
        return "EXCEPTION"
    status = getattr(outcome, "status", None)
    return str(getattr(status, "value", status) or "").upper()


def _al_chase_outcome_is_failure(outcome) -> bool:
    """True when a `chase_order()` call did not reach a genuinely
    filled (full or partial) state. Covers both a raised exception AND
    a non-raising failure `ChaseResult` — e.g. the per-(account,
    symbol, exchange) position-resolution lock's contended-fast-fail
    path, or ordinary attempt exhaustion, neither of which raises;
    both leave `status` at FAILED/CANCELLED/HELD rather than
    FILLED/PARTIAL. Used for BOTH the parent's and the hedge leg's own
    outcome — it is not hedge-specific despite most of its callers
    being in this file's hedge section."""
    return _al_chase_status_str(outcome) not in ("FILLED", "PARTIAL")


async def _al_handle_hedge_chase_outcome(
    agent_shim, account: str, hedge_ctx: dict, outcome,
) -> None:
    """Log + alert (never raise) when the hedge leg's own `chase_order()`
    call did not fill. The parent's own result is `_action_place_order`'s
    sole success/failure signal (operator decision — see Part A design
    in `.claude/PLAN.md`): a hedge-leg failure never turns a correctly
    placed parent order into a reported action failure. It still needs
    to reach the operator promptly, since it leaves an intended-to-be-
    hedged position naked — the same gap the unhedged-short-option
    alert (Part B) independently exists to catch."""
    if not _al_chase_outcome_is_failure(outcome):
        return
    from backend.shared.helpers.utils import mask_account
    symbol = hedge_ctx["symbol"]
    exchange = hedge_ctx["exchange"]
    side = hedge_ctx["side"]
    qty = hedge_ctx["qty"]
    logger.warning(
        f"[LIVE] place_order hedge leg chase failed for "
        f"{mask_account(account)} {exchange}/{symbol} {side} {qty}: "
        f"{outcome} — parent order placed successfully, position may "
        f"be naked",
        extra={"self_alerted": True},
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent_shim, 'slug', 'place_order_hedge')}",
            error=f"hedge leg chase failed: {outcome}",
            agent_id=getattr(agent_shim, "id", None),
        )
    except Exception:
        pass


def _al_detect_hedge_pairing_issue(parent_outcome, hedge_outcome) -> "str | None":
    """After both legs' `chase_order()` calls have settled, detect the
    two shapes where the parent/hedge pairing itself is wrong even
    though neither leg necessarily "failed" in isolation:

      - The parent did not fully fill (any status other than FILLED,
        including an exception or PARTIAL) while the hedge leg IS live
        (FILLED or PARTIAL) — the hedge is now an orphan, naked
        position with no (or a smaller) parent position behind it.
      - The hedge leg itself is only PARTIAL — it covers less than the
        full intended hedge quantity, leaving part of the parent's own
        position unhedged even when the parent filled completely.

    Returns a human-readable reason, or None when the pairing is fine.
    Deliberately does NOT re-alert the plain "hedge is dead"
    (FAILED/CANCELLED/HELD/exception) case — `_al_handle_hedge_chase_
    outcome` already covers that; this function only ever fires when
    the hedge leg is otherwise reported live.
    """
    hedge_status = _al_chase_status_str(hedge_outcome)
    hedge_is_live = hedge_status in ("FILLED", "PARTIAL")
    parent_filled_fully = _al_chase_status_str(parent_outcome) == "FILLED"

    if not parent_filled_fully and hedge_is_live:
        return (
            f"parent order did not fully fill (status={_al_chase_status_str(parent_outcome)}) "
            f"while the hedge leg is live (status={hedge_status}) — hedge may now be "
            f"unmatched or orphaned"
        )
    if hedge_status == "PARTIAL":
        return "hedge leg only partially filled — part of the intended hedge coverage is missing"
    return None


async def _al_alert_hedge_pairing_issue(
    agent_shim, account: str, symbol: str, exchange: str, side: str, qty: int, reason: str,
) -> None:
    """Fire the operator-facing alert for an orphaned/under-covered
    hedge pairing (see `_al_detect_hedge_pairing_issue`). Scoped to the
    PARENT order's own identity — that is the position the operator
    actually placed and is now tracking, whether or not it filled.
    Never raises."""
    from backend.shared.helpers.utils import mask_account
    logger.warning(
        f"[LIVE] place_order hedge pairing issue for "
        f"{mask_account(account)} {exchange}/{symbol} {side} {qty}: {reason}",
        extra={"self_alerted": True},
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent_shim, 'slug', 'place_order_hedge')}",
            error=f"hedge pairing issue: {reason}",
            agent_id=getattr(agent_shim, "id", None),
        )
    except Exception:
        pass


async def _action_place_order(agent, context: dict, params: dict):
    """
    Place an order using the chase engine (live mode).

    Mirrors the pattern in `_action_live_close_position`:
      1. Resolve params.
      2. Persist AlgoOrder(mode='live', status='OPEN') BEFORE the broker call
         so the order row exists even if the service dies mid-chase.
      3. Call chase_order(); on failure run basket_margin diagnosis and re-raise
         so execute() writes an action_failed event.

    `params.chase_level` ('LOW'|'MED'|'HIGH', case-insensitive — the
    canonical place_order params_schema field as of Phase 2 of the
    order/agent grammar unification, backend/config/grammars/
    order_fields.yaml) threads through to the same `_live_chase_config()`
    mapping every manual-ticket chase already uses
    (backend/api/routes/orders_helpers.py), instead of building a bare
    `ChaseConfig(exchange=exchange, product=product)` with no aggressiveness
    knob at all. Falls back to the pre-Phase-2 undocumented
    `params.chase_aggressiveness` ('low'|'med'|'high') for any agent action
    JSON authored before `chase_level` existed, then to 'med' when neither
    is set. `ChaseConfig`'s own dataclass defaults (interval_seconds=20,
    aggression_step=0.10, max_attempts=20) are byte-identical to the 'med'
    tier, so an existing agent with neither key keeps its exact prior chase
    cadence. ('low' is NOT byte-identical here — it's the slower/patient
    tier, interval=30/step=0.05/attempts=30 — so it is intentionally not
    used as the silent default.)

    `params.hedge_pct` (Part A, 2026-11): when set and `symbol` parses
    as an NFO option, `_al_resolve_hedge_plan`/`_al_prepare_hedge_leg`
    stage a second order (further-OTM, opposite side, same option
    type) and both legs' `chase_order()` calls run CONCURRENTLY via
    `asyncio.gather` — a sequential await would leave the position
    naked for the parent's entire chase window, defeating the hedge's
    purpose. A hedge-resolution failure (no match, closing order,
    non-option symbol, preflight block, DB-write failure) never blocks
    or delays the parent order; omitting `hedge_pct` entirely is
    byte-identical to this function's pre-hedge behavior.
    """
    import asyncio
    from backend.api.algo.chase import chase_order
    from backend.api.routes.orders_helpers import _live_chase_config
    from backend.brokers import get_broker

    _shim, account, symbol, exchange, side, qty, price, product = (
        _al_place_resolve_params(agent, context, params)
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

    # Emit preflight_ok event (fire-and-forget); capture row id for chase.
    _oid = await _place_order_write_intent(
        _shim, pf, account, symbol, exchange, side, qty, price,
        product=product,
    )
    if _oid is None:
        # AlgoOrder pre-persist failed (DB exception) — fail closed.
        # Never call chase_order() without a tracking row behind it.
        await _on_algo_order_write_failure(
            _shim, "place_order", account, symbol, exchange, side, qty, price,
        )
        # 2026-10 notification-masking audit — this RuntimeError can
        # surface through `_al_action_failed_audit`'s `logger.error(...)`
        # (masked in the log pipeline by the centralized filter) but also
        # through `write_audit_event`'s summary text, which is not a
        # logger call; mask the account at the source either way.
        from backend.shared.helpers.utils import mask_account
        raise RuntimeError(
            f"place_order: AlgoOrder pre-persist failed for {mask_account(account)} "
            f"{exchange}/{symbol} {side} {qty} — refusing to place an "
            f"untracked live order"
        )

    aggressiveness = str(
        params.get("chase_level") or params.get("chase_aggressiveness") or "med"
    ).lower()
    cfg = _live_chase_config(aggressiveness, product=product)
    cfg.exchange = exchange or "NFO"

    hedge_ctx = await _al_resolve_hedge_plan(
        agent, context, account, symbol, exchange, side, qty, price,
        params.get("hedge_pct"),
    )
    hedge_prepared = None
    if hedge_ctx is not None:
        hedge_prepared = await _al_prepare_hedge_leg(
            _shim, account, exchange, hedge_ctx, product, aggressiveness,
        )

    parent_coro = chase_order(
        account=account, symbol=symbol,
        transaction_type=side, quantity=qty,
        cfg=cfg,
        algo_order_id=_oid,
        agent_id=_shim.id,
    )

    if hedge_prepared is None:
        try:
            result = await parent_coro
        except Exception as e:
            await _place_order_on_failure(e, context, account, symbol, exchange, side, qty, price, product)
            raise
    else:
        hedge_oid, hedge_cfg = hedge_prepared
        hedge_coro = chase_order(
            account=account, symbol=hedge_ctx["symbol"],
            transaction_type=hedge_ctx["side"], quantity=hedge_ctx["qty"],
            cfg=hedge_cfg,
            algo_order_id=hedge_oid,
            agent_id=_shim.id,
        )
        # return_exceptions=True — a hedge-leg failure must never
        # propagate through this gather and abort the parent's own
        # chase; each outcome is examined independently below.
        parent_outcome, hedge_outcome = await asyncio.gather(
            parent_coro, hedge_coro, return_exceptions=True,
        )
        await _al_handle_hedge_chase_outcome(_shim, account, hedge_ctx, hedge_outcome)
        pairing_issue = _al_detect_hedge_pairing_issue(parent_outcome, hedge_outcome)
        if pairing_issue:
            await _al_alert_hedge_pairing_issue(_shim, account, symbol, exchange, side, qty, pairing_issue)
        if isinstance(parent_outcome, Exception):
            await _place_order_on_failure(
                parent_outcome, context, account, symbol, exchange, side, qty, price, product,
            )
            raise parent_outcome
        result = parent_outcome

    # Stash the real chase outcome into context (not a changed return
    # contract — a side effect only) so actions.py's notify wrapper can
    # describe a successful-but-not-fully-filled order (e.g. PARTIAL)
    # accurately instead of a blanket "placed". Mirrors the existing
    # context["agent_kind"] stash-in-context pattern (actions.py:697).
    status = getattr(result, "status", None)
    context["_action_result_detail"] = {
        "status": status.value if hasattr(status, "value") else str(status),
        "detail": getattr(result, "detail", ""),
    }


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
        await asyncio.to_thread(
            send_order_failure_alert,
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
    # 2026-10-23 audit fix (O9) — `send_order_failure_alert` just below
    # already fires this failure's own operator alert. Without
    # `self_alerted=True`, this ERROR-level log line ALSO matches
    # ERROR_AGENT's blanket catch-all condition (any ERROR-or-above
    # record, regardless of logger/tag — see log_store.py:tags_for),
    # double-alerting the same close_position failure. See
    # event_agents.py:_error_agent_self_alerted.
    logger.error(f"[LIVE] close_position failed for {account} {exchange}/{symbol} "
                 f"{side} {qty}: {e} | diag: {diag}",
                 extra={"self_alerted": True})
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        await asyncio.to_thread(
            send_order_failure_alert,
            account=account, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent, 'slug', 'close_position')}",
            error=f"{e} | {diag}",
        )
    except Exception:
        pass


def _al_close_resolve_params(
    params: dict,
) -> "tuple[str, str, str, int, str | None, str | None]":
    """Extract close_position params from the action dict (raw extraction
    only — no position-aware resolution; see `_al_close_resolve_position`
    for that).

    Returns (account, symbol, exchange, qty_param, side_param,
    product_param). `qty_param` is 0 when omitted — the caller treats 0
    as "flatten the full position", deriving the real quantity from the
    live broker position. `side_param` is None when omitted so the
    caller can derive it from the position's own sign, per the
    `close_position` params_schema (`agent_grammar.yaml`): "Override
    auto-derived side. Default: long -> SELL, short -> BUY."
    `product_param` is likewise None when omitted (2026-10 residual
    fix) — an explicit operator override still wins, but the OLD
    unconditional `"NRML"` default here used to silently discard the
    real position's own product before `_al_close_resolve_position` (or
    its caller) ever got a chance to resolve it from the live broker
    row, which is the exact same bug class Phase 1 (commit `12b8e166`)
    already fixed for `_chase_build_tasks`'s identical hardcode.

    2026-10 audit fix: this function used to hard-default `side="SELL"`
    unconditionally and require `quantity > 0` — silently DOUBLING a
    short position's exposure (a BUY-side position closed with a
    hardcoded SELL order instead of a covering BUY) whenever an agent
    omitted `side`/`quantity`, even though the schema documents both as
    optional with position-derived defaults. Raises ValueError only for
    missing account/symbol identity fields or a negative quantity —
    never for an omitted side/quantity/product, which are now all valid
    inputs.
    """
    account  = str(params.get("account") or "")
    symbol   = str(params.get("symbol") or params.get("tradingsymbol") or "")
    exchange = str(params.get("exchange") or "NFO")
    qty_raw  = params.get("quantity")
    if qty_raw is None:
        qty_raw = params.get("qty")
    qty_param = int(qty_raw) if qty_raw else 0
    side_raw   = params.get("side") or params.get("transaction_type")
    side_param = str(side_raw).upper() if side_raw else None
    product_raw   = params.get("product")
    product_param = str(product_raw).upper() if product_raw else None
    if not account or not symbol:
        raise ValueError(
            f"close_position: missing required params (account={account!r}, "
            f"symbol={symbol!r})"
        )
    if qty_param < 0:
        raise ValueError(
            f"close_position: quantity must not be negative (got {qty_param})"
        )
    return account, symbol, exchange, qty_param, side_param, product_param


async def _al_close_resolve_position(
    account: str, symbol: str, exchange: str,
    side_param: "str | None", qty_param: int, product_param: "str | None",
) -> "tuple[str, int, str] | None":
    """Resolve + verify the real side/qty/product to close against the
    live broker position.

    2026-10 audit fix: `_action_live_close_position` used to hard-default
    `side="SELL"` unconditionally and pass `intent="close"` to
    `run_preflight` with ZERO verification against the real position — a
    short position (net_qty < 0) closed with the hardcoded SELL default
    would DOUBLE the short instead of covering it, and the unverified
    `intent="close"` bypassed the G2 5-lot fat-finger cap for an order
    that was never confirmed to actually offset anything. Fixed by
    reusing `orders_place.py`'s `_fetch_net_position_qty` (sign) and
    `_verify_close_intent` (sign + magnitude) — the EXACT same
    lookup+verification pattern the manual-ticket close path
    (`_ticket_enforce_lot_and_fat_finger`) already relies on — instead of
    reinventing it here. All three helpers read through the shared 30s
    raw-broker-DataFrame cache (`_RAW_CACHE`), so this adds no extra
    broker round-trip versus the single lookup either one does alone.

    2026-10 residual fix (same bug class Phase 1 fixed for
    `_chase_build_tasks`): `product` now resolves from the REAL
    position row's own `product` column (MIS/NRML/CNC) via
    `_fetch_net_position_product` — sharing the exact row
    `_fetch_net_position_qty` reads, so qty/side and product can never
    come from two different rows when a symbol momentarily carries more
    than one product open at once. `product_param` (an explicit
    operator override) wins when present; the real position's product
    is next; `"NRML"` is only the final fallback for the (should-be-
    rare) case where the broker's own positions row is missing the
    column.

    Returns (side, qty, product) only when the close is verified: a
    live position exists, is non-flat, the (possibly operator-
    overridden) side genuinely offsets it, and the qty (defaulted to
    the full held quantity when omitted) does not exceed the held
    quantity. Returns None in every other case — callers MUST refuse to
    place/chase on None, never fall back to placing an unverified close.
    """
    from backend.api.routes.orders_place import (
        _fetch_net_position_qty, _fetch_net_position_product,
        _verify_close_intent,
    )

    net_qty = await _fetch_net_position_qty(symbol, exchange, account)
    if net_qty is None or net_qty == 0:
        return None

    derived_side = "SELL" if net_qty > 0 else "BUY"
    side = (side_param or derived_side).upper()
    qty  = int(qty_param) if qty_param else int(abs(net_qty))
    if qty <= 0:
        return None

    verified = await _verify_close_intent(symbol, exchange, side, account, qty)
    if not verified:
        return None

    real_product = await _fetch_net_position_product(symbol, exchange, account)
    product = product_param or real_product or "NRML"
    return side, qty, product


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

    Resolves account / symbol from params, then resolves the real
    side/qty/product to close by looking up the live broker position
    (`_al_close_resolve_position`) — side defaults to the direction that
    actually offsets the held position (long -> SELL, short -> BUY);
    quantity defaults to the full held quantity when omitted; product
    resolves from the real position row's own product (MIS/NRML/CNC),
    not a hardcoded default (2026-10 residual fix — same bug class
    Phase 1 fixed for `_chase_build_tasks`: an MIS close placed under
    the wrong product does not net against the real MIS position on
    the broker side). The operator-supplied `side`/`quantity`/`product`
    (if any) must still verify against the real position before
    `intent="close"` is allowed to bypass the G2 fat-finger cap; an
    unverified close is refused (REJECTED row + alert), never silently
    placed. Fetches LTP from the broker as the initial limit price,
    then delegates to chase_order() which handles cancel-and-re-place
    until filled or attempt-cap.

    An AlgoOrder(mode='live') row is written before the first placement.
    The chase engine drives the actual Kite calls; any placement or fill
    event is logged by chase.py itself.
    """
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus
    from backend.api.algo.actions import _write_live_order

    account, symbol, exchange, qty_param, side_param, product_param = (
        _al_close_resolve_params(params)
    )

    resolved = await _al_close_resolve_position(
        account, symbol, exchange, side_param, qty_param, product_param,
    )
    if resolved is None:
        pf_unverified = {
            "blocked": [{
                "code": "CLOSE_INTENT_UNVERIFIED",
                "reason": (
                    "could not verify this close against the real broker "
                    "position (no live position found, position is flat, "
                    "requested quantity exceeds the held quantity, or the "
                    "requested side does not offset the held position)"
                ),
            }],
        }
        await _close_position_preflight_block(
            pf_unverified, agent, account, symbol, exchange,
            side_param or "SELL", qty_param, None,
        )
        return  # abort — never place/chase an unverified close

    side, qty, product = resolved
    broker, price = await _al_close_fetch_broker_ltp(account, exchange, symbol)

    # ── Preflight (G1 lot-multiple; G2 5-lot cap bypassed for closes,
    # now reached only after the verified-intent gate above) ───────────
    pf = await run_preflight(account, {
        "exchange": exchange, "tradingsymbol": symbol, "side": side,
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
    _oid = await _write_live_order(agent, "close_position", {
        "account": account, "symbol": symbol, "exchange": exchange,
        "side": side, "qty": qty, "price": price,
    }, status="OPEN")
    if _oid is None:
        # AlgoOrder pre-persist failed (DB exception) — fail closed.
        # Never call chase_order() without a tracking row behind it.
        await _on_algo_order_write_failure(
            agent, "close_position", account, symbol, exchange, side, qty, price,
        )
        # 2026-10 notification-masking audit — same source-side mask as
        # the sibling site in `_action_live_place_order` above.
        from backend.shared.helpers.utils import mask_account
        raise RuntimeError(
            f"close_position: AlgoOrder pre-persist failed for {mask_account(account)} "
            f"{exchange}/{symbol} {side} {qty} — refusing to place an "
            f"untracked live order"
        )

    cfg = ChaseConfig(exchange=exchange, product=product, intent="close")
    try:
        _result = await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
            algo_order_id=_oid,
            agent_id=getattr(agent, "id", None),
        )
    except Exception as e:
        await _close_position_on_failure(
            e, agent, broker, account, symbol, exchange, side, qty, price, product,
        )
        raise

    # 2026-10-23 chase robustness (item 5) — a FAILED/HELD ChaseResult
    # was previously silently discarded here: chase_order() reports
    # almost every abort as a RETURNED result, not a raised exception,
    # so this action's own execute()-level bookkeeping (the agent's
    # event trail showing success/action_failed) never reflected a
    # chase that genuinely gave up. Every ChaseStatus.FAILED/HELD path
    # INSIDE chase.py already fires its own operator alert — EXCEPT the
    # market-closed and non-prod-branch early returns, which happen
    # before chase_order() even constructs its `emit`/alert plumbing.
    # Alert ONLY for that specific uncovered case; every other
    # FAILED/HELD just raises so `execute()` records action_failed,
    # without firing a second, duplicate alert.
    if _result.status == ChaseStatus.FAILED:
        _detail = _result.detail or ""
        if "chase not started" in _detail or "non-prod branch" in _detail:
            try:
                from backend.shared.helpers.alert_utils import send_order_failure_alert
                await asyncio.to_thread(
                    send_order_failure_alert,
                    account=account, symbol=symbol, exchange=exchange,
                    side=side, qty=qty, mode="live",
                    source=f"agent:{getattr(agent, 'slug', 'close_position')}",
                    error=_detail,
                )
            except Exception:
                pass
        raise RuntimeError(f"close_position chase ended in FAILED: {_detail}")
    if _result.status == ChaseStatus.HELD:
        raise RuntimeError(f"close_position chase HELD: {_result.detail}")


def _al_modify_build_kwargs(params: dict) -> dict:
    """Build the kwargs dict for modify_order from action params.

    `agent_grammar.yaml`'s `modify_order` params_schema documents
    `new_qty` / `new_price` / `new_trigger` as the canonical field names
    (alongside `account` / `broker_order_id`), but this handler has
    always actually read `quantity` / `price` / `trigger_price` instead
    — a schema/behavior mismatch flagged by a 2026-10 audit. An agent
    written to match the documented schema silently did nothing in live
    mode (no error — the params it set were just never read). Fixed:
    the schema-documented key is now checked FIRST (using `is not None`
    semantics, matching this function's existing convention) with the
    original internal key name kept as a fallback so any
    already-authored agent using either vocabulary still works.
    `order_type` / `validity` have no schema-documented alias — they are
    internal-only knobs, unchanged.
    """
    kwargs: dict = {}
    for schema_field, legacy_field, kwarg in (
        ("new_qty",     "quantity",      "quantity"),
        ("new_price",   "price",         "price"),
        ("new_trigger", "trigger_price", "trigger_price"),
    ):
        v = params.get(schema_field)
        if v is None:
            v = params.get(legacy_field)
        if v is not None:
            kwargs[kwarg] = v
    for field in ("order_type", "validity"):
        v = params.get(field)
        if v is not None:
            kwargs[field] = v
    return kwargs


async def _al_modify_fetch_order_meta(order_id: str) -> "tuple[str | None, str | None]":
    """Fetch (exchange, tradingsymbol) from the AlgoOrder row for a given
    broker_order_id.

    Returns (None, None) when the row does not exist or the DB call
    fails. Single query serves both the pre-existing "fill in a missing
    `exchange` kwarg for Groww" use (Slice Q) and the new G1
    lot-multiple / `translate_qty` resolution below — both need the
    row's own exchange, and the latter additionally needs the symbol to
    resolve `lot_size`.
    """
    try:
        from sqlalchemy import select as _select
        from backend.api.database import async_session as _as
        from backend.api.models import AlgoOrder as _AO
        async with _as() as _s:
            _row = (await _s.execute(
                _select(_AO).where(_AO.broker_order_id == order_id)
            )).scalar_one_or_none()
        if _row:
            return (_row.exchange or None), (_row.symbol or None)
    except Exception:
        pass
    return None, None


async def _al_modify_resolve_qty(
    broker, raw_qty: int, exchange: "str | None", symbol: "str | None",
) -> int:
    """Resolve a modify_order quantity to the broker's wire convention,
    with a G1 lot-multiple preflight check applied uniformly across every
    F&O exchange (not just MCX/NCO) — see CLAUDE.md "Lot/contract oversize
    guards" (C7 fix) for why MCX/NCO is not special-cased here.

    2026-10 fix: `_action_live_modify_order` used to send `quantity`
    straight to `broker.modify_order()` with a comment claiming this was
    deliberate ("supply Kite qty"). `order_fields.yaml` documents the
    `qty` field as lots × lot_size (contracts) — an agent author
    following that documentation on MCX/NCO got an N× oversize modify,
    the exact "Option qty vs lot_size" trap CLAUDE.md's math guards
    describe. Every other order-placing path in this codebase resolves
    `lot_size` and calls `broker.translate_qty(exchange, raw_qty,
    lot_size)` before touching the broker (see
    `orders_place.py`'s `broker.translate_qty(data.exchange or "NFO",
    qty, ls_for_translate)` call for the canonical pattern) — this
    mirrors that.

    Fails closed (raises) rather than guessing when `exchange`/`symbol`
    can't be resolved, or when `lot_size` resolution itself comes back
    as the cache-miss sentinel (0) — sending a bare, unverified quantity
    straight to the broker is exactly the failure mode this fix closes.
    `broker.translate_qty` is a no-op for non-MCX/NCO exchanges once
    `lot_size` is confirmed (equity lot_size is always 1), so this adds
    no behavior change for NSE/BSE/CDS modifies beyond the new G1 check.
    """
    if not exchange or not symbol:
        raise RuntimeError(
            f"modify_order: cannot resolve exchange/symbol for this order "
            f"— refusing to send quantity={raw_qty} to the broker without "
            f"lot-size verification"
        )
    from backend.brokers.adapters.kite import get_lot_size
    lot_size = await get_lot_size(exchange, symbol)
    if not lot_size:
        raise RuntimeError(
            f"modify_order: lot_size unresolved for {exchange}/{symbol} "
            f"(instruments cache miss) — refusing to send "
            f"quantity={raw_qty} to the broker without lot-size verification"
        )
    if lot_size > 1 and raw_qty % lot_size != 0:
        raise ValueError(
            f"modify_order: G1 lot-multiple violation — quantity={raw_qty} "
            f"is not a multiple of lot_size={lot_size} for {exchange}/{symbol}"
        )
    return broker.translate_qty(exchange, raw_qty, lot_size)


async def _al_modify_write_reject(order_id: str, e: Exception) -> None:
    """Record a modify_order broker failure on the matching AlgoOrder row(s).

    2026-10 audit fix: this used to blindly write status="REJECTED" on
    every modify_order exception, with no check of the row's CURRENT
    status first. REJECTED is a FINAL status (see
    models.ALGO_ORDER_FINAL_STATUSES), and a modify failure (timeout,
    rate limit, broker-side validation error) does NOT mean the
    underlying broker order is gone — it may still be resting live and
    fill later. Writing REJECTED here would permanently block that
    order's real FILLED postback from ever applying (orders_postback.py's
    own final-status guard refuses to move a row OUT of a final status),
    stranding a live fill with no take-profit arm and no FIFO ledger
    write — a worse outcome than the original bug. So this function now
    only annotates `detail` for operator visibility and never mutates
    `status`, and skips rows already in a final status entirely so a
    genuinely-terminal row's own terminal record is never clobbered.

    Looks up by `broker_order_id` with `.scalars().all()` (the column is
    indexed but not DB-unique) and locks matching rows via
    `.with_for_update()` before mutating, consistent with the
    postback/chase final-status-guard pattern elsewhere in this codebase.
    """
    try:
        from sqlalchemy import select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder, ALGO_ORDER_FINAL_STATUSES
        async with async_session() as s:
            rows = (await s.execute(
                sql_select(AlgoOrder)
                .where(AlgoOrder.broker_order_id == order_id)
                .with_for_update()
            )).scalars().all()
            changed = False
            for row in rows:
                if row.status in ALGO_ORDER_FINAL_STATUSES:
                    logger.warning(
                        "modify_order failed for broker_order_id=%s but AlgoOrder "
                        "#%s is already in final status %s — leaving status "
                        "unchanged, not recording as REJECTED",
                        order_id, getattr(row, "id", "?"), row.status,
                    )
                    continue
                row.detail = f"modify failed: {e}"[:240]
                changed = True
            if changed:
                await s.commit()
    except Exception as db_exc:
        logger.warning(
            "modify_order failure bookkeeping failed for broker_order_id=%s: %s",
            order_id, db_exc,
        )


async def _action_live_modify_order(agent, context: dict, params: dict):
    """
    Modify an open broker order.  Wraps kite.modify_order in run_in_executor.
    Updates the matching AlgoOrder row on success.

    `broker_order_id` is read first (the `modify_order` params_schema's
    documented key, `agent_grammar.yaml`), falling back to the legacy
    `order_id` key — same schema/legacy dual-read pattern as
    `_al_modify_build_kwargs`'s qty/price/trigger fields below.

    2026-10 fix: quantity used to go straight to `broker.modify_order()`
    with a comment claiming this was deliberate ("supply Kite qty").
    `order_fields.yaml` documents `qty` as lots × lot_size (contracts) —
    an agent author following that documentation on MCX/NCO got an N×
    oversize modify. Now, whenever a quantity is actually being modified,
    `_al_modify_resolve_qty` runs the G1 lot-multiple check and
    `broker.translate_qty` before the quantity reaches the broker — see
    that function's docstring. Price-only / trigger-only modifies (no
    quantity key present) are completely unaffected.
    """
    import asyncio
    from backend.brokers import get_broker

    account  = str(params.get("account") or "")
    order_id = str(params.get("broker_order_id") or params.get("order_id") or "")
    variety  = str(params.get("variety") or "regular")

    if not account or not order_id:
        raise ValueError(f"modify_order: account and order_id are required")

    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    kwargs = _al_modify_build_kwargs(params)

    # Resolve the AlgoOrder row's own exchange/symbol once — used both as
    # the pre-existing fallback for the broker call's `exchange` kwarg
    # (Slice Q, so Groww's segment resolver doesn't raise ValueError on
    # empty exchange) and, new in this fix, for G1 lot-multiple
    # validation + translate_qty whenever `quantity` is in kwargs.
    row_exchange, row_symbol = await _al_modify_fetch_order_meta(order_id)

    if "exchange" not in kwargs and row_exchange:
        kwargs["exchange"] = row_exchange

    try:
        if "quantity" in kwargs:
            exch_for_qty = kwargs.get("exchange") or row_exchange
            kwargs["quantity"] = await _al_modify_resolve_qty(
                broker, int(kwargs["quantity"]), exch_for_qty, row_symbol,
            )
        await loop.run_in_executor(
            None,
            lambda: broker.modify_order(order_id, variety=variety, **kwargs)
        )
    except Exception as e:
        # Update the AlgoOrder row to REJECTED so the operator can see it.
        # Covers both a real broker-call failure AND a G1/lot_size
        # resolution failure above — neither ever reached the broker in
        # the latter case, but annotating `detail` still gives the
        # operator visibility into why the modify never happened.
        await _al_modify_write_reject(order_id, e)
        raise


async def _action_live_cancel_order(agent, context: dict, params: dict):
    """
    Cancel a single open broker order.  Wraps kite.cancel_order.
    Marks the matching AlgoOrder row CANCELLED on success.

    2026-10 audit fix: `broker_order_id` is the key the `cancel_order`
    params_schema (`agent_grammar.yaml`) documents and the only key
    `routes/agents.py` accepts when saving an agent — this handler used
    to read ONLY the legacy `order_id` key, so every schema-conformant
    agent silently did nothing (empty order_id -> ValueError on every
    fire). Fixed with the same dual-key read `_action_live_modify_order`
    already uses for the same field. Also added: the FINAL-status guard
    (`ALGO_ORDER_FINAL_STATUSES`) + `.with_for_update()` row lock that
    `_al_modify_write_reject` already applies for modify_order failures —
    a row already FILLED/REJECTED must never be overwritten to CANCELLED
    by a late/duplicate cancel confirmation.

    2026-10 fix (chase mark_killed gap): also calls
    `chase.mark_killed(order_id)` -- without this, chase's poll loop sees
    the resulting CANCELLED status, `is_killed()` reports False (nothing
    had ever recorded this id as operator-killed), and it takes the
    `cancelled_continue` branch, silently re-placing the order at a fresh
    price. Marked BEFORE the broker call (not after) deliberately mirroring
    `_rco_kill_live_mode` (routes/orders.py) -- this function awaits
    `run_in_executor` for the actual cancel, which yields the event loop,
    so a chase poll could land and read `is_killed()` DURING that await;
    marking first closes that window. Never unmarked on cancel failure --
    failing toward "chase won't re-place" is the safe direction even if
    the cancel actually failed to land at the broker.
    """
    import asyncio
    from backend.brokers import get_broker
    from sqlalchemy import select as sql_select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder, ALGO_ORDER_FINAL_STATUSES
    from backend.api.algo.chase import mark_killed

    account  = str(params.get("account") or "")
    order_id = str(params.get("broker_order_id") or params.get("order_id") or "")
    variety  = str(params.get("variety") or "regular")

    if not account or not order_id:
        raise ValueError(
            f"cancel_order: account and broker_order_id are required"
        )

    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    # Mark BEFORE the broker call (not after): run_in_executor below
    # yields the event loop while the cancel is in flight, and a chase
    # poll landing during that window must already see is_killed()==True.
    # Never unmarked on failure -- failing toward "chase won't re-place"
    # is the safe direction even if the cancel call below raises.
    try:
        mark_killed(order_id)
    except Exception:
        pass

    await loop.run_in_executor(
        None, lambda: broker.cancel_order(order_id, variety=variety)
    )

    # Mark CANCELLED in our order log — never clobber an already-final row.
    try:
        async with async_session() as s:
            rows = (await s.execute(
                sql_select(AlgoOrder)
                .where(AlgoOrder.broker_order_id == order_id)
                .with_for_update()
            )).scalars().all()
            changed = False
            for row in rows:
                if row.status in ALGO_ORDER_FINAL_STATUSES:
                    logger.warning(
                        "cancel_order: AlgoOrder #%s already in final "
                        "status %s — leaving status unchanged, not "
                        "overwriting to CANCELLED",
                        getattr(row, "id", "?"), row.status,
                    )
                    continue
                row.status = "CANCELLED"
                row.detail = f"Cancelled by agent {agent.slug}"
                changed = True
            if changed:
                await s.commit()
    except Exception as db_e:
        logger.warning(f"[LIVE] cancel_order DB update failed: {db_e}")


async def _al_cancel_broker_orders(
    broker, scope_account: str, loop,
    symbol: "str | None" = None, side: "str | None" = None,
) -> "tuple[int, int]":
    """Cancel open orders for a single broker account, optionally
    narrowed to a specific `symbol`/`side`.

    Skips the broker entirely when scope_account is set and does not
    match. When `symbol` and/or `side` are set, only orders whose own
    `tradingsymbol`/`transaction_type` match are cancelled — an order
    missing either key entirely (e.g. a broker whose `orders()` dicts
    use different field names) is left alone rather than guessed at,
    fail-safe. Returns (cancelled_count, error_count).

    2026-10 audit fix: this used to ignore `symbol`/`side` entirely —
    `cancel_all_orders(scope=account, account=X, symbol=Y)` cancelled
    EVERY open order on account X, including manual orders unrelated to
    the agent, instead of only orders on symbol Y.

    2026-10 fix (chase mark_killed gap): every open order this function
    attempts to cancel now also calls `chase.mark_killed(oid)` so chase's
    poll loop recognises the cancel as operator-initiated and never
    silently re-places it at a fresh price. Marked BEFORE the
    `run_in_executor` cancel call (not after/on success only), mirroring
    `_action_live_cancel_order`'s identical fix and for the same reason:
    `run_in_executor` yields the event loop while the cancel is in
    flight, so a chase poll could land and read `is_killed()` DURING
    that window. Marking is deliberately NOT reversed when the cancel
    attempt itself raises — every order in this loop was explicitly
    selected by the operator's cancel-all scope, so failing toward
    "chase won't re-place" matches that intent even if the broker call
    for this particular order failed to land.
    """
    from backend.api.algo.chase import mark_killed

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
        if symbol:
            open_orders = [
                o for o in open_orders
                if str(o.get("tradingsymbol") or "").upper() == symbol.upper()
            ]
        if side:
            open_orders = [
                o for o in open_orders
                if str(o.get("transaction_type") or "").upper() == side.upper()
            ]
        for o in open_orders:
            oid     = str(o.get("order_id", ""))
            variety = str(o.get("variety") or "regular")
            if not oid:
                continue
            # Mark BEFORE attempting the cancel -- see docstring. Not
            # unmarked on failure below.
            try:
                mark_killed(oid)
            except Exception:
                pass
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
        # Count this as an error too, not just a log line -- a listing
        # failure must never present as "0 cancelled, 0 errors" (reads
        # identically to "nothing to cancel"), especially now that the
        # new /api/orders/cancel-all route surfaces these counts directly
        # to the operator after a destructive action.
        errors += 1
        logger.error(f"[LIVE] cancel_all_orders: order list failed for [{acct}]: {e}")

    return cancelled, errors


async def _al_accounts_for_trader_role() -> "list[str]":
    """Every account assigned to a trader-role user, deduped. `trader` is
    the only role with a real per-user `assigned_accounts` list — every
    other role is firm-wide and sees all accounts regardless of list
    contents (`_FIRM_WIDE_ROLES` in `rbac.py`), so "accounts for role=X"
    has no well-defined answer for any role other than trader."""
    from sqlalchemy import select as _sql_select
    from backend.api.database import async_session
    from backend.api.models import User
    async with async_session() as s:
        rows = (await s.execute(
            _sql_select(User.assigned_accounts).where(User.role == "trader")
        )).scalars().all()
    accts: set[str] = set()
    for lst in rows:
        for a in (lst or []):
            if a:
                accts.add(str(a))
    return sorted(accts)


async def _al_accounts_for_trader_user(username: str) -> "list[str]":
    """One trader-role user's own `assigned_accounts`. Raises ValueError
    for an unknown user or a user whose role isn't trader — same
    well-definedness reasoning as `_al_accounts_for_trader_role`: a
    firm-wide role's `assigned_accounts` list is unused/irrelevant to
    that user's real visibility, so narrowing to it would silently
    under- or over-scope relative to what the operator actually sees."""
    from sqlalchemy import select as _sql_select
    from backend.api.database import async_session
    from backend.api.models import User
    async with async_session() as s:
        row = (await s.execute(
            _sql_select(User.role, User.assigned_accounts)
            .where(User.username == username)
        )).first()
    if row is None:
        raise ValueError(f"scope=user: no such user {username!r}")
    role, assigned = row
    if (role or "").strip().lower() != "trader":
        raise ValueError(
            f"scope=user is only well-defined for a trader-role user — "
            f"{username!r} has role={role!r}, which is firm-wide and has "
            "no per-user account list to narrow to"
        )
    return sorted({str(a) for a in (assigned or []) if a})


async def _al_resolve_scope_accounts(
    scope: "str | None", account: "str | None",
    role: "str | None", user: "str | None",
) -> "list[str] | None":
    """Resolve params.scope/account/role/user into the real account set a
    fan-out action (cancel_all_orders, chase_close_positions) should
    narrow to. Returns `None` for scope='total' (or unset) — "no
    narrowing", today's existing default behaviour, 100% backward
    compatible with every already-saved agent that never set these.

    scope='role'/'user' is deliberately restricted to the `trader` role
    (see the two helpers above) — every other role is firm-wide, so
    "accounts for role=admin" or "accounts for user=<a partner>" has no
    well-defined answer today. Raises ValueError rather than silently
    resolving to an empty or a wrong set.
    """
    scope = (scope or "total").lower()
    if scope == "account":
        acct = str(account or "").strip()
        if not acct:
            raise ValueError("scope=account requires account")
        return [acct]
    if scope == "role":
        role_norm = str(role or "").strip().lower()
        if not role_norm:
            raise ValueError("scope=role requires role")
        if role_norm != "trader":
            raise ValueError(
                f"scope=role is only well-defined for role='trader' — "
                f"role={role_norm!r} is firm-wide and has no per-user "
                "account list to narrow to"
            )
        return await _al_accounts_for_trader_role()
    if scope == "user":
        username = str(user or "").strip()
        if not username:
            raise ValueError("scope=user requires user")
        return await _al_accounts_for_trader_user(username)
    return None


async def cancel_all_orders_scoped(
    scope: "str | None" = None,
    account: "str | None" = None,
    symbol: "str | None" = None,
    side: "str | None" = None,
    role: "str | None" = None,
    user: "str | None" = None,
) -> "tuple[int, int]":
    """
    Shared implementation of "cancel every open order across all accounts
    (or a scoped account), optionally narrowed by `symbol`/`side`".

    Extracted from `_action_live_cancel_all_orders` (2026-10) so the agent
    DO-action path AND the `POST /api/orders/cancel-all` REST route call
    the exact same scope/account/symbol/side narrowing logic — no
    duplicated refusal rule to drift out of sync.

    Routes through the Broker registry — broker.orders() lists the
    account's open orders, broker.cancel_order() fires the cancel. All
    calls are wrapped in run_in_executor since the underlying SDKs are
    synchronous. Returns `(total_cancelled, total_errors)`.

    2026-10 audit fix: `symbol`/`side` (both documented in the
    `cancel_all_orders` params_schema, `agent_grammar.yaml`) used to be
    silently ignored — now threaded through to `_al_cancel_broker_orders`
    so a scoped cancel only touches matching orders, not every open
    order on the account.

    `scope="account"` without an `account` value is refused outright
    (ValueError) rather than silently falling through to a fleet-wide
    cancel — the one case that would have been WIDER than intended.
    `account` otherwise narrows the cancel whenever it's set, regardless
    of `scope` — this is the pre-existing (safe, narrowing-only) behavior
    and is deliberately NOT relaxed to "only gate when scope=='account'",
    which would have widened an accidental account-scoped cancel under
    scope='total' to every account — the opposite of this fix's intent.

    2026-10-31 — `scope="role"`/`scope="user"` (operator: "I should be
    able to select an account in actions... for accounts, roles, users,
    etc. If the parameters are not mentioned, it should be for all").
    Resolved via `_al_resolve_scope_accounts()` to a trader-role account
    set (see that function's docstring for why role/user scoping is
    restricted to `trader` — every other role is firm-wide and has no
    well-defined per-user account list). An empty resolved set (a
    trader-role query matching nobody, or a trader with no accounts
    assigned) narrows to ZERO accounts — same fail-safe convention as
    `trader`'s own empty `assigned_accounts` elsewhere in this app, not
    an error. Orthogonal to the pre-existing `account` narrowing above:
    both can combine (role/user narrows WHICH brokers are even
    considered; `account`, if also set, narrows within that further).
    """
    import asyncio
    from backend.brokers.registry import all_brokers

    loop = asyncio.get_running_loop()
    scope = str(scope or "total").lower()
    scope_account = str(account or "")
    if scope == "account" and not scope_account:
        raise ValueError(
            "cancel_all_orders: account is required when scope=account"
        )
    role_user_accounts: "set[str] | None" = None
    if scope == "role":
        role_norm = str(role or "").strip().lower()
        if not role_norm:
            raise ValueError("cancel_all_orders: role is required when scope=role")
        if role_norm != "trader":
            raise ValueError(
                f"cancel_all_orders: scope=role is only well-defined for "
                f"role='trader' — role={role_norm!r} is firm-wide and has "
                "no per-user account list to narrow to"
            )
        role_user_accounts = set(await _al_accounts_for_trader_role())
    elif scope == "user":
        username = str(user or "").strip()
        if not username:
            raise ValueError("cancel_all_orders: user is required when scope=user")
        role_user_accounts = set(await _al_accounts_for_trader_user(username))
    symbol = str(symbol) if symbol else None
    side = str(side).upper() if side else None

    total_cancelled = 0
    total_errors = 0

    for broker in all_brokers():
        if role_user_accounts is not None and broker.account not in role_user_accounts:
            continue
        cancelled, errors = await _al_cancel_broker_orders(
            broker, scope_account, loop, symbol=symbol, side=side,
        )
        total_cancelled += cancelled
        total_errors    += errors

    return total_cancelled, total_errors


async def _action_live_cancel_all_orders(agent, context: dict, params: dict):
    """
    Agent DO-action wrapper around `cancel_all_orders_scoped` — reads the
    agent-grammar params (`scope`/`account`/`symbol`/`side`) and logs the
    aggregate result tagged with the firing agent's slug.

    See `cancel_all_orders_scoped`'s own docstring for the narrowing/
    refusal semantics (unchanged by this extraction).
    """
    scope = params.get("scope")
    scope_account = params.get("account")
    symbol = params.get("symbol")
    side = params.get("side")
    role = params.get("role")
    user = params.get("user")

    total_cancelled, total_errors = await cancel_all_orders_scoped(
        scope=scope, account=scope_account, symbol=symbol, side=side,
        role=role, user=user,
    )

    logger.info(f"[LIVE] cancel_all_orders complete: {total_cancelled} cancelled, "
                f"{total_errors} errors (agent={agent.slug}, scope={scope or 'total'}, "
                f"symbol={symbol}, side={side}, role={role}, user={user})")


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


def _al_positions_filter(rows: list, scope_accts: "str | list[str] | set[str] | None") -> list:
    """Apply account scope filter and drop zero-qty rows. `scope_accts`
    may be a single account string (legacy 'account' scope) or an
    iterable of accounts (2026-10-31 — 'role'/'user' scope, see
    `_al_resolve_scope_accounts`); either form narrows to rows whose
    account matches.

    `None` or an empty string -> no filter at all, every row kept
    (today's pre-existing 'no account given' convention). A non-string
    iterable is checked via `is not None`, NOT truthiness -- an explicit
    empty list/set (a role/user query that resolved to zero accounts)
    must narrow to ZERO rows, never silently fall through to "keep
    everything" the way a bare `elif scope_accts:` would (the exact
    falsy-empty-collection trap this app's own CLAUDE.md repeatedly
    flags elsewhere)."""
    if isinstance(scope_accts, str):
        if scope_accts:
            rows = [r for r in rows if str(r.get("account")) == scope_accts]
    elif scope_accts is not None:
        allowed = {str(a) for a in scope_accts}
        rows = [r for r in rows if str(r.get("account")) in allowed]
    return [r for r in rows if int(r.get("quantity") or 0) != 0]


async def _chase_resolve_positions(context: dict, params: dict) -> list[dict]:
    """Read df_positions from context, apply scope filter, drop zero-qty rows.

    Returns an empty list when the DataFrame is missing, empty, or cannot
    be converted — caller checks and returns early.
    """
    scope_accts = await _al_resolve_scope_accounts(
        params.get("scope"), params.get("account"),
        params.get("role"), params.get("user"),
    )

    rows = _al_positions_to_rows(context.get("df_positions"))
    return _al_positions_filter(rows, scope_accts)


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
        await asyncio.to_thread(
            send_order_failure_alert,
            account=acct, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode="live",
            source=f"agent:{getattr(agent, 'slug', 'chase_close_positions')}",
            error=f"PREFLIGHT BLOCKED [{codes}]: {reasons}",
        )
    except Exception:
        pass


async def _chase_build_tasks(
    agent, rows: list[dict]
) -> "tuple[list, list[dict], list[dict]]":
    """Run preflight for each position row; build chase task list.

    For each row:
      - Run run_preflight; on failure write REJECTED AlgoOrder + alert + skip.
      - On success, persist the OPEN AlgoOrder row. If that write fails
        (DB exception, returns None), log + alert + skip this position —
        never call chase_order() without a tracking row behind it. This
        mirrors the preflight-blocked-skip-this-position pattern
        immediately above: one position's failure never aborts the
        others already queued in the same loop.
      - Otherwise append the chase_order task.

    Returns (chase_tasks, task_rows, refused_rows) where task_rows[i]
    matches chase_tasks[i]. `refused_rows` lets the caller raise after
    `gather()` so the agent cycle still records action_failed for
    visibility, without orphaning the asyncio.Tasks already created for
    other positions in this same loop.
    """
    import asyncio
    from backend.api.algo.chase import chase_order, ChaseConfig
    from backend.api.algo.actions import _write_live_order

    chase_tasks:  list = []
    task_rows:    list[dict] = []
    refused_rows: list[dict] = []

    for p in rows:
        acct     = str(p.get("account", ""))
        symbol   = str(p.get("tradingsymbol", ""))
        exchange = str(p.get("exchange") or "NFO")
        qty_held = int(p.get("quantity") or 0)
        side     = "SELL" if qty_held > 0 else "BUY"
        qty      = abs(qty_held)
        # Real product off the position row (MIS/NRML/CNC) — "NRML" is
        # only a fallback for the (should-be-rare) case where the
        # broker's own positions row is missing the column, never the
        # default used when real data is available. A hardcoded NRML
        # here previously sent an MIS close as an NRML order, which
        # does not net against the real MIS position on the broker's
        # side — the position stayed open while a stray NRML position
        # was created.
        #
        # `p` comes from `df.to_dict(orient="records")`
        # (_al_positions_to_rows), which does NOT convert a missing
        # cell to None — it stays a real float('nan'), and
        # bool(float('nan')) is True in Python, so a bare `or "NRML"`
        # would silently produce the literal string "nan" instead of
        # falling back (same bug class fixed in orders_place.py's
        # _fetch_net_position_product by commit 3ea29a24 — that fix
        # covered only that function, not this one).
        _product_raw = p.get("product")
        # NaN != NaN is the one float where this is true — avoids adding
        # a pandas import to a file that doesn't otherwise need one.
        _product_missing = _product_raw is None or _product_raw != _product_raw
        product = "NRML" if _product_missing else str(_product_raw)

        # Best effort initial limit price from LTP in context row.
        ltp   = p.get("last_price") or p.get("close_price")
        price = float(ltp) if ltp is not None else None

        # ── Preflight (G1 lot-multiple; G2 5-lot cap bypassed for closes) ─
        pf = await run_preflight(acct, {
            "exchange": exchange, "tradingsymbol": symbol, "side": side,
            "quantity": qty, "order_type": "LIMIT",
            "product": product,
            "price": price or 0, "variety": "regular",
            "intent": "close",   # signals G2 bypass inside run_preflight
        })
        if not pf["ok"]:
            await _al_chase_handle_blocked(
                agent, acct, symbol, exchange, side, qty, price, pf
            )
            continue  # skip this position; other positions in the loop proceed

        # Persist intent row before broker call.
        _oid = await _write_live_order(agent, "chase_close_positions", {
            "account": acct, "symbol": symbol, "exchange": exchange,
            "side": side, "qty": qty, "price": price,
        }, status="OPEN")
        if _oid is None:
            # AlgoOrder pre-persist failed (DB exception) — fail closed.
            # Never call chase_order() without a tracking row behind it.
            await _on_algo_order_write_failure(
                agent, "chase_close_positions", acct, symbol, exchange, side, qty, price,
            )
            refused_rows.append(p)
            continue  # skip this position; other positions in the loop proceed

        cfg = ChaseConfig(exchange=exchange, product=product, intent="close")
        chase_tasks.append(
            asyncio.create_task(
                chase_order(account=acct, symbol=symbol,
                            transaction_type=side, quantity=qty, cfg=cfg,
                            algo_order_id=_oid, agent_id=getattr(agent, "id", None))
            )
        )
        task_rows.append(p)
        logger.info(f"[LIVE] chase_close_positions: queued {side} {qty} {symbol} [{acct}]")

    return chase_tasks, task_rows, refused_rows


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
            await asyncio.to_thread(
                send_order_failure_alert,
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

    Scope resolution — params.scope (via `_chase_resolve_positions` ->
    `_al_resolve_scope_accounts`, 2026-10-31):
      'total'   (default) — every position across all accounts
      'account'           — positions for params.account only
      'role'              — positions for every trader-role user's own
                             assigned accounts (params.role must be
                             'trader' — the only role with a well-defined
                             per-user account list; any other role raises)
      'user'              — positions for one trader-role user's own
                             assigned accounts (params.user; raises if
                             that user's role isn't trader)

    For each non-zero position, derives the closing side (SELL for long,
    BUY for short), fetches LTP for the initial limit, writes an
    AlgoOrder(mode='live') row, then fires chase_order() as an asyncio
    task so multiple positions close concurrently (same pattern as the
    expiry engine). FUTURE: this is the clearest example of why a shared
    rate limiter across concurrent chases would matter — see the note at
    chase.py's chase_order() docstring. Not implemented yet.

    We deliberately do NOT use ExpiryEngine.scan_positions() here because
    that scanner applies expiry-day ITM/NTM filters that are irrelevant
    for a generic loss-agent close.  Instead we read directly from
    context['df_positions'] (the live Kite snapshot already in context)
    which is a pandas DataFrame with columns: account, tradingsymbol,
    exchange, quantity, last_price, close_price, …
    """
    import asyncio

    scope = (params.get("scope") or "total").lower()

    rows = await _chase_resolve_positions(context, params)
    if not rows:
        logger.warning(f"[LIVE] chase_close_positions: no positions in context "
                       f"(agent={agent.slug}, scope={scope})")
        return

    chase_tasks, task_rows, refused_rows = await _chase_build_tasks(agent, rows)
    if not chase_tasks:
        if refused_rows:
            # Every position was refused (AlgoOrder pre-persist failed) —
            # no chase_order() calls were made at all. Raise so execute()
            # logs action_failed instead of silently doing nothing.
            raise RuntimeError(
                f"chase_close_positions: all {len(refused_rows)} position(s) "
                f"refused — AlgoOrder write failed for every position"
            )
        return

    # Await all chase tasks concurrently — each manages its own retry loop.
    results = await asyncio.gather(*chase_tasks, return_exceptions=True)
    await _chase_handle_results(results, task_rows, agent)

    if refused_rows:
        # Some (not all) positions were refused — the others above still
        # proceeded to chase_order(). Raise AFTER gather/handle_results so
        # the already-created tasks are never orphaned, but the agent
        # cycle still records action_failed for operator visibility.
        raise RuntimeError(
            f"chase_close_positions: {len(refused_rows)} position(s) refused "
            f"— AlgoOrder write failed; {len(chase_tasks)} other position(s) "
            f"still proceeded"
        )


def _al_expiry_context_shim(context: dict | None):
    """Thin delegator to the shared `expiry_context_shim()`
    (2026-11-24, audit finding #18) — kept as its own name/module
    location purely so existing callers/imports/tests in this module
    are unaffected. Previously a byte-identical duplicate of
    `agent_engine.py:_v2_expiry_context_shim`, deliberately copy-pasted
    instead of imported to avoid a circular import between this module
    and `agent_engine.py`. The real body now lives in
    `expiry_context_shim.py`, which neither module needs to import the
    other to reach — see that module's own docstring for the 2026-11-23
    `.now`-omission incident this duplication once let slip through."""
    from backend.api.algo.expiry_context_shim import expiry_context_shim
    return expiry_context_shim(context)


async def _action_live_expiry_auto_close(agent, context: dict, params: dict):
    """
    Expiry-day surgical close. One shared action, called by both
    `expiry-day-mcx-itm-auto-close` and `expiry-day-nse-itm-auto-close`
    (2026-11-26 exchange-pure split of what was briefly one combined
    `expiry-day-itm-auto-close` agent, 2026-10-29 to 2026-11-26 — see
    `agent_engine.py`'s own history comment above
    `_EXPIRY_DAY_AWARENESS_AGENTS` for the live incident that fixed).
    Each caller passes its own `params.exchange` ("MCX" or "NFO"), so
    this single function never has to guess which exchange it should
    act on — see the `params.exchange` paragraph below, which was
    already built to support this and needed no code change for the
    split.

    Wraps the legacy ExpiryEngine so the agent path inherits the
    battle-tested rules (`ExpiryEngine.classify_prepared()` is the
    SSOT — see its own docstring):
      - NFO: close ALL ITM + NTM expiring today (no hedging exception
        — Indian equity F&O is settled per-leg, no broker netting).
      - MCX: per (account, underlying, expiry) group, apply the real
        4-rule greedy theta-priority netting (long-CE ↔ short-CE,
        long-PE ↔ short-PE, long-CE ↔ long-PE, short-CE ↔ short-PE)
        plus same-underlying FUT positions as delta-offset partners
        (long-CE ↔ short-FUT, long-PE ↔ long-FUT, etc.) — only the
        RESIDUAL (non-zero) qty after netting is closed. This is no
        longer a crude "net qty sums to zero" sum — a synthetic combo
        (long CE + short PE, same strike) sums to zero but has no
        valid opt-opt rule and is correctly flagged; a "guts" position
        (long CE + long PE, different strikes, same sign) sums
        non-zero but IS a valid rule-3 pair and correctly nets to
        zero.

    Reads NTM buffer + chase config from `algo.expiry_*` settings (same
    knobs the bg task uses), so a single /admin/settings change tunes
    both paths.

    `before_cutoff()` per-exchange gating is deliberately NOT used here
    (unlike the pre-2026-10-29 version) — the agent's own
    `is_expiry_preclose_close_window` condition already gates WHETHER
    this action runs at all. It does NOT, by itself, say WHICH exchange
    caused it to run — the condition is a single flat `funds.total`
    boolean with no exchange identity (audit finding, 2026-10-29). This
    action re-derives that independently via `_expiry_preclose_
    exchanges()` (the exact same per-exchange window check the
    condition token itself uses) and filters `scan_positions()`'s
    result to ONLY the exchange(s) actually in-window right now — an
    NFO-only fire must never also close/stage whatever MCX residual
    happens to exist at that moment, using MCX's moneyness from hours
    before its own close (and vice versa). `close_positions()` itself
    still routes every close through `expiry_close_held()` — HELD by
    default (`HoldCategory.EXPIRY_CLOSE`), never a live broker call
    unless the operator has explicitly released that category.

    `scan_positions()` is synchronous and makes real, blocking broker
    calls (positions/instruments/LTP) — run via `asyncio.to_thread` so
    it cannot stall the event loop (chase polls, postbacks, SSE) for
    its duration, matching the alert siblings' own
    `_v2_expiry_exchange_breakdown_or_empty()` (agent_engine.py).

    `params.exchange` (2026-11-24 fix, audit finding #4): when set, the
    live path intersects it with the real pre-close window check above
    — never bypassing that check, only narrowing it — so
    `expiry_auto_close(exchange='NFO')` behaves the same way across
    live/paper/sim, instead of live silently closing whatever OTHER
    exchange happens to be in-window too. Since the 2026-11-26
    exchange-pure split, BOTH default agents (`expiry-day-mcx-itm-auto-
    close`, `expiry-day-nse-itm-auto-close`) now set this explicitly —
    no code change was needed here to support the split, only the
    agent definitions. Omitted (e.g. a bare custom agent with no
    `exchange` param), it processes every exchange currently
    in-window, unchanged from before the 2026-11-24 fix.

    2026-11-23 fix (Part C — the expiry-close hold/auto-release
    mechanism): every HELD row this action's `close_positions()` call
    stages now gets a real `auto_release_at` deadline, via
    `_EXPIRY_HOLD_RELEASE_POLICY` below — "release it 30 minutes after
    staging if the operator does not respond, never later than
    close_time minus the high-tier chase's own worst-case duration".
    Before this, a HELD expiry-close row sat forever until an operator
    manually clicked Release — the gap this whole mechanism was
    originally meant to close.
    """
    from backend.api.algo.expiry import ExpiryEngine
    from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
        _expiry_preclose_exchanges,
    )
    from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_close_window import (
        CLOSE_LEAD_MINUTES,
    )

    shim = _al_expiry_context_shim(context)
    exchanges_in_window = _expiry_preclose_exchanges(shim, None, CLOSE_LEAD_MINUTES)
    requested_exchange = (params or {}).get("exchange")
    if requested_exchange:
        exchanges_in_window = exchanges_in_window & {requested_exchange.upper()}
    if not exchanges_in_window:
        logger.info(
            f"[LIVE] expiry_auto_close: agent {agent.slug} fired but no "
            f"exchange is currently within its own pre-close window "
            f"(stale context or a race) — skipping rather than guessing"
        )
        return

    engine = ExpiryEngine(agent_id=getattr(agent, "id", None))
    try:
        to_close = await asyncio.to_thread(engine.scan_positions)
    except Exception as e:
        logger.error(f"[LIVE] expiry_auto_close: scan failed for agent {agent.slug}: {e}")
        return

    to_close = [p for p in to_close if (p.exchange or "").upper() in exchanges_in_window]
    if not to_close:
        # 2026-11-24 fix (audit finding #11): an UNKNOWN-moneyness leg
        # (underlying_ltp <= 0 — a failed LTP fetch, or no brokers
        # configured, see `_fetch_underlying_ltps`) is never flagged
        # `needs_close` by `classify_prepared()`, so `to_close` reads
        # identically empty whether the book is genuinely clean OR the
        # spot-price fetch just failed. The alert-renderer siblings
        # (`_v2_expiry_preclose_alert_section` / `_v2_expiry_exchange_
        # section`) surface UNKNOWN lines loudly on this same data gap
        # — this action must not stay silent on it ("never a false
        # all-clear"). It still does NOT
        # close anything on unresolved data; it only stops claiming
        # "nothing to close" when the real answer is "don't know".
        unresolved = [
            p for p in (engine.state.positions or [])
            if (p.exchange or "").upper() in exchanges_in_window
            and p.moneyness == "UNKNOWN"
        ]
        if unresolved:
            logger.critical(
                f"[LIVE] expiry_auto_close: agent {agent.slug} — "
                f"{len(unresolved)} expiring position(s) in "
                f"{sorted(exchanges_in_window)} have UNKNOWN moneyness "
                f"(underlying LTP unresolved) and were skipped; this is "
                f"NOT a confirmed clean scan — needs manual review: "
                f"{[p.tradingsymbol for p in unresolved]}"
            )
        else:
            logger.info(f"[LIVE] expiry_auto_close: no positions need closing in "
                        f"{sorted(exchanges_in_window)} (agent={agent.slug})")
        return

    by_exch: dict[str, int] = {}
    for p in to_close:
        by_exch[p.exchange] = by_exch.get(p.exchange, 0) + 1
    logger.info(f"[LIVE] expiry_auto_close: agent {agent.slug} closing "
                f"{len(to_close)} positions ({by_exch})")
    await engine.close_positions(to_close, release_policy=_EXPIRY_HOLD_RELEASE_POLICY)
    logger.info(f"[LIVE] expiry_auto_close: agent {agent.slug} done — "
                f"closed={len(engine.state.closed)} failed={len(engine.state.failed)} "
                f"slippage=₹{engine.state.total_slippage:.2f}")


# Generic, DSL-expressible hold auto-release policy (see
# `order_hold._resolve_hold_auto_release_at`'s own docstring for the
# two recognised shapes) — this one is the operator's own explicit
# instruction for expiry-close holds: "release it 30 mins if user does
# not respond, whatever it takes". The rolling 30-minute deadline is
# clamped so it never runs later than close_time minus the high-tier
# chase's own worst-case duration (plus a fixed scan-latency margin —
# see `_floor_minutes_for_tier`), so a late-staged hold still gets a
# real deadline comfortably before close rather than one that leaves no
# time for the resumed chase to run.
_EXPIRY_HOLD_RELEASE_POLICY: dict = {"after_min": 30, "floor_tier": "high"}


# ═══════════════════════════════════════════════════════════════════════
#  Sprint 3 (auto-close-short-option plan) — require_release hold check
# ═══════════════════════════════════════════════════════════════════════
#
# Generic infrastructure: reusable by ANY future agent with
# `Agent.require_release = True`, not only the specific exit-rule agent
# this plan eventually seeds. Called from `actions._dispatch_live_action`'s
# single chokepoint — see that function's own comment for why the check
# must stay there (mode=='live' only) and never be hoisted earlier.

async def _al_find_held_agent_close(
    account: str, symbol: str, exchange: str, side: str,
) -> "int | None":
    """Existing non-terminal `AGENT_CLOSE` HELD row for this exact
    identity, if any. Queried BEFORE staging a new hold so a re-firing
    agent (e.g. escalation past cooldown, or a stale-latch re-match on
    the next engine tick) never produces a SECOND action-required alert
    for the same still-pending close. `record_held_order`'s own internal
    dedup guard (identical identity match, inside a `.with_for_update()`
    locked SELECT) still protects against the genuine DB-level race —
    this is a cheaper, unlocked pre-check purely to decide whether to
    alert again, not a correctness-critical lock."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    try:
        async with async_session() as s:
            return (await s.execute(
                select(AlgoOrder.id).where(
                    AlgoOrder.account == account,
                    AlgoOrder.symbol == symbol,
                    AlgoOrder.exchange == exchange,
                    AlgoOrder.transaction_type == side,
                    AlgoOrder.status == "HELD",
                )
            )).scalars().first()
    except Exception as e:
        logger.warning(f"[AGENT-CLOSE-HOLD] existing-hold lookup failed: {e}")
        return None


async def _al_agent_close_recently_cancelled_this_session(
    agent_id: "int | None", account: str, symbol: str, exchange: str, side: str,
) -> bool:
    """True when the most recent `AGENT_CLOSE` hold for this exact
    identity was explicitly cancelled by the operator earlier in the
    CURRENT trading session (see `expiry_freeze.session_date_of`).

    `cancel_held_order()` clears `hold_json` on cancel, so the category
    can't be read back from it after the fact — the discriminator here
    is `status == 'CANCELLED'` + `broker_order_id == ''` (a hold that was
    staged but NEVER placed at the broker; `record_held_order` always
    writes an empty string there, and nothing else in this codebase
    writes a CANCELLED row with an empty broker_order_id) + the matching
    identity fields + `agent_id`.

    Approximation, stated explicitly (not silently): this checks ONLY
    the session boundary, not "until the position is closed some other
    way" — re-verifying the live position here would require a broker
    call on every mere condition-match, which conflicts with this whole
    gate's own "zero broker calls while staging a hold" invariant. A
    position closed by another path during the suppression window is
    not separately detected by this check.

    On any lookup failure, fails TOWARD STAGING (returns False) — a
    hold places nothing at the broker, so continuing to offer it for
    operator review is the safer failure mode, not silently suppressing
    it forever because of a transient DB error.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.api.algo.expiry_freeze import session_date_of
    try:
        async with async_session() as s:
            row = (await s.execute(
                select(AlgoOrder).where(
                    AlgoOrder.account == account,
                    AlgoOrder.symbol == symbol,
                    AlgoOrder.exchange == exchange,
                    AlgoOrder.transaction_type == side,
                    AlgoOrder.agent_id == agent_id,
                    AlgoOrder.status == "CANCELLED",
                    AlgoOrder.broker_order_id == "",
                ).order_by(AlgoOrder.created_at.desc()).limit(1)
            )).scalars().first()
        if row is None:
            return False
        now = datetime.now(timezone.utc)
        return session_date_of(row.created_at) == session_date_of(now)
    except Exception as e:
        logger.warning(f"[AGENT-CLOSE-HOLD] cancel-suppression lookup failed: {e}")
        return False


def _al_agent_close_hold_message(
    account: str, symbol: str, exchange: str, side: str, qty: int, reason: str,
) -> str:
    """Build the action-required alert body (content only — no header).

    2026-11-08 audit fix (item 4) — this used to build its own origin
    header via `format_notification_header(...).splitlines()[0]`, which
    discarded the timestamp line. Header construction is now the
    renderer's job (`event_agents.py:_render_agent_close_hold`), the
    same producer/renderer split every other converted alert in this
    file follows, so the full header (identity + timestamp) survives.

    The account is masked explicitly here — `_AccountMaskFilter`
    (log_store.py) masks only `record.msg`/`exc_text`, never an `extra`
    dict's own values, and this body is passed through `extra['body']`,
    not the logged message string. `mask_account` is called exactly
    once (it is not idempotent — a double-mask of an already-masked
    value is wrong)."""
    from backend.shared.helpers.utils import mask_account
    masked_account = mask_account(account)
    return (
        f"{masked_account} {side} {qty} {symbol} ({exchange}) staged for "
        f"release, not placed — {reason}"
    )


async def _al_send_agent_close_hold_alert(
    agent, held_order_id: int, account: str, symbol: str, exchange: str,
    side: str, qty: int, reason: str, *, title_prefix: str = "Agent close held",
) -> None:
    """Fire one action-required alert for a newly-staged hold. Writes ONE
    log record via `_DISPATCH_LOGGER`, tagged `alert_event="agent_close_hold"`
    — a dedicated seeded agent (`event_agents.AGENT_CLOSE_HOLD_ALERT_AGENT` +
    its `_render_agent_close_hold` renderer) picks this up. Channel
    selection is resolved by `alert_routing.py`'s per-slug override for
    that agent's slug, not a per-record `channels` literal here (2026-11
    channel-authoring retirement — this used to embed its own hardcoded
    `[telegram, ntfy]` channel list in `extra`; every alert now routes
    through the shared tier/per-slug/catch-all chain the same way a
    plain log-only agent already does). `action_required=True` still
    runs through the existing binary gate
    (`event_agents._apply_action_required_ntfy_gate`/`_apply_action_
    required_email_gate`), forcing urgent ntfy plus a forced email
    entry regardless of the per-slug channel list. Never raises — a
    failed alert must not prevent the hold itself from having been
    staged.

    `title_prefix` (2026-11-24, audit finding #6): defaults to the exact
    original "Agent close held" text, so every pre-existing AGENT_CLOSE
    caller is byte-identical to before. `close_positions()` (expiry.py)
    is the one other caller, passing "Expiry close held" — same
    mechanism, same renderer (`_render_agent_close_hold` already reads
    `extra["title"]` verbatim), no new alert agent/renderer needed."""
    try:
        from backend.shared.helpers.alert_utils import _DISPATCH_LOGGER
        agent_key = getattr(agent, "slug", None) or getattr(agent, "id", None) or "unknown"
        message = _al_agent_close_hold_message(
            account, symbol, exchange, side, qty, reason,
        )
        _DISPATCH_LOGGER.warning(
            f"[AGENT-CLOSE-HOLD] {message}",
            extra={
                "tags": ["agent", "alert"], "alert_event": "agent_close_hold",
                "title": f"{title_prefix} — {agent_key}", "body": message,
                "action_required": True,
                "agent_id": getattr(agent, "id", None),
                "agent_name": getattr(agent, "name", None),
                "held_order_id": held_order_id,
            },
        )
    except Exception as log_e:
        logger.error(
            f"[AGENT-CLOSE-HOLD] alert log record failed for held order "
            f"{held_order_id}: {log_e}"
        )


def _al_resolve_release_timeout_minutes(agent) -> "int | None":
    """`agent.release_timeout_minutes` only if it is a genuine positive
    int — never a bare truthiness check (a MagicMock test double would
    auto-vivify a truthy, non-int attribute). `0` and any non-positive
    value resolve to `None` ("never auto-releases"), matching `hold_
    record`'s own "else None" convention — there is no meaningful
    "auto-release after 0 minutes"."""
    v = getattr(agent, "release_timeout_minutes", None)
    return v if isinstance(v, int) and v > 0 else None


async def _al_maybe_stage_agent_close_hold(agent, params: dict, context: dict) -> None:
    """The `require_release` hold check itself (piece 4 of Sprint 3).

    Called from `actions._dispatch_live_action`'s chokepoint whenever
    `agent.require_release is True` AND `action_type == "close_position"`
    — this function is ALWAYS the terminal step on that path; the caller
    never falls through to the real broker handler regardless of what
    happens here (a verification failure, a suppressed re-stage, or a
    DB failure while staging all fail CLOSED — the close is refused, not
    placed live).

    Resolves the close the EXACT SAME way `_action_live_close_position`
    does — `_al_close_resolve_params` + `_al_close_resolve_position`,
    reused verbatim, not duplicated with drift. A `ValueError` from
    `_al_close_resolve_params` (missing account/symbol, negative qty) is
    allowed to propagate, exactly as it already does for the real
    close_position path.
    """
    account, symbol, exchange, qty_param, side_param, product_param = (
        _al_close_resolve_params(params)
    )
    resolved = await _al_close_resolve_position(
        account, symbol, exchange, side_param, qty_param, product_param,
    )
    if resolved is None:
        logger.info(
            f"[AGENT-CLOSE-HOLD] agent {getattr(agent, 'slug', '?')}: close "
            f"could not be verified against the real position for "
            f"{exchange}/{symbol} — nothing staged, nothing placed"
        )
        return
    side, qty, product = resolved
    agent_id = getattr(agent, "id", None)

    # 2026-11-24 fix (audit finding #3): generalized from expiry.py's
    # own in-flight guard — without this, a released AGENT_CLOSE or
    # EXPIRY_CLOSE hold that is `OPEN` and still chasing was invisible
    # to this function's dedup below (which only sees `status=='HELD'`
    # rows), so a second HELD row could stage and auto-release while
    # the first chase was still live, flipping the leg. Identity-only
    # (no HoldCategory filter), so it also blocks against a hold the
    # OTHER category is already working for this exact leg.
    from backend.api.algo.order_inflight import _exp_leg_in_flight_this_session
    if await _exp_leg_in_flight_this_session(account, symbol, exchange, side):
        logger.info(
            f"[AGENT-CLOSE-HOLD] agent {getattr(agent, 'slug', '?')}: "
            f"{side} {symbol} ({exchange}) already has an unresolved order "
            f"this session (HELD/OPEN/CANCEL_FAILED) — skipping re-stage"
        )
        return

    if await _al_agent_close_recently_cancelled_this_session(
        agent_id, account, symbol, exchange, side,
    ):
        logger.info(
            f"[AGENT-CLOSE-HOLD] agent {getattr(agent, 'slug', '?')}: "
            f"{side} {symbol} ({exchange}) was explicitly cancelled earlier "
            f"this session — suppressing re-stage"
        )
        return

    existing_id = await _al_find_held_agent_close(account, symbol, exchange, side)
    if existing_id is not None:
        logger.info(
            f"[AGENT-CLOSE-HOLD] agent {getattr(agent, 'slug', '?')}: "
            f"{side} {symbol} ({exchange}) already has a HELD row "
            f"id={existing_id} — not re-alerting"
        )
        return

    from datetime import datetime, timedelta, timezone
    timeout_minutes = _al_resolve_release_timeout_minutes(agent)
    held_at_for_deadline = datetime.now(timezone.utc)
    auto_release_at = (
        held_at_for_deadline + timedelta(minutes=timeout_minutes)
        if timeout_minutes is not None else None
    )

    from backend.api.algo.order_hold import HoldCategory
    from backend.api.algo.order_hold_gate import record_held_order
    reason = f"require_release agent '{getattr(agent, 'slug', '?')}' close_position"
    row_id = await record_held_order(
        HoldCategory.AGENT_CLOSE,
        account=account, symbol=symbol, exchange=exchange, side=side,
        qty=qty, product=product, reason=reason, agent_id=agent_id,
        auto_release_at=auto_release_at,
    )
    if row_id is None:
        logger.error(
            f"[AGENT-CLOSE-HOLD] agent {getattr(agent, 'slug', '?')}: failed "
            f"to stage hold for {side} {symbol} ({exchange}) — the close is "
            f"REFUSED, not placed live"
        )
        return

    await _al_send_agent_close_hold_alert(
        agent, row_id, account, symbol, exchange, side, qty, reason,
    )
