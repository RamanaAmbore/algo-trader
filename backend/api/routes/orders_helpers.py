"""
orders_helpers.py — Shared constants, pure utilities, and data helpers for the
orders route family.

Extracted from orders.py (4322 LOC → split) as Commit 1 of the RED-zone split.

Symbols exported for re-use by orders_place.py, orders_postback.py,
orders_basket.py, and orders.py itself.

No imports from orders.py — this module must be importable stand-alone.
"""

from __future__ import annotations

import asyncio
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import msgspec
from litestar.exceptions import HTTPException

from backend.api.schemas import OrderRow, OrdersResponse
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ── Validation constants ──────────────────────────────────────────────────────
_VARIETIES   = {"regular", "amo", "co"}
_ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
_PRODUCTS    = {"CNC", "MIS", "NRML"}
_TXN_TYPES   = {"BUY", "SELL"}
_EXCHANGES   = {"NSE", "BSE", "NFO", "CDS", "MCX", "BFO", "NCO", "BCD"}
_VALIDITIES  = {"DAY", "IOC"}

_ORDERS_TTL  = 15   # orders refresh faster — 15 s cache

# ── Live-chase task registry ──────────────────────────────────────────────────
# Keep strong references to running chase_order tasks so they are not garbage-
# collected before completion.  Tasks remove themselves via the discard callback.
_LIVE_CHASE_TASKS: set[asyncio.Task] = set()

# ── Live-order circuit breaker ────────────────────────────────────────────────
# Stop the operator (or an agent) from re-attempting the same rejected
# order again and again. Track rejection timestamps per
# (account, symbol, side, qty) tuple. After REJECTION_THRESHOLD
# rejections in the last REJECTION_WINDOW_S seconds, the next attempt
# returns 423 (Locked) without hitting the broker, and a Telegram +
# email alert fires so the operator knows the breaker tripped.
#
# Module-level state — lost on service restart. That's fine; the
# operator's debug session rarely exceeds a single working day.

_REJECTION_TRACKER: dict[str, list[float]] = {}
_REJECTION_WINDOW_S = 3600       # 1 hour
_REJECTION_THRESHOLD = 3
_BREAKER_ALERT_COOLDOWN_S = 600  # 10 min between breaker-trip alerts per key
_BREAKER_LAST_ALERT: dict[str, float] = {}


def _rejection_key(account: str, symbol: str, side: str, qty: int) -> str:
    return f"{account}|{symbol}|{side}|{qty}"


def _prune_rejection_window(key: str) -> None:
    now = _time.time()
    cutoff = now - _REJECTION_WINDOW_S
    _REJECTION_TRACKER[key] = [t for t in _REJECTION_TRACKER.get(key, []) if t > cutoff]


def _rejection_count(key: str) -> int:
    _prune_rejection_window(key)
    return len(_REJECTION_TRACKER.get(key, []))


def _record_rejection(key: str) -> int:
    """Append now() to the rejection list for this key, return new count."""
    _prune_rejection_window(key)
    _REJECTION_TRACKER.setdefault(key, []).append(_time.time())
    return len(_REJECTION_TRACKER[key])


def _clear_rejections(key: str) -> None:
    """Reset on a successful placement."""
    _REJECTION_TRACKER.pop(key, None)
    _BREAKER_LAST_ALERT.pop(key, None)


def _maybe_send_breaker_alert(key: str, account: str, symbol: str,
                               side: str, qty: int, reason: str) -> None:
    """Fire one alert per key per cooldown window. Returns silently on
    any send failure so the trip itself isn't blocked by infra issues."""
    now = _time.time()
    last = _BREAKER_LAST_ALERT.get(key, 0)
    if now - last < _BREAKER_ALERT_COOLDOWN_S:
        return
    _BREAKER_LAST_ALERT[key] = now
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange="—",
            side=side, qty=qty, mode="live",
            source="circuit-breaker",
            error=(f"BREAKER TRIPPED — {_REJECTION_THRESHOLD}+ rejections in "
                   f"the last {_REJECTION_WINDOW_S//60} min. "
                   f"Further submits blocked until the breaker resets. "
                   f"Last reason: {reason[:200]}"),
        )
    except Exception as e:
        logger.warning(f"[BREAKER] alert dispatch failed for {key}: {e}")


# ── Tick-size index ───────────────────────────────────────────────────────────
# Built on first lookup from the instruments cache, rebuilt only when the
# cache version stamp changes. Pre-fix the lookup did a linear scan through
# ~10-50k instrument rows on EVERY `_align_price_to_tick` call; the ticket
# route calls it twice per placement (price + trigger_price), so a single
# order paid ~100k linear iterations. Indexed by (exchange, symbol) tuple → O(1).
_TICK_INDEX: dict[tuple[str, str], float] = {}
_TICK_INDEX_STAMP: object | None = None


def _rebuild_tick_index(items) -> None:
    """Rebuild the (exchange, symbol) → tick_size dict from the
    instruments cache. Called once per cache refresh; subsequent
    `_align_price_to_tick` calls hit the dict directly."""
    global _TICK_INDEX
    new_index: dict[tuple[str, str], float] = {}
    for inst in items:
        ts = float(inst.ts or 0)
        if ts > 0:
            new_index[(inst.e.upper(), inst.s.upper())] = ts
    _TICK_INDEX = new_index


def _snap_to_tick(price: float, tick: float) -> float:
    """Round *price* half-up to the nearest valid tick grid value.

    Uses integer-scaled arithmetic to avoid binary float artefacts
    (0.05-tick grids misbehave with naive floating-point division).
    """
    scale = round(1.0 / tick) if tick < 1 else 1
    if scale > 1:
        aligned = round(float(price) * scale) / scale
    else:
        aligned = round(float(price) / tick) * tick
    return round(aligned, 4)


async def _ensure_tick_index() -> None:
    """Refresh `_TICK_INDEX` from the instruments cache when the cache version stamp changes."""
    global _TICK_INDEX_STAMP
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
    resp = await get_or_fetch("instruments", _fetch_instruments, ttl_seconds=_TTL_SECONDS)
    # Identity comparison is enough — get_or_fetch returns the SAME instance
    # while the cache entry is valid, then a new instance on refresh.
    if resp is not _TICK_INDEX_STAMP or not _TICK_INDEX:
        _rebuild_tick_index(resp.items if resp else [])
        _TICK_INDEX_STAMP = resp


async def _align_price_to_tick(exchange: str, symbol: str,
                                price: float | None) -> float | None:
    """Snap *price* to the nearest valid tick for the instrument.

    Kite rejects orders whose LIMIT / trigger price isn't a multiple
    of the contract's `tick_size` with "Exchange invalid price — the
    entered price is not as per ticker price". Operators routinely
    enter ₹9961.50 for a MCX commodity that ticks at ₹1 (whole
    rupees); we round to the nearest valid tick before sending.

    Reads tick_size from the in-process `_TICK_INDEX` (O(1) dict
    lookup, built lazily from the instruments cache). Returns the
    input unchanged when:
      - price is None or 0
      - the instrument isn't in the cache (let Kite reject explicitly)
      - tick_size resolves to a non-positive value (defensive)

    Rounding policy: half-up to the nearest tick, then clamped to the
    same tick grid. For options with tick=0.05, 12.37 → 12.35;
    for commodities with tick=1, 9961.50 → 9962.
    """
    if price is None or price == 0:
        return price
    try:
        await _ensure_tick_index()
    except Exception:
        return price
    tick = _TICK_INDEX.get(((exchange or "").upper(), (symbol or "").upper()))
    if not tick or tick <= 0:
        return price
    aligned = _snap_to_tick(price, tick)
    if aligned != price:
        logger.info(f"[TICK] aligned {symbol} price {price} → {aligned} (tick={tick})")
    return aligned


# ── Broker accessor ───────────────────────────────────────────────────────────

def _broker_for(account: str):
    """Return the `Broker` adapter for `account`. Replaces the prior
    `_kite_for(account)` helper that exposed a raw KiteConnect handle.
    All downstream callers use Broker ABC methods (place_order,
    modify_order, cancel_order, orders, etc.) so the order routes are
    now broker-agnostic — adding a Groww/Dhan account requires no
    edits here."""
    from backend.brokers.registry import get_broker
    try:
        return get_broker(account)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account '{account}' not found")


# ── Chase helpers ─────────────────────────────────────────────────────────────

def _live_chase_config(aggressiveness: str, intent: str | None = None,
                       product: str = "NRML", variety: str = "regular",
                       validity: str = "DAY"):
    """Map operator-facing L/M/H aggressiveness to ChaseConfig.

    Industry analogue: IBKR Adaptive Algo Patient / Normal / Urgent.
      low   — patient: long interval, small aggression step, more
              attempts. Order rests near midpoint, eases into
              taking liquidity only when the market doesn't come.
      med   — balanced (chase.py defaults).
      high  — urgent: short interval, big aggression step, fewer
              attempts. Cross the spread fast.

    Engine-side defaults still come from /admin/settings (algo.*)
    when the request doesn't carry an aggressiveness override.
    Default: 'low' — the operator's standing instruction is "be
    patient on entry"; callers explicitly bump to med/high when
    they want more fill speed at the cost of slippage.

    intent is forwarded to ChaseConfig so that close orders
    (intent="close") bypass the 50-lot Kite ceiling on each
    re-place attempt inside the chase loop.

    D1 fix (2026-09): product/variety/validity are now forwarded onto
    ChaseConfig too — every re-place attempt was previously hardcoded
    to ChaseConfig's dataclass defaults (product="NRML",
    variety="regular", validity="DAY") regardless of what the
    operator actually selected on the ticket (e.g. MIS intraday), so
    a chased MIS close was silently re-placed as NRML on every
    attempt. Defaults here match ChaseConfig's own defaults so every
    OTHER caller (test_close_intent_chase.py, any future caller that
    doesn't pass these kwargs) keeps its current behavior unchanged.
    """
    from backend.api.algo.chase import ChaseConfig
    a = (aggressiveness or "low").lower()
    if a == "high":
        cfg = ChaseConfig(interval_seconds=10, aggression_step=0.25,
                          max_attempts=10)
        level = "high"
    elif a == "med":
        cfg = ChaseConfig(interval_seconds=20, aggression_step=0.10,
                          max_attempts=20)
        level = "med"
    else:
        # low (default) — patient: peg passively, ease into the
        # spread only after enough ticks pass. Also the fallback tier
        # for any unrecognised `aggressiveness` string, so `level` is
        # pinned to "low" here too (NOT the raw `a`) — otherwise a
        # malformed value (e.g. a client typo; `chase_aggressiveness`
        # is an unvalidated free-form str in schemas.py) would run at
        # the LOW tuple while stamping a non-canonical `level` that
        # `chase.py`'s hold writer can't recognise, falls back to
        # "CHASE_MED", and resumes a HELD release at the WRONG
        # (MED, not LOW) tier.
        cfg = ChaseConfig(interval_seconds=30, aggression_step=0.05,
                          max_attempts=30)
        level = "low"
    cfg.intent = intent
    cfg.product = product
    cfg.variety = variety
    cfg.validity = validity
    # P1 fix (2026-10): stamp the NORMALISED L/M/H tag (one of the three
    # branch-local `level` values above, never the raw `a`) onto the cfg
    # itself so a hold recorded mid-chase
    # (`chase.py:_ch_hold_on_repeated_rejection`) can persist which tier
    # the order was ORIGINALLY placed with — see `ChaseConfig.level`'s
    # own docstring.
    cfg.level = level
    return cfg


# D5 fix (2026-09) — module constant so tests can shrink the confirm
# window via monkeypatch instead of sleeping the full 15s.
_LIVE_CHASE_CONFIRM_TIMEOUT_S = 15.0


async def _start_live_chase(account: str, symbol: str, exchange: str,
                            transaction_type: str, quantity: int,
                            aggressiveness: str,
                            algo_order_id: int | None = None,
                            intent: str | None = None,
                            product: str = "NRML",
                            variety: str = "regular",
                            validity: str = "DAY") -> str:
    """Place + chase a LIVE order in the background.

    Spawns `chase_order()` as an asyncio task and synchronously
    returns the first broker order_id (so the ticket can confirm
    placement to the operator immediately). The chase task keeps
    running after this returns — re-quoting the limit per the
    aggressiveness config until the order fills or the attempt
    cap is hit.

    Phase 0.5 — algo_order_id is plumbed into chase_order so it can
    keep AlgoOrder.broker_order_id in lockstep with each replace, and
    so its terminal handler can identify the row even when broker_id
    has mutated since the original place. Without this, chased orders
    silently failed to flip to FILLED in the DB → templates never
    attached on fill.

    D5 fix (2026-09) — a ticket-side failure/timeout used to leave the
    spawned `chase_order` task running unattended: `chase_order`
    tolerates its first attempt's error (retries up to
    `_MAX_CHASE_ERRORS`) while this function's `on_event` resolved
    `fut` with an exception on the very FIRST "error" event, so the
    ticket route returned 400 immediately while the background task
    kept going — possibly placing a live order nobody was watching.
    Now: on an "error"/"chase_failed" pre-placement event, the task is
    cancelled outright (safe — chase_order parks in `asyncio.sleep`
    between attempts, or is already finishing on chase_failed). On a
    15s confirm-timeout (chase_order's FIRST place_order call may
    still be in flight on the executor thread — NOT safe to hard
    -cancel, since chase_order has no CancelledError handling and the
    broker call could still land), the task is left running but
    "abandoned": if it later reports order_placed, that order id is
    immediately fed to chase.py's existing `mark_killed()` so the next
    poll cycle's `_ch_post_replace_kill_check` cancels it at the
    broker and terminates the chase — closing the unattended-chase
    window without touching chase.py's core logic. A done-callback
    also fast-fails the ticket (instead of eating the full timeout)
    when `chase_order` returns without ever emitting an event at all
    (e.g. market-closed / non-prod-branch early-return paths).
    """
    from backend.api.algo.chase import chase_order
    cfg = _live_chase_config(aggressiveness, intent=intent,
                             product=product, variety=variety,
                             validity=validity)
    cfg.exchange = exchange or "NFO"

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    # Set once the ticket-side wait gives up on this chase (error or
    # timeout) so on_event can (a) suppress the misleading
    # "post-placement failure" alert for a chase we already gave up
    # on, and (b) kill any order that lands late.
    _abandoned = {"v": False}

    def on_event(evt: str, detail: dict):
        # First order_placed event resolves the future. Subsequent
        # events (cancel + re-place per attempt) are no-ops here
        # — the chase task keeps logging them via chase.py's own
        # `logger.info` calls.
        if not fut.done():
            if evt == "order_placed":
                fut.set_result(str(detail.get("order_id") or ""))
            elif evt in ("error", "chase_failed"):
                fut.set_exception(RuntimeError(
                    detail.get("error") or "chase failed before initial placement"
                ))
        elif _abandoned["v"] or fut.cancelled():
            # The ticket-side wait already gave up (timeout or error) —
            # OR `fut` was cancelled by `asyncio.wait_for`'s own timeout
            # handling before this function's `except asyncio.TimeoutError`
            # block got a chance to flip `_abandoned["v"]` (a genuine
            # race: wait_for cancels the awaited future synchronously
            # when its timer fires, one loop tick before the awaiting
            # coroutine resumes to run the except block). Checking
            # `fut.cancelled()` directly closes that gap — without it, a
            # `order_placed` landing in that narrow window would fall
            # through to the "post-placement" branch below and do
            # nothing, leaving exactly the unattended live order D5
            # exists to prevent.
            #
            # A late order_placed means chase_order's in-flight first
            # attempt actually landed at the broker after we stopped
            # waiting — kill it via chase.py's own operator-kill signal
            # so the next poll cancels it and the chase terminates.
            if evt == "order_placed":
                _late_id = str(detail.get("order_id") or "")
                if _late_id:
                    from backend.api.algo.chase import mark_killed
                    mark_killed(_late_id)
                    logger.warning(
                        "[CHASE-ABANDONED] late order_placed after ticket-side "
                        "give-up — killed order_id=%s algo_order_id=%s %s %s "
                        "acct=%s qty=%s",
                        _late_id, algo_order_id, symbol, transaction_type,
                        account, quantity,
                    )
            # Suppress the post-placement alert below for an abandoned
            # chase — the operator already saw the ticket-side failure.
        else:
            # Initial placement already succeeded (future resolved).
            #
            # 2026-10-23 chase robustness (item 5) — this used to call
            # send_order_failure_alert on EVERY "error"/"chase_failed"
            # event, including a plain RECOVERABLE "error" that chase
            # keeps retrying from (and very often fills cleanly on the
            # very next attempt) — a false alarm that violates the
            # action-required ntfy gate (a mid-chase retry is not
            # action-required) and confused the operator with a
            # "failure" alert for an order that went on to fill fine.
            # Separately, EVERY terminal "chase_failed"/"chase_cancel_
            # failed" emission site inside chase.py (consecutive-errors
            # abort, exhaustion, non-recoverable rejection/exception,
            # stuck, place-timeout) already fires its own
            # send_order_failure_alert — alerting here on "chase_failed"
            # too was ALWAYS a guaranteed duplicate. Fix: log only, for
            # local telemetry/debugging; no alert from this handler at
            # all — chase.py's own alerting already covers every
            # genuinely terminal outcome.
            if evt in ("error", "chase_failed"):
                _err = detail.get("error") or f"chase {evt}"
                logger.info(
                    "[CHASE-POST-PLACEMENT] %s algo_order_id=%s %s %s "
                    "acct=%s qty=%s: %s",
                    evt, algo_order_id, symbol, transaction_type,
                    account, quantity, _err,
                )

    _chase_task = asyncio.create_task(chase_order(
        account=account, symbol=symbol,
        transaction_type=transaction_type, quantity=quantity,
        cfg=cfg, on_event=on_event,
        algo_order_id=algo_order_id,
    ))
    _LIVE_CHASE_TASKS.add(_chase_task)
    _chase_task.add_done_callback(_LIVE_CHASE_TASKS.discard)

    def _on_task_done(t: asyncio.Task) -> None:
        # Covers paths where chase_order returns/raises WITHOUT ever
        # emitting an event (market-closed / non-prod-branch early
        # returns) — without this, the ticket route silently ate the
        # full confirm-timeout for a blank TimeoutError instead of
        # failing fast with the real reason.
        if fut.done() or t.cancelled():
            return
        _exc = t.exception()
        if _exc is not None:
            fut.set_exception(RuntimeError(f"chase task raised before placement: {_exc}"))
        else:
            _result = t.result()
            _detail = getattr(_result, "detail", None) or "chase ended before initial placement"
            fut.set_exception(RuntimeError(_detail))

    _chase_task.add_done_callback(_on_task_done)

    # Confirm timeout — chase_order's first iteration fetches depth
    # and fires place_order; even a cold market should land
    # under 5 s. This gives Kite room for a slow first call.
    try:
        return await asyncio.wait_for(fut, timeout=_LIVE_CHASE_CONFIRM_TIMEOUT_S)
    except RuntimeError:
        # Pre-placement "error"/"chase_failed" event — chase_order is
        # parked in its inter-attempt sleep (safe to cancel) or already
        # finishing (chase_failed). Cancel outright to close the
        # unattended-chase window immediately.
        _abandoned["v"] = True
        if not _chase_task.done():
            _chase_task.cancel()
        raise
    except asyncio.TimeoutError:
        # No event at all within the confirm window — chase_order's
        # FIRST place_order call may still be in flight on the executor
        # thread. Do NOT hard-cancel (chase_order has no CancelledError
        # handling; the broker call would land untracked). Mark
        # abandoned so a late order_placed gets killed via on_event
        # above, and surface a descriptive error instead of a blank
        # TimeoutError.
        _abandoned["v"] = True
        raise RuntimeError(
            f"chase confirm timed out after {_LIVE_CHASE_CONFIRM_TIMEOUT_S}s "
            f"waiting for the first order_placed event — chase task left "
            f"running; any late placement will be killed"
        ) from None


# ── Row builders ──────────────────────────────────────────────────────────────

# Broker ids CONFIRMED to report MCX/NCO order-row quantity fields
# (quantity / pending_quantity / filled_quantity) in LOTS — the exchange
# wire convention — rather than CONTRACTS. Accepts both the canonical
# `Broker.broker_id` value ("zerodha_kite") and the short-form id used by
# the postback fan-out helpers ("kite"), plus "dhan". Mirrors the
# lots→contracts conversion already applied to the positions DataFrame in
# `backend/brokers/broker_apis.py:_annotate_lot_size` (~line 1992-2003)
# and to postback fill quantities in
# `backend/api/routes/orders.py:_mcx_postback_qty_to_contracts`. Groww
# reports MCX quantity in contracts already and must NOT appear here —
# double-converting it would silently under-report Groww MCX order qty.
_MCX_LOTS_CONVENTION_BROKER_IDS = frozenset({"zerodha_kite", "kite", "dhan"})


def _mcx_row_qty_to_contracts(exchange: str, symbol: str, broker_id: str, qty) -> int:
    """Convert a raw order-row quantity field from LOTS to CONTRACTS for
    MCX/NCO rows sourced from Kite/Dhan.

    Reads `_LOT_INDEX` directly (no cache refresh — same synchronous,
    best-effort pattern as `_mcx_postback_qty_to_contracts`) because this
    runs inside a `ThreadPoolExecutor` worker (`_fetch_orders._one_account`)
    with no running event loop to `await` a refresh on.

    Returns qty unchanged (best-effort, never raises) when: the exchange
    isn't MCX/NCO, the broker isn't in the lots-convention allow-list
    (Groww, paper, simulated fan-out), or the lot-size cache is cold —
    in the cold-cache case the raw value passes through with a warning
    rather than risk under/over-converting.
    """
    try:
        _qty_int = int(qty or 0)
    except (TypeError, ValueError):
        return 0
    if (exchange or "").upper() not in ("MCX", "NCO"):
        return _qty_int
    if str(broker_id or "").lower() not in _MCX_LOTS_CONVENTION_BROKER_IDS:
        return _qty_int
    try:
        from backend.brokers.adapters.kite import _LOT_INDEX
        lot_size = _LOT_INDEX.get((exchange, symbol), 0)
        if lot_size > 1:
            return _qty_int * lot_size
        logger.warning(
            f"[MCX-ORDER-ROW-QTY] cold/missing lot_size for {exchange}/{symbol} — "
            f"passing raw qty={_qty_int} through unconverted"
        )
    except Exception as _lex:
        logger.warning(f"[MCX-ORDER-ROW-QTY] lot_size lookup failed for {exchange}/{symbol}: {_lex}")
    return _qty_int


def _mcx_qty_unverified(exchange: str, symbol: str, broker_id: str, qty) -> bool:
    """True when this row's MCX/NCO quantity is still in lots because the lot size is not loaded."""
    try:
        if int(qty or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if (exchange or "").upper() not in ("MCX", "NCO"):
        return False
    if str(broker_id or "").lower() not in _MCX_LOTS_CONVENTION_BROKER_IDS:
        return False
    try:
        from backend.brokers.adapters.kite import _LOT_INDEX
        return _LOT_INDEX.get((exchange, symbol), 0) <= 1
    except Exception:
        return True


def _row_from_dict(d: dict, account: str, broker_id: str = "") -> OrderRow:
    """Build an `OrderRow` from a broker-native order dict.

    `broker_id` gates the MCX/NCO lots→contracts normalization applied to
    `quantity`, `pending_quantity`, and `filled_quantity` — all three
    carry the same broker-native unit for a given order (Kite/Dhan ship
    all three in LOTS for MCX/NCO, same as their intraday position
    fields; see `_mcx_row_qty_to_contracts`). Every other `OrderRow`
    consumer (ticket qty math, modify-ticket lot/contract round-trip,
    chase) treats `quantity` as CONTRACTS — leaving these fields
    unnormalized silently resizes MCX order modifications.
    """
    exchange = str(d.get("exchange", ""))
    symbol = str(d.get("tradingsymbol", ""))
    return OrderRow(
        order_id=str(d.get("order_id", "")),
        account=account,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=str(d.get("transaction_type", "")),
        quantity=_mcx_row_qty_to_contracts(exchange, symbol, broker_id, d.get("quantity")),
        qty_unverified=_mcx_qty_unverified(exchange, symbol, broker_id, d.get("quantity")),
        pending_quantity=_mcx_row_qty_to_contracts(exchange, symbol, broker_id, d.get("pending_quantity")),
        filled_quantity=_mcx_row_qty_to_contracts(exchange, symbol, broker_id, d.get("filled_quantity")),
        price=float(d.get("price") or 0),
        trigger_price=float(d.get("trigger_price") or 0),
        average_price=float(d.get("average_price") or 0),
        status=str(d.get("status", "")),
        order_type=str(d.get("order_type", "")),
        product=str(d.get("product", "")),
        variety=str(d.get("variety", "")),
        order_timestamp=str(d.get("order_timestamp", "")),
        exchange_timestamp=str(d.get("exchange_timestamp") or ""),
        status_message=str(d.get("status_message") or ""),
        tag=str(d.get("tag") or ""),
    )


_BROKER_ORDERS_TIMEOUT = 8


def _fetch_orders() -> OrdersResponse:
    import concurrent.futures as _cf
    from backend.brokers.registry import all_brokers

    brokers = list(all_brokers())
    if not brokers:
        return OrdersResponse(rows=[], refreshed_at=timestamp_display())

    def _one_account(broker) -> list[OrderRow]:  # type: ignore[no-untyped-def]
        account = broker.account
        broker_id = getattr(broker, "broker_id", "")
        try:
            return [_row_from_dict(o, account, broker_id) for o in reversed(broker.orders() or [])]
        except Exception as e:
            from backend.shared.helpers.recovery import already_logged
            if not already_logged(e):
                logger.error(f"Orders list failed for {account}: {e}")
            return []

    results: list[list[OrderRow]] = []
    pool = ThreadPoolExecutor(max_workers=min(len(brokers), 4))
    futs = [(pool.submit(_one_account, b), b.account) for b in brokers]
    for fut, account in futs:
        try:
            results.append(fut.result(timeout=_BROKER_ORDERS_TIMEOUT))
        except _cf.TimeoutError:
            logger.warning(
                f"orders list timed out for {account} after {_BROKER_ORDERS_TIMEOUT}s"
            )
            results.append([])
        except Exception as exc:
            logger.error(f"orders list failed for {account}: {exc}")
            results.append([])
    pool.shutdown(wait=False, cancel_futures=True)

    rows: list[OrderRow] = [row for chunk in results for row in chunk]
    return OrdersResponse(rows=rows, refreshed_at=timestamp_display())


# ── Canonical single-order-view accessor (2026-10-23) ──────────────────
#
# Research (read-only, same session) found THREE separate, divergent
# reconcile implementations already in this codebase disagreeing on
# safety behavior: `_task_open_order_watchdog` (background.py) and the
# two admin `/algo/reconcile` routes (orders.py) unconditionally flip a
# row to UNFILLED when the broker response is missing/empty (risky on a
# transient API glitch); `orders.py:list_active_chases`'s own inline
# reconcile correctly treats that as "unavailable, don't guess" and is
# also the only one of the three that writes FIFO ledger entries.
#
# `get_broker_order_truth` + `apply_order_truth` below are the single
# canonical "what is this order's real broker-side state right now"
# accessor, built around the SAFE semantics. Chase's own item-8
# auto-reconcile (chase.py — the periodic in-active-chase check and the
# post-ambiguous-abort backstop) is the first caller. Migrating
# `_task_open_order_watchdog` and the two admin reconcile routes onto
# this accessor is explicitly OUT of scope here — tracked as separate
# follow-up work (see the "Order poll" plan) — but this function is
# written generally enough (broker_order_id in, resolved status out,
# safe-by-construction on a missing response) that doing so later is a
# thin wrapper, not a rewrite.
#
# Deliberately does NOT import anything from orders.py (this module's
# own docstring: "No imports from orders.py — must be importable
# stand-alone") — `_broker_order_snapshot` below is a small, intentional
# duplicate of `orders.py:_chase_snapshot_broker_status_by_id`'s body
# (built on this file's own `_fetch_orders`), not an import of it.
# Reconciling the two into one is also follow-up work, not this change.

import dataclasses as _dc

_ORDER_TRUTH_LIVE_TERMINAL = frozenset({"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"})
_ORDER_TRUTH_KITE_TO_ALGO = {
    "COMPLETE":  "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED":  "REJECTED",
    "EXPIRED":   "UNFILLED",
}


@_dc.dataclass(frozen=True)
class OrderTruth:
    """Result of the canonical single-order-view accessor.

    `available=False` means the broker response was missing, empty, or
    the id itself was falsy — callers MUST treat this as "unavailable,
    don't guess" and leave the row untouched. This is the exact safety
    property the existing watchdog/admin-reconcile paths lack today
    (they coerce this case to a guessed UNFILLED).

    `algo_status` is the broker's status mapped onto AlgoOrder.status
    vocabulary (FILLED/CANCELLED/REJECTED/UNFILLED) ONLY when the
    broker's own status is genuinely terminal; `None` when the order is
    still resting (OPEN/TRIGGER_PENDING at the broker) — a real, usable
    read, just not yet a terminal answer.
    """
    available: bool
    algo_status: "str | None" = None
    average_price: "float | None" = None
    raw_broker_status: "str | None" = None


async def _broker_order_snapshot() -> dict[str, dict]:
    """Broker-wide order-book snapshot keyed by broker_order_id, cached
    15s via the shared `get_or_fetch("orders", ...)` cache so a sweep or
    a chase's periodic check costs at most one broker round-trip per
    account per TTL window, shared with every other `/api/orders` reader.
    Mirrors `orders.py:_chase_snapshot_broker_status_by_id`'s body
    exactly (see the module-level note above for why this isn't a direct
    import). On any failure, returns `{}` — callers see every id as
    unavailable, never a guessed status.
    """
    out: dict[str, dict] = {}
    try:
        from backend.api.cache import get_or_fetch
        from backend.brokers.adapters.kite import ensure_lot_index
        await ensure_lot_index()
        _ord_resp = await asyncio.wait_for(
            get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL),
            timeout=10.0,
        )
        for _o in (_ord_resp.rows or []):
            _bid = str(getattr(_o, "order_id", "") or "")
            if _bid:
                out[_bid] = {
                    "status": str(getattr(_o, "status", "") or "").upper(),
                    "average_price": float(getattr(_o, "average_price", 0) or 0),
                }
    except Exception as _e:
        logger.debug(f"_broker_order_snapshot failed (treated as unavailable): {_e}")
    return out


async def get_broker_order_truth(
    broker_order_id: "str | None",
    broker_status_by_id: "dict | None" = None,
) -> "OrderTruth":
    """Canonical single-order-view read. SAFE by construction: a falsy
    `broker_order_id`, or one absent from the snapshot, returns
    `OrderTruth(available=False)` — never a guessed terminal status.

    `broker_status_by_id` lets a caller scanning MANY rows (a future
    sweep) pass in one already-fetched shared snapshot instead of
    refetching per row; omitted single-row callers (chase.py) fetch a
    fresh (cache-backed) one.
    """
    if not broker_order_id:
        return OrderTruth(available=False)
    if broker_status_by_id is None:
        broker_status_by_id = await _broker_order_snapshot()
    bo = broker_status_by_id.get(str(broker_order_id))
    if not bo or not bo.get("status"):
        return OrderTruth(available=False)
    raw = str(bo["status"]).upper()
    if raw not in _ORDER_TRUTH_LIVE_TERMINAL:
        return OrderTruth(available=True, algo_status=None, raw_broker_status=raw)
    avg = bo.get("average_price")
    return OrderTruth(
        available=True,
        algo_status=_ORDER_TRUTH_KITE_TO_ALGO[raw],
        average_price=float(avg) if avg else None,
        raw_broker_status=raw,
    )


async def apply_order_truth(algo_order_id: "int | None", truth: "OrderTruth") -> bool:
    """Lock the AlgoOrder row (`with_for_update()`, matching the
    existing final-status-guard pattern in chase.py/orders_postback.py)
    and write `truth.algo_status` when it's resolved (terminal) AND
    genuinely differs from the row's current status. Writes the FIFO
    ledger entry via `_pb_write_ledger_fills` on a FILLED resolution —
    so every caller of this function gets ledger correctness, not just
    chase's own path.

    No-op (returns `False`) when: `algo_order_id` is None,
    `truth.available` is False, `truth.algo_status` is None (broker
    order isn't terminal yet), the row isn't found, the row's status
    already matches, or the row is already in
    `ALGO_ORDER_FINAL_STATUSES` (never overwrite an already-final row —
    the same invariant every other writer in this codebase enforces).
    This is the SAFE "don't guess" behavior this function exists to
    guarantee for every caller.

    Known scope gap (deliberate, see the module-level note above): does
    NOT fire template-attach on a reconciled FILLED — only chase's own
    normal fill path and `orders.py:list_active_chases`'s existing
    reconcile sweep do that today. Flagged, not silently dropped.
    """
    if algo_order_id is None or not truth.available or truth.algo_status is None:
        return False
    from sqlalchemy import select as _at_select
    from backend.api.database import async_session as _at_async_session
    from backend.api.models import AlgoOrder as _AtAlgoOrder, ALGO_ORDER_FINAL_STATUSES
    async with _at_async_session() as _s:
        row = (await _s.execute(
            _at_select(_AtAlgoOrder).where(_AtAlgoOrder.id == int(algo_order_id))
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            return False
        if row.status == truth.algo_status:
            return False
        if row.status in ALGO_ORDER_FINAL_STATUSES:
            logger.warning(
                "[ORDER-TRUTH] refusing to move AlgoOrder #%s from final "
                "status %s to %s — late/racing reconcile, ignoring.",
                algo_order_id, row.status, truth.algo_status,
            )
            return False
        row.status = truth.algo_status
        if truth.algo_status == "FILLED":
            if truth.average_price:
                row.fill_price = float(truth.average_price)
            row.filled_quantity = int(row.quantity or 0)
            from datetime import datetime as _dt, timezone as _tz
            row.filled_at = _dt.now(_tz.utc)
        await _s.commit()
        if truth.algo_status == "FILLED":
            from backend.api.routes.orders_postback import _pb_write_ledger_fills
            await _pb_write_ledger_fills(_s, [row])
            await _s.commit()
    return True


# ── AlgoOrder response structs ────────────────────────────────────────────────

class AlgoOrderEventInfo(msgspec.Struct):
    """One row from the per-order timeline."""
    id: int
    order_id: int
    ts: str          # ISO-8601 UTC timestamp
    kind: str
    message: str
    payload_json: str | None


class AlgoOrderInfo(msgspec.Struct, kw_only=True):
    """Shape exposed to the frontend Order-log tab. Thin wrapper over the
    AlgoOrder row — adds a display-ready price string would be nice but
    the frontend formats it for locale anyway.

    Hotfix 2026-06-20 — `kw_only=True` because the field order interleaves
    required fields (`attempts`, `status`, `engine`, `mode`, `detail`,
    `created_at`) after optional ones (`current_limit`, `fill_price`).
    msgspec ≥ a recent version refuses to load this without kw_only. Every
    construction site passes by keyword anyway, so kw_only is the lowest-
    risk fix — no reordering needed."""
    id: int
    account: str
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    initial_price: float | None
    # Audit fix (M-6) — current re-quoted limit; the chase loop
    # updates this on every cancel-and-replace via _sync_algo_order_id.
    # The chase panel renders this in place of initial_price when set,
    # so the limit column reflects the LIVE broker limit instead of
    # the first attempt's price.
    current_limit: float | None = None
    fill_price: float | None = None
    # Current chase attempt number (1-based: 1 = placed once, not yet
    # re-quoted). Written live on every cancel-and-replace placement by
    # `_sync_algo_order_id` (see L659/L702 above), finalized by
    # `_chase_apply_terminal_mutation` on fill/unfilled/cancel/reject —
    # so the Order tab can show "chase #3" as it's happening, not just
    # after the chase reaches a terminal state.
    attempts: int
    status: str
    engine: str
    mode: str
    detail: str | None
    created_at: str
    # Basket / TP metadata — None on legacy rows.
    target_pct: float | None = None
    target_abs: float | None = None
    parent_order_id: int | None = None
    basket_tag: str | None = None
    # Phase 2 — surface template attachment so the chase panel +
    # /orders log can show a "Tmpl ✓" chip on rows that picked up
    # auto-brackets at submit. attached_gtts_json is the post-fill
    # JSON list of {kind, label, id} dicts persisted by
    # _fire_template_attach_on_fill. Both null on legacy rows or on
    # rows that picked the 'none' template.
    template_id: int | None = None
    attached_gtts_json: str | None = None
    # Hold state from AlgoOrder.hold_json (see order_hold.hold_record); None when not held.
    hold_json: str | None = None
    # Sprint B — broker's running cumulative filled quantity. Lets the
    # frontend show partial-fill progress on OPEN/CANCELLED rows without
    # relying on the detail string for parsing.
    filled_quantity: int | None = None
    # Reverse linkage — every AlgoOrder row that points to THIS row via
    # parent_order_id. Lets the OrderCard render a "wing: #N" chip on
    # the parent so the operator sees the auto-attached protective leg
    # without having to scroll through the activity log. Empty for
    # standalone orders; one or more ids when a template's wing
    # (or any future child mechanism) attached on fill.
    child_order_ids: list[int] = []
    # Chase timing fields — populated from the DB columns written by
    # _sync_algo_order_id on every cancel-and-replace. None on legacy
    # rows, non-chased orders, or rows predating the migration.
    # interval_seconds: the configured cadence between re-quotes.
    # last_attempt_at / next_attempt_at: Unix epoch seconds; the UI
    # can compute a countdown as (next_attempt_at - Date.now()/1000).
    interval_seconds: Optional[int] = None
    next_attempt_at: Optional[float] = None
    last_attempt_at: Optional[float] = None
    # Sprint 2a (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 2,
    # §2) — broker_order_id was already a real AlgoOrder column but never
    # surfaced here, so the frontend had no shared identity key to dedup
    # a broker-fetched order row against its algo-tracked counterpart
    # (OrderBook.svelte's dedup and ChaseCard's own dedup both compared
    # against this field assuming it existed). source/agent_id are also
    # real AlgoOrder columns (Sprint 1a/1b-i) not yet surfaced — needed by
    # an upcoming "Origin" chip fix.
    broker_order_id: Optional[str] = None
    source: Optional[str] = None
    agent_id: Optional[int] = None
    # 2026-10-23 chase robustness (fix #6) — every broker_order_id this
    # row has superseded via cancel-and-replace, oldest-first. Populated
    # from AlgoOrder.superseded_broker_order_ids_json (parsed JSON list,
    # `[]` default on null/empty/parse failure). Authoritative fix for
    # the frontend's duplicate-open-order-card race — OrderBook.svelte
    # already feature-detects this exact field name/shape
    # (`_backendSupersededShipped`).
    superseded_broker_order_ids: list[str] = []


async def _fetch_child_order_ids(session, parent_ids: list[int]) -> dict[int, list[int]]:
    """One round-trip lookup of child rows for the given parents. Returns
    {parent_id: [child_id, ...]}. Empty dict when parent_ids is empty so
    callers can early-return."""
    if not parent_ids:
        return {}
    from sqlalchemy import select as _sql_select
    from backend.api.models import AlgoOrder as _AlgoOrder
    children = (await session.execute(
        _sql_select(_AlgoOrder.id, _AlgoOrder.parent_order_id)
        .where(_AlgoOrder.parent_order_id.in_(parent_ids))
    )).all()
    out: dict[int, list[int]] = {}
    for child_id, parent_id in children:
        out.setdefault(int(parent_id), []).append(int(child_id))
    return out


# ── Target-pct / overrides helpers ────────────────────────────────────────────

def _resolve_target_pct(override: float | None) -> float:
    """Return the effective TP fraction for a new order.

    A take-profit is placed only when the request sets one. With no target in
    the request, no take-profit is placed: an implicit default target would
    create an unexpected exit order on every fill. A negative value is clamped
    to 0 (disabled).
    """
    if override is None:
        return 0.0
    return max(0.0, float(override))


def _ticket_overrides_dict(data) -> dict:
    """Pack the override fields from a TicketOrderRequest /
    TicketPreviewRequest into the dict shape apply_template_to_order
    expects.

    Includes the legacy `target_pct` → `tp_pct` shim:
      - target_pct is fractional (0.30 = +30%) for v1 callers
      - tp_pct on the override dict is % units (30.0 = +30%) to match
        templates_seed / OrderTemplate columns
      - When both are set, the explicit tp_pct_override wins
    """
    overrides: dict = {
        "tp_pct":             data.tp_pct_override,
        "sl_pct":             data.sl_pct_override,
        "wing_premium_pct":   data.wing_premium_pct_override,
        "wing_strike_offset": data.wing_strike_offset_override,
        # Chain-tab pre-submission spread gate (backend.api.algo.
        # spread_check.resolve_max_spread_pct reads this key). Not a
        # resolve_template_plan field — getattr-guarded since not every
        # caller of this helper's request struct carries it.
        "wing_max_spread_pct": getattr(data, "wing_max_spread_pct_override", None),
        # TP leg order type override (LIMIT | MARKET) — see
        # TicketOrderRequest.tp_order_type_override. getattr-guarded for
        # the same reason as wing_max_spread_pct above.
        "tp_order_type":       getattr(data, "tp_order_type_override", None),
    }
    if overrides["tp_pct"] is None and getattr(data, "target_pct", None) is not None:
        try:
            overrides["tp_pct"] = float(data.target_pct) * 100.0
        except (TypeError, ValueError):
            pass
    return overrides


def _build_overrides_json(leg) -> str | None:
    """Serialize per-leg template parameter overrides into a JSON string
    for persistence on AlgoOrder.template_overrides_json. Returns None
    when no overrides were supplied (caller leaves the DB column null).

    Mirrors the override keys `apply_template_to_order` expects in its
    `overrides` dict so the postback handler can pass the parsed JSON
    straight through.
    """
    payload = {}
    # Audit fix — also serialize sl_trail_pct + tp_scales_json so
    # operator overrides of those fields persist through the postback
    # handler's override-replay path (the attach pipeline already
    # honors both fields when present in the overrides dict; the
    # serializer just wasn't carrying them).
    for src_key, dst_key in (
        ("tp_pct_override",             "tp_pct"),
        ("sl_pct_override",             "sl_pct"),
        ("wing_premium_pct_override",   "wing_premium_pct"),
        ("wing_strike_offset_override", "wing_strike_offset"),
        ("wing_max_spread_pct_override", "wing_max_spread_pct"),
        ("sl_trail_pct_override",       "sl_trail_pct"),
        ("tp_scales_json_override",     "tp_scales_json"),
        ("tp_order_type_override",      "tp_order_type"),
    ):
        v = getattr(leg, src_key, None)
        if v is not None:
            payload[dst_key] = v
    if not payload:
        return None
    # Guard (tp_order_type_override gap) — tp_order_type only ever
    # MODIFIES an existing TP leg (see template_attach._parse_template_
    # overrides), it never CREATES one; it's deliberately excluded from
    # `template_attach.has_any_override`'s key set for that reason. A
    # ticket that combines the legacy v1 `target_pct` field with ONLY
    # tp_order_type_override (no tp_pct_override) would otherwise
    # persist a non-empty `template_overrides_json` here — which (a)
    # suppresses the legacy single-TP shim (`_pb_wants_take_profit_arm`
    # / `_ch_maybe_fire_auto_tp` treat any non-empty override JSON as
    # "ad-hoc template present") and (b) `has_any_override` still
    # returns False for a tp_order_type-only dict, so the ad-hoc path
    # also builds nothing — net result: the take-profit silently
    # vanishes instead of arming at the legacy target_pct. Treat a
    # tp_order_type-only payload as "no overrides" so the legacy shim
    # keeps firing exactly as before this field existed.
    if set(payload) == {"tp_order_type"}:
        return None
    import json as _json
    return _json.dumps(payload)
