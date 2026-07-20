"""
orders_basket.py — Basket order dispatch and margin computation helpers.

Extracted from orders.py (4322 LOC → split) as Commit 3 of the RED-zone split.

Exports:
  basket_margin_handler   — implements POST /api/orders/basket/margin logic
  basket_order_handler    — implements POST /api/orders/basket logic

These are called by OrdersController methods; the controller delegates to
functions here so the controller file stays thin.

No imports from orders.py at module level — lazy imports in function bodies
avoid circular dependencies.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from litestar import Request
from litestar.exceptions import HTTPException

from backend.api.schemas import (
    BasketGroup,
    BasketGroupResult,
    BasketLegResult,
    BasketMarginGroupResult,
    BasketMarginResponse,
    BasketOrderRequest,
    BasketOrderResponse,
)
from backend.api.routes.orders_helpers import (
    _EXCHANGES,
    _TXN_TYPES,
    _broker_for,
    _build_overrides_json,
    _resolve_target_pct,
    logger,
)
from backend.api.routes.orders_place import _attach_basket_leg_template
from backend.api.auth_guard import is_admin_request


async def basket_margin_handler(
    data: BasketOrderRequest,
    request: Request,
) -> BasketMarginResponse:
    """
    Compute the offset-aware margin for a basket of orders WITHOUT placing them.

    Calls broker.basket_order_margins(orders) per account in parallel.
    This is the true Kite basket benefit: spreads and hedges reduce
    the required margin vs. summing per-leg margin individually.

    Demo sessions → 403. Non-admin → 403.
    """
    if getattr(request.state, "is_demo", False):
        raise HTTPException(status_code=403, detail="Demo: basket margin requires sign-in.")
    if not is_admin_request(request):
        raise HTTPException(status_code=403, detail="Admin access required.")

    async def _margin_for_group(grp: BasketGroup) -> BasketMarginGroupResult:
        account = (grp.account or "").strip()
        if not account:
            return BasketMarginGroupResult(
                account=account, required=None, available=None,
                shortfall=None, error="account is required",
            )
        try:
            broker = _broker_for(account)
            from backend.brokers.adapters.kite import get_lot_size as _bm_get_lot_size
            # Build the payload with broker-translated qty per leg.
            # v2 API convention (2026-07-08): request `leg.quantity` is
            # LOTS for F&O, shares for equity. We compute contracts =
            # lots × lot_size, then call translate_qty to convert to
            # Kite's basket_order_margins convention (lots for MCX,
            # contracts everywhere else). For NFO/CDS/BFO translate_qty
            # is a no-op, so contracts pass through as expected; for
            # MCX contracts → lots which is what Kite expects here.
            _FO = ("NFO", "MCX", "CDS", "BFO", "BCD", "NCO")
            orders_payload = []
            _bad_lot_legs: list[str] = []   # M20: collect unresolvable legs
            for leg in grp.legs:
                _leg_exch = leg.exchange.upper()
                _leg_sym  = leg.tradingsymbol.upper()
                _leg_input = int(leg.quantity or 0)   # lots (F&O) or shares (equity)
                _leg_lot  = await _bm_get_lot_size(_leg_exch, _leg_sym)
                if _leg_exch in _FO:
                    if _leg_lot <= 0:
                        # M20: track legs with unresolvable lot_size so the
                        # group-level error field surfaces the issue instead
                        # of silently skipping (placement path will 503 anyway;
                        # margin preview is best-effort but should indicate why
                        # a leg was excluded).
                        _bad_lot_legs.append(f"{_leg_exch}/{_leg_sym}")
                        continue
                    _leg_contracts = _leg_input * _leg_lot
                else:
                    _leg_contracts = _leg_input
                _leg_bq   = broker.translate_qty(_leg_exch, _leg_contracts, _leg_lot)
                orders_payload.append({
                    "exchange":         _leg_exch,
                    "tradingsymbol":    _leg_sym,
                    "transaction_type": leg.transaction_type.upper(),
                    "variety":          leg.variety or "regular",
                    "product":          leg.product or "NRML",
                    "order_type":       leg.order_type or "LIMIT",
                    "quantity":         _leg_bq,
                    "price":            float(leg.price or 0),
                    "trigger_price":    float(leg.trigger_price or 0),
                })
                logger.info(
                    f"[BASKET-MARGIN] qty translated "
                    f"{_leg_exch}/{_leg_sym}: input_lots={_leg_input} "
                    f"→ contracts={_leg_contracts} → kite_qty={_leg_bq} "
                    f"(lot_size={_leg_lot})"
                )
            result = await asyncio.to_thread(broker.basket_order_margins, orders_payload)
            # Kite's basket_order_margins returns a LIST — one entry per
            # input leg. Each entry has {"initial": {...}, "final": {...}}.
            # The NET basket margin is the LAST element's final.total (Kite
            # accumulates the hedge offset incrementally). The old code tried
            # result.get("final") on a list → always {}, giving required=0.
            required: float = 0.0
            available: float = 0.0
            if isinstance(result, list) and result:
                # Use the last entry — it carries the cumulative basket total.
                _last = result[-1]
                if isinstance(_last, dict):
                    _final = (_last.get("final") or {})
                    _avail_block = (_last.get("initial") or {}).get("available") or {}
                    for _branch in ("total",):
                        _v = _final.get(_branch)
                        if _v is not None:
                            try:
                                required = float(_v)
                            except (TypeError, ValueError):
                                pass
                            break
                    _cash = _avail_block.get("cash") or _avail_block.get("adhoc_margin") or 0
                    try:
                        available = float(_cash)
                    except (TypeError, ValueError):
                        available = 0.0
            elif isinstance(result, dict):
                # Fallback for non-list response shapes (non-Kite adapters).
                _final = (result.get("final") or {})
                _avail = (result.get("initial") or {}).get("available") or {}
                try:
                    required = float(_final.get("total") or 0)
                except (TypeError, ValueError):
                    required = 0.0
                try:
                    available = float(_avail.get("cash") or 0)
                except (TypeError, ValueError):
                    available = 0.0
            # Sanity-check: negative required means Kite returned a credit
            # margin or an error signal — treat as zero for display purposes
            # but log for diagnostics.
            if required < 0:
                logger.warning(
                    f"[BASKET-MARGIN] negative margin from broker for "
                    f"{account}: required={required:.2f} — treating as 0. "
                    f"Likely anomalous Kite response; check payload qty units."
                )
                required = 0.0
            shortfall = max(0.0, required - available)
            # M20: if any F&O legs had unresolvable lot_size, surface them
            # in the group-level error field so the operator is not silently
            # misled by a margin figure that excludes those legs.
            _bad_leg_err: str | None = None
            if _bad_lot_legs:
                _bad_leg_err = (
                    f"lot_size unresolvable for legs: "
                    f"{', '.join(_bad_lot_legs)} — margin excludes these legs"
                )
                logger.warning(
                    "[BASKET-MARGIN] unresolvable lot_size for %s acct=%s: "
                    "excluded from margin calc: %s",
                    account, account, _bad_lot_legs,
                )
            return BasketMarginGroupResult(
                account=account,
                required=required,
                available=available,
                shortfall=shortfall,
                error=_bad_leg_err,
            )
        except HTTPException:
            raise
        except Exception as _e:
            logger.warning(f"[BASKET-MARGIN] account={account} error={_e}")
            return BasketMarginGroupResult(
                account=account, required=None, available=None,
                shortfall=None, error=str(_e)[:500],
            )

    results = await asyncio.gather(*[_margin_for_group(g) for g in data.groups])
    return BasketMarginResponse(groups=list(results))


async def basket_order_handler(
    data: BasketOrderRequest,
    request: Request,
) -> BasketOrderResponse:
    """
    True multi-account basket order endpoint.

    Legs are grouped by account.  Per group:
      - LIVE mode: each leg is dispatched via broker.place_order with a
        shared `tag="rb-bk-<uuid>"`.  Groups run concurrently
        via asyncio.gather; legs within a group run in sequence (Kite
        expects sequential placement for a basket — the tag links them
        server-side).
      - PAPER mode: each leg is persisted as an AlgoOrder and registered
        with the prod paper engine.
      - SHADOW mode: logs the Kite payload + computes basket_margin
        without executing.

    Demo → 403 (consistent with /ticket).
    Non-prod branch + mode=live → 403.
    paper_trading_mode=ON + mode=live → 403.
    """
    from backend.api.database import async_session as _async_session2
    from backend.api.models import AlgoOrder as _AlgoOrder2
    from backend.shared.helpers.utils import is_prod_branch
    from backend.shared.helpers.settings import get_bool as _get_bool
    from backend.api.cache import invalidate

    if getattr(request.state, "is_demo", False):
        raise HTTPException(status_code=403,
            detail="Demo: basket orders require sign-in.")
    if not is_admin_request(request):
        raise HTTPException(status_code=403,
            detail="Admin access required for basket orders.")

    # Resolve effective mode — same gate as /ticket.
    _ptm = _get_bool("execution.paper_trading_mode", False)
    _shadow = _get_bool("execution.shadow_mode", False)
    _shadow_on = _shadow and is_prod_branch()
    # Determine mode: shadow > paper_trading_mode > LIVE.
    if _shadow_on:
        eff_mode = "shadow"
    elif not is_prod_branch() or _ptm:
        eff_mode = "paper"
    else:
        eff_mode = "live"

    eff_target_pct = _resolve_target_pct(data.target_pct)

    async def _dispatch_group(grp: BasketGroup) -> BasketGroupResult:
        account = (grp.account or "").strip()
        if not account:
            return BasketGroupResult(
                account=account,
                basket_id="",
                results=[BasketLegResult(leg_index=i, order_id=None,
                                        status="error",
                                        error="account is required")
                         for i in range(len(grp.legs))],
            )

        # Use _loaded_accounts() so the cutover-flag path works
        # (Connections().conn is empty under RAMBOQ_USE_CONN_SERVICE=1).
        from backend.brokers.registry import _loaded_accounts
        if account not in _loaded_accounts():
            return BasketGroupResult(
                account=account,
                basket_id="",
                results=[BasketLegResult(leg_index=i, order_id=None,
                                        status="error",
                                        error=f"unknown account: {account}")
                         for i in range(len(grp.legs))],
            )

        # Kite's `tag` field is hard-capped at 20 chars (the broker
        # rejects anything longer with "invalid tags - maximum
        # allowed length is 20"). `rb-bk-` + 12 hex = 18 chars,
        # leaves 2 chars of headroom for any future suffix.
        basket_id = f"rb-bk-{_uuid.uuid4().hex[:12]}"
        leg_results: list[BasketLegResult] = []

        for i, leg in enumerate(grp.legs):
            sym = leg.tradingsymbol.upper().strip()
            side = leg.transaction_type.upper()
            input_qty = int(leg.quantity or 0)   # LOTS for F&O, shares for equity
            exch = (leg.exchange or "NFO").upper()

            # Basic validation per leg.
            if not sym or input_qty <= 0 or side not in _TXN_TYPES or exch not in _EXCHANGES:
                leg_results.append(BasketLegResult(
                    leg_index=i, order_id=None, status="error",
                    error=f"invalid leg: sym={sym} qty={input_qty} side={side} exch={exch}",
                ))
                continue

            # F&O: request qty is LOTS. Resolve lot_size + multiply to
            # contracts for internal use. Same convention as /ticket.
            _FO = ("NFO", "MCX", "CDS", "BFO", "BCD", "NCO")
            if exch in _FO:
                from backend.brokers.adapters.kite import get_lot_size as _ls_lookup
                try:
                    _lot = int(await _ls_lookup(exch, sym) or 0)
                except Exception:
                    _lot = 0
                if _lot <= 0:
                    logger.error(
                        "[FO-LOT-GUARD] basket leg rejected: acct=%s sym=%s "
                        "lots=%s — lot_size unresolvable for %s (instruments "
                        "cache cold). Refusing to prevent oversize order.",
                        account, sym, input_qty, exch,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=(f"lot_size for {sym} on {exch} unavailable "
                                f"(cache cold) — retry in a moment"),
                    )
                qty = input_qty * _lot   # contracts
                # G2 fat-finger cap — 5-lot cap for F&O (MCX exempt;
                # its own 20-lot cap fires just below).
                _lots = input_qty
                _leg_close = (getattr(leg, "intent", None) or "").lower() == "close"
                _is_mcx = exch in ("MCX", "NCO")
                if not _leg_close and not _is_mcx and _lots > 5:
                    logger.warning(
                        "[FAT-FINGER-GUARD] basket leg rejected: "
                        "acct=%s sym=%s lots=%s lot_size=%s",
                        account, sym, _lots, _lot,
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="error",
                        error=(f"{_lots} lots exceeds 5-lot safety cap "
                               f"(lot_size={_lot})"),
                    ))
                    continue
                # MCX 20-lot cap — close orders are exempt (operator must
                # be able to close any open position regardless of size).
                if _is_mcx and not _leg_close and _lots > 20:
                    logger.error(
                        "[MCX-SIZE-GUARD] basket leg rejected: "
                        "acct=%s sym=%s lots=%s lot_size=%s "
                        "— exceeds 20-lot safety cap.",
                        account, sym, _lots, _lot,
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="error",
                        error=(f"Order size {_lots} lots for {sym} exceeds "
                               f"the 20-lot MCX safety limit. Contact support "
                               f"to increase the limit."),
                    ))
                    continue
            else:
                qty = input_qty   # equity: raw shares

            if eff_mode == "live":
                # ── LIVE safety checks ────────────────────────────────────
                # 1. Market-hours gate — skip for AMO legs (placed when
                #    exchange is closed by design).
                _leg_variety = (leg.variety or "regular").lower()
                if _leg_variety != "amo":
                    from backend.api.algo.agent_engine import (
                        _symbol_exchange_open as _bk_seg_open,
                        _build_now_ctx as _bk_now_ctx,
                    )
                    if not _bk_seg_open(exch, _bk_now_ctx()):
                        logger.warning(
                            "[BASKET-LIVE] leg %d rejected: exchange %s closed "
                            "(not AMO). acct=%s sym=%s",
                            i, exch, account, sym,
                        )
                        leg_results.append(BasketLegResult(
                            leg_index=i, order_id=None, status="error",
                            error=f"Exchange {exch} is closed — use variety=amo or retry during market hours",
                        ))
                        continue

                # 2. MCX 20-lot cap — `_lot` is always set here because MCX/NCO
                #    is in _FO; the F&O block above resolved it before we reach
                #    this LIVE branch. `qty` is contracts = input_qty × _lot.
                if exch in ("MCX", "NCO"):
                    _bk_lots_for_cap = int(qty // _lot) if _lot > 0 else 0
                    _MCX_CAP = 20
                    if _bk_lots_for_cap > _MCX_CAP:
                        logger.error(
                            "[BASKET-LIVE] leg %d rejected: MCX size cap. "
                            "acct=%s sym=%s lots=%d > %d",
                            i, account, sym, _bk_lots_for_cap, _MCX_CAP,
                        )
                        leg_results.append(BasketLegResult(
                            leg_index=i, order_id=None, status="error",
                            error=f"MCX lot cap: {_bk_lots_for_cap} > {_MCX_CAP}",
                        ))
                        continue

                # 3. Preflight (margin shortfall + segment inactive).
                #    intent="close" bypasses G2 inside run_preflight.
                _leg_intent = getattr(leg, "intent", None)
                try:
                    from backend.api.algo.actions import run_preflight as _bk_run_preflight
                    _bk_pf = await _bk_run_preflight(account, {
                        "exchange":         exch,
                        "tradingsymbol":    sym,
                        "quantity":         qty,
                        "order_type":       (leg.order_type or "LIMIT"),
                        "product":          (leg.product or "NRML"),
                        "variety":          _leg_variety,
                        "transaction_type": side,
                        "intent":           _leg_intent,
                        "price":            float(leg.price or 0),
                        "trigger_price":    float(leg.trigger_price or 0),
                    })
                except Exception as _bk_pf_err:
                    logger.error(
                        "[BASKET-LIVE] preflight raised for acct=%s leg %d %s: %s",
                        account, i, sym, _bk_pf_err,
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="error",
                        error=f"Preflight check failed: {str(_bk_pf_err)[:240]}",
                    ))
                    continue
                if not _bk_pf.get("ok"):
                    _bk_blocked_codes = [b.get("code", "?") for b in _bk_pf.get("blocked", [])]
                    _bk_first_block = (_bk_pf.get("blocked") or [{}])[0]
                    # M8: include the full blocked-codes list in the error so
                    # the operator sees every blocker, not just the first one.
                    _bk_all_reasons = "; ".join(
                        b.get("reason") or b.get("code", "?")
                        for b in _bk_pf.get("blocked", [])
                    ) or f"preflight blocked: {', '.join(_bk_blocked_codes)}"
                    if any(c in ("MARGIN_SHORTFALL", "SEGMENT_INACTIVE") for c in _bk_blocked_codes):
                        logger.warning(
                            "[BASKET-LIVE] leg %d blocked by preflight: "
                            "acct=%s sym=%s codes=%s",
                            i, account, sym, _bk_blocked_codes,
                        )
                        leg_results.append(BasketLegResult(
                            leg_index=i, order_id=None, status="error",
                            error=_bk_all_reasons[:500],
                        ))
                        continue
                    # Other preflight blockers (LOT_MULTIPLE, FAT_FINGER etc.)
                    # were already caught by the guards above; log and continue
                    # placing so only margin/segment issues gate here.
                    logger.warning(
                        "[BASKET-LIVE] leg %d preflight non-critical blocker(s): "
                        "acct=%s sym=%s codes=%s — proceeding to place",
                        i, account, sym, _bk_blocked_codes,
                    )
                # ── end LIVE safety checks ────────────────────────────────

                try:
                    broker = _broker_for(account)
                    from backend.brokers.adapters.kite import get_lot_size
                    from backend.api.routes.orders_helpers import _align_price_to_tick
                    _ls = await get_lot_size(exch, sym)
                    _kq = broker.translate_qty(exch, qty, _ls)
                    # M16: align limit/SL prices to the tick grid so the
                    # broker doesn't reject with "invalid price" on F&O legs
                    # whose price was typed by the operator without rounding.
                    _leg_order_type = (leg.order_type or "LIMIT").upper()
                    _leg_price = float(leg.price or 0)
                    _leg_trig  = float(leg.trigger_price or 0)
                    if _leg_order_type in ("LIMIT", "SL", "SL-M"):
                        if _leg_price > 0:
                            _leg_price = await _align_price_to_tick(exch, sym, _leg_price)
                        if _leg_trig > 0:
                            _leg_trig = await _align_price_to_tick(exch, sym, _leg_trig)
                    kite_oid = await asyncio.to_thread(
                        broker.place_order,
                        variety=leg.variety or "regular",
                        exchange=exch,
                        tradingsymbol=sym,
                        transaction_type=side,
                        quantity=_kq,
                        product=leg.product or "NRML",
                        order_type=_leg_order_type,
                        price=_leg_price,
                        trigger_price=_leg_trig,
                        validity="DAY",
                        tag=basket_id,
                    )
                    # Persist AlgoOrder row so the order book tracks it.
                    async with _async_session2() as _s:
                        _r = _AlgoOrder2(
                            account=account, symbol=sym, exchange=exch,
                            transaction_type=side, quantity=qty,
                            initial_price=float(leg.price or 0) or None,
                            broker_order_id=str(kite_oid),
                            status="OPEN", engine="live", mode="live",
                            basket_tag=basket_id,
                            strategy_id=leg.strategy_id,
                            target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                            template_id=leg.template_id,
                            template_overrides_json=_build_overrides_json(leg),
                            product=(leg.product or "NRML"),
                        )
                        _s.add(_r)
                        await _s.commit()
                        _live_aid = _r.id
                    invalidate("orders")
                    await _attach_basket_leg_template(
                        algo_order_id=_live_aid,
                        template_id=leg.template_id,
                        account=account, sym=sym, side=side, qty=qty,
                        exch=exch,
                        price=float(leg.price or 0),
                        product=(leg.product or "NRML"),
                    )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=str(kite_oid), status="OPEN",
                    ))
                except Exception as _e:
                    # Use 500 chars so broker messages like "Margin required:
                    # 29870396.16. Margin available: 9216518.60. Add
                    # 20653877.56 to place this order." reach the UI in full.
                    _full_err = str(_e)
                    # M10: surface margin shortfall as WARNING (not ERROR) so
                    # it's clearly distinguished from system faults in logs.
                    _is_margin_err = any(
                        kw in _full_err.lower()
                        for kw in ("margin required", "margin available",
                                   "insufficient funds", "margin shortfall")
                    )
                    if _is_margin_err:
                        logger.warning(
                            "[BASKET-LIVE] margin shortfall: acct=%s leg %d "
                            "%s %s qty=%s: %s",
                            account, i, side, sym, qty, _full_err,
                        )
                    else:
                        logger.error(
                            f"[BASKET-LIVE] {account} leg {i} {side} {qty} "
                            f"{sym}@{leg.price} rejected: {_full_err}"
                        )
                    leg_results.append(BasketLegResult(
                        leg_index=i, order_id=None, status="error",
                        error=_full_err[:500],
                    ))

            elif eff_mode == "shadow":
                # Log payload; compute basket margin for the group but
                # don't place any orders.
                logger.info(
                    f"[SHADOW-BASKET] {account} leg {i}: {side} {qty} {sym} "
                    f"@{leg.price} tag={basket_id}"
                )
                async with _async_session2() as _s:
                    _r = _AlgoOrder2(
                        account=account, symbol=sym, exchange=exch,
                        transaction_type=side, quantity=qty,
                        initial_price=float(leg.price or 0) or None,
                        status="OPEN", engine="shadow", mode="shadow",
                        basket_tag=basket_id,
                        strategy_id=leg.strategy_id,
                        target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                        template_id=leg.template_id,
                        template_overrides_json=_build_overrides_json(leg),
                        product=(leg.product or "NRML"),
                        detail=f"[SHADOW-BASKET] leg {i} tag={basket_id}",
                    )
                    _s.add(_r)
                    await _s.commit()
                    _shadow_aid = _r.id
                await _attach_basket_leg_template(
                    algo_order_id=_shadow_aid,
                    template_id=leg.template_id,
                    account=account, sym=sym, side=side, qty=qty,
                    exch=exch,
                    price=float(leg.price or 0),
                    product=(leg.product or "NRML"),
                )
                leg_results.append(BasketLegResult(
                    leg_index=i, order_id=None, status="SHADOW",
                ))

            else:
                # Paper mode.
                from backend.api.algo.paper import get_prod_paper_engine
                _manual_aid2: int | None = None
                try:
                    from backend.api.algo.agent_engine import get_agent_id_by_slug as _ga
                    _manual_aid2 = await _ga("manual")
                except Exception:
                    pass
                _detail = (f"[PAPER-BASKET] {side} {qty} {sym} "
                           f"tag={basket_id}")
                async with _async_session2() as _s:
                    _r = _AlgoOrder2(
                        account=account, symbol=sym, exchange=exch,
                        transaction_type=side, quantity=qty,
                        initial_price=float(leg.price or 0) or None,
                        status="OPEN", engine="paper", mode="paper",
                        agent_id=_manual_aid2,
                        basket_tag=basket_id,
                        strategy_id=leg.strategy_id,
                        target_pct=(eff_target_pct if eff_target_pct > 0 else None),
                        template_id=leg.template_id,
                        template_overrides_json=_build_overrides_json(leg),
                        product=(leg.product or "NRML"),
                        detail=_detail,
                    )
                    _s.add(_r)
                    await _s.commit()
                    _paper_id = _r.id

                if leg.price and qty > 0:
                    try:
                        eng = get_prod_paper_engine()
                        eng.register_open_order({
                            "algo_order_id": _paper_id,
                            "account":       account,
                            "symbol":        sym,
                            "side":          side,
                            "qty":           qty,
                            "limit_price":   float(leg.price),
                            "initial_price": float(leg.price),
                            "exchange":      exch,
                            "agent_slug":    "basket-ticket",
                            "action_type":   "place_order",
                            "chase_agg":     "low",
                            "strategy_id":   getattr(leg, "strategy_id", None),
                            "is_close_intent": (getattr(leg, "intent", "") or "").lower() == "close",
                        })
                    except Exception as _pe:
                        logger.warning(
                            f"[PAPER-BASKET] engine register failed for "
                            f"#{_paper_id}: {_pe}"
                        )

                await _attach_basket_leg_template(
                    algo_order_id=_paper_id,
                    template_id=leg.template_id,
                    account=account, sym=sym, side=side, qty=qty,
                    exch=exch,
                    price=float(leg.price or 0),
                    product=(leg.product or "NRML"),
                )
                leg_results.append(BasketLegResult(
                    leg_index=i, order_id=str(_paper_id), status="PAPER",
                ))

        # Compute offset-aware margin for the group (best-effort; not a
        # gate — operators already see it before submitting via /basket/margin).
        margin_required: float | None = None
        margin_available: float | None = None
        try:
            broker = _broker_for(account)
            from backend.brokers.adapters.kite import get_lot_size as _disp_get_lot_size
            # Same qty translation as the /basket-margin endpoint —
            # v2 API: input is LOTS for F&O; multiply → contracts, then
            # translate_qty converts to Kite's basket_order_margins
            # convention (lots for MCX, contracts everywhere else).
            _FO = ("NFO", "MCX", "CDS", "BFO", "BCD", "NCO")
            _disp_payload = []
            for leg in grp.legs:
                _d_exch = (leg.exchange or "NFO").upper()
                _d_sym  = leg.tradingsymbol.upper()
                _d_input = int(leg.quantity or 0)   # lots (F&O) or shares (equity)
                _d_lot  = await _disp_get_lot_size(_d_exch, _d_sym)
                if _d_exch in _FO:
                    if _d_lot <= 0:
                        continue
                    _d_contracts = _d_input * _d_lot
                else:
                    _d_contracts = _d_input
                _d_bq   = broker.translate_qty(_d_exch, _d_contracts, _d_lot)
                _disp_payload.append({
                    "exchange":         _d_exch,
                    "tradingsymbol":    _d_sym,
                    "transaction_type": leg.transaction_type.upper(),
                    "variety":          leg.variety or "regular",
                    "product":          leg.product or "NRML",
                    "order_type":       leg.order_type or "LIMIT",
                    "quantity":         _d_bq,
                    "price":            float(leg.price or 0),
                    "trigger_price":    float(leg.trigger_price or 0),
                })
            mr = await asyncio.to_thread(broker.basket_order_margins, _disp_payload)
            # Kite returns a list; use the last entry's final.total.
            if isinstance(mr, list) and mr:
                _mr_last = mr[-1]
                final_m = (_mr_last.get("final") or {}) if isinstance(_mr_last, dict) else {}
                avail_m = ((_mr_last.get("initial") or {}).get("available") or {}) if isinstance(_mr_last, dict) else {}
            elif isinstance(mr, dict):
                final_m = (mr.get("final") or {})
                avail_m = (mr.get("initial") or {}).get("available") or {}
            else:
                final_m, avail_m = {}, {}
            _mr_req = float(final_m.get("total") or 0)
            _mr_ava = float(avail_m.get("cash") or avail_m.get("adhoc_margin") or 0)
            # Negative margin is not displayable — suppress.
            if _mr_req >= 0:
                margin_required  = _mr_req
                margin_available = _mr_ava
        except Exception as _me:
            logger.debug(f"[BASKET] margin lookup failed for {account}: {_me}")

        return BasketGroupResult(
            account=account,
            basket_id=basket_id,
            results=leg_results,
            margin_required=margin_required,
            margin_available=margin_available,
        )

    group_results = await asyncio.gather(*[_dispatch_group(g) for g in data.groups])
    return BasketOrderResponse(groups=list(group_results))
