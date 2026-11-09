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
from backend.api.algo.actions_preflight import MCX_MAX_LOTS, FO_FAT_FINGER_LOT_CAP

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


async def _opl_price_from_broker(tradingsymbol: str, exchange: str = "NFO") -> Optional[float]:
    """Try to resolve LTP via a one-shot broker.ltp() call.

    Returns the float price when found and positive, else None.
    Extracted from _opp_resolve_notional_price to reduce CC there.

    exchange must be the actual exchange of the instrument (e.g. "MCX" for
    commodity futures) — the broker key format is "{exchange}:{symbol}".
    Passing the wrong exchange causes a key miss and returns None."""
    try:
        from backend.brokers.registry import get_market_data_broker
        broker = get_market_data_broker()
        key = f"{exchange.upper()}:{tradingsymbol.upper()}"
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
    exchange: str = "NFO",
) -> float:
    """Resolve the price for new-notional calculation. Priority: price_hint
    → ticker LTP → broker.ltp(). Raises 503 when no price is resolvable.

    exchange is the actual exchange of the instrument — passed through to
    _opl_price_from_broker so the broker key is "{exchange}:{symbol}"."""
    # Explicit zero guard: price_hint==0 means MARKET order (caller did not
    # supply a price); treat it the same as None so we fall through to the
    # ticker / broker LTP chain rather than computing 0 × qty = ₹0 notional.
    px: Optional[float] = (
        float(price_hint) if price_hint is not None and price_hint > 0 else None
    )
    if px is None:
        px = _opl_price_from_ticker(tradingsymbol)
    if px is None:
        px = await _opl_price_from_broker(tradingsymbol, exchange)
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
    exchange: str = "NFO",
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

    px = await _opp_resolve_notional_price(tradingsymbol, price_hint, exchange)
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


# GTT trigger validation (`_gtt_direction_errors`/`_gtt_spec_errors`/
# `_validate_gtt_triggers`) and the per-row template-attach lock registry
# (`_TEMPLATE_ATTACH_LOCKS`/`_TEMPLATE_ATTACH_META_LOCK`/`_TPL_LOCK_TTL_S`/
# `_get_template_attach_lock`), the persist-failure hold
# (`_TPL_ATTACH_PERSIST_PENDING`), and `_opl_reconcile_attach_eligible`
# were removed (Bracket/Template-GTT removal, T3.1, 2026-11) — all were
# Bracket-only, used solely by `/retry-template` and the fill-triggered
# attach pipeline, both deleted along with
# `backend/api/algo/template_attach.py`.


async def _fetch_net_position_row(
    sym: str, exchange: str, account: str, *,
    product: str | None = None, force_refresh: bool = False,
) -> dict | None:
    """Return the matched position row (as a dict) for (account, sym), or
    None when no matching row exists / the account column can't be
    identified / the broker fetch fails / the only matching frame is a
    stale substitute.

    Shared single-row lookup behind `_fetch_net_position_qty` (qty only)
    and the close-path product resolver (`_fetch_net_position_product`,
    actions_live.py's residual 2026-10 close_position product fix) — a
    symbol can carry more than one open row for the same account (e.g.
    both an MIS and an NRML position simultaneously), so qty and product
    MUST come from the exact same `rows.iloc[0]` rather than two
    independent broker-row lookups that could pick different rows if a
    position changes between calls. Both callers sit behind the same
    cached broker DataFrame fetch (`broker_apis.fetch_positions`'s own
    30s TTL), so calling this more than once per close costs no extra
    broker round-trip in the common case.

    Exchange-matched (2026-11-04 orphan-GTT audit fix, defect 1): `exchange`
    used to be accepted and never used, so a symbol carrying more than one
    open row for the same account (e.g. an MIS and an NRML position on the
    same exchange, or the rarer cross-exchange listing) could silently
    match the wrong row via a bare `rows.iloc[0]` with no tie-breaker.
    Applied only when the frame actually carries an `exchange` column
    (every real broker frame does) — a frame missing it is matched as
    before rather than failing closed, since the pre-existing account-col
    guard already protects against the one failure mode (wrong account)
    that genuinely matters.

    `product`, when given, is an ADDITIONAL filter on top of exchange —
    optional because `_fetch_net_position_qty`/`_is_offsetting_position`
    don't know the product ahead of time (they ARE the product-agnostic
    "does ANY row net offset" check); only a caller that already knows
    which specific product row it means should pass it — e.g. the
    orphan-GTT safety net (`background.py:_oco_cancel_closed_position_gtts`),
    which reads the row's own `AlgoOrder.product`.

    `force_refresh` (defect 2): threads straight to `fetch_positions()`'s
    own TTL-bypass. Without it, a caller can be served a cached frame up
    to 30s stale (API-layer cache) or, in conn-service mode, up to a
    further ~30s stale (conn_service's own cache) — long enough for a
    same-day close-then-re-enter on the identical symbol to still read
    "flat" from the pre-re-entry snapshot.

    A frame that is itself a stale substitute (`df.attrs['stale']` —
    circuit-open or DB-fallback, see `broker_apis._stale_substitute_frame`)
    is never matched: every existing caller already treats this
    function's `None` as "unknown, fail safe" (template-detach stays
    conservative; the orphan-GTT check never treats unknown as flat), so
    skipping a stale frame outright is strictly safer than returning a
    row that might already be wrong.
    """
    try:
        from backend.brokers.broker_apis import fetch_positions as _fp
        dfs = await asyncio.to_thread(_fp, force_refresh=force_refresh)
        for df in dfs:
            if df is None or df.empty:
                continue
            if bool(df.attrs.get("stale")):
                continue
            acct_col = next(
                (c for c in df.columns if "account" in c.lower() or "user" in c.lower()),
                None,
            )
            if acct_col is None:
                # Can't identify an account column on this broker's
                # DataFrame — without it the symbol-only mask below would
                # silently match the first row from ANY account, returning
                # a wrong-account quantity instead of failing closed. Sprint
                # 1a fix: refuse rather than guess.
                return None
            mask = df["tradingsymbol"].str.upper() == sym.upper()
            mask &= df[acct_col].astype(str).str.upper() == account.upper()
            if exchange and "exchange" in df.columns:
                mask &= df["exchange"].astype(str).str.upper() == exchange.upper()
            if product and "product" in df.columns:
                mask &= df["product"].astype(str).str.upper() == product.upper()
            rows = df[mask]
            if rows.empty:
                continue
            return rows.iloc[0].to_dict()
    except Exception:
        pass
    return None


async def _fetch_net_position_qty(
    sym: str, exchange: str, account: str, *,
    product: str | None = None, force_refresh: bool = False,
) -> float | None:
    """Return the signed net held quantity for (account, sym), or None when
    no matching position row exists or the broker fetch fails.

    Uses cached broker positions (30s TTL) — lightweight, no extra broker
    call typically. Shared by `_is_offsetting_position` (sign-only check)
    and `_verify_close_intent` (sign + magnitude check) so both read the
    same position snapshot rather than each doing their own broker call.
    Both existing callers omit `product`/`force_refresh` (default
    behaviour unchanged); see `_fetch_net_position_row` for why.
    """
    row = await _fetch_net_position_row(
        sym, exchange, account, product=product, force_refresh=force_refresh,
    )
    if row is None:
        return None
    try:
        return float(row.get("quantity"))
    except (TypeError, ValueError):
        return None


async def _fetch_net_position_product(sym: str, exchange: str, account: str) -> str | None:
    """Return the real position row's own `product` field (MIS/NRML/CNC)
    for (account, sym), or None when no matching row exists / the field
    is absent / the broker fetch fails.

    2026-10 residual fix (close_position product): shares the exact same
    row-match helper `_fetch_net_position_qty` reads
    (`_fetch_net_position_row`) so qty and product can never be read
    from two different rows when a symbol momentarily carries more than
    one product open at once.

    NaN guard: a DataFrame column with a missing value for this row can
    carry a real `float('nan')`, not `None` — and `bool(float('nan'))`
    is `True` in Python, so a plain `if val else None` would silently
    turn that into the literal string `"NAN"` (Kite would reject it
    outright; Dhan's `_PRODUCT_TO_DHAN.get(..., "INTRADAY")` would
    instead silently coerce it to INTRADAY). `pandas.isna` catches both
    `None` and float NaN.
    """
    import pandas as pd

    row = await _fetch_net_position_row(sym, exchange, account)
    if row is None:
        return None
    val = row.get("product")
    if val is None or pd.isna(val):
        return None
    return str(val).upper() if val else None


async def _is_offsetting_position(sym: str, exchange: str, side: str, account: str) -> bool:
    """Return True when placing `side` would reduce/close an existing position.

    BUY against a net SHORT → True. SELL against a net LONG → True.
    Fails open (returns False) if position fetch fails — used only to
    decide whether to detach a template (low-stakes: worst case a
    template stays attached that should have detached, or vice versa).
    Do NOT reuse this fail-open sign-only check for anything that grants
    a safety-cap bypass — see `_verify_close_intent` for that.
    """
    net_qty = await _fetch_net_position_qty(sym, exchange, account)
    if net_qty is None:
        return False
    if side.upper() == "BUY" and net_qty < 0:
        return True   # BUY closes a SHORT
    if side.upper() == "SELL" and net_qty > 0:
        return True   # SELL closes a LONG
    return False


# `_ticket_strip_template_on_close` removed (Bracket/Template-GTT
# removal, T3.1, 2026-11) — it conditionally stripped `template_id` +
# override fields for a close/offset order before persist. Now that
# `_ticket_persist_live_algo_order`/`_opp_paper_persist_row` never
# persist those fields for ANY order (close or not — see their own
# edits), the close-specific strip is redundant: there is no template
# state left to strip by the time a row reaches persist.


async def _verify_close_intent(
    sym: str, exchange: str, side: str, account: str, contracts: int,
) -> bool:
    """Server-side verification that a CLIENT-CLAIMED 'close' intent is
    real — both sign (the order genuinely offsets the held position) AND
    magnitude (the order's contract qty does not exceed the actual held
    qty). 2026-09 council audit fix (devil's-advocate lens): the frontend's
    `classifyIntent()` (orderTicketSubmit.js) decides 'close' from SIGN
    alone — long position + SELL is classified 'close' regardless of how
    large the SELL qty is. `_ticket_enforce_lot_and_fat_finger` used to
    trust that client-supplied flag directly to bypass the 5-lot
    fat-finger cap (G2) and, per the G1 close-path exception, the 50-lot
    adapter ceiling too — so a fat-fingered qty far larger than the real
    position (e.g. 1 lot held, SELL 500 typed by mistake) sailed through
    every quantity safety net specifically because the sign happened to
    match. This function is the missing magnitude check.

    Fails CLOSED (returns False) on any position-fetch error or when no
    matching position row exists — unlike `_is_offsetting_position`'s
    fail-open design. That function's False just keeps a template
    attached (safe default for a low-stakes decision); this function's
    False forces the caller back onto the FULL G1/G2/ceiling safety path
    (safe default for a decision that grants a cap bypass) — inability to
    verify must never grant the bypass.

    @param contracts  the order's contract quantity (already lots × lot_size)
    @returns True only when `side` genuinely offsets the held position AND
        `contracts` does not exceed the absolute held quantity.
    """
    net_qty = await _fetch_net_position_qty(sym, exchange, account)
    if net_qty is None:
        return False
    side_upper = side.upper()
    if side_upper == "BUY" and net_qty < 0:
        return contracts <= abs(net_qty)
    if side_upper == "SELL" and net_qty > 0:
        return contracts <= net_qty
    return False


# The fill-triggered template-attach pipeline
# (_maybe_fire_template_attach_for_reconcile, _opp_load_row_for_attach,
# _opl_build_sibling_map, _opl_build_group_sibling_map,
# _opp_build_attach_entries, _opp_persist_attached_gtts,
# _opp_persist_with_retry, _opp_attached_ids, _opp_handle_persist_failure,
# _opp_flush_pending_persist, _opp_handle_fill_path_exception,
# _fire_template_attach_on_fill) and the ticket/basket preview-attach
# shims (_maybe_attach_template_to_ticket, _attach_basket_leg_template)
# were removed (Bracket/Template-GTT removal, T3.1, 2026-11) — all
# called apply_template_to_order, which was deleted along with
# backend/api/algo/template_attach.py.



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
    filled_qty: "int | None" = None,
) -> "tuple[int, str, float] | None":
    """Persist the TP child AlgoOrder row. Returns (tp_id, tp_side, tp_price)
    or None when idempotency/guard checks say skip.

    2026-09-27 audit fix (finding #1): the existence check + insert used to
    run as two unlocked statements, so two concurrent callers observing the
    same fill (e.g. chase's poll + a racing postback) could both pass the
    existence check before either committed, arming TWO TP orders for one
    fill. Fixed by locking the PARENT row via SELECT ... FOR UPDATE first —
    the second concurrent caller blocks until the first's TP insert commits,
    then its own existence check sees the just-committed row and skips.

    2026-09-27 audit fix (finding #1 bundle): `filled_qty`, when passed by
    the caller, is used instead of `parent.quantity` (the ORIGINAL order
    size) — a partial fill must arm its TP for the quantity actually
    filled, not the full original order.
    """
    from sqlalchemy import select as _sel, func as _func
    from backend.api.database import async_session as _async_session
    from backend.api.models import AlgoOrder as _AlgoOrder

    async with _async_session() as _s:
        parent = (await _s.execute(
            _sel(_AlgoOrder).where(_AlgoOrder.id == parent_row_id)
            .with_for_update()
        )).scalar_one_or_none()
        if parent is None:
            return None

        existing = (await _s.execute(
            _sel(_func.count(_AlgoOrder.id)).where(
                _AlgoOrder.parent_order_id == parent_row_id
            )
        )).scalar_one()
        if existing:
            return None

        qty = int(filled_qty) if filled_qty else int(parent.quantity or 0)
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
        from backend.api.routes.orders_helpers import _align_price_to_tick
        tp_price = await _align_price_to_tick(parent.exchange, parent.symbol, tp_price)
        tp_price = max(0.01, tp_price)

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
            # Sprint 2b — legacy v1 TP children created by this shim get a
            # fixed origin tag (never client-supplied; this helper has no
            # caller-side source to thread through).
            source="take_profit",
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
    filled_qty: "int | None" = None,
) -> None:
    """[LEGACY — Phase 2 deprecation marker] Arm a take-profit child
    order on fill via the v1 fractional target_pct path.

    The rich template-attach pipeline (apply_template_to_order →
    _fire_template_attach_on_fill) that used to also fire alongside this
    shim was removed (Bracket/Template-GTT removal, T3.1, 2026-11) along
    with `backend/api/algo/template_attach.py`. This v1 single-target-TP
    shim is now the ONLY TP-arming path; it survives for Lab MCP scripts +
    legacy callers that still pass `data.target_pct`.

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
            filled_qty=filled_qty,
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

    2026-09 council audit fix (devil's-advocate lens) — a CLIENT-claimed
    `data.intent == "close"` is no longer trusted directly for the cap
    bypass. The frontend's `classifyIntent()` decides 'close' from SIGN
    alone (long position + SELL → 'close'), with no magnitude check — a
    fat-fingered qty far larger than the real held position (e.g. 1 lot
    held, SELL 500 typed by mistake) was previously classified 'close'
    and sailed through this cap, and the G1 close-path exception, and the
    50-lot adapter ceiling, uncapped. `_verify_close_intent` independently
    re-derives 'close' server-side from the actual broker position (sign
    AND magnitude) before the bypass is granted; a claimed-but-unverified
    close falls back to the full cap below rather than being trusted.

    Follow-up fix (2026-09-27, orders-page audit deepened per advisor
    review): the verification above only ever fed ITS OWN local `_is_close`
    — every OTHER consumer of `data.intent` downstream in the same request
    (`_ticket_check_mcx_size_cap`'s close exemption, `_ticket_run_preflight`'s
    preflight dict, and the `broker.place_order`/GTT calls) independently
    re-read the raw, still-unverified `data.intent` field, silently
    recreating the exact bypass this function had just closed — for MCX/
    NCO specifically, where G2 above is already exempt, `_ticket_check_
    mcx_size_cap` was the ONLY guard standing between a claimed-but-fake
    close and an unlimited-size live order. Fixed by mutating `data.intent`
    in place to the server-verified value ('close' only if verification
    actually passed, else None) once here — `TicketOrderRequest` is a
    plain (non-frozen) msgspec.Struct already mutated the same way by
    `_ticket_gate_market_hours_and_align_price` (price/trigger_price
    alignment) elsewhere in this file, so every downstream reader of
    `data.intent` automatically sees the verified value with no changes
    needed at those call sites.
    """
    if data.exchange not in _FO_EXCHANGES:
        return
    if lot_size <= 1:
        return
    _lots = contracts // lot_size
    _claimed_close = (getattr(data, "intent", None) or "").lower() == "close"
    _is_mcx = data.exchange in ("MCX", "NCO")
    _is_close = False
    if _claimed_close:
        side_upper = (getattr(data, "side", None) or "").upper()
        _is_close = await _verify_close_intent(
            sym=sym, exchange=str(data.exchange), side=side_upper,
            account=account, contracts=contracts,
        )
        if not _is_close:
            logger.warning(
                "[FAT-FINGER-GUARD] client claimed close intent but server "
                "verification failed (sign mismatch or qty exceeds actual "
                "position) — falling back to full cap: acct=%s sym=%s "
                "lots=%s exchange=%s",
                account, sym, _lots, data.exchange,
            )
    # Propagate the VERIFIED value to every other consumer of data.intent
    # in this request (_ticket_check_mcx_size_cap, _ticket_run_preflight,
    # broker.place_order/GTT calls) — see docstring follow-up note above.
    data.intent = "close" if _is_close else None
    if _is_close and _lots > FO_FAT_FINGER_LOT_CAP:
        logger.info(
            "[FAT-FINGER-GUARD] verified close intent bypasses G2 cap: "
            "acct=%s sym=%s lots=%s lot_size=%s exchange=%s",
            account, sym, _lots, lot_size, data.exchange,
        )
    if not _is_close and not _is_mcx and _lots > FO_FAT_FINGER_LOT_CAP:
        logger.warning(
            "[FAT-FINGER-GUARD] rejected: acct=%s sym=%s lots=%s "
            "lot_size=%s (contracts=%d, cap: %d)",
            account, sym, _lots, lot_size, contracts, FO_FAT_FINGER_LOT_CAP,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refusing order — {_lots} lots exceeds the "
                f"{FO_FAT_FINGER_LOT_CAP}-lot safety cap (lot_size={lot_size}). "
                f"Split into ≤{FO_FAT_FINGER_LOT_CAP}-lot orders or contact "
                "ops to raise the cap."
            ),
        )


def _ticket_closed_market_detail(exchange: str, sym: str) -> str:
    """Build the 409 detail message for a closed-exchange order rejection.

    Mentions the evening-session override (2026-10-20 fix) when the
    exchange's segment config declares `evening_open_on_holidays: true`
    (e.g. MCX) — the generic "market hours" wording alone doesn't explain
    why the same exchange can be open past 17:00 on a holiday."""
    from backend.api.algo.agent_engine import _segment_for_exchange
    from backend.shared.helpers.utils import config as _cfg

    seg = (exchange or "").upper()
    seg_name = _segment_for_exchange(exchange)
    seg_cfg = ((_cfg.get("market_segments", {}) or {}).get(seg_name, {}) or {}) if seg_name else {}
    if seg_cfg.get("evening_open_on_holidays"):
        return (f"Exchange {seg} is closed. Orders for {sym} can only be "
                f"placed during {seg}'s market hours, or its evening "
                f"session on a holiday.")
    return (f"Exchange {seg} is closed. Orders for {sym} can only be "
            f"placed during {seg}'s market hours (IST holidays apply).")


async def _ticket_gate_market_hours_and_align_price(data, sym: str) -> None:
    """Reject orders on closed exchanges and align price + trigger_price
    to the tick grid. Mutates `data.price` + `data.trigger_price`."""
    from backend.api.algo.agent_engine import (
        _symbol_exchange_open, _build_now_ctx, _prewarm_special_sessions_for_exchange,
    )
    from backend.api.routes.orders_helpers import _align_price_to_tick

    target_exchange = data.exchange or "NFO"
    # Pre-warm the special-sessions cache from THIS async context before
    # the sync _build_now_ctx() call below (2026-10-20 fix) — closes the
    # cold-cache gap for this call site specifically.
    await _prewarm_special_sessions_for_exchange(target_exchange)
    if not _symbol_exchange_open(target_exchange, _build_now_ctx()):
        raise HTTPException(status_code=409,
            detail=_ticket_closed_market_detail(target_exchange, sym))

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
                source=(data.source or "ticket"),
                mcp_request_id=getattr(data, "mcp_request_id", None),
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
    """Hard MCX_MAX_LOTS-lot cap for MCX/NCO. Input `contracts = lots ×
    lot_size` (already validated) so lots = contracts // lot_size is exact.

    Close orders are exempt — the operator must be able to close any
    open position regardless of size, so no cap is enforced when
    intent == 'close'.

    `data.intent` here is the value `_ticket_enforce_lot_and_fat_finger`
    already normalized to the SERVER-VERIFIED result earlier in the same
    request (called first in `ticket_order_handler`, before this function
    runs from `_ticket_place_live`) — not the raw client claim. For MCX/
    NCO specifically this close exemption is the ONLY size guard (G2 above
    exempts MCX/NCO unconditionally), so trusting an unverified claim here
    would have been a complete bypass of this cap (2026-09-27 audit)."""
    _ticket_exch = (data.exchange or "NFO")
    if _ticket_exch not in ("MCX", "NCO"):
        return
    if (getattr(data, "intent", None) or "").lower() == "close":
        return
    _lots = max(1, contracts // lot_size) if lot_size > 0 else contracts
    if _lots > MCX_MAX_LOTS:
        logger.error(
            f"[MCX-SIZE-GUARD] {_ticket_exch}/{sym}: lots={_lots} "
            f"lot_size={lot_size} — exceeds {MCX_MAX_LOTS}-lot "
            f"safety cap. Refusing order."
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Order size {_lots} lots for {sym} exceeds "
                f"the {MCX_MAX_LOTS}-lot safety limit. If intentional, "
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
    from backend.api.routes.orders_helpers import _resolve_target_pct
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
                # template_id / template_hold_override /
                # template_overrides_json are no longer persisted
                # (Bracket/Template-GTT removal, T3.1, 2026-11) — the
                # frontend may still send these fields until its own
                # removal sub-sprint lands; silently dropping them (not
                # refusing the order) is the deliberate policy.
                product=(data.product or "NRML"),
                intent=getattr(data, "intent", None),
                # Sprint 2b — persist the client-supplied origin tag (defaults
                # to "ticket" on TicketOrderRequest) onto the AlgoOrder row
                # itself, not just agent_events. Lets chain/command tabs
                # distinguish themselves once they start sending `source`.
                source=(data.source or "ticket"),
                mcp_request_id=getattr(data, "mcp_request_id", None),
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
            intent=getattr(data, "intent", None),
            # D1 fix (2026-09) — thread the operator's actual
            # product/variety/validity into the chase loop so every
            # re-place attempt matches the ticket, instead of the
            # previously-hardcoded ChaseConfig defaults (NRML/regular/
            # DAY) regardless of what was selected (e.g. MIS intraday).
            # TicketOrderRequest has no `validity` field (only
            # ModifyOrderRequest does) — getattr fallback mirrors the
            # direct-place branch below's hardcoded "DAY".
            product=(data.product or "NRML"),
            variety=(data.variety or "regular"),
            validity=(getattr(data, "validity", None) or "DAY"),
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
            qty=qty, mode="live", source="ticket",
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
        _broker_for, ChaseConfirmTimeout,
    )
    from backend.shared.helpers.utils import mask_account

    masked_acct = mask_account(account)
    kite_msg = str(e)
    # 2026-10-23 audit fix (O8) — a ChaseConfirmTimeout is a CLIENT-SIDE
    # confirm-window timeout (`_start_live_chase`'s `asyncio.wait_for`),
    # not a genuine broker rejection — the order may have actually
    # landed and been killed via the abandonment path. Recording it
    # against the circuit breaker risked a false trip under slow-broker
    # conditions (e.g. a multi-account pre-fill position fetch). Skip
    # the breaker bookkeeping for this exception type only; everything
    # else below (diagnosis, logging, alert, HTTPException) is unchanged.
    _is_confirm_timeout = isinstance(e, ChaseConfirmTimeout)
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
    if not _is_confirm_timeout:
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
    algo_order_id: int | None = None,
) -> "object":
    """Post-place success bookkeeping: invalidate cache, log, record event,
    clear circuit-breaker. Returns TicketOrderResponse."""
    from backend.api.schemas import TicketOrderResponse
    from backend.api.cache import invalidate
    from backend.api.routes.orders_helpers import _clear_rejections
    from backend.shared.helpers.utils import mask_account
    from backend.api.algo.order_events import write_event

    chase_tag = (f" CHASE[{(data.chase_aggressiveness or 'low').lower()}]"
                 if chase_eligible else "")
    invalidate("orders")
    masked = mask_account(account)
    logger.info(f"Ticket LIVE order: {order_id} [{masked}] "
                f"{side} {qty} {sym}{chase_tag}")

    # Record the "placed" event in the order lifecycle event log using the
    # AlgoOrder DB PK (algo_order_id), not the broker order ID.
    if algo_order_id is not None:
        try:
            asyncio.create_task(write_event(
                algo_order_id, "placed",
                f"live ticket {side} {qty} {sym}",
            ))
        except Exception:
            pass

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

    # 2026-09 council audit fix (perf lens): this success path previously
    # invalidated only the "orders" cache and scheduled no positions
    # refresh at all -- that machinery lived exclusively in the postback
    # fan-out (_postback_broadcast_fanout -> _positions_refresh_after_fill),
    # which fires reliably for Kite but is the ONLY path for Dhan/Groww,
    # whose postback delivery is documented as unreliable/manually-
    # configured. An operator placing a live Dhan/Groww order with the
    # webhook unconfigured would see the order acknowledged instantly but
    # positions would not reflect the fill for up to 5 minutes (the
    # _task_performance poll backstop) -- directly undermining "call
    # position APIs immediately". Scoped to Dhan/Groww only: Kite's own
    # postback already triggers this same function promptly on a real
    # fill, so running it again here would just double broker calls for
    # no benefit. The function is self-verifying (polls actual broker
    # positions and only fires positions_refreshed once quantity genuinely
    # changed), so triggering it speculatively right after PLACEMENT
    # (not confirmed fill) is safe -- it silently no-ops after ~7s for an
    # order that's still resting unfilled, same as it always has for its
    # existing postback-triggered callers.
    try:
        from backend.brokers.registry import _broker_id_for
        if _broker_id_for(account) in ("dhan", "groww"):
            from backend.api.routes.orders import (
                _positions_refresh_after_fill, _subscribe_filled_pairs,
            )
            _qty_delta = qty * (1 if (side or "").upper() == "BUY" else -1)
            asyncio.create_task(
                _positions_refresh_after_fill(account, sym, _qty_delta)
            )
            # Gap fix (2026-10): for Dhan/Groww, this ticket-success path
            # is the ONLY fill-adjacent mechanism that reliably runs when
            # postback delivery isn't configured (see the comment block
            # above) — _positions_refresh_after_fill refreshes position
            # DATA but never touched the ticker subscription, so a filled
            # Dhan/Groww order's WS tick stayed symbol-unaddressable for
            # up to 5 minutes. Fired speculatively right after placement
            # (same as the sibling call above) — subscribing is idempotent
            # and harmless even if the order never fills.
            asyncio.create_task(
                _subscribe_filled_pairs([(sym, data.exchange or "NFO")])
            )
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

    # _ticket_check_mcx_lot_cache was removed here: the function's return
    # value (_mcx_ls_for_translate) was never consumed — _ls_for_translate
    # is derived independently below from `lot_size`. The call logged a
    # spurious WARNING on every non-MCX ticket. Defense-in-depth MCX
    # guards remain in _ticket_validate_input (lot_size resolution) and
    # _ticket_check_mcx_size_cap (size-cap enforcement below).

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
        # 2026-09-28 audit fix — a pre-persist failure used to be
        # swallowed as "best-effort" and the live order placed anyway
        # with algo_order_id=None. For a chase-eligible order that means
        # the ENTIRE chase loop runs untracked: _sync_algo_order_id
        # no-ops on every cancel-and-replace (no row to write attempts /
        # last_attempt_at / next_attempt_at / current_limit to), and
        # _chase_terminal_update_db's fallback broker_order_id lookup
        # finds nothing either — so on fill, TP-arm / template-attach /
        # FIFO ledger write are ALL silently skipped, not just delayed.
        # The operator sees a real broker order they can't track in
        # /chases/active, with none of the exit automation they
        # configured. Root-caused live via AlgoOrder #1088 (2026-09-28,
        # ~12 min chase, request_id=None, attempts=0, every timing field
        # NULL — a postback-created orphan row was the ONLY trace).
        # Fail closed instead: refuse to place until the tracking row
        # exists, so a genuine DB blip surfaces as a clear, retryable
        # error rather than an untracked live order.
        if _live_algo_id is None:
            raise HTTPException(
                status_code=503,
                detail="Could not create the order tracking row — order "
                       "NOT placed. Please retry.",
            )
        order_id, chase_eligible = await _ticket_place_or_chase_live(
            data, account, sym, side, qty, _live_algo_id, _ls_for_translate,
        )
        if _live_algo_id is not None and order_id:
            await _ticket_seed_broker_order_id(_live_algo_id, order_id)
        return await _opp_live_handle_success(
            data, account, sym, side, qty, order_id, chase_eligible, _bk_key,
            algo_order_id=_live_algo_id,
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
    from backend.api.routes.orders_helpers import _resolve_target_pct

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
                # template_id / template_hold_override /
                # template_overrides_json are no longer persisted
                # (Bracket/Template-GTT removal, T3.1, 2026-11) — see
                # the identical note in _ticket_persist_live_algo_order.
                product=(data.product or "NRML"),
                source=(data.source or "ticket"),
                mcp_request_id=getattr(data, "mcp_request_id", None),
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
    chase-loop where applicable, and records a manual_event before
    returning.

    `_maybe_attach_template_to_ticket` (the template-attach preview
    shim) was removed (Bracket/Template-GTT removal, T3.1, 2026-11) —
    `TicketOrderResponse.template_attachment` was removed with it
    (T3.6)."""
    from backend.api.schemas import TicketOrderResponse
    from backend.shared.helpers.utils import mask_account

    algo_order_id = await _opp_paper_persist_row(data, request, account, sym, side, qty)
    _opp_paper_register_chase(data, account, sym, side, qty, algo_order_id)
    _opp_paper_write_placed_event(algo_order_id, account, sym, side, qty, data)

    masked = mask_account(account)
    logger.info(f"Ticket paper order: {algo_order_id} [{masked}] {side} {qty} {sym}")
    _opp_paper_record_manual_event(algo_order_id, account, sym, side, qty, data)

    return TicketOrderResponse(
        order_id=str(algo_order_id),
        mode="paper",
        status="OPEN",
        detail=f"Paper order #{algo_order_id} placed — chase loop will fill it on the next bid/ask cross.",
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

    # The Dhan-MCX-GTT-capability gate and the wing_premium_pct /
    # wing_max_spread_pct override-value guards were removed (Bracket/
    # Template-GTT removal, T3.1, 2026-11). All three existed only to
    # protect a template attach that will now never happen —
    # `template_id` and every override field are silently dropped before
    # persist (see `_ticket_persist_live_algo_order`), never persisted
    # or acted on. Refusing an otherwise-ordinary order because the
    # frontend still auto-stamps one of these fields (it does, until the
    # frontend sub-sprint lands) would block a real, valid order for no
    # reason.
    await _ticket_enforce_lot_and_fat_finger(data, account, sym, qty, lot_size)

    if data.strategy_id:
        await _enforce_capacity_guard(
            strategy_id=int(data.strategy_id),
            account=account,
            tradingsymbol=sym,
            exchange=(data.exchange or "NFO"),
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
        resp = await _ticket_place_live(data, request, account, sym, side, qty, lot_size)
    else:
        # ── PAPER / default branch ────────────────────────────────────────
        resp = await _ticket_place_paper(data, request, account, sym, side, qty)

    # Draft cleanup (2026-10) — ONLY reached on a CONFIRMED successful
    # placement: both _ticket_place_live and _ticket_place_paper either
    # return a TicketOrderResponse here or raise HTTPException on any
    # failure (_ticket_handle_live_place_error always raises, never
    # returns), so an exception propagates straight past this block and
    # the draft is left untouched. Best-effort on top of
    # _draft_delete_by_id's own internal try/except: a delete failure here
    # must NEVER surface as a /ticket failure — the real order already
    # succeeded, and surfacing an error would make the client retry and
    # place a SECOND real order.
    if getattr(data, "draft_id", None):
        try:
            from backend.api.routes.orders import _draft_delete_by_id
            await _draft_delete_by_id(int(data.draft_id))
        except Exception as _e:
            logger.warning(
                f"[DRAFT] post-success cleanup failed for draft_id={data.draft_id}: {_e}"
            )

    return resp
