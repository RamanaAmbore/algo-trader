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
                                    template_id=None,
                                    template_slug=None,
                                    overrides: "dict | None" = None) -> "int | None":
    """Write OPEN AlgoOrder row and fire preflight_ok event (best-effort).

    Returns the AlgoOrder row id so callers can pass algo_order_id to chase_order.

    `product`/`template_id`/`template_slug`/`overrides` are applied via
    `_place_order_set_product_template` AFTER `_write_live_order` returns —
    that shared constructor (used by close_position/chase_close_positions
    too) is never touched. The helper swallows its own exceptions, so a
    failure there can never turn a real `intent_id` into None here.
    """
    from backend.api.algo.actions import _write_live_order

    try:
        intent_id = await _write_live_order(
            agent_shim, "place_order",
            {"account": account, "symbol": symbol, "exchange": exchange,
             "side": side, "qty": qty, "price": price},
            status="OPEN",
        )
        if intent_id:
            await _place_order_set_product_template(
                intent_id, product, template_id,
                template_slug=template_slug, overrides=overrides,
            )
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
) -> "tuple[object, str, str, str, str, int, object, str, object, object]":
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

    `template_slug` is read alongside `template_id` (2026-10 fix) — the
    place_order params_schema (agent_grammar.yaml) documents both as
    mutually exclusive, and the Automation page's Bracket picker writes
    `template_slug`, not `template_id`. Prior to this fix only
    `template_id` was ever read here, so an agent configured via
    `template_slug` silently got a naked live entry with zero exits.
    `template_id` still wins when both are set (matches
    `load_template_for_slug_or_id`'s own id-over-slug priority).

    Returns (shim, account, symbol, exchange, side, qty, price, product,
    template_id, template_slug).
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
        params.get("template_id"),
        params.get("template_slug"),
    )


async def _place_order_resolve_template_slug(template_slug: str) -> "int | None":
    """Resolve a `template_slug` to its `OrderTemplate.id`.

    Reuses `template_attach.load_template_for_slug_or_id` — the SAME
    resolver every other template-attach path (OrderTicket, basket, the
    sim-mode agent action via `_maybe_attach_template_from_action`)
    already relies on for id-over-slug priority + the `OrderTemplate.slug
    == slug` DB lookup. The live place_order path needs its own call
    site for this (rather than passing `template_slug` straight through
    like the sim path does) because `AlgoOrder.template_id` is a plain
    int FK column with no slug column — resolution has to happen before
    persist, not at fill time inside `apply_template_to_order`.

    Returns None (and logs a warning) when the slug does not resolve to
    any OrderTemplate row, or when the lookup itself fails — callers
    treat None the same as "no template requested" rather than guessing.
    """
    from backend.api.algo.template_attach import load_template_for_slug_or_id

    try:
        tmpl = await load_template_for_slug_or_id(
            template_id=None, template_slug=str(template_slug)
        )
    except Exception as e:
        logger.warning(
            f"[LIVE] place_order: template_slug={template_slug!r} lookup failed: {e}"
        )
        return None
    if tmpl is None:
        logger.warning(
            f"[LIVE] place_order: template_slug={template_slug!r} did not resolve "
            f"to any OrderTemplate — no template will be attached on fill"
        )
        return None
    return int(tmpl["id"])


def _place_order_overrides_json(overrides: "dict | None") -> "str | None":
    """Serialize place_order per-leg template overrides to a JSON string
    for `AlgoOrder.template_overrides_json`.

    Keys/shape mirror `orders_helpers._build_overrides_json` exactly
    (tp_pct / sl_pct / wing_premium_pct / wing_strike_offset) — the same
    shape `_opp_load_row_for_attach` (orders_place.py) parses back out of
    this column and feeds to `apply_template_to_order` at fill time.
    Returns None when `overrides` is empty/None or every value is None,
    so the DB column is left NULL rather than storing an empty object.
    """
    if not overrides:
        return None
    payload = {k: v for k, v in overrides.items() if v is not None}
    if not payload:
        return None
    import json
    return json.dumps(payload)


async def _place_order_set_product_template(
    row_id: int, product: str, template_id,
    template_slug=None, overrides: "dict | None" = None,
) -> None:
    """Set product/template_id/template_overrides_json on a freshly-written
    place_order AlgoOrder row.

    Sprint 1a (docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md §2.2): the
    shared `_write_live_order` constructor (actions.py) is also used by
    `close_position` and `chase_close_positions` — neither of which sets
    these fields today — so this is deliberately NOT added there.
    Instead it's a narrow, place_order-only follow-up UPDATE scoped to
    the single row just created, run immediately after
    `_write_live_order` returns so the postback / chase-terminal
    template-auto-attach path (which reads `AlgoOrder.template_id`,
    e.g. `orders_postback.py`, `chase.py:184`) sees the value the agent
    action requested on fill.

    Behavioral note: this is the first time a live agent-placed
    `place_order` action can carry a non-NULL `template_id` through to
    its AlgoOrder row — until now that auto-attach path only ever fired
    for OrderTicket/basket-submitted orders. An agent action that
    specifies `params.template_id` (OR `params.template_slug` — resolved
    here via `_place_order_resolve_template_slug`, 2026-10 fix) will now
    have real exit GTTs (and possibly a wing order) armed on fill, exactly
    like a manually ticketed templated order. The four `*_override`
    params (tp_pct / sl_pct / wing_premium_pct / wing_strike_offset) are
    now persisted too, via `template_overrides_json` — the SAME column
    `_opp_load_row_for_attach` already reads back for OrderTicket/basket
    fills, so no change was needed on the fill-time consumer side.
    Swallows all exceptions (logs + returns) so a failure here can never
    take down the caller's `intent_id`.
    """
    resolved_template_id = template_id
    if resolved_template_id is None and template_slug:
        resolved_template_id = await _place_order_resolve_template_slug(template_slug)

    overrides_json = _place_order_overrides_json(overrides)

    if not product and resolved_template_id is None and overrides_json is None:
        return
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import update as sa_update

    values: dict = {}
    if product:
        values["product"] = str(product)
    if resolved_template_id is not None:
        try:
            values["template_id"] = int(resolved_template_id)
        except (TypeError, ValueError):
            logger.warning(
                f"[LIVE] place_order ignoring non-numeric "
                f"template_id={resolved_template_id!r}"
            )
    if overrides_json is not None:
        values["template_overrides_json"] = overrides_json
    if not values:
        return
    try:
        async with async_session() as s:
            await s.execute(
                sa_update(AlgoOrder).where(AlgoOrder.id == row_id).values(**values)
            )
            await s.commit()
    except Exception as e:
        logger.warning(
            f"[LIVE] place_order product/template_id update failed for row {row_id}: {e}"
        )


def _al_place_build_overrides(params: dict) -> "dict | None":
    """Build the template-override dict from a live place_order action's
    params, for persisting onto `AlgoOrder.template_overrides_json`.

    Reuses `actions._build_template_overrides` (lazy import to avoid a
    circular import — `actions.py` imports from this module too) — the
    EXACT same override-resolution function the sim-mode path already
    calls via `_maybe_attach_template_from_action`, including its
    legacy `target_pct` → `tp_pct` back-compat mapping. Returns None
    (rather than a dict of all-None values) when none of the four
    `*_override` params (or `target_pct`) were supplied, so
    `_place_order_set_product_template` can tell "no overrides" apart
    from "overrides explicitly set to null".
    """
    from backend.api.algo.actions import _build_template_overrides

    overrides = _build_template_overrides(params)
    overrides = {k: v for k, v in overrides.items() if v is not None}
    return overrides or None


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
    """
    import asyncio
    from backend.api.algo.chase import chase_order
    from backend.api.routes.orders_helpers import _live_chase_config
    from backend.brokers import get_broker

    _shim, account, symbol, exchange, side, qty, price, product, template_id, template_slug = (
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
        product=product, template_id=template_id, template_slug=template_slug,
        overrides=_al_place_build_overrides(params),
    )
    if _oid is None:
        # AlgoOrder pre-persist failed (DB exception) — fail closed.
        # Never call chase_order() without a tracking row behind it.
        await _on_algo_order_write_failure(
            _shim, "place_order", account, symbol, exchange, side, qty, price,
        )
        raise RuntimeError(
            f"place_order: AlgoOrder pre-persist failed for {account} "
            f"{exchange}/{symbol} {side} {qty} — refusing to place an "
            f"untracked live order"
        )

    aggressiveness = str(
        params.get("chase_level") or params.get("chase_aggressiveness") or "med"
    ).lower()
    cfg = _live_chase_config(aggressiveness, product=product)
    cfg.exchange = exchange or "NFO"
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
            algo_order_id=_oid,
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
    logger.error(f"[LIVE] close_position failed for {account} {exchange}/{symbol} "
                 f"{side} {qty}: {e} | diag: {diag}")
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
        raise RuntimeError(
            f"close_position: AlgoOrder pre-persist failed for {account} "
            f"{exchange}/{symbol} {side} {qty} — refusing to place an "
            f"untracked live order"
        )

    cfg = ChaseConfig(exchange=exchange, product=product, intent="close")
    try:
        await chase_order(
            account=account, symbol=symbol,
            transaction_type=side, quantity=qty,
            cfg=cfg,
            algo_order_id=_oid,
        )
    except Exception as e:
        await _close_position_on_failure(
            e, agent, broker, account, symbol, exchange, side, qty, price, product,
        )
        raise


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

        cfg = ChaseConfig(exchange=exchange, product="NRML", intent="close")
        chase_tasks.append(
            asyncio.create_task(
                chase_order(account=acct, symbol=symbol,
                            transaction_type=side, quantity=qty, cfg=cfg,
                            algo_order_id=_oid)
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

    Scope resolution — params.scope:
      'total'   (default) — every position across all accounts
      'account'           — positions for params.account only

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

    rows = _chase_resolve_positions(context, params)
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

    from backend.api.algo.order_hold_gate import before_cutoff
    if before_cutoff(exch):
        logger.info(f"[LIVE] expiry_auto_close: {exch} before cut-off; scan deferred (agent={agent.slug})")
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
