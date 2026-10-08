"""Release a held automated order: re-check the position and price, then send and chase.

Adding hold-support for a NEW agent category (a fourth `HoldCategory`, and
beyond) is a registration, not a change to the dispatcher in
`orders_held.py`. To wire one up end to end:

  1. Add a member to `HoldCategory` in `order_hold.py` (e.g. `MY_AGENT`).
  2. Wherever your agent decides to hold instead of fire, call
     `order_hold_gate.record_held_order(category=HoldCategory.MY_AGENT, ...)`.
  3. Write a release function with the shape
     `async def release_my_agent_hold(order_id: int, actor: str) -> dict`,
     returning `{"ok": bool, "reason": str, "status": str}`. Reuse whichever
     existing release function's SHAPE matches what your hold represents:
       - `release_held_order`'s pattern (re-check the live position + a
         fresh quote before placing a NEW close order) if your category
         means "place something new on release".
       - `release_repeated_rejection_hold`'s pattern (resume `chase_order`
         with the row's own already-persisted fields; no position/price
         pre-check for plain OPEN orders, but a close-intent row DOES get
         re-verified against the current position before resuming — see
         that function's own docstring, P1 fix 2026-10) if your category
         means "resume something already in flight".
  4. Register it in `_RELEASE_HANDLERS` below, keyed by
     `HoldCategory.MY_AGENT.value`. No route code to touch — the
     `/api/orders/held/{id}/release` route (`orders_held.py`) dispatches
     purely through `get_release_handler()`.

  IMPORTANT: a category you forget to register in `_RELEASE_HANDLERS`
  silently falls through to `_DEFAULT_RELEASE_HANDLER`
  (`release_held_order`) on release — which applies CLOSE semantics (the
  `position_matches` check, and `intent="close"` on the resumed order).
  That is correct only for `EXPIRY_CLOSE`-shaped holds. If your new
  category's hold does not represent "close an existing position", it
  MUST be registered, or release will silently misbehave.

  IMPORTANT: `order_hold_gate.record_held_order` hardcodes
  `engine="live"` / `mode="live"` on the row it writes. A category whose agent can run in
  paper/sim/replay/shadow mode must NOT use `record_held_order` as-is for
  its hold — doing so creates a live-mode row, and releasing it later
  places a REAL broker order (see the mode-gate invariant on
  `_fire_template_attach_on_fill` in `orders_place.py` / CLAUDE.md, which
  exists to stop exactly this class of bug). Such a category needs its
  own hold-recording path that threads the agent's real mode through.
"""
import asyncio
from datetime import datetime, timezone
from typing import Callable

from backend.shared.helpers.ramboq_logger import get_logger

from backend.api.algo.order_hold import HoldCategory, parse_hold_record, release_price

logger = get_logger(__name__)


def position_matches(side: str, expected_qty: int, net_qty: int) -> tuple[bool, str]:
    """A SELL close needs a long position of expected_qty; a BUY close needs a short one."""
    want = expected_qty if side.upper() == "SELL" else -expected_qty
    if net_qty == want:
        return True, "ok"
    return False, f"position changed: net {net_qty}, expected {want}"


def _find_net_qty(positions: dict, symbol: str, exchange: str) -> int | None:
    rows = (positions or {}).get("net") or []
    for r in rows:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper() \
                and str(r.get("exchange", "")).upper() == exchange.upper():
            return int(r.get("quantity") or 0)
    return 0


def _positions_net_rows_from_dfs(dfs) -> list[dict]:
    """Convert `broker_apis.fetch_positions()`'s `list[DataFrame]` result —
    already normalised to CONTRACTS via `_annotate_lot_size` (Kite ships
    MCX/NCO intraday `quantity` in LOTS; NFO/equity in contracts already) —
    into the same `{"net": [...]}` row-dict shape `_find_net_qty` expects
    from a raw `broker.positions()` call.

    This is the C1/MCX fix: `release_held_order` used to compare
    `row.quantity` (already contracts, since it was recorded via
    `ExpiryEngine._fetch_option_positions` → `broker_apis.fetch_positions()`)
    against a RAW `broker.positions()` call, where Kite reports MCX
    quantity in LOTS. Routing through the same normalised fetch here puts
    both sides of `position_matches` in the SAME unit (contracts) — see
    CLAUDE.md's "Option qty vs lot_size" guard.
    """
    rows: list[dict] = []
    for df in (dfs or []):
        if df is None or getattr(df, "empty", True):
            continue
        for rec in df.to_dict("records"):
            rows.append({
                "tradingsymbol": rec.get("tradingsymbol", ""),
                "exchange": rec.get("exchange", ""),
                "quantity": rec.get("quantity", 0),
            })
    return rows


def _positions_data_unreliable(dfs) -> bool:
    """True when any per-account frame returned by
    `broker_apis.fetch_positions()` is a degraded/stale substitute
    (circuit-breaker open, Dhan interval-skip throttle, or a genuine
    fetch failure) rather than a fresh broker read.

    Release must fail closed on a degraded read — comparing a held
    order's quantity against a last-known-good (possibly minutes-old)
    substitute frame instead of the live broker position would defeat
    the whole point of the position-match check.
    """
    if dfs is None:
        return True
    for df in dfs:
        if df is None:
            continue
        attrs = getattr(df, "attrs", {}) or {}
        if attrs.get("stale") or attrs.get("fetch_failed") \
                or attrs.get("circuit_open") or attrs.get("interval_skipped"):
            return True
    return False


def _chase_level_from_price_policy(price_policy: str | None) -> str:
    """Map a held row's `hold_json["price_policy"]` tag (e.g. `"CHASE_HIGH"`,
    the record_held_order/`_ch_hold_on_repeated_rejection` default
    `"CHASE_MED"`, or legacy `"n/a"`) back to the L/M/H level string
    `orders_helpers._live_chase_config` expects.

    P1 fix (2026-10): this is the read side of persisting the ORIGINAL
    chase's aggressiveness across a hold/release cycle — see
    `ChaseConfig.level`'s docstring (chase.py) and
    `_live_chase_config`'s `cfg.level = a` line (orders_helpers.py) for
    the write side. Unknown, missing, or legacy (`"n/a"`) tags fall back
    to `"med"` — the exact tuple every release path hardcoded before this
    fix — so a row held before this change, or one whose hold was never
    told a level (e.g. expiry-close's own fixed engine cfg), keeps
    today's resumed-chase behaviour unchanged.
    """
    if not price_policy:
        return "med"
    tag = price_policy.strip().upper()
    if tag.startswith("CHASE_"):
        tag = tag[len("CHASE_"):]
    tag = tag.lower()
    return tag if tag in ("low", "med", "high") else "med"


async def _verify_close_still_valid(
    account: str, symbol: str, exchange: str, side: str, remaining_qty: int,
) -> tuple[bool, str]:
    """Re-verify a HELD row's claimed `intent="close"` against the broker's
    CURRENT position immediately before resuming its chase on release.

    P1 fix (2026-10): `release_repeated_rejection_hold` used to resume a
    close-intent chase purely from the row's own already-persisted fields
    with no fresh check at all. A repeated-rejection hold can sit for
    hours awaiting operator review; a plausible operator response during
    that window is to flatten the position some other way. Resuming on
    release without re-checking would then place a FRESH opening order
    (wrong direction) with NO size cap, since `intent="close"` also
    bypasses the 50-lot adapter ceiling.

    Deliberately reuses THIS module's own `_positions_data_unreliable` /
    `_positions_net_rows_from_dfs` / `_find_net_qty` helpers — the same
    normalised-contracts, fail-closed-on-stale-data pipeline
    `release_held_order` already runs for its own position check — rather
    than `orders_place.py`'s `_verify_close_intent`, which has no
    staleness guard against a last-known-good substitute frame (exactly
    the gap this fix needs to close) and would silently verify "ok"
    against stale data.

    Sign + magnitude only (not exact equality): a SELL-close is valid
    while the account is still net LONG by at least `remaining_qty`; a
    BUY-close is valid while still net SHORT by at least `remaining_qty`.
    An operator adding to the position (making it net larger, same sign)
    must not refuse a legitimate resume.

    @returns (ok, reason). `remaining_qty <= 0` (fully filled already,
    nothing left to resume) is vacuously True.
    """
    if remaining_qty <= 0:
        return True, "nothing remaining to close"
    try:
        from backend.brokers import broker_apis
        pos_dfs = await asyncio.get_running_loop().run_in_executor(
            None, lambda: broker_apis.fetch_positions(account=account))
    except Exception as e:
        return False, f"position check failed: {e}"
    if _positions_data_unreliable(pos_dfs):
        return False, "position check failed: positions data stale or unavailable"
    positions = {"net": _positions_net_rows_from_dfs(pos_dfs)}
    net = _find_net_qty(positions, symbol, exchange)
    side_upper = (side or "").upper()
    if side_upper == "SELL" and net > 0 and remaining_qty <= net:
        return True, "ok"
    if side_upper == "BUY" and net < 0 and remaining_qty <= abs(net):
        return True, "ok"
    return False, (
        f"position no longer supports close: net={net}, "
        f"remaining={remaining_qty}, side={side_upper}"
    )


async def release_held_order(order_id: int, actor: str) -> dict:
    """Return {'ok': bool, 'reason': str, 'status': str}. Sends and chases only when all checks pass."""
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    from backend.brokers import get_broker
    from backend.api.routes.orders_helpers import _ensure_tick_index, _TICK_INDEX

    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "order not found", "status": ""}
        if row.status != "HELD":
            return {"ok": False, "reason": f"order is {row.status}, not HELD", "status": row.status}
        rec = parse_hold_record(row.hold_json) or {}

        try:
            broker = get_broker(row.account)
            # Normalised fetch (contracts, not raw Kite lots for MCX/NCO) —
            # see `_positions_net_rows_from_dfs`'s docstring for why a raw
            # `broker.positions()` call must never feed this comparison.
            from backend.brokers import broker_apis
            pos_dfs = await asyncio.get_running_loop().run_in_executor(
                None, lambda: broker_apis.fetch_positions(account=row.account))
            if _positions_data_unreliable(pos_dfs):
                return {
                    "ok": False,
                    "reason": "position check failed: positions data stale or unavailable",
                    "status": "HELD",
                }
            positions = {"net": _positions_net_rows_from_dfs(pos_dfs)}
            net = _find_net_qty(positions, row.symbol, row.exchange)
            ok, why = position_matches(row.transaction_type, int(row.quantity), net or 0)
            if not ok:
                return {"ok": False, "reason": why, "status": "HELD"}

            key = f"{row.exchange}:{row.symbol}"
            quote = (await asyncio.get_running_loop().run_in_executor(
                None, broker.quote, [key])) or {}
            q = quote.get(key) or {}
            depth = q.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0].get("price")
            ask = (depth.get("sell") or [{}])[0].get("price")
            await _ensure_tick_index()
            tick = _TICK_INDEX.get((row.exchange.upper(), row.symbol.upper()), 0.0)
            ok, price, why = release_price(
                bid, ask, q.get("last_price"), tick,
                float(q.get("lower_circuit_limit") or 0),
                float(q.get("upper_circuit_limit") or 0) or 1e12,
            )
            if not ok:
                return {"ok": False, "reason": f"price check failed: {why}", "status": "HELD"}
        except Exception as e:
            logger.error(f"[RELEASE] check failed for order {order_id}: {e}")
            return {"ok": False, "reason": f"broker check failed: {e}", "status": "HELD"}

        row.status = "OPEN"
        # Clear hold_json alongside the status change, matching the sibling
        # release paths (release_template_exit, release_repeated_rejection_hold).
        # orders_held.py's list_held query matches `status == "HELD" OR
        # hold_json IS NOT NULL` — leaving hold_json populated here left a
        # released expiry-close row stuck in HeldOrdersCard forever, and both
        # Release and Cancel on it then 409'd ("not HELD").
        row.hold_json = None
        row.detail = f"{row.detail or ''} · released by {actor} at {datetime.now(timezone.utc).isoformat()}"
        await s.commit()
        account, symbol, exchange = row.account, row.symbol, row.exchange
        side, qty, product, row_id = row.transaction_type, int(row.quantity), row.product, row.id
        agent_id = getattr(row, "agent_id", None)
        # P1 fix (2026-10): resume at the chase tier the row was originally
        # held/placed with (see `_chase_level_from_price_policy`'s docstring)
        # instead of a hardcoded MED tuple.
        chase_level = _chase_level_from_price_policy(rec.get("price_policy"))

    from backend.api.algo.order_events import write_event
    await write_event(row_id, "released", f"Released by {actor}: {side} {qty} {symbol}", {"actor": actor})

    asyncio.create_task(_chase_released(
        row_id, account, symbol, exchange, side, qty, product, chase_level,
        agent_id=agent_id,
    ))
    logger.info(f"[RELEASE] order {row_id} released by {actor}")
    return {"ok": True, "reason": "released; chasing", "status": "OPEN"}


async def _chase_released(row_id, account, symbol, exchange, side, qty, product,
                          level: str = "med", agent_id: "int | None" = None) -> None:
    from backend.api.algo.chase import chase_order
    from backend.api.routes.orders_helpers import _live_chase_config
    # P1 fix (2026-10): was a hardcoded MED tuple regardless of `level` —
    # now goes through the SAME single source of truth every other
    # chase-starting path uses (`orders_helpers._live_chase_config`),
    # collapsing a third independent copy of the L/M/H tier table.
    cfg = _live_chase_config(level, intent="close", product=product)
    cfg.exchange = exchange
    try:
        await chase_order(account=account, symbol=symbol, transaction_type=side,
                          quantity=qty, cfg=cfg, algo_order_id=row_id,
                          agent_id=agent_id)
    except Exception as e:
        logger.error(f"[RELEASE] chase failed for order {row_id}: {e}")


async def release_repeated_rejection_hold(order_id: int, actor: str) -> dict:
    """Release a chase row held after two consecutive price-shaped
    REJECTED outcomes (`_ch_hold_on_repeated_rejection`, chase.py).

    Unlike `release_held_order` (the expiry-close release path), this
    does NOT assume the held order is a position close: that path's
    `position_matches` check and hardcoded `intent="close"` are correct
    ONLY for expiry-close holds. A repeated-rejection hold can originate
    from a plain OPEN order just as easily as a close, so this reads the
    row's OWN already-persisted `transaction_type` / `quantity` /
    `exchange` / `product` / `intent` (set at original order placement,
    untouched by the hold) and resumes `chase_order()` with those exact
    values — no position/price pre-check for a plain OPEN order, since the
    only reason THAT row was held is "the broker kept rejecting the
    price", not "the position changed underneath it".

    P1 fix (2026-10): a row whose persisted `intent == "close"` IS now
    re-verified against the CURRENT broker position (`_verify_close_still_
    valid`) immediately before resuming, because this hold can sit for
    hours awaiting operator review, and the operator may have flattened
    the position some other way in that window. Resuming unconditionally
    would then place a FRESH opening order (wrong direction) with no size
    cap, since `intent="close"` also bypasses the 50-lot adapter ceiling.
    On a failed re-verification the release is refused (row stays HELD,
    `hold_json` untouched, no chase resumed, operator alerted) instead of
    silently misfiring.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select

    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "order not found", "status": ""}
        if row.status != "HELD":
            return {"ok": False, "reason": f"order is {row.status}, not HELD", "status": row.status}
        rec = parse_hold_record(row.hold_json) or {}
        if rec.get("category") != "agent_order":
            return {"ok": False, "reason": "not a repeated-rejection hold", "status": row.status}

        if row.intent == "close":
            remaining = int(row.quantity) - int(row.filled_quantity or 0)
            ok, why = await _verify_close_still_valid(
                row.account, row.symbol, row.exchange, row.transaction_type, remaining,
            )
            if not ok:
                logger.warning(
                    "[RELEASE] refusing to resume close-intent chase for "
                    "order %s: %s", order_id, why,
                )
                try:
                    from backend.shared.helpers.alert_utils import send_order_failure_alert
                    send_order_failure_alert(
                        account=row.account, symbol=row.symbol, exchange=row.exchange,
                        side=row.transaction_type, qty=remaining, mode="live",
                        source="release",
                        error=f"refused to resume close-intent chase on release: {why}",
                    )
                except Exception:
                    pass
                return {"ok": False, "reason": f"position check failed: {why}", "status": "HELD"}

        # Mark chase-active BEFORE committing status="OPEN" — otherwise
        # the row sits OPEN with its old rejected broker_order_id, visible
        # to the ~3s /chases/active reconcile sweep, for the window between
        # this commit and _resume_chase_after_hold's chase_order() actually
        # starting (which is what normally marks it active). If that sweep
        # runs in the gap it can flip the row back to a final REJECTED,
        # and the resumed chase's own _sync_algo_order_id then aborts with
        # the exact false-CRITICAL this whole mechanism exists to prevent.
        from backend.api.algo.chase import _ch_mark_chase_active
        _ch_mark_chase_active(row.id)

        row.status = "OPEN"
        row.hold_json = None
        row.detail = (
            f"{row.detail or ''} · released by {actor} at "
            f"{datetime.now(timezone.utc).isoformat()} (resuming chase)"
        )
        await s.commit()
        account, symbol, exchange = row.account, row.symbol, row.exchange
        side, qty, product, row_id = row.transaction_type, int(row.quantity), row.product, row.id
        intent = row.intent or None
        already_filled = int(row.filled_quantity or 0)
        agent_id = getattr(row, "agent_id", None)
        # P1 fix (2026-10): resume at the original chase tier (see
        # `_chase_level_from_price_policy`'s docstring), not a hardcoded
        # MED tuple.
        chase_level = _chase_level_from_price_policy(rec.get("price_policy"))

    from backend.api.algo.order_events import write_event
    await write_event(
        row_id, "released",
        f"Released by {actor}: {side} {qty} {symbol} (resuming chase)",
        {"actor": actor},
    )

    asyncio.create_task(_resume_chase_after_hold(
        row_id, account, symbol, exchange, side, qty, product, intent, already_filled,
        chase_level, agent_id=agent_id,
    ))
    logger.info(f"[RELEASE] repeated-rejection hold {row_id} released by {actor} — resuming chase")
    return {"ok": True, "reason": "released; chasing resumed", "status": "OPEN"}


async def _resume_chase_after_hold(
    row_id, account, symbol, exchange, side, qty, product, intent, already_filled,
    level: str = "med", agent_id: "int | None" = None,
) -> None:
    from backend.api.algo.chase import chase_order, _ch_mark_chase_inactive
    from backend.api.routes.orders_helpers import _live_chase_config
    # P1 fix (2026-10): was a hardcoded MED tuple regardless of `level` —
    # now goes through the SAME single source of truth every other
    # chase-starting path uses (`orders_helpers._live_chase_config`),
    # collapsing a third independent copy of the L/M/H tier table.
    cfg = _live_chase_config(level, intent=intent, product=product)
    cfg.exchange = exchange
    try:
        await chase_order(account=account, symbol=symbol, transaction_type=side,
                          quantity=qty, cfg=cfg, algo_order_id=row_id,
                          already_filled=already_filled, agent_id=agent_id)
    except Exception as e:
        logger.error(f"[RELEASE] resume-chase failed for order {row_id}: {e}")
    finally:
        # Releases the pre-emptive mark taken in release_repeated_rejection_hold
        # before this task started. chase_order() itself also marks/unmarks
        # active around its own body (refcounted, so the two marks stack
        # correctly and this one's release here is independent of that).
        _ch_mark_chase_inactive(row_id)


async def release_template_exit(order_id: int, actor: str) -> dict:
    """Release held template exits: place the exit GTTs for a filled parent.

    CRITICAL FIX (2026-10): `_fire_template_attach_on_fill` re-checks
    `template_exit_held()` against the SAME still-held global switch /
    per-order override before doing anything — so calling it here with
    no way to skip that check meant every release immediately re-held
    the order again (via `hold_template_exit`), while this function
    still returned a hardcoded `{"ok": True, "status": "FILLED"}`
    regardless of what actually happened. The operator was told exits
    were placed when nothing was ever sent to the broker. Fixed by
    calling with `bypass_hold=True` — releasing IS the operator's
    explicit authorization, so the hold must not re-fire on this path —
    and by inspecting the REAL `AttachResult`: `ok` is only `True` when
    it actually produced a GTT id or a wing order id. On a non-success,
    the hold is restored via `hold_template_exit()` so the row stays in
    HeldOrdersCard and a second Release attempt remains possible,
    instead of silently falling out of view with nothing armed.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    from backend.api.routes.orders_place import _fire_template_attach_on_fill

    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "order not found", "status": ""}
        rec = parse_hold_record(row.hold_json) or {}
        if rec.get("category") != "template_exit":
            return {"ok": False, "reason": "no held template exits on this order", "status": row.status}
        if row.attached_gtts_json:
            row.hold_json = None
            await s.commit()
            return {"ok": True, "reason": "exits already attached", "status": row.status}
        row.hold_json = None
        await s.commit()
        row_status = row.status
        args = dict(parent_row_id=row.id, parent_account=row.account,
                    parent_symbol=row.symbol, parent_exchange=row.exchange,
                    parent_side=row.transaction_type, parent_qty=int(row.quantity),
                    fill_price=float(row.fill_price or 0), template_id=int(row.template_id or 0),
                    parent_product=row.product or "NRML", mode=row.mode or "live")
    from backend.api.algo.order_events import write_event
    await write_event(order_id, "released", f"Template exits released by {actor}", {"actor": actor})
    result = await _fire_template_attach_on_fill(**args, bypass_hold=True)
    if result is not None and (result.gtt_ids or result.wing_order_id):
        return {"ok": True, "reason": "template exits placed", "status": row_status}
    from backend.api.algo.order_hold_gate import hold_template_exit
    await hold_template_exit(order_id, args["parent_symbol"])
    return {"ok": False, "reason": "template exit attach failed — hold restored", "status": row_status}


# ── Release dispatch registry ───────────────────────────────────────────
#
# Keyed by `HoldCategory.value` (the string stored in `hold_json["category"]`
# — see `order_hold.py:hold_record`). The route (`orders_held.py`) looks up
# the handler for a held row's category here instead of an if/elif chain.
#
# The dict below is built once at import time and holds references to the
# functions as they exist THEN. Tests patch handlers by module attribute
# name (e.g. `patch("backend.api.algo.order_release.release_held_order",
# ...)`) — that mutates this module's `__dict__`, not the dict's stored
# reference. `get_release_handler()` re-resolves each handler's CURRENT
# value by name every call, so a patched/mocked handler is always what
# actually runs.
_RELEASE_HANDLERS: dict[str | None, Callable] = {
    HoldCategory.TEMPLATE_EXIT.value: release_template_exit,
    HoldCategory.AGENT_ORDER.value: release_repeated_rejection_hold,
}
_DEFAULT_RELEASE_HANDLER: Callable = release_held_order  # expiry_close + anything unregistered


def get_release_handler(category: str | None) -> Callable:
    """Return the release function registered for `category`, re-resolved
    by name against this module's current globals so test monkeypatching
    of a handler by module attribute always takes effect (see the registry
    comment above)."""
    fn = _RELEASE_HANDLERS.get(category, _DEFAULT_RELEASE_HANDLER)
    return globals().get(fn.__name__, fn)


async def cancel_held_order(order_id: int, actor: str) -> dict:
    """Abandon a held order instead of releasing it. Pure DB state transition —
    no held order (any category) has a live resting broker order, so no
    broker-side cancel call is needed (see `order_hold.py`'s own docstring and
    the structure of the three `release_*` functions above: each one PLACES
    something new on release, none of them resume/cancel an already-resting
    broker order).

    Deliberate deviation from "generic across all hold categories, don't
    branch by category": `EXPIRY_CLOSE` and `AGENT_ORDER` holds both set
    `row.status = "HELD"` at hold-time (`order_hold_gate.py:record_held_order`,
    `chase.py`'s repeated-rejection hold), but `TEMPLATE_EXIT` holds
    (`order_hold_gate.py:hold_template_exit`) do NOT touch `row.status` —
    they mark a FILLED parent's pending exit-attach with `hold_json` only,
    leaving `row.status == "FILLED"`. Branching on hold category (not
    `row.status`) would mean a template_exit cancel could never succeed
    (status would always read "FILLED, not HELD"), defeating the feature for
    one of the three listed categories. Branching on row state instead:
    - `status == "HELD"` (expiry_close / agent_order): transition to
      CANCELLED, same as the literal spec.
    - `status != "HELD"` but a `template_exit` hold is pending: clear the
      hold only (abandon the pending exit attach), leave `status` untouched.
      Never overwrite a FILLED parent's status to CANCELLED — that would
      misstate a real broker fill and corrupt order history/reconcile.
    - Anything else: refuse, mirroring `release_held_order`'s own refusal
      shape.
    """
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from sqlalchemy import select

    async with async_session() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == order_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": "order not found", "status": ""}

        rec = parse_hold_record(row.hold_json) or {}
        category = rec.get("category")
        now = datetime.now(timezone.utc).isoformat()

        if row.status == "HELD":
            row.status = "CANCELLED"
            row.hold_json = None
            row.detail = f"{row.detail or ''} · cancelled by {actor} at {now}"
            await s.commit()
            account, symbol, exchange = row.account, row.symbol, row.exchange
            side, qty, row_id = row.transaction_type, int(row.quantity), row.id
            new_status = "CANCELLED"
            msg = f"Cancelled by {actor}: {side} {qty} {symbol}"
        elif category == "template_exit" and row.hold_json:
            row.hold_json = None
            row.detail = f"{row.detail or ''} · template exits cancelled by {actor} at {now}"
            await s.commit()
            account, symbol, exchange = row.account, row.symbol, row.exchange
            side, qty, row_id = row.transaction_type, int(row.quantity), row.id
            new_status = row.status
            msg = f"Template exits cancelled by {actor}: {side} {qty} {symbol}"
        else:
            return {"ok": False, "reason": f"order is {row.status}, not HELD", "status": row.status}

    from backend.api.algo.order_events import write_event
    await write_event(row_id, "cancelled", msg, {"actor": actor})
    logger.info(f"[CANCEL] order {row_id} cancelled by {actor}")
    return {"ok": True, "reason": "cancelled", "status": new_status}
