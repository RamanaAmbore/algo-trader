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
    elif a == "med":
        cfg = ChaseConfig(interval_seconds=20, aggression_step=0.10,
                          max_attempts=20)
    else:
        # low (default) — patient: peg passively, ease into the
        # spread only after enough ticks pass.
        cfg = ChaseConfig(interval_seconds=30, aggression_step=0.05,
                          max_attempts=30)
    cfg.intent = intent
    cfg.product = product
    cfg.variety = variety
    cfg.validity = validity
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
            # Initial placement already succeeded (future resolved). Alert on
            # terminal chase failures so the operator knows the fill didn't land.
            if evt in ("error", "chase_failed"):
                _err = detail.get("error") or f"chase {evt}"
                logger.warning(
                    "[CHASE-POST-PLACEMENT] %s algo_order_id=%s %s %s "
                    "acct=%s qty=%s: %s",
                    evt, algo_order_id, symbol, transaction_type,
                    account, quantity, _err,
                )
                try:
                    from backend.shared.helpers.alert_utils import send_order_failure_alert
                    send_order_failure_alert(
                        account=account, symbol=symbol, exchange=exchange,
                        side=transaction_type, qty=quantity, mode="live",
                        source="chase:post-placement", error=_err,
                    )
                except Exception:
                    pass

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
    # How many times the chase engine re-quoted this order before a
    # terminal state. Bumped live on every `modify` event so the
    # Order tab can show "chase #3" as it's happening, not just
    # after fill/unfilled.
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

    Priority:
      1. explicit `override` from the request (including 0.0 to disable TP)
      2. `algo.default_target_pct` DB setting
      3. hard-coded fallback of 0.30
    A negative value is clamped to 0 (disabled).
    """
    if override is not None:
        return max(0.0, float(override))
    from backend.shared.helpers.settings import get_float
    return max(0.0, get_float("algo.default_target_pct", 0.30))


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
        ("sl_trail_pct_override",       "sl_trail_pct"),
        ("tp_scales_json_override",     "tp_scales_json"),
    ):
        v = getattr(leg, src_key, None)
        if v is not None:
            payload[dst_key] = v
    if not payload:
        return None
    import json as _json
    return _json.dumps(payload)
