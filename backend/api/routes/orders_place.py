"""
orders_place.py — Capacity guard, template-attach machinery, and take-profit
arming for the order placement pipeline.

Extracted from orders.py (4322 LOC → split) as Commit 4 of the RED-zone split.

All symbols in this module are imported by orders.py and re-exported so that
existing callers of `from backend.api.routes.orders import ...` continue to work.

No imports from orders.py at module level — lazy imports inside function bodies
are used where needed to prevent circular imports.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Optional

from litestar.exceptions import HTTPException

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Capacity guard ────────────────────────────────────────────────────────────

async def _opp_load_strategy_cap(
    s,
    strategy_id: int,
    account: str,
    tradingsymbol: str,
    side_kite: str,
) -> tuple[Optional[float], float]:
    """Load strategy cap and current open_notional. Returns (cap, open_notional).
    Returns (None, 0.0) when the guard should be skipped (strategy missing,
    no cap configured, or close-intent detected)."""
    from backend.api.models import Strategy, StrategyLot
    from backend.api.algo.lot_ledger import detect_close_intent
    from sqlalchemy import select as _select, func as _func

    strat = await s.get(Strategy, int(strategy_id))
    if strat is None:
        logger.warning(
            "[CAP-GUARD] strategy_id=%s not found — skipping capacity check "
            "for %s %s. Possible stale attribution; verify on /strategies.",
            strategy_id, account, tradingsymbol,
        )
        return None, 0.0
    if strat.capacity_cap_inr is None:
        return None, 0.0
    cap = float(strat.capacity_cap_inr)
    if cap <= 0:
        return None, 0.0
    is_close = await detect_close_intent(
        s,
        strategy_id=int(strategy_id),
        account=account,
        symbol=tradingsymbol,
        side_kite=side_kite,
    )
    if is_close:
        return None, 0.0
    open_notional = (await s.execute(
        _select(_func.coalesce(
            _func.sum(StrategyLot.remaining_qty * StrategyLot.open_price),
            0.0,
        )).where(
            StrategyLot.strategy_id == int(strategy_id),
            StrategyLot.remaining_qty > 0,
        )
    )).scalar_one() or 0.0
    return cap, float(open_notional)


def _opl_price_from_ticker(tradingsymbol: str) -> Optional[float]:
    """Try to resolve LTP for tradingsymbol from the in-process ticker cache.

    Returns the float price when found and positive, else None.
    Extracted from _opp_resolve_notional_price to reduce CC there."""
    try:
        from backend.brokers.kite_ticker import get_ticker as _get_ticker
        t = _get_ticker().get_ltp_by_sym(tradingsymbol.upper())
        if t is not None and t > 0:
            return float(t)
    except Exception:
        pass
    return None


async def _opl_price_from_broker(tradingsymbol: str) -> Optional[float]:
    """Try to resolve LTP via a one-shot broker.ltp() call.

    Returns the float price when found and positive, else None.
    Extracted from _opp_resolve_notional_price to reduce CC there."""
    try:
        from backend.brokers.registry import get_market_data_broker
        broker = get_market_data_broker()
        key = f"NFO:{tradingsymbol.upper()}"
        quote = await asyncio.to_thread(broker.ltp, [key])
        v = (quote or {}).get(key)
        if isinstance(v, dict):
            lp = float(v.get("last_price") or 0.0)
            if lp > 0:
                return lp
    except Exception:
        pass
    return None


async def _opp_resolve_notional_price(
    tradingsymbol: str,
    price_hint: Optional[float],
) -> float:
    """Resolve the price for new-notional calculation. Priority: price_hint
    → ticker LTP → broker.ltp(). Raises 503 when no price is resolvable."""
    # Explicit zero guard: price_hint==0 means MARKET order (caller did not
    # supply a price); treat it the same as None so we fall through to the
    # ticker / broker LTP chain rather than computing 0 × qty = ₹0 notional.
    px: Optional[float] = (
        float(price_hint) if price_hint is not None and price_hint > 0 else None
    )
    if px is None:
        px = _opl_price_from_ticker(tradingsymbol)
    if px is None:
        px = await _opl_price_from_broker(tradingsymbol)
    if px is None or px <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                "Capacity guard cannot resolve price for "
                f"{tradingsymbol} — pass an explicit limit price or "
                "retry once the ticker has the symbol subscribed."
            ),
        )
    return px


async def _enforce_capacity_guard(
    *,
    strategy_id: int,
    account: str,
    tradingsymbol: str,
    side_kite: str,
    quantity: int,
    price_hint: Optional[float],
) -> None:
    """Pre-trade capacity guard for the strategy attached to this order.

    Raises 403 when (current_open_notional + new_notional) would
    exceed `Strategy.capacity_cap_inr`. Returns silently otherwise.

    Skip semantics:
      - Strategy not found → silent skip. Order placement isn't the
        right surface to surface a stale strategy_id; the operator
        will see the bad attribution on /strategies.
      - capacity_cap_inr is NULL → silent skip (no cap configured).
      - Close intent detected → silent skip (reduces exposure; the
        FIFO match consumes existing lots).

    Pricing for new notional:
      1. price_hint (operator-typed limit / SL price) — preferred,
         matches Kite's accept-validation pricing.
      2. KiteTicker tick_map LTP for the symbol — covers MARKET orders
         when the operator's book has the symbol subscribed.
      3. broker.ltp() one-shot — last-resort batched call (rare, only
         for unsubscribed MARKET orders).
      4. Hard-fail with 503 if no price can be resolved — capacity
         math can't run without a price; refusing here is safer than
         letting an unbounded order through.

    The new-notional formula is intentionally OVER-CONSERVATIVE for
    pyramiding scenarios where part of the order would consume an
    existing lot — qty × price treats every contract as new exposure.
    Trade-off: false positives ("near the cap") in marginal cases,
    no false negatives. Operator can always raise the cap or split
    the order.
    """
    if quantity <= 0:
        return
    from backend.api.database import async_session

    async with async_session() as s:
        cap, open_notional = await _opp_load_strategy_cap(
            s, strategy_id, account, tradingsymbol, side_kite,
        )
        if cap is None:
            return

    px = await _opp_resolve_notional_price(tradingsymbol, price_hint)
    new_notional = float(quantity) * px
    projected = open_notional + new_notional
    if projected > cap:
        breach = projected - cap
        raise HTTPException(
            status_code=403,
            detail=(
                f"Capacity cap breach — strategy cap ₹{cap:,.0f}, "
                f"current open ₹{open_notional:,.0f}, "
                f"this order ₹{new_notional:,.0f}. "
                f"Would exceed by ₹{breach:,.0f}. "
                f"Reduce qty or raise the cap on /strategies."
            ),
        )


# ── Template-attach machinery ─────────────────────────────────────────────────

# Phase 3D #4 — per-parent-row in-process lock so the postback
# handler and chase terminal can't both pass the
# `attached_gtts_json is None` check simultaneously and double-place
# the GTT at the broker. uvicorn runs with --workers 1 in prod, so
# in-process locking is sufficient; the meta-lock guards the registry
# write itself.
#
# Audit fix (M-5) — strong dict with TTL replaces the prior
# WeakValueDictionary. The weakref pattern was "safe in current
# deployment" (single-worker asyncio + the caller's local strong ref
# keeps the entry alive across `async with`) but fragile to future
# call-signature changes: any refactor that introduced an extra
# `await` between the lock-mint and the `async with` acquisition
# could let the GC reclaim the lock mid-handoff, allowing two waiters
# to acquire DIFFERENT lock objects for the same parent_row_id and
# double-place the GTT. Switching to a strong dict eliminates that
# class of bug; the TTL sweep keeps memory bounded by retiring entries
# that haven't been touched in `_TPL_LOCK_TTL_S` seconds (default 4 h
# — well beyond the worst-case fill-to-attach latency including a
# slow reconcile sweep).
_TEMPLATE_ATTACH_LOCKS: dict[int, tuple[asyncio.Lock, float]] = {}
_TEMPLATE_ATTACH_META_LOCK = asyncio.Lock()
# 4 h — longest realistic live-chase window is ~30 min (max_attempts ×
# interval); 4 h gives 8× headroom so a slow overnight reconcile sweep
# still finds the lock before it expires. Raised from 1 h (M6).
_TPL_LOCK_TTL_S = 14400


async def _get_template_attach_lock(parent_row_id: int) -> "asyncio.Lock":
    """Lazily mint one asyncio.Lock per parent_row_id. The meta-lock
    only protects the registry's get-or-create; the per-row lock then
    serialises the read–decide–write triplet inside the fire fn.

    Lazy TTL sweep: every get-or-create scans the registry for entries
    older than _TPL_LOCK_TTL_S and drops them. Operator-facing fill
    flows complete well within the TTL (chase + postback + reconcile
    all measure in seconds to minutes), so eviction never races a
    live waiter — by the time an entry's age exceeds the TTL its
    attach has long since committed `attached_gtts_json`."""
    async with _TEMPLATE_ATTACH_META_LOCK:
        now = _time.monotonic()
        # Lazy sweep — drop stale entries. Inline rather than a
        # background task so the registry stays bounded without an
        # extra sweep coroutine.
        _stale = [k for k, (_, ts) in _TEMPLATE_ATTACH_LOCKS.items()
                  if now - ts > _TPL_LOCK_TTL_S]
        for k in _stale:
            _TEMPLATE_ATTACH_LOCKS.pop(k, None)
        if _stale:
            logger.debug(
                "[TPL-LOCK] evicted %d stale lock(s): %s",
                len(_stale), _stale,
            )
        entry = _TEMPLATE_ATTACH_LOCKS.get(parent_row_id)
        if entry is None:
            lk = asyncio.Lock()
            _TEMPLATE_ATTACH_LOCKS[parent_row_id] = (lk, now)
            return lk
        # Bump the timestamp on every access — entries don't expire
        # while a row is being actively reconciled.
        _TEMPLATE_ATTACH_LOCKS[parent_row_id] = (entry[0], now)
        return entry[0]


def _opl_reconcile_attach_eligible(row) -> bool:
    """Return True when a reconciled row should trigger template attach.

    Guards: must be live mode, must be a parent with a template_id,
    and must have a fill_price. Extracted from
    _maybe_fire_template_attach_for_reconcile to reduce CC there."""
    if (row.mode or "").lower() != "live":
        return False
    if not (row.template_id and row.parent_order_id is None):
        return False
    return bool(row.fill_price)


def _maybe_fire_template_attach_for_reconcile(row) -> None:
    """Sprint A helper — when the reconcile path flips an AlgoOrder to
    FILLED, fire the template attach if the row carries a template_id
    and is a parent (parent_order_id IS NULL). Same idempotency guard
    inside `_fire_template_attach_on_fill` ensures duplicate firings
    (postback arriving after reconcile) are safe.

    M11 (intent tagging) — SKIPPED. AlgoOrder has no `intent` column;
    adding one requires a migration. Tracked separately; implement when
    the migration ships.
    """
    try:
        if not _opl_reconcile_attach_eligible(row):
            return
        # Sprint B (#4) — partial fills get their actual filled qty.
        _attach_qty = (
            int(row.filled_quantity)
            if int(row.filled_quantity or 0) > 0
            else int(row.quantity or 0)
        )
        asyncio.create_task(_fire_template_attach_on_fill(
            parent_row_id=int(row.id),
            parent_account=str(row.account),
            parent_symbol=str(row.symbol),
            parent_exchange=str(row.exchange or "NFO"),
            parent_side=str(row.transaction_type),
            parent_qty=_attach_qty,
            fill_price=float(row.fill_price),
            template_id=int(row.template_id),
            parent_product=str(row.product or "NRML"),
        ))
    except Exception as e:
        logger.warning(f"reconcile template attach failed for #{row.id}: {e}")


async def _opp_load_row_for_attach(
    parent_row_id: int,
) -> Optional[dict]:
    """Load the AlgoOrder row for template-attach idempotency check.

    Returns None when: row has vanished, or already has attached_gtts_json
    (duplicate postback). Otherwise returns {'overrides': dict} with
    parsed template_overrides_json (empty dict when null/unset).
    """
    import json as _json
    from sqlalchemy import select as _sel_t
    from backend.api.database import async_session as _async_s
    from backend.api.models import AlgoOrder as _AO

    async with _async_s() as _s:
        _row = (await _s.execute(
            _sel_t(_AO).where(_AO.id == parent_row_id)
        )).scalar_one_or_none()
        if _row is None:
            logger.warning(
                f"[TPL-ATTACH] parent row #{parent_row_id} vanished "
                f"before postback fired"
            )
            return None
        if _row.attached_gtts_json:
            return None
        _row_overrides: dict = {}
        if _row.template_overrides_json:
            try:
                _parsed = _json.loads(_row.template_overrides_json)
                if isinstance(_parsed, dict):
                    _row_overrides = _parsed
            except Exception as _e:
                logger.warning(
                    f"[TPL-ATTACH] could not parse template_overrides_json "
                    f"for parent #{parent_row_id}: {_e}"
                )
    return {"overrides": _row_overrides}


def _opl_build_sibling_map(sibling_pairs) -> dict[str, str]:
    """Build a bidirectional sibling-id map from sibling_pairs.

    Extracted from _opp_build_attach_entries to reduce CC there."""
    sibling_by_id: dict[str, str] = {}
    for a, b in (sibling_pairs or []):
        if a and b:
            sibling_by_id[str(a)] = str(b)
            sibling_by_id[str(b)] = str(a)
    return sibling_by_id


def _opp_build_attach_entries(
    result,
    fill_price: float,
    parent_side: str,
) -> list:
    """Build the attached-GTTs list from an apply_template_to_order result."""
    sibling_by_id = _opl_build_sibling_map(result.sibling_pairs)
    attached = []
    for spec in (result.plan.gtts or []):
        if not spec.placed_id:
            continue
        entry: dict = {
            "kind":  "gtt",
            "label": spec.label,
            "id":    spec.placed_id,
        }
        _sib = sibling_by_id.get(str(spec.placed_id))
        if _sib:
            entry["sibling_id"]      = _sib
            entry["parent_account"]  = str(result.plan.parent_account)
            entry["parent_exchange"] = str(result.plan.parent_exchange)
        if spec.sl_trail_pct is not None and spec.trigger_values:
            entry["sl_trail_pct"]   = float(spec.sl_trail_pct)
            entry["trigger_values"] = list(spec.trigger_values)
            entry["highest_ltp"]    = float(fill_price)
            entry["low_ltp"]        = float(fill_price)
            entry["parent_side"]    = parent_side
        attached.append(entry)
    return attached


async def _opp_persist_attached_gtts(
    parent_row_id: int,
    attached: list,
    fill_price: float,
) -> None:
    """Persist the attached-GTTs list back onto AlgoOrder.attached_gtts_json."""
    import json as _json2
    from sqlalchemy import select as _sel_t
    from backend.api.database import async_session as _async_s
    from backend.api.models import AlgoOrder as _AO

    async with _async_s() as _s2:
        _upd = (await _s2.execute(
            _sel_t(_AO).where(_AO.id == parent_row_id)
        )).scalar_one_or_none()
        if _upd is not None:
            _upd.attached_gtts_json = _json2.dumps(attached)
        await _s2.commit()
    logger.info(
        f"[TPL-ATTACH] attached {len(attached)} GTT(s) "
        f"to parent #{parent_row_id} fill ₹{fill_price}"
    )


async def _fire_template_attach_on_fill(
    *,
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    parent_side: str,
    parent_qty: int,
    fill_price: float,
    template_id: int,
    parent_product: str = "NRML",
) -> None:
    """Fire apply_plan_live for a templated parent order that just
    flipped to FILLED via Kite/Dhan/Groww postback.

    Persists the returned gtt_ids back onto AlgoOrder.attached_gtts_json
    so the operator can cancel them later if they manually close the
    parent. Errors are logged but never raised — the postback ACK has
    already returned to the broker.

    Idempotency: if attached_gtts_json is already populated for this
    parent, skip — postbacks can arrive multiple times for the same
    fill on Kite (delivery retry) and we don't want duplicate GTTs.

    `parent_product` — exit GTT legs inherit this. NRML for F&O carry,
    MIS for intraday equity / F&O, CNC for delivery. Defaulted only as
    a back-compat shim for callers that don't have the row's product
    in hand; real callers (postback handler, chase terminal) read it
    off AlgoOrder.product (Phase 3C #2).
    """
    if not fill_price or fill_price <= 0:
        return
    # Phase 3D #4 — serialise concurrent calls for the same parent_row_id
    # so the postback handler and chase terminal can't both pass the
    # `attached_gtts_json is None` idempotency check simultaneously
    # and double-place GTTs at the broker. The lock is per-row +
    # in-process (uvicorn --workers 1 on prod) so there's zero
    # contention against unrelated fills.
    _row_lock = await _get_template_attach_lock(parent_row_id)
    async with _row_lock:
        try:
            from backend.api.algo.template_attach import apply_template_to_order

            row_info = await _opp_load_row_for_attach(parent_row_id)
            if row_info is None:
                return

            result = await apply_template_to_order(
                template_id=template_id,
                template_slug=None,
                overrides=row_info["overrides"],
                parent_account=parent_account,
                parent_symbol=parent_symbol,
                parent_side=parent_side,
                parent_qty=parent_qty,
                parent_exchange=parent_exchange,
                parent_fill_price=fill_price,
                parent_product=parent_product,
                parent_order_id=parent_row_id,
                apply_path="live",
            )
            if result is None:
                return

            attached = _opp_build_attach_entries(result, fill_price, parent_side)
            if attached:
                await _opp_persist_attached_gtts(parent_row_id, attached, fill_price)

        except Exception as _e:
            logger.warning(
                f"[TPL-ATTACH] _fire_template_attach_on_fill failed "
                f"for parent #{parent_row_id}: {_e}"
            )


async def _maybe_attach_template_to_ticket(
    data, account: str, sym: str, side: str, qty: int,
    algo_order_id: int | None,
) -> dict | None:
    """Run the template-attach RESOLVER for a paper ticket order and
    return the planned artefacts WITHOUT placing any broker orders.

    Phase 3C #1 — pre-fix this called apply_template_to_order with
    `apply_path="auto"`. When SimDriver was inactive (which is
    almost always for a real paper ticket) "auto" routed to the
    LIVE path and silently placed real Kite GTTs against the
    operator's submitted LIMIT price BEFORE the parent ever filled.
    Two correctness problems compounded:
      1. The parent might fill at a price OTHER than data.price
         (MARKET orders, partial slip, IOC) so the exit triggers
         got computed off the wrong reference.
      2. The paper engine itself was never told about the template,
         so its own fill events never fired the attach.

    Fix: always run the resolver in PREVIEW mode here so the API
    response carries the planned chips for the OrderTicket preview,
    but no broker order is sent. The paper engine fires the actual
    attach when its order fills via _fire_template_attach_on_fill
    using the engine's reported fill_price.
    """
    if algo_order_id is None:
        return None
    from backend.api.algo.template_attach import apply_template_to_order
    from backend.api.routes.orders_helpers import _ticket_overrides_dict
    try:
        result = await apply_template_to_order(
            template_id=data.template_id,
            template_slug=None,
            overrides=_ticket_overrides_dict(data),
            parent_account=account,
            parent_symbol=sym,
            parent_side=side,
            parent_qty=qty,
            parent_exchange=(data.exchange or "NFO"),
            parent_fill_price=float(data.price or 0.0),
            parent_product=(data.product or "NRML"),
            parent_order_id=algo_order_id,
            apply_path="preview",
        )
    except Exception as e:
        logger.error(f"[TICKET-TEMPLATE] preview failed for #{algo_order_id}: {e}")
        return None
    if result is None:
        return None
    return result.to_dict()


async def _attach_basket_leg_template(
    *,
    algo_order_id: int | None,
    template_id: int | None,
    account: str,
    sym: str,
    side: str,
    qty: int,
    exch: str,
    price: float,
    product: str,
) -> None:
    """Best-effort template ATTACH-AT-FILL plan for one basket leg.

    Sprint A fix (audit finding #7): the previous shape called
    `apply_template_to_order(apply_path="auto")` at submit time using
    the operator's limit `price` as a synthetic fill price, which
    placed broker GTTs at the wrong reference if the parent filled
    away from the limit. Now we just persist `template_id` on the
    AlgoOrder row (done at the caller) — the postback handler / chase
    terminal will fire the real attach via
    `_fire_template_attach_on_fill` with the actual fill price.

    Kept as a no-op shim for callsite compatibility; future work can
    inline the persist step here if it makes the basket flow clearer.
    """
    return


# ── Take-profit arming ────────────────────────────────────────────────────────

async def _opp_arm_tp_persist_row(
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    parent_side: str,
    fill_price: float,
    target_pct: float,
    target_abs: "float | None",
    parent_mode: str,
) -> "tuple[int, str, float] | None":
    """Persist the TP child AlgoOrder row. Returns (tp_id, tp_side, tp_price)
    or None when idempotency/guard checks say skip."""
    from sqlalchemy import select as _sel, func as _func
    from backend.api.database import async_session as _async_session
    from backend.api.models import AlgoOrder as _AlgoOrder

    async with _async_session() as _s:
        existing = (await _s.execute(
            _sel(_func.count(_AlgoOrder.id)).where(
                _AlgoOrder.parent_order_id == parent_row_id
            )
        )).scalar_one()
        if existing:
            return None

        parent = (await _s.execute(
            _sel(_AlgoOrder).where(_AlgoOrder.id == parent_row_id)
        )).scalar_one_or_none()
        if parent is None:
            return None
        qty = int(parent.quantity or 0)
        if not qty:
            return None

        parent_side_u = (parent_side or "BUY").upper()
        tp_side = "SELL" if parent_side_u == "BUY" else "BUY"
        pct_delta = float(target_pct or 0.0)
        abs_delta = float(target_abs or 0.0)
        if tp_side == "SELL":
            tp_price = fill_price * (1.0 + pct_delta) + abs_delta
        else:
            tp_price = fill_price * (1.0 - pct_delta) - abs_delta
        tp_price = max(0.01, round(tp_price, 2))

        tp_detail = (
            f"TP +{pct_delta*100:.0f}% · parent #{parent_row_id} "
            f"fill ₹{fill_price:.2f} → limit ₹{tp_price:.2f}"
        )
        tp_row = _AlgoOrder(
            account=parent_account,
            symbol=parent_symbol,
            exchange=parent_exchange,
            transaction_type=tp_side,
            quantity=qty,
            initial_price=tp_price,
            status="OPEN",
            engine="target",
            mode=parent_mode,
            target_pct=target_pct,
            target_abs=target_abs,
            parent_order_id=parent_row_id,
            detail=tp_detail,
        )
        _s.add(tp_row)
        await _s.commit()
        return tp_row.id, tp_side, tp_price


async def _opp_arm_tp_paper_register(
    tp_id: int,
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    tp_side: str,
    qty: int,
    tp_price: float,
) -> None:
    """Register the TP row with the paper engine."""
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        eng = get_prod_paper_engine()
        eng.register_open_order({
            "algo_order_id":  tp_id,
            "account":        parent_account,
            "symbol":         parent_symbol,
            "side":           tp_side,
            "qty":            qty,
            "limit_price":    tp_price,
            "initial_price":  tp_price,
            "exchange":       parent_exchange,
            "agent_slug":     "auto-tp",
            "action_type":    "place_order",
            "chase_agg":      "low",
            "is_close_intent": True,
        })
    except Exception as _pe:
        logger.warning(f"[TP] paper engine register failed for tp #{tp_id}: {_pe}")


async def _opp_arm_tp_live_place(
    tp_id: int,
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    tp_side: str,
    qty: int,
    tp_price: float,
    parent_product: str,
) -> None:
    """Place the live TP limit order at the broker and link broker_order_id."""
    try:
        from sqlalchemy import select as _sel
        from backend.api.routes.orders_helpers import _broker_for
        from backend.brokers.adapters.kite import get_lot_size
        from backend.api.database import async_session as _async_session2
        from backend.api.models import AlgoOrder as _AO2

        broker = _broker_for(parent_account)
        _ls = await get_lot_size(parent_exchange, parent_symbol)
        # G1 guard — parent qty was already validated but a corrupt row
        # must not reach Kite as a sub-lot order.
        if _ls and _ls > 1 and qty % _ls != 0:
            logger.error(
                f"[TP-G1] BLOCKED auto-TP for parent #{parent_row_id}: "
                f"qty={qty} is not a multiple of lot_size={_ls} "
                f"({parent_exchange}/{parent_symbol}). "
                f"Refusing to arm TP to avoid sub-lot order."
            )
            return
        _kq = broker.translate_qty(parent_exchange, qty, _ls)
        kite_order_id = broker.place_order(
            variety="regular",
            exchange=parent_exchange,
            tradingsymbol=parent_symbol,
            transaction_type=tp_side,
            quantity=_kq,
            product=parent_product or "NRML",
            order_type="LIMIT",
            price=tp_price,
            validity="DAY",
            tag=f"rb-tp-{parent_row_id}",
            intent="close",
        )
        async with _async_session2() as _s2:
            tp_upd = (await _s2.execute(
                _sel(_AO2).where(_AO2.id == tp_id)
            )).scalar_one_or_none()
            if tp_upd is not None:
                tp_upd.broker_order_id = str(kite_order_id)
            await _s2.commit()
        logger.info(f"[TP] live TP order placed: broker={kite_order_id} "
                    f"tp_id={tp_id} parent={parent_row_id}")
    except Exception as _le:
        logger.error(f"[TP] live TP placement failed for parent #{parent_row_id}: {_le}")


async def _opl_arm_tp_dispatch(
    tp_id: int, parent_row_id: int, parent_account: str,
    parent_symbol: str, parent_exchange: str, tp_side: str,
    tp_price: float, parent_mode: str, parent_product: str,
) -> None:
    """Resolve qty from the TP row and dispatch paper/live branch.

    Extracted from _arm_take_profit to reduce CC there."""
    from sqlalchemy import select as _sel
    from backend.api.database import async_session as _async_session
    from backend.api.models import AlgoOrder as _AlgoOrder
    async with _async_session() as _qs:
        _tp = (await _qs.execute(
            _sel(_AlgoOrder).where(_AlgoOrder.id == tp_id)
        )).scalar_one_or_none()
        qty = int(_tp.quantity or 0) if _tp else 0

    if parent_mode == "paper":
        await _opp_arm_tp_paper_register(
            tp_id, parent_row_id, parent_account, parent_symbol,
            parent_exchange, tp_side, qty, tp_price,
        )
    elif parent_mode == "live":
        await _opp_arm_tp_live_place(
            tp_id, parent_row_id, parent_account, parent_symbol,
            parent_exchange, tp_side, qty, tp_price, parent_product,
        )


async def _arm_take_profit(
    parent_row_id: int,
    parent_account: str,
    parent_symbol: str,
    parent_exchange: str,
    parent_side: str,      # "BUY" | "SELL"
    fill_price: float,
    target_pct: float,
    target_abs: float | None,
    parent_mode: str,      # "paper" | "live"
    parent_product: str = "NRML",
) -> None:
    """[LEGACY — Phase 2 deprecation marker] Arm a take-profit child
    order on fill via the v1 fractional target_pct path.

    Prefer attaching an OrderTemplate to the parent order (Phase 0+);
    the template pipeline (apply_template_to_order →
    _fire_template_attach_on_fill) supports rich TP / SL / Wing /
    MARKET-TP / wing-by-premium scan, broker-native OCO, and
    chained chase orders. This helper survives as the back-compat
    shim for Lab MCP scripts + legacy callers that still pass
    `data.target_pct`; both the postback handler and chase terminal
    handler keep firing both shim + template attach so neither path
    interferes with the other (each is idempotent against double
    fires).

    Called from the postback handler and _emit_chase_terminal when a parent
    order reaches FILLED.  Idempotent: skips if a child row already exists
    for this parent.

    Logic:
      - BUY parent  → SELL TP  @ fill_price × (1 + target_pct)
      - SELL parent → BUY  TP  @ fill_price × (1 - target_pct)
      If target_abs is also set, adds it on top of the pct delta.
    """
    if not fill_price or fill_price <= 0:
        return
    if not target_pct and not target_abs:
        return

    try:
        result = await _opp_arm_tp_persist_row(
            parent_row_id, parent_account, parent_symbol, parent_exchange,
            parent_side, fill_price, target_pct, target_abs, parent_mode,
        )
        if result is None:
            return
        tp_id, tp_side, tp_price = result
        await _opl_arm_tp_dispatch(
            tp_id, parent_row_id, parent_account, parent_symbol,
            parent_exchange, tp_side, tp_price, parent_mode, parent_product,
        )
        logger.info(f"[TP] armed: tp_id={tp_id} parent={parent_row_id} "
                    f"side={tp_side} limit=₹{tp_price:.2f} mode={parent_mode}")

    except Exception as _e:
        logger.warning(f"[TP] _arm_take_profit failed for parent #{parent_row_id}: {_e}")


# ── Ticket order handler ──────────────────────────────────────────────────────

_FO_EXCHANGES = ("NFO", "MCX", "CDS", "BFO", "BCD", "NCO")


def _validate_ticket_mode(data, request) -> None:
    """Reject draft mode and demo-user attempts at live/shadow orders."""
    if data.mode == "draft":
        raise HTTPException(status_code=400,
            detail="Drafts are client-side; the backend doesn't track them.")
    if data.mode not in ("paper", "live"):
        raise HTTPException(status_code=400,
            detail=f"unknown mode '{data.mode}'")
    if getattr(request.state, "is_demo", False):
        raise HTTPException(status_code=403,
            detail="Demo mode — orders cannot be placed. Sign in to trade.")


async def _validate_ticket_strategy_scope(data, request) -> None:
    """RBAC guard: traders may only submit to strategies they are assigned."""
    if not data.strategy_id:
        return
    from backend.api.rbac import (
        normalise_role, resolve_role_from_connection,
        user_scope_for_connection,
    )
    role = normalise_role(resolve_role_from_connection(request))
    if role == "trader":
        _, allowed_strategies = await user_scope_for_connection(request)
        if int(data.strategy_id) not in (allowed_strategies or []):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Strategy {data.strategy_id} not in your "
                    f"assigned_strategies list. Ask an admin to "
                    f"grant access, or pick a strategy you own."
                ),
            )


def _opl_check_enum_field(value, allowed: set, field_name: str) -> None:
    """Raise 400 when value is set but not in the allowed set.

    Extracted from _validate_ticket_enums to collapse the repetitive
    if-field-not-in-set → raise pattern."""
    if value and value not in allowed:
        raise HTTPException(status_code=400,
            detail=f"{field_name} must be one of {sorted(allowed)}")


def _opl_check_price_requirements(data) -> None:
    """Raise 400 when LIMIT/SL orders lack a price or trigger_price.

    Extracted from _validate_ticket_enums to separate concerns."""
    if data.order_type in ("LIMIT", "SL") and not data.price:
        raise HTTPException(status_code=400, detail="price is required for LIMIT/SL")
    if data.order_type in ("SL", "SL-M") and not data.trigger_price:
        raise HTTPException(status_code=400, detail="trigger_price is required for SL/SL-M")


def _validate_ticket_enums(data) -> None:
    """Validate side, symbol, quantity, and all enum fields in the ticket."""
    from backend.api.routes.orders_helpers import (
        _TXN_TYPES, _EXCHANGES, _PRODUCTS, _ORDER_TYPES, _VARIETIES,
    )
    side = (data.side or "").upper()
    if side not in _TXN_TYPES:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    sym = (data.tradingsymbol or "").upper().strip()
    input_qty = int(data.quantity or 0)
    if not sym or input_qty <= 0:
        raise HTTPException(status_code=400,
            detail="tradingsymbol and quantity > 0 are required")
    _opl_check_enum_field(data.exchange,   _EXCHANGES,  "exchange")
    _opl_check_enum_field(data.product,    _PRODUCTS,   "product")
    _opl_check_enum_field(data.order_type, _ORDER_TYPES,"order_type")
    _opl_check_enum_field(data.variety,    _VARIETIES,  "variety")
    _opl_check_price_requirements(data)


async def _resolve_fno_qty(data, exch: str, sym: str, input_qty: int) -> tuple[int, int]:
    """Resolve F&O lot_size and convert input lots to contracts.

    For equity (exch not in _FO_EXCHANGES) returns (input_qty, 1) unchanged.
    For F&O: fetches lot_size from instruments cache, applies the frontend hint
    as a cold-cache fallback, raises 503 if lot_size is still unresolvable, then
    returns (contracts, lot_size) where contracts = input_qty × lot_size.
    """
    if exch not in _FO_EXCHANGES:
        return input_qty, 1
    from backend.brokers.adapters.kite import get_lot_size as _get_lot_size
    try:
        _lot = int(await _get_lot_size(exch, sym) or 0)
    except Exception:
        _lot = 0
    # Fallback to the frontend-supplied hint when the backend cache is cold.
    _hint = int(data.lot_size_hint or 0)
    lot_size = _lot if _lot > 0 else _hint
    if lot_size <= 0:
        side = (data.side or "").upper()
        logger.error(
            f"[FO-LOT-GUARD] lot_size unknown for {exch}/{sym} "
            f"(cache returned {_lot}, no lot_size_hint in request). "
            f"Refusing {side} lots={input_qty} to prevent oversize order."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"lot_size for {sym} on {exch} is not available "
                f"(instruments cache cold). Retry in a moment — the cache "
                f"warms automatically at startup and market open."
            ),
        )
    contracts = input_qty * lot_size
    return contracts, lot_size


async def _ticket_validate_input(data, request) -> tuple[str, str, int, int]:
    """Mode/demo gate, RBAC strategy scope, and enum validation for the
    ticket payload.

    Returns `(side, sym, contracts, lot_size)` where:
      - `contracts` is the internal contract quantity used throughout
        the downstream pipeline (preflight, AlgoOrder.quantity, broker
        translate_qty). For F&O the request's `data.quantity` is in
        LOTS; we resolve `lot_size` and compute `contracts = lots ×
        lot_size` here. For equity, `contracts = data.quantity` (raw
        shares).
      - `lot_size` is the resolved instrument lot_size, or 1 for equity.
        Returned so the caller can reuse it for translate_qty without
        a second cache lookup.

    Raises HTTPException on any validation failure. Raises 503 when an
    F&O request lands with an unresolvable lot_size (instruments cache
    cold) — same guard pattern the MCX cold-cache incident (2026-07-01)
    surfaced, now applied to every F&O exchange since ALL of them now
    depend on lot_size for the input-lots → contracts multiplication.
    """
    _validate_ticket_mode(data, request)
    await _validate_ticket_strategy_scope(data, request)
    _validate_ticket_enums(data)

    side = (data.side or "").upper()
    sym = (data.tradingsymbol or "").upper().strip()
    input_qty = int(data.quantity or 0)
    exch = (data.exchange or "NFO")
    contracts, lot_size = await _resolve_fno_qty(data, exch, sym, input_qty)

    return side, sym, contracts, lot_size


def _ticket_validate_account(data) -> str:
    """Resolve + validate account against `_loaded_accounts()`."""
    account = (data.account or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="Account is required.")
    from backend.brokers.registry import _loaded_accounts
    if account not in _loaded_accounts():
        raise HTTPException(status_code=400, detail=f"Unknown account: {account}.")
    return account


async def _ticket_enforce_lot_and_fat_finger(
    data, account: str, sym: str, contracts: int, lot_size: int,
) -> None:
    """G2 (FAT_FINGER_5_LOT_CAP) for F&O exchanges. G1 (LOT_MULTIPLE) is
    NO LONGER NEEDED because request qty is already in LOTS — the
    contract quantity is guaranteed to be `lots × lot_size`, a valid
    multiple by construction.

    Close intent + MCX/NCO exempt from the fat-finger cap (MCX enforces
    its own 20-lot cap downstream in the live path).
    """
    if data.exchange not in _FO_EXCHANGES:
        return
    if lot_size <= 1:
        return
    _lots = contracts // lot_size
    _is_close = (getattr(data, "intent", None) or "").lower() == "close"
    _is_mcx = data.exchange in ("MCX", "NCO")
    if _is_close and _lots > 5:
        logger.info(
            "[FAT-FINGER-GUARD] close intent bypasses G2 cap: "
            "acct=%s sym=%s lots=%s lot_size=%s exchange=%s",
            account, sym, _lots, lot_size, data.exchange,
        )
    if not _is_close and not _is_mcx and _lots > 5:
        logger.warning(
            "[FAT-FINGER-GUARD] rejected: acct=%s sym=%s lots=%s "
            "lot_size=%s (contracts=%d, cap: 5)",
            account, sym, _lots, lot_size, contracts,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refusing order — {_lots} lots exceeds the "
                f"5-lot safety cap (lot_size={lot_size}). "
                "Split into ≤5-lot orders or contact ops to raise "
                "the cap."
            ),
        )


async def _ticket_gate_market_hours_and_align_price(data, sym: str) -> None:
    """Reject orders on closed exchanges and align price + trigger_price
    to the tick grid. Mutates `data.price` + `data.trigger_price`."""
    from backend.api.algo.agent_engine import _symbol_exchange_open, _build_now_ctx
    from backend.api.routes.orders_helpers import _align_price_to_tick

    target_exchange = data.exchange or "NFO"
    if not _symbol_exchange_open(target_exchange, _build_now_ctx()):
        seg = (target_exchange or "").upper()
        raise HTTPException(status_code=409,
            detail=(f"Exchange {seg} is closed. Orders for {sym} "
                    f"can only be placed during {seg}'s market "
                    f"hours (IST holidays apply)."))

    _exch_for_snap = (data.exchange or "NFO")
    data.price         = await _align_price_to_tick(_exch_for_snap, sym, data.price)
    data.trigger_price = await _align_price_to_tick(_exch_for_snap, sym, data.trigger_price)


async def _ticket_check_mcx_lot_cache(
    data, sym: str, side: str, qty: int, lot_size: int,
) -> int:
    """MCX cold-cache guard. `lot_size` has already been resolved by
    `_ticket_validate_input` (which now applies the same guard to ALL
    F&O exchanges, not just MCX, because the input-lots → contracts
    multiplication now depends on lot_size everywhere). Kept as a shim
    for readability + defense-in-depth: raises 503 if lot_size is 0
    for MCX/NCO specifically (should never happen post-validate but a
    zero-cost extra check).

    Returns the lot_size unchanged for MCX/NCO, or 0 for non-MCX (the
    downstream size-cap helper only fires on MCX so 0 is a no-op there).
    """
    logger.warning(
        "_ticket_check_mcx_lot_cache: fallback path reached — "
        "expected _ticket_validate_input to have resolved lot_size. "
        f"exchange={data.exchange} sym={sym} side={side} lots={qty}."
    )
    if (data.exchange or "NFO") not in ("MCX", "NCO"):
        return 0
    if lot_size <= 0:
        # Should be unreachable — validate_input raises 503 first — but
        # keep the guard for defense-in-depth.
        logger.error(
            f"[MCX-LOT-GUARD] lot_size unknown for {data.exchange}/{sym} "
            f"post-validate (unexpected). Refusing {side} lots={qty}."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"lot_size for {sym} on {data.exchange} is not available "
                f"(instruments cache cold). Retry in a moment — the cache "
                f"warms automatically at startup and market open."
            ),
        )
    return lot_size


def _opl_preflight_log_suffix(pf: dict) -> str:
    """Return the blocked-codes suffix for the preflight log line.

    Returns '' when ok, else ' — N blocker(s): CODE1, CODE2'.
    Extracted from _ticket_run_preflight to remove inline ternary."""
    if pf.get("ok"):
        return ""
    codes = ', '.join(b.get('code', '?') for b in pf.get('blocked', []))
    return f" — {len(pf['blocked'])} blocker(s): {codes}"


async def _ticket_run_preflight(data, account: str, sym: str, side: str, qty: int) -> dict:
    """Preflight the LIVE order via the actions engine. Raises 503 on
    broker-side failure. Returns the preflight verdict dict."""
    from backend.api.algo.actions import run_preflight as _run_preflight
    try:
        _pf = await _run_preflight(account, {
            "exchange":         (data.exchange or "NFO"),
            "tradingsymbol":    sym,
            "quantity":         qty,
            "order_type":       (data.order_type or "LIMIT"),
            "product":          (data.product or "NRML"),
            "variety":          (data.variety or "regular"),
            "transaction_type": side,
            "intent":           getattr(data, "intent", None),
            "price":            data.price or 0,
            "trigger_price":    data.trigger_price or 0,
        })
    except Exception as _pf_err:
        logger.error(f"[LIVE-TICKET] preflight raised for {account} "
                     f"{sym}: {_pf_err}")
        raise HTTPException(
            status_code=503,
            detail=f"Preflight check failed: {str(_pf_err)[:240]} — "
                   "broker may be unreachable. Try again.",
        ) from _pf_err
    label = 'ok' if _pf['ok'] else 'BLOCKED'
    logger.info(
        f"[LIVE-TICKET] preflight {label} "
        f"acct={account} {sym} {side} qty={qty}"
        + _opl_preflight_log_suffix(_pf)
    )
    return _pf


async def _ticket_record_preflight_block(
    data, account: str, sym: str, side: str, qty: int, pf: dict,
) -> None:
    """Persist a REJECTED AlgoOrder + preflight_block event when
    preflight blocks the order. Best-effort — swallows persistence errors
    so the caller still returns the 422 to the operator."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    try:
        from backend.api.algo.agent_engine import get_agent_id_by_slug as _g_aid
        from backend.api.algo.order_events import write_event as _write_ev
        _live_manual_aid: int | None = None
        try:
            _live_manual_aid = await _g_aid("manual")
        except Exception:
            pass
        async with async_session() as _s:
            _row = AlgoOrder(
                account=account, symbol=sym,
                exchange=(data.exchange or "NFO"),
                transaction_type=side, quantity=qty,
                initial_price=(float(data.price)
                               if data.price is not None else None),
                status="REJECTED", engine="live", mode="live",
                agent_id=_live_manual_aid,
                strategy_id=data.strategy_id,
                detail=f"preflight blocked: "
                       f"{', '.join(b.get('code','?') for b in pf['blocked'])}",
            )
            _s.add(_row)
            await _s.commit()
            _algo_id = _row.id
        await _write_ev(
            _algo_id, "preflight_block",
            f"{', '.join(b.get('reason','?') for b in pf['blocked'])[:300]}",
            payload={"blocked": pf["blocked"],
                     "diagnostics": pf.get("diagnostics", {})},
        )
        try:
            from backend.api.audit import write_audit_event as _wae
            _block_codes = ', '.join(b.get('code', '?') for b in pf['blocked'])
            _wae(
                category="order.reject",
                action="PREFLIGHT_BLOCKED",
                actor_username="operator",
                actor_role="admin",
                target_type="algo_order",
                target_id=str(_algo_id),
                summary=(
                    f"preflight blocked: {_block_codes} — "
                    f"{sym} {side} {qty} acct={account}"
                )[:1000],
            )
        except Exception as _aue:
            logger.debug(f"[LIVE-TICKET] preflight_block audit write skipped: {_aue}")
    except Exception as _ev_err:
        logger.warning(f"[LIVE-TICKET] preflight_block log failed: "
                       f"{_ev_err}")


def _ticket_check_mcx_size_cap(
    data, sym: str, contracts: int, lot_size: int,
) -> None:
    """Hard 20-lot cap for MCX/NCO. Input `contracts = lots × lot_size`
    (already validated) so lots = contracts // lot_size is exact.

    Close orders are exempt — the operator must be able to close any
    open position regardless of size, so no cap is enforced when
    intent == 'close'."""
    _ticket_exch = (data.exchange or "NFO")
    if _ticket_exch not in ("MCX", "NCO"):
        return
    if (getattr(data, "intent", None) or "").lower() == "close":
        return
    _MCX_MAX_LOTS = 20
    _lots = max(1, contracts // lot_size) if lot_size > 0 else contracts
    if _lots > _MCX_MAX_LOTS:
        logger.error(
            f"[MCX-SIZE-GUARD] {_ticket_exch}/{sym}: lots={_lots} "
            f"lot_size={lot_size} — exceeds {_MCX_MAX_LOTS}-lot "
            f"safety cap. Refusing order."
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Order size {_lots} lots for {sym} exceeds "
                f"the {_MCX_MAX_LOTS}-lot safety limit. If intentional, "
                f"contact support to increase the limit."
            ),
        )


async def _ticket_persist_live_algo_order(
    data, request, account: str, sym: str, side: str, qty: int,
) -> Optional[int]:
    """Pre-persist an OPEN AlgoOrder row before firing the broker call.
    Returns the row id, or None when persistence failed (broker call
    proceeds either way — persistence failure is best-effort)."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.api.routes.orders_helpers import (
        _resolve_target_pct, _build_overrides_json,
    )
    try:
        from backend.api.algo.agent_engine import get_agent_id_by_slug as _g_aid
        _live_manual_aid: int | None = None
        try:
            _live_manual_aid = await _g_aid("manual")
        except Exception:
            pass
        _eff_target_pct = _resolve_target_pct(data.target_pct)
        _req_id = (request.scope.get("state") or {}).get("request_id")
        # M13: idempotency — if this request_id already has a persisted
        # AlgoOrder (duplicate HTTP submission or retry), return the
        # existing id rather than inserting a second row.
        if _req_id:
            from sqlalchemy import select as _sel_idem
            from datetime import datetime, timezone as _tz_idem, timedelta as _td_idem
            _idem_cutoff = datetime.now(_tz_idem.utc) - _td_idem(seconds=60)
            async with async_session() as _s_chk:
                _existing = (await _s_chk.execute(
                    _sel_idem(AlgoOrder.id).where(
                        AlgoOrder.request_id == _req_id,
                        AlgoOrder.created_at >= _idem_cutoff,
                    ).limit(1)
                )).scalar_one_or_none()
            if _existing is not None:
                logger.info(
                    "[LIVE-TICKET] idempotent request_id=%s → "
                    "returning existing AlgoOrder #%s",
                    _req_id, _existing,
                )
                return int(_existing)
        async with async_session() as _s_pre:
            _live_row = AlgoOrder(
                account=account, symbol=sym,
                exchange=(data.exchange or "NFO"),
                transaction_type=side, quantity=qty,
                initial_price=(float(data.price)
                               if data.price is not None else None),
                status="OPEN", engine="live", mode="live",
                agent_id=_live_manual_aid,
                strategy_id=data.strategy_id,
                broker_order_id=None,
                request_id=_req_id,
                target_pct=(_eff_target_pct
                            if _eff_target_pct > 0 else None),
                template_id=data.template_id,
                template_overrides_json=_build_overrides_json(data),
                product=(data.product or "NRML"),
                detail=f"[LIVE-TICKET] manual {side} {qty} {sym}"
                       f"{' @₹' + str(data.price) if data.price else ''}",
            )
            _s_pre.add(_live_row)
            await _s_pre.commit()
            return _live_row.id
    except Exception as _e_pre:
        logger.warning(
            f"[LIVE-TICKET] AlgoOrder pre-persist failed: {_e_pre}"
        )
        return None


def _opl_chase_eligible(data, order_type: str) -> bool:
    """Return True when the ticket qualifies for the live chase loop.

    Requires chase=True, LIMIT order type, and a positive price.
    Extracted from _ticket_place_or_chase_live to reduce CC there."""
    return bool(
        data.chase
        and order_type == "LIMIT"
        and data.price is not None
        and data.price > 0
    )


async def _ticket_place_or_chase_live(
    data, account: str, sym: str, side: str, qty: int,
    live_algo_id: Optional[int], ls_for_translate: int,
) -> tuple[object, bool]:
    """Fire either the live chase loop or a direct broker.place_order.
    Returns `(order_id, chase_eligible)`."""
    from backend.api.routes.orders_helpers import _start_live_chase, _broker_for

    order_type = (data.order_type or "LIMIT")
    chase_eligible = _opl_chase_eligible(data, order_type)
    if chase_eligible:
        order_id = await _start_live_chase(
            account=account,
            symbol=sym,
            exchange=(data.exchange or "NFO"),
            transaction_type=side,
            quantity=qty,
            aggressiveness=(data.chase_aggressiveness or "low"),
            algo_order_id=live_algo_id,
        )
    else:
        broker = _broker_for(account)
        _kq_ticket = broker.translate_qty(
            data.exchange or "NFO", qty, ls_for_translate)
        order_id = broker.place_order(
            intent=getattr(data, "intent", None),
            variety=(data.variety or "regular"),
            exchange=(data.exchange or "NFO"),
            tradingsymbol=sym,
            transaction_type=side,
            quantity=_kq_ticket,
            product=(data.product or "NRML"),
            order_type=order_type,
            price=data.price,
            trigger_price=data.trigger_price,
            validity="DAY",
            tag=f"rq-{live_algo_id}" if live_algo_id else "ramboq-ticket",
        )
    return order_id, chase_eligible


async def _ticket_seed_broker_order_id(live_algo_id: int, order_id) -> None:
    """Best-effort write of `broker_order_id` back onto the AlgoOrder row."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    try:
        from sqlalchemy import select as _sel_seed
        async with async_session() as _s_seed:
            _r = (await _s_seed.execute(
                _sel_seed(AlgoOrder).where(
                    AlgoOrder.id == live_algo_id
                )
            )).scalar_one_or_none()
            if _r is not None:
                _r.broker_order_id = str(order_id)
                await _s_seed.commit()
    except Exception as _e_seed:
        logger.debug(
            f"[LIVE-TICKET] broker_order_id seed failed: {_e_seed}"
        )


def _opl_send_failure_alert(account: str, sym: str, data, side: str,
                            qty: int, kite_msg: str) -> None:
    """Best-effort Telegram/email alert for a live ticket failure.

    Extracted from _ticket_handle_live_place_error to reduce CC there."""
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=sym,
            exchange=(data.exchange or "NFO"), side=side,
            qty=qty, mode="live", source="agent:manual:ticket",
            error=kite_msg,
        )
    except Exception:
        pass


def _opl_record_manual_failure(account: str, sym: str, data, side: str,
                                qty: int, kite_msg: str) -> None:
    """Best-effort agent_events write for a live ticket failure.

    Extracted from _ticket_handle_live_place_error to reduce CC there."""
    try:
        from backend.api.algo.agent_engine import record_manual_event
        asyncio.create_task(record_manual_event(
            outcome="action_failure", source=data.source or "ticket",
            account=account, symbol=sym,
            exchange=(data.exchange or "NFO"), side=side,
            qty=qty, mode="live", error=kite_msg,
        ))
    except Exception:
        pass


async def _ticket_handle_live_place_error(
    data, account: str, sym: str, side: str, qty: int, order_type: str,
    e: Exception, bk_key: str,
) -> None:
    """Diagnose broker failure, log, alert, and record the rejection.
    Never returns — always raises the final HTTPException(400) so the
    caller propagates it."""
    from backend.api.algo.actions import diagnose_live_failure
    from backend.api.routes.orders_helpers import (
        _REJECTION_THRESHOLD, _record_rejection, _maybe_send_breaker_alert,
        _broker_for,
    )
    from backend.shared.helpers.utils import mask_account

    masked_acct = mask_account(account)
    kite_msg = str(e)
    diag_order = {
        "exchange": data.exchange or "NFO",
        "symbol":   sym,
        "side":     side,
        "qty":      qty,
        "order_type": order_type,
        "product":  data.product or "NRML",
        "price":    data.price or 0,
        "variety":  data.variety or "regular",
    }
    try:
        diag = await diagnose_live_failure(_broker_for(account), diag_order, kite_msg)
    except Exception:
        diag = "diagnosis unavailable"
    logger.error(
        f"[LIVE-TICKET] place_order failed for {masked_acct} "
        f"{(data.exchange or 'NFO')}/{sym} {(data.product or 'NRML')} "
        f"{side} {qty} {order_type}"
        f"{f' @{data.price}' if data.price else ''}: "
        f"{kite_msg} | diag: {diag}"
    )
    _opl_send_failure_alert(account, sym, data, side, qty, kite_msg)
    _opl_record_manual_failure(account, sym, data, side, qty, kite_msg)
    _new_count = _record_rejection(bk_key)
    if _new_count >= _REJECTION_THRESHOLD:
        _maybe_send_breaker_alert(bk_key, account, sym, side, qty,
                                   kite_msg[:200])
    raise HTTPException(
        status_code=400,
        detail=f"{kite_msg} ({diag})"[:400],
    )


def _opp_live_check_mode_gates(account: str, sym: str, side: str, qty: int) -> str:
    """Verify branch + paper_trading_mode gates and circuit-breaker.
    Returns the rejection-tracker key on success. Raises HTTPException when
    any gate blocks the order."""
    from backend.api.routes.orders_helpers import (
        _REJECTION_THRESHOLD, _rejection_key, _rejection_count,
        _maybe_send_breaker_alert,
    )
    from backend.shared.helpers.utils import is_prod_branch
    from backend.shared.helpers.settings import get_bool

    if not is_prod_branch():
        raise HTTPException(status_code=403,
            detail="LIVE mode is disabled on non-prod branches; use PAPER on dev.")
    if get_bool("execution.paper_trading_mode", False):
        raise HTTPException(status_code=403,
            detail="LIVE disabled — paper_trading_mode is ON. Toggle in /admin/execution (LIVE mode).")

    _bk_key = _rejection_key(account, sym, side, qty)
    _bk_count = _rejection_count(_bk_key)
    if _bk_count >= _REJECTION_THRESHOLD:
        _maybe_send_breaker_alert(_bk_key, account, sym, side, qty,
                                   f"{_bk_count} rejections in last hour")
        logger.warning(
            f"[BREAKER] BLOCKED ticket — {_bk_key} has "
            f"{_bk_count} rejections in last hour"
        )
        raise HTTPException(
            status_code=423,
            detail=(f"Circuit breaker: {_REJECTION_THRESHOLD}+ rejections "
                    f"in the last hour for {sym} {side} {qty} on {account}. "
                    "Further attempts blocked until the breaker resets. "
                    "Check margin / segment scope, then wait or reset."),
        )
    return _bk_key


async def _opp_live_handle_success(
    data, account: str, sym: str, side: str, qty: int,
    order_id, chase_eligible: bool, bk_key: str,
) -> "object":
    """Post-place success bookkeeping: invalidate cache, log, record event,
    clear circuit-breaker. Returns TicketOrderResponse."""
    from backend.api.schemas import TicketOrderResponse
    from backend.api.cache import invalidate
    from backend.api.routes.orders_helpers import _clear_rejections
    from backend.shared.helpers.utils import mask_account

    chase_tag = (f" CHASE[{(data.chase_aggressiveness or 'low').lower()}]"
                 if chase_eligible else "")
    invalidate("orders")
    masked = mask_account(account)
    logger.info(f"Ticket LIVE order: {order_id} [{masked}] "
                f"{side} {qty} {sym}{chase_tag}")
    try:
        from backend.api.algo.agent_engine import record_manual_event
        asyncio.create_task(record_manual_event(
            outcome="action_success", source=data.source or "ticket",
            account=account, symbol=sym,
            exchange=(data.exchange or "NFO"), side=side,
            qty=qty, mode="live", order_id=str(order_id),
        ))
    except Exception:
        pass
    _clear_rejections(bk_key)
    return TicketOrderResponse(
        order_id=str(order_id),
        mode="live",
        status="OPEN",
        detail=(f"Live broker order #{order_id} placed at {account}"
                + (f" — chasing [{(data.chase_aggressiveness or 'low').lower()}]"
                   if chase_eligible else "")
                + "."),
    )


async def _ticket_place_live(
    data, request, account: str, sym: str, side: str, qty: int, lot_size: int,
):
    """LIVE branch orchestrator. Runs all live-mode guards, preflight,
    broker place, and success/failure book-keeping. Returns a
    TicketOrderResponse or raises HTTPException.

    `qty` is the internal contract quantity (already computed from
    request lots × lot_size in `_ticket_validate_input`). `lot_size`
    is the resolved instrument lot_size (1 for equity)."""
    from backend.api.routes.orders_helpers import (
        _REJECTION_THRESHOLD, _record_rejection, _maybe_send_breaker_alert,
    )

    _bk_key = _opp_live_check_mode_gates(account, sym, side, qty)

    # Defense-in-depth MCX lot_size cold-cache guard — validate_input already
    # raises 503 first for all F&O, but re-check for MCX/NCO to thread the
    # resolved lot_size through to _ticket_check_mcx_size_cap.
    _mcx_ls_for_translate = await _ticket_check_mcx_lot_cache(data, sym, side, qty, lot_size)

    _pf = await _ticket_run_preflight(data, account, sym, side, qty)
    if not _pf["ok"]:
        await _ticket_record_preflight_block(data, account, sym, side, qty, _pf)
        _new_count = _record_rejection(_bk_key)
        if _new_count >= _REJECTION_THRESHOLD:
            _reason = ', '.join(b.get('code', '?') for b in _pf['blocked'])
            _maybe_send_breaker_alert(_bk_key, account, sym, side, qty, _reason)
        raise HTTPException(
            status_code=422,
            detail={"blocked": _pf["blocked"],
                    "diagnostics": _pf.get("diagnostics", {})},
        )

    order_type = (data.order_type or "LIMIT")
    # Pass lot_size for ALL F&O exchanges — translate_qty is a no-op for
    # non-MCX/NCO but being explicit eliminates the ls=0 latent trap.
    _ls_for_translate: int = lot_size if lot_size > 1 else 0
    _ticket_check_mcx_size_cap(data, sym, qty, lot_size)

    try:
        _live_algo_id = await _ticket_persist_live_algo_order(
            data, request, account, sym, side, qty,
        )
        order_id, chase_eligible = await _ticket_place_or_chase_live(
            data, account, sym, side, qty, _live_algo_id, _ls_for_translate,
        )
        if _live_algo_id is not None and order_id:
            await _ticket_seed_broker_order_id(_live_algo_id, order_id)
        return await _opp_live_handle_success(
            data, account, sym, side, qty, order_id, chase_eligible, _bk_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        await _ticket_handle_live_place_error(
            data, account, sym, side, qty, order_type, e, _bk_key,
        )


async def _opp_paper_persist_row(
    data, request, account: str, sym: str, side: str, qty: int,
) -> int:
    """Persist the paper AlgoOrder row. Returns algo_order_id. Raises 500 on DB failure."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.api.routes.orders_helpers import _resolve_target_pct, _build_overrides_json

    detail = (f"[PAPER-TICKET] manual {side} {qty} {sym} "
              f"@₹{data.price:.2f}" if data.price is not None
              else f"[PAPER-TICKET] manual {side} {qty} {sym} @MARKET")
    _manual_aid: int | None = None
    try:
        from backend.api.algo.agent_engine import get_agent_id_by_slug
        _manual_aid = await get_agent_id_by_slug("manual")
    except Exception:
        pass
    _eff_target_pct = _resolve_target_pct(data.target_pct)
    _req_id = (request.scope.get("state") or {}).get("request_id")
    try:
        async with async_session() as s:
            row = AlgoOrder(
                account=account, symbol=sym, exchange=(data.exchange or "NFO"),
                transaction_type=side, quantity=qty,
                initial_price=(float(data.price) if data.price is not None else None),
                status="OPEN", engine="paper", mode="paper",
                agent_id=_manual_aid,
                strategy_id=data.strategy_id,
                request_id=_req_id,
                target_pct=(_eff_target_pct if _eff_target_pct > 0 else None),
                template_id=data.template_id,
                template_overrides_json=_build_overrides_json(data),
                product=(data.product or "NRML"),
                detail=detail,
            )
            s.add(row)
            await s.commit()
            return row.id
    except Exception as e:
        logger.error(f"[PAPER-TICKET] DB write failed: {e}")
        raise HTTPException(status_code=500, detail=f"DB write failed: {e}")


def _opp_paper_register_chase(
    data, account: str, sym: str, side: str, qty: int, algo_order_id: int,
) -> None:
    """Register the paper order with the chase engine when chase is requested."""
    from backend.api.algo.paper import get_prod_paper_engine
    if data.price is not None and qty > 0 and data.chase:
        try:
            agg = (data.chase_aggressiveness or "low").lower()
            if agg not in ("low", "med", "high"):
                agg = "low"
            engine = get_prod_paper_engine()
            engine.register_open_order({
                "algo_order_id": algo_order_id,
                "account":       account,
                "symbol":        sym,
                "side":          side,
                "qty":           qty,
                "limit_price":   float(data.price),
                "initial_price": float(data.price),
                "exchange":      (data.exchange or "NFO"),
                "agent_slug":    "manual-ticket",
                "action_type":   "place_order",
                "chase_agg":     agg,
                "strategy_id":      data.strategy_id,
                "is_close_intent":  (getattr(data, "intent", "") or "").lower() == "close",
            })
        except Exception as e:
            logger.warning(f"[PAPER-TICKET] engine register failed: {e}")
    elif not data.chase:
        logger.info(f"[PAPER-TICKET] chase opted out — order #{algo_order_id} "
                    f"resting at limit ₹{data.price}")


def _opp_paper_write_placed_event(
    algo_order_id: int, account: str, sym: str, side: str, qty: int, data,
) -> None:
    """Fire-and-forget: write the 'placed' AlgoOrderEvent."""
    try:
        from backend.api.algo.order_events import write_event as _write_evt
        asyncio.create_task(_write_evt(
            algo_order_id, "placed",
            f"[PAPER-TICKET] manual {side} {qty} {sym} "
            f"{'@₹' + f'{data.price:.2f}' if data.price is not None else '@MARKET'}",
            payload={
                "account": account, "price": data.price,
                "exchange": data.exchange or "NFO",
                "source": "ticket",
            },
        ))
    except Exception:
        pass


def _opp_paper_record_manual_event(
    algo_order_id: int, account: str, sym: str, side: str, qty: int, data,
) -> None:
    """Fire-and-forget: record the manual_event for the paper ticket."""
    try:
        from backend.api.algo.agent_engine import record_manual_event
        asyncio.create_task(record_manual_event(
            outcome="action_success", source=data.source or "ticket",
            account=account, symbol=sym,
            exchange=(data.exchange or "NFO"), side=side,
            qty=qty, mode="paper", order_id=str(algo_order_id),
        ))
    except Exception:
        pass


async def _ticket_place_paper(data, request, account: str, sym: str, side: str, qty: int):
    """PAPER branch orchestrator. Persists the AlgoOrder, registers the
    chase-loop where applicable, records a manual_event, and (finally)
    attaches any template attachment before returning."""
    from backend.api.schemas import TicketOrderResponse
    from backend.shared.helpers.utils import mask_account

    algo_order_id = await _opp_paper_persist_row(data, request, account, sym, side, qty)
    _opp_paper_register_chase(data, account, sym, side, qty, algo_order_id)
    _opp_paper_write_placed_event(algo_order_id, account, sym, side, qty, data)

    masked = mask_account(account)
    logger.info(f"Ticket paper order: {algo_order_id} [{masked}] {side} {qty} {sym}")
    _opp_paper_record_manual_event(algo_order_id, account, sym, side, qty, data)

    attachment_dict = await _maybe_attach_template_to_ticket(
        data, account, sym, side, qty, algo_order_id)

    return TicketOrderResponse(
        order_id=str(algo_order_id),
        mode="paper",
        status="OPEN",
        detail=f"Paper order #{algo_order_id} placed — chase loop will fill it on the next bid/ask cross.",
        template_attachment=attachment_dict,
    )


async def ticket_order_handler(data, request) -> object:  # type: ignore[return]
    """
    Full ticket-order logic extracted from OrdersController.ticket_order.

    Accepts the same (data: TicketOrderRequest, request: Request) signature;
    returns a TicketOrderResponse.  Delegated to by the thin controller shim.

    Unit convention (v2 API — P0 fix 2026-07-08):
      - `data.quantity` from the request is LOTS for F&O exchanges
        (NFO/MCX/CDS/BFO/BCD/NCO). For equity it's raw shares.
      - Internally we track `qty` in CONTRACTS everywhere (AlgoOrder rows,
        preflight, chase, broker translate_qty). The conversion from
        lots → contracts happens in `_ticket_validate_input`.
      - Downstream helpers keep receiving contracts — no signature
        changes across the postback / chase / template-attach paths.
    """
    from backend.shared.helpers.utils import config
    from backend.shared.helpers.settings import get_bool

    side, sym, qty, lot_size = await _ticket_validate_input(data, request)
    account = _ticket_validate_account(data)

    await _ticket_enforce_lot_and_fat_finger(data, account, sym, qty, lot_size)

    if data.strategy_id:
        await _enforce_capacity_guard(
            strategy_id=int(data.strategy_id),
            account=account,
            tradingsymbol=sym,
            side_kite=side,
            quantity=qty,
            price_hint=(float(data.price)
                        if data.price is not None else None),
        )

    await _ticket_gate_market_hours_and_align_price(data, sym)

    _ptm_now = get_bool("execution.paper_trading_mode", False)
    _shadow_now = get_bool("execution.shadow_mode", False)
    logger.info(
        f"[ticket-mode] requested={data.mode!r} "
        f"paper_trading_mode={_ptm_now} shadow_mode={_shadow_now} "
        f"branch={config.get('deploy_branch','?')!r}"
    )

    if data.mode == "live":
        return await _ticket_place_live(data, request, account, sym, side, qty, lot_size)

    # ── PAPER / default branch ────────────────────────────────────────────
    return await _ticket_place_paper(data, request, account, sym, side, qty)
