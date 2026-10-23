"""
Adaptive limit-order chase engine.

Market orders are not allowed for most options. This module places LIMIT orders
and progressively adjusts the price using market depth until filled.

Reusable: called by expiry engine, interpreter buy/sell, or any future strategy.

Usage:
    result = await chase_order(account, symbol, 'SELL', 50, exchange='NFO')
    # result: ChaseResult(order_id, fill_price, attempts, slippage, status)
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from sqlalchemy import select as _sql_select

from backend.api.cache import _store as _cache_store
from backend.api.database import async_session as _async_session
from backend.api.models import AlgoOrder as _AlgoOrder
from backend.brokers.errors import (
    BrokerAuthError as _BrokerAuthError,
    BrokerCapabilityError as _BrokerCapabilityError,
    BrokerInputError as _BrokerInputError,
    BrokerRateLimitError as _BrokerRateLimitError,
)
from backend.brokers.registry import get_broker as _get_broker_registry
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


_OUTCOME_TO_STATUS: dict[str, str] = {
    "chase_fill":      "FILLED",
    "chase_unfilled":  "UNFILLED",
    "chase_cancelled": "CANCELLED",
    "chase_failed":    "REJECTED",
    # 2026-10-23 chase robustness (fix #1) — a cancel-unconfirmed abort or
    # a consecutive-client-side-errors abort means the broker-side fate of
    # the order is genuinely UNKNOWN (it may still be resting live). Both
    # used to route through "chase_failed" -> REJECTED, a status in
    # ALGO_ORDER_FINAL_STATUSES that permanently blocks any later real
    # fill from ever being recorded. CANCEL_FAILED already exists for
    # exactly this "might still be live" case (see
    # models.ALGO_ORDER_FINAL_STATUSES's own docstring) and is
    # deliberately excluded from that frozenset. Verified safe to
    # introduce as a brand-new outcome: grepped frontend/src for
    # chase_failed/chase_unfilled/chase_cancelled (zero matches — the
    # frontend only reads AlgoOrder.status, never the raw outcome
    # string), and LogPanel.svelte's `_agentLevel` humanizer classifies
    # any `*_failed`-shaped event_type as 'error' generically.
    "chase_cancel_failed": "CANCEL_FAILED",
}


# 2026-09-30 audit fix (Bug 1) — offsetting-position check staleness.
# `_ch_check_and_fire_template_attach` decides whether a fill "offsets"
# (closes/reduces) an existing position by re-reading the broker position
# AFTER the fill has already landed. For a FULL close this always reads
# flat/zero — indistinguishable from "no prior position at all" — so the
# post-fill read misclassifies a plain close as a brand-new open and
# incorrectly arms exit orders on it, regardless of whether the 30s
# positions cache happens to be stale or freshly refreshed at that moment
# (a fresh post-fill read is just as wrong as a stale one here — freshness
# does not fix this, it can only change WHICH wrong answer you get).
#
# Fix: capture the signed net position ONCE in `chase_order()` before the
# FIRST order of this chase is placed — a genuine pre-fill snapshot, not a
# cache-timing gamble — and thread it through to the terminal-fill
# snapshot via this module-level dict (keyed by algo_order_id, the same
# key `_emit_chase_terminal` already receives explicitly). Popped (not
# just read) in `_emit_chase_terminal` on every terminal outcome so an
# entry never lingers past the chase that created it.
_CH_PRE_FILL_NET_QTY: dict[int, float | None] = {}


async def _ch_capture_pre_fill_net_qty(
    account: str, exchange: str, symbol: str,
) -> float | None:
    """Snapshot the signed net held quantity for (account, symbol) BEFORE
    this chase places its first order.

    Forces a fresh (non-cached) broker read so a chase that starts
    moments after an unrelated earlier fill doesn't inherit a stale
    value either way. Fails open (returns None) on any error — callers
    treat a missing/None pre-fill snapshot as "not offsetting", matching
    `_is_offsetting_position`'s own existing fail-open contract.
    """
    try:
        from backend.brokers.broker_apis import _raw_cache_invalidate
        _raw_cache_invalidate("positions")
    except Exception:
        pass
    try:
        from backend.api.routes.orders_place import _fetch_net_position_qty
        return await _fetch_net_position_qty(symbol, exchange, account)
    except Exception as _e:
        logger.warning("[CHASE] pre-fill position capture failed: %s", _e)
        return None


async def _ch_seed_pre_fill_net_qty(
    algo_order_id: int | None, account: str, exchange: str, symbol: str,
) -> None:
    """Capture + store the pre-fill net position for `algo_order_id`,
    guarded no-op when there's no id to key the snapshot by (only
    templated chases, which always pass algo_order_id, can reach the
    downstream offsetting check). Extracted so `chase_order()` calls this
    unconditionally as a single statement — keeps the None-guard out of
    `chase_order`'s own already-high cyclomatic complexity.
    """
    if algo_order_id is None:
        return
    _CH_PRE_FILL_NET_QTY[algo_order_id] = await _ch_capture_pre_fill_net_qty(
        account, exchange, symbol,
    )


_SENTINEL_NO_CAPTURE = object()


def _ch_apply_pre_fill_net_qty_to_snap(
    algo_order_id: int | None, row_snap: dict | None,
) -> None:
    """Pop the pre-fill net position captured at chase_order() entry (if
    any) and thread it onto `row_snap["pre_fill_net_qty"]` so
    `_ch_check_and_fire_template_attach` can use genuine pre-fill state
    instead of a post-fill broker re-read (Bug 1 fix, 2026-09-30).

    Pops unconditionally on every terminal outcome (not just chase_fill)
    so a cancelled/failed/unfilled chase never leaves a stale entry in
    `_CH_PRE_FILL_NET_QTY`. No-op when there's nothing to pop or nowhere
    to put it — extracted so `_emit_chase_terminal` calls this as a
    single unconditional statement.
    """
    if algo_order_id is None:
        return
    pre_fill_net_qty = _CH_PRE_FILL_NET_QTY.pop(algo_order_id, _SENTINEL_NO_CAPTURE)
    if pre_fill_net_qty is _SENTINEL_NO_CAPTURE or row_snap is None:
        return
    row_snap["pre_fill_net_qty"] = pre_fill_net_qty


def _ch_is_offsetting_sign(net_qty: float | None, side: str) -> bool:
    """Sign-only offsetting test — mirrors
    `orders_place._is_offsetting_position`'s own logic, applied to an
    already-captured net quantity instead of a fresh broker call.
    """
    if net_qty is None:
        return False
    if side.upper() == "BUY" and net_qty < 0:
        return True   # BUY closes a SHORT
    if side.upper() == "SELL" and net_qty > 0:
        return True   # SELL closes a LONG
    return False


def _chase_apply_terminal_mutation(
    row,
    new_status: str,
    attempts: int,
    final_price: float | None,
    error: str | None,
) -> None:
    """Mutate an AlgoOrder ORM row in-place for a terminal chase outcome.

    Called inside an active SQLAlchemy session before commit. Applies status
    transition, attempts, fill_price/filled_at (FILLED only), and error detail.
    """
    row.status = new_status
    if attempts:
        try:
            row.attempts = int(attempts)
        except (TypeError, ValueError):
            pass
    if new_status == "FILLED":
        if final_price is not None:
            try:
                row.fill_price = float(final_price)
            except (TypeError, ValueError):
                pass
        row.filled_at = datetime.now(timezone.utc)
    if error:
        row.detail = (row.detail or "")[:200] + f" · {error[:120]}"


def _chase_snapshot_algo_row(row, broker_order_id: str) -> dict:
    """Snapshot all AlgoOrder fields needed by downstream attach paths.

    Called AFTER the optional mutation + commit so readers see post-commit values.
    Returns a plain dict — intentionally no ORM references.
    """
    return {
        "id":                int(row.id),
        "agent_id":          getattr(row, "agent_id", None),
        "target_pct":        row.target_pct,
        "target_abs":        row.target_abs,
        "parent_order_id":   row.parent_order_id,
        "template_id":       row.template_id,
        "template_overrides_json": getattr(row, "template_overrides_json", None),
        "account":           str(row.account or ""),
        "symbol":            str(row.symbol or broker_order_id),
        "exchange":          str(row.exchange or "NFO"),
        "transaction_type":  str(row.transaction_type or ""),
        "product":           str(row.product or "NRML"),
        "mode":              str(row.mode or "live"),
        "filled_quantity":   int(row.filled_quantity or 0),
        "quantity":          int(row.quantity or 0),
        "intent":            str(row.intent or ""),
        "is_close_intent":   bool(getattr(row, "is_close_intent", False)),
    }


async def _chase_terminal_update_db(
    algo_order_id: int | None,
    broker_order_id: str,
    outcome: str,
    attempts: int,
    final_price: float | None,
    error: str | None,
) -> tuple[int | None, dict | None]:
    """Look up AlgoOrder, mutate status/fill fields, commit, return (agent_id, row_snap).

    Lookup priority: algo_order_id (Phase 0.5 — chase passes the row
    id explicitly, immune to mid-chase broker_order_id mutation) →
    broker_order_id (legacy callers that don't yet pass the row id).

    Snapshots all fields needed by the downstream auto-TP + template
    attach paths AFTER the optional mutation + commit so subsequent
    readers see post-commit values. Returns (None, None) when no row
    is found.
    """
    _new_status = _OUTCOME_TO_STATUS.get(outcome)

    agent_id = None
    _row_snap: dict | None = None
    async with _async_session() as _s:
        # 2026-09-27 audit fix: FOR UPDATE — this path previously had no
        # locking at all, so it could race a postback delivery for the SAME
        # order (either broker firing its own terminal event concurrently
        # with chase's own poll-derived terminal outcome). See
        # orders_postback.py's identical fix for the full rationale.
        row = None
        if algo_order_id is not None:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                .with_for_update()
            )).scalar_one_or_none()
        # 2026-10-23 audit fix — only run the broker_order_id fallback
        # lookup when it's a real (non-empty) id. Several abort paths now
        # call this with `broker_order_id=""` when nothing was ever
        # placed (see the "always write a marker" fix below) — querying
        # `WHERE broker_order_id == ''` could match an unrelated row that
        # happens to carry a stored empty string, or raise
        # MultipleResultsFound if more than one does, either of which
        # would silently mutate/crash on an arbitrary row.
        if row is None and broker_order_id:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(
                    _AlgoOrder.broker_order_id == broker_order_id
                ).with_for_update()
            )).scalar_one_or_none()
        if row is not None:
            agent_id = getattr(row, "agent_id", None)
            if _new_status and row.status != _new_status:
                # 2026-09-27 audit fix: same FINAL-status guard as the
                # postback paths (models.ALGO_ORDER_FINAL_STATUSES) — a
                # row already FILLED/REJECTED must never be overwritten by
                # chase's own (possibly late/racing) terminal outcome.
                from backend.api.models import ALGO_ORDER_FINAL_STATUSES
                if row.status in ALGO_ORDER_FINAL_STATUSES:
                    logger.warning(
                        "[CHASE] refusing to move AlgoOrder #%s from final "
                        "status %s to %s — late/racing terminal update, "
                        "ignoring.", getattr(row, "id", "?"), row.status, _new_status,
                    )
                else:
                    _chase_apply_terminal_mutation(row, _new_status, attempts, final_price, error)
                    await _s.commit()
                    if _new_status == "FILLED":
                        # 2026-09-27 audit fix (finding #4): chase's own
                        # terminal-fill path never wrote FIFO ledger
                        # entries — only the Kite postback path did. A
                        # chase-filled order with a strategy_id silently
                        # never reached the strategy lot ledger.
                        from backend.api.routes.orders_postback import _pb_write_ledger_fills
                        await _pb_write_ledger_fills(_s, [row])
                        await _s.commit()
                        # Gap fix (2026-10): chase is the primary
                        # fill-detection path for Dhan/Groww (whose
                        # postback delivery is unreliable/manually
                        # configured — see CLAUDE.md) and can also race
                        # ahead of a Kite postback. Without this, a
                        # chase-filled order's WS tick subscription
                        # silently waited for the next _task_performance
                        # cycle (up to 5 min) instead of becoming
                        # symbol-addressable immediately.
                        from backend.api.routes.orders import _subscribe_filled_pairs
                        await _subscribe_filled_pairs([(row.symbol, row.exchange)])
            # Snapshot AFTER the optional mutation + commit so the
            # downstream attach paths read post-commit values.
            _row_snap = _chase_snapshot_algo_row(row, broker_order_id)
    return agent_id, _row_snap


def _ch_maybe_fire_auto_tp(snap: dict, final_price: float) -> None:
    """Fire legacy single-target TP shim when template_id IS NULL.

    `template_overrides_json` exclusion mirrors the identical Sprint 8
    fix in orders_postback._pb_wants_take_profit_arm — prevents a row
    carrying both a legacy target_pct AND a v2 override field from
    being double-armed by this shim AND the ad-hoc template-attach
    path (`_ch_maybe_fire_template_attach` below).
    """
    if not (snap.get("target_pct") or snap.get("target_abs")):
        return
    if snap.get("parent_order_id") is not None:
        return
    if snap.get("template_id") is not None:
        return
    if snap.get("template_overrides_json"):
        return
    from backend.api.routes.orders import _arm_take_profit
    # 2026-09-27 audit fix (finding #5/#1 bundle): pass the parent's real
    # product (was silently defaulting to NRML — snap["product"] is already
    # captured by _chase_snapshot_algo_row) and the actual filled quantity
    # (chase can take partials — snap["filled_quantity"]).
    asyncio.create_task(_arm_take_profit(
        parent_row_id=snap["id"],
        parent_account=snap["account"],
        parent_symbol=snap["symbol"],
        parent_exchange=snap["exchange"],
        parent_side=snap["transaction_type"],
        fill_price=float(final_price),
        target_pct=float(snap.get("target_pct") or 0.0),
        target_abs=(snap.get("target_abs") and float(snap.get("target_abs"))),
        parent_mode=snap["mode"],
        parent_product=snap["product"],
        filled_qty=snap["filled_quantity"] or None,
    ))


async def _ch_check_and_fire_template_attach(snap: dict, final_price: float) -> None:
    """Async guard + fire, run as a detached task by _ch_maybe_fire_template_attach.

    2026-09-27 audit fix (finding #6): chase's fill path used to fire
    template attach (arming exit GTTs) unconditionally once a template_id
    was present, unlike the Kite postback path
    (_pb_check_and_fire_template_attach) which runs an offsetting-position
    check first so a close/reduce order that happens to carry a stale
    template_id never arms exit GTTs on itself. Mirrors both of that
    path's guards: explicit close-intent (matching
    _opl_reconcile_attach_eligible) and the sign-only
    _is_offsetting_position check.

    2026-09-30 audit fix (Bug 1): the sign-only check used to call
    `_is_offsetting_position`, which reads the broker position AFTER this
    fill has already landed — for a full close that read is always flat/
    zero, indistinguishable from "no prior position", so it misclassified
    a plain close as a new open regardless of cache freshness. When a
    genuine PRE-fill snapshot was captured at chase_order() entry (see
    `_CH_PRE_FILL_NET_QTY` / `_ch_capture_pre_fill_net_qty`), use that
    instead via the sign-only `_ch_is_offsetting_sign` helper. Falls back
    to the legacy post-fill `_is_offsetting_position` call only when no
    pre-fill snapshot is present on `snap` (e.g. a legacy caller that
    never set `algo_order_id`), preserving prior behaviour for that edge.
    """
    if (snap.get("intent") or "").lower() == "close" or snap.get("is_close_intent"):
        return
    if "pre_fill_net_qty" in snap:
        is_offsetting = _ch_is_offsetting_sign(
            snap["pre_fill_net_qty"], snap["transaction_type"],
        )
    else:
        from backend.api.routes.orders_place import _is_offsetting_position
        is_offsetting = await _is_offsetting_position(
            sym=snap["symbol"], exchange=snap["exchange"],
            side=snap["transaction_type"], account=snap["account"],
        )
    if is_offsetting:
        logger.info(
            "[CHASE] skipping template attach — order offsets existing "
            "position for #%s %s", snap["id"], snap["symbol"],
        )
        return

    from backend.api.routes.orders import _fire_template_attach_on_fill
    # Sprint B (#4) — size exit GTTs against the ACTUAL filled qty
    # when the chase took partials.
    attach_qty = (
        snap["filled_quantity"] if snap["filled_quantity"] > 0 else snap["quantity"]
    )
    await _fire_template_attach_on_fill(
        parent_row_id=snap["id"],
        parent_account=snap["account"],
        parent_symbol=snap["symbol"],
        parent_exchange=snap["exchange"],
        parent_side=snap["transaction_type"],
        parent_qty=attach_qty,
        fill_price=float(final_price),
        template_id=(int(snap["template_id"]) if snap.get("template_id") else None),
        parent_product=snap["product"],
        # chase_order() is the real-broker cancel-and-replace engine — only
        # ever invoked for mode=='live' rows — but thread the row's own
        # mode through explicitly rather than relying on that invariant
        # implicitly, matching every other caller of this function.
        mode=str(snap["mode"]),
    )


def _ch_maybe_fire_template_attach(snap: dict, final_price: float) -> None:
    """Fire template attach task on chase fill (idempotent via GTT guard).

    Sprint 8 fix: fires when EITHER a saved template_id is set OR the
    row carries ad-hoc override fields (template_overrides_json
    non-empty) — mirrors the identical fix in orders_postback.
    _pb_wants_template_attach and paper._paper_maybe_fire_template_attach.
    Previously this required template_id truthy, so a chase-filled
    ticket submitted with only tp_pct_override/sl_pct_override/wing_*
    override fields (no saved template) never reached
    apply_template_to_order's ad-hoc path on a real fill.
    """
    if not (snap.get("template_id") or snap.get("template_overrides_json")):
        return
    if snap.get("parent_order_id") is not None:
        return
    asyncio.create_task(_ch_check_and_fire_template_attach(snap, final_price))


def _chase_terminal_fire_fill_hooks(
    row_snap: dict,
    outcome: str,
    final_price: float | None,
) -> None:
    """Fire auto-TP shim and/or template attach task on chase_fill.

    Only runs when outcome == 'chase_fill' and a filled row_snap is
    available. Both hooks are gated on parent_order_id IS NULL to
    prevent TP-of-TP chains. This function is synchronous — it only
    calls asyncio.create_task(); no awaits.
    """
    if outcome != "chase_fill" or not final_price or row_snap is None:
        return
    try:
        _ch_maybe_fire_auto_tp(row_snap, final_price)
        _ch_maybe_fire_template_attach(row_snap, final_price)
    except Exception as _tp_e:
        logger.warning(f"_emit_chase_terminal TP arm failed: {_tp_e}", exc_info=True)


async def _emit_chase_terminal(
    broker_order_id: str,
    outcome: str,          # chase_fill | chase_unfilled | chase_failed | chase_cancelled
    symbol: str,
    side: str,
    qty: int,
    *,
    final_price: float | None = None,
    attempts: int = 0,
    slippage: float | None = None,
    error: str | None = None,
    algo_order_id: int | None = None,
) -> None:
    """Look up the AlgoOrder row + update its terminal status, write a
    chase-terminal AgentEvent, and on chase_fill fire any attached
    template's GTT + wing. Fire-and-forget.

    Lookup priority: algo_order_id (Phase 0.5 — chase passes the row
    id explicitly, immune to mid-chase broker_order_id mutation) →
    broker_order_id (legacy callers that don't yet pass the row id).
    Without the algo_order_id path, chased orders silently failed to
    flip to FILLED because their AlgoOrder row stayed pinned to the
    FIRST broker_order_id while _emit_chase_terminal was called with
    the LATEST one.
    """
    try:
        from backend.api.algo.agent_engine import record_chase_terminal  # circular: lazy OK

        # Audit fix — snapshot the fields the downstream Auto-TP + template
        # attach paths need BEFORE commit so we don't have to re-open a
        # second session to re-fetch the same row. Halves the DB
        # round-trips on every chase fill event.
        agent_id, _row_snap = await _chase_terminal_update_db(
            algo_order_id, broker_order_id, outcome, attempts, final_price, error
        )

        # Bug 1 fix (2026-09-30) — thread the pre-fill net position
        # captured at chase_order() entry onto the row snapshot.
        _ch_apply_pre_fill_net_qty_to_snap(algo_order_id, _row_snap)

        await record_chase_terminal(
            agent_id=agent_id,
            outcome=outcome,
            symbol=symbol,
            side=side,
            qty=qty,
            final_price=final_price,
            attempts=attempts,
            slippage=slippage,
            error=error,
        )

        # Auto TP + Template attach — fire after a chase fill on a
        # parent row (parent_order_id IS NULL so we never create
        # TP-of-TP chains).
        _chase_terminal_fire_fill_hooks(_row_snap, outcome, final_price)

    except Exception as _e:
        logger.warning(
            f"_emit_chase_terminal failed — template attach or DB update may not have fired: {_e}",
            exc_info=True,
        )

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chase")

# Maximum consecutive broker errors before a chase loop is aborted and an
# alert is fired.  Distinct from `max_attempts` (the unfilled cap): this
# counts Kite SDK exceptions, not quote-crossing retries.
# 3 consecutive broker errors abort the chase before Kite's rate-limit
# cool-off (30s per _RATE_LIMIT_COOLOFF_SECONDS in registry.py) would
# extend the retry window indefinitely against a broken session.
_MAX_CHASE_ERRORS = 3

# 2026-10 fix — "if any order fails, identify it as recoverable or not;
# if recoverable, retry (with a changed price)". Typed `BrokerError`
# subclasses (backend/brokers/errors.py) are the real signal: bad input,
# an unsupported capability, or an invalid session fail IDENTICALLY on
# every retry, so retrying just burns attempts and risks tripping the
# broker's own rate limit. Network blips and rate-limit responses ARE
# worth retrying — the chase loop re-reads depth and computes a fresh
# price every iteration, so a retry after one of these is never a blind
# resend of the same price/order.
#
# Type alone is not sufficient, though — `_ch_is_recoverable_error` also
# checks the exception's own message against `_CH_NON_RECOVERABLE_REJECTION_HINTS`
# (margin/rms/risk/permission/blocked) BEFORE this type check, so even an
# untyped exception (or a typed-but-not-listed-here one, e.g. a
# BrokerOrderError whose message says "margin shortfall") is refused a
# retry — matching the same operator instruction `_ch_rejection_is_recoverable`
# already enforces for broker status-poll REJECTED messages.
_CH_NON_RECOVERABLE_ERRORS = (_BrokerInputError, _BrokerCapabilityError, _BrokerAuthError)

# Extra backoff when the broker itself says "slow down" — longer than a
# normal re-quote interval so a burst of rate-limited attempts doesn't
# just re-trip the same cooldown (mirrors registry.py's own
# _RATE_LIMIT_COOLOFF_SECONDS convention for the connection layer).
_CH_RATE_LIMIT_BACKOFF_SECONDS = 30

# REJECTED broker statuses whose message text names a PRICE-shaped cause
# (out of circuit/price band, stale quote, tick violation) — exactly the
# class of failure chase exists to fix by re-pricing on the next
# attempt.
_CH_RECOVERABLE_REJECTION_HINTS = ("price", "circuit", "tick", "range", "stale", "band")

# Explicit non-recoverable override, checked BEFORE the hint list above —
# operator instruction (2026-10): "margin errors are not recoverable".
# A margin/RMS/risk-control rejection means the SAME retry (or any
# repriced variant of it) would fail identically — the account's
# available margin doesn't change because the limit price moves — so it
# must never match a recoverable hint even if its message also happens
# to mention "price" or "range" (e.g. "margin shortfall for this price
# range"). Checked first so it always wins over the recoverable list.
_CH_NON_RECOVERABLE_REJECTION_HINTS = ("margin", "rms", "risk", "permission", "blocked")


def _ch_is_recoverable_error(exc: Exception) -> bool:
    """Classify a chase-loop exception as recoverable (worth a fresh,
    repriced retry) or not.

    Checked in two stages:
    1. Message-hint override — operator instruction (2026-10): "margin
       errors are not recoverable". If `str(exc)` matches any of
       `_CH_NON_RECOVERABLE_REJECTION_HINTS` (margin/rms/risk/permission/
       blocked), the exception is non-recoverable regardless of its type —
       even a plain untyped `Exception("margin shortfall")` must be
       refused, mirroring `_ch_rejection_is_recoverable`'s handling of the
       same wording on a broker status-poll rejection.
    2. Typed classification — otherwise, `isinstance(exc, _CH_NON_RECOVERABLE_ERRORS)`
       decides. Untyped exceptions (a plain Exception, or an SDK error we
       haven't wrapped in the typed hierarchy yet) whose message doesn't
       match stage 1 default to recoverable — the same conservative
       fail-safe default used elsewhere in this module (see
       `_ch_mcx_lots_broker`'s docstring): treating an unknown error as
       non-recoverable would silently abandon chases on errors we simply
       haven't classified, which is worse than a bounded retry (capped by
       `_MAX_CHASE_ERRORS` regardless of classification either way).
    """
    msg = str(exc).lower()
    if any(h in msg for h in _CH_NON_RECOVERABLE_REJECTION_HINTS):
        return False
    return not isinstance(exc, _CH_NON_RECOVERABLE_ERRORS)


def _ch_rejection_is_recoverable(status_message: str) -> bool:
    """True when a broker REJECTED status looks price-related — worth
    one more repriced attempt rather than aborting the whole chase.

    Margin/RMS/risk-control/permission rejections are NEVER recoverable
    (operator instruction, 2026-10) regardless of what else the message
    says — checked before the price-hint list.
    """
    msg = (status_message or "").lower()
    if any(h in msg for h in _CH_NON_RECOVERABLE_REJECTION_HINTS):
        return False
    return any(h in msg for h in _CH_RECOVERABLE_REJECTION_HINTS)


# ── Operator kill signal ─────────────────────────────────────────────
# When the operator clicks Kill in the chase panel, the kill_chase
# route cancels the live broker order AND adds its broker_order_id
# here. The chase loop checks this set before placing each new
# attempt — if the *previous* attempt's broker_order_id is killed,
# the loop terminates instead of re-placing. Without this signal the
# loop's CANCELLED-status handler treated the operator kill the same
# way it treats a broker auto-cancel (circuit/session-end) and
# silently placed a new order — operator saw the row "reappear" on
# the next poll.
#
# Sprint B (audit #10): switched from `set[str]` to `dict[str, float]`
# (broker_order_id → expire_epoch) so stale entries self-prune. A
# previously-killed order ages out 60 minutes after the kill, which
# is well past the longest realistic chase lifecycle (max_attempts ×
# interval_seconds for the slowest config = 30 × 30s = 15 min). The
# lazy sweep happens inside `is_killed()` — every read drops every
# entry past its expiry — so the dict stays bounded without a
# dedicated background task.
import time as _time
# 1 h — operator session length cap; a killed order that "comes back"
# after 60 min was almost certainly a fresh placement on a new session,
# not a resurrection of the killed one. Prevents stale kill flags from
# surviving across trading days.
_KILLED_TTL_SECONDS = 3600
_KILLED_ORDER_IDS: dict[str, float] = {}
_KILLED_LOCK = __import__("threading").Lock()


def mark_killed(broker_order_id: str) -> None:
    """Signal the chase loop that this broker_order_id was killed by
    the operator. The loop checks `is_killed()` after every status
    poll and terminates instead of placing a fresh order. Idempotent;
    multiple kills (same id) are fine."""
    if not broker_order_id:
        return
    with _KILLED_LOCK:
        _KILLED_ORDER_IDS[str(broker_order_id)] = _time.monotonic() + _KILLED_TTL_SECONDS


def is_killed(broker_order_id: str) -> bool:
    if not broker_order_id:
        return False
    with _KILLED_LOCK:
        now = _time.monotonic()
        # Lazy sweep — drop every expired entry on read so the dict
        # stays bounded without a background task. Iterating in a list
        # snapshot so we can mutate during traversal.
        for k, expires in list(_KILLED_ORDER_IDS.items()):
            if expires <= now:
                _KILLED_ORDER_IDS.pop(k, None)
        return str(broker_order_id) in _KILLED_ORDER_IDS


# 2026-10-23 Kill-lifecycle fix — `_KILLED_ORDER_IDS` above is keyed by
# broker_order_id, which chase's own cancel-and-replace cycle REPLACES
# every attempt. If the operator's Kill click reads the AlgoOrder row's
# broker_order_id at t0 and chase swaps in a brand-new broker_order_id
# at t0+ε (a replacement already in flight), `mark_killed(old_id)` never
# matches the new id — chase keeps chasing, completely unaware an
# operator tried to stop it, and the operator sees a confusing
# "KILL FAILED" (the broker genuinely can't cancel an id that's already
# been cancelled-and-replaced) while live orders keep being placed
# underneath. This second, ALGO-ORDER-id-keyed registry survives a
# broker_order_id swap: the chase loop checks `is_order_killed(row.id)`
# (not just `is_killed(current_order_id)`) at the top of every attempt
# iteration (`_ch_loop_guards`) AND immediately after every replacement
# (`_ch_post_replace_kill_check`), so the operator's kill intent is
# honored no matter which broker_order_id is currently in flight. Same
# TTL/lazy-sweep shape as `_KILLED_ORDER_IDS` above — see that registry's
# own docstring for the full reasoning.
_KILLED_ALGO_ORDER_IDS: dict[int, float] = {}


def mark_order_killed(algo_order_id: "int | None") -> None:
    """Signal the chase loop that the WHOLE chase for this AlgoOrder id
    was killed by the operator — survives a cancel-and-replace cycle
    that swaps the broker_order_id out from under a stale
    `mark_killed(broker_order_id)` call. No-op for `None`. Idempotent."""
    if algo_order_id is None:
        return
    with _KILLED_LOCK:
        _KILLED_ALGO_ORDER_IDS[int(algo_order_id)] = _time.monotonic() + _KILLED_TTL_SECONDS


def is_order_killed(algo_order_id: "int | None") -> bool:
    if algo_order_id is None:
        return False
    with _KILLED_LOCK:
        now = _time.monotonic()
        for k, expires in list(_KILLED_ALGO_ORDER_IDS.items()):
            if expires <= now:
                _KILLED_ALGO_ORDER_IDS.pop(k, None)
        return int(algo_order_id) in _KILLED_ALGO_ORDER_IDS


# ── In-process "is this row currently being chased" signal ─────────────
# 2026-10 fix — AlgoOrder.status conflates two different questions: (a)
# what is the live status of the CURRENTLY-resting broker order (which a
# postback correctly and immediately reflects), and (b) has the overall
# chase INTENT given up (which only the chase loop itself can decide,
# since only it knows whether it plans to retry). A postback has no way
# to predict chase's future retry decision — and shouldn't have to.
#
# Confirmed operator timing fact: Kite's rejection postback for an order
# arrives within a few seconds of the rejection — reliably, not a rare
# race. `_ch_poll_handle_rejected`'s own price-shaped-rejection retry
# ("rejected_continue") almost always loses that race against the
# postback, so a postback handler must withhold REJECTED/CANCELLED/
# UNFILLED status writes for a row it can see is still under an active
# chase retry loop — only chase's own terminal handler
# (`_chase_terminal_update_db`) may finalize such a row. A genuine
# FILLED/COMPLETE is never withheld — a fill is never ambiguous.
#
# Keyed by `dict[int, int]` (refcount), not a `set`, mirroring the
# reasoning but NOT the shape of `_KILLED_ORDER_IDS` above: a manual
# `/held/{id}/release` re-chase and a service-restart recovery chase can
# both legitimately target the SAME algo_order_id in overlapping windows
# (rare, but possible), and a plain set would let the first one to exit
# clear the marker out from under the other still-running one.
_ACTIVE_CHASE_REFCOUNT: dict[int, int] = {}
_ACTIVE_CHASE_LOCK = __import__("threading").Lock()


def _ch_mark_chase_active(algo_order_id: int | None) -> None:
    """Increment the active-chase refcount for `algo_order_id`. No-op
    for None (untracked/legacy callers — `is_chase_active(None)` always
    reports False anyway)."""
    if algo_order_id is None:
        return
    with _ACTIVE_CHASE_LOCK:
        _ACTIVE_CHASE_REFCOUNT[int(algo_order_id)] = (
            _ACTIVE_CHASE_REFCOUNT.get(int(algo_order_id), 0) + 1
        )


def _ch_mark_chase_inactive(algo_order_id: int | None) -> None:
    """Decrement (and prune at zero) the active-chase refcount for
    `algo_order_id`. Always called from the `chase_order()` wrapper's
    `finally` block, so it runs on every return/exception path."""
    if algo_order_id is None:
        return
    with _ACTIVE_CHASE_LOCK:
        key = int(algo_order_id)
        if key not in _ACTIVE_CHASE_REFCOUNT:
            return
        _ACTIVE_CHASE_REFCOUNT[key] -= 1
        if _ACTIVE_CHASE_REFCOUNT[key] <= 0:
            _ACTIVE_CHASE_REFCOUNT.pop(key, None)


def is_chase_active(algo_order_id: "int | None") -> bool:
    """True when `algo_order_id` is CURRENTLY inside an active
    `chase_order()` retry loop in THIS process.

    Used by the postback handlers (`orders_postback.py`) and the
    `/chases/active` reconcile sweep (`orders.py`) to withhold a
    non-FILLED terminal status write for a row chase may still retry.
    Also used directly by `backend/api/algo/order_poll.py:
    reconcile_row_and_followup` (2026-10-23) as a full no-op gate —
    including for a FILLED resolution, which the non-FILLED-only
    withhold above deliberately does NOT cover — so a background
    corrector (the open-order watchdog, the periodic order-poll sweep,
    postback's follow-up verification) can never race ahead of chase's
    own fill detection and fire a false action-required alert for a
    perfectly normal, self-resolving chase fill.
    """
    if algo_order_id is None:
        return False
    with _ACTIVE_CHASE_LOCK:
        return int(algo_order_id) in _ACTIVE_CHASE_REFCOUNT


# ── Item 8b: immediate backstop reconcile after an ambiguous abort ──────
# 2026-10-23 chase robustness. An "ambiguous" abort (cancel-unconfirmed,
# consecutive-errors, stuck, place-timeout — every path that writes
# CANCEL_FAILED because the broker-side fate of the order genuinely
# couldn't be confirmed) gets ONE immediate follow-up reconcile attempt
# right after `chase_order()`'s wrapper clears the active-chase marker,
# instead of relying solely on a later periodic sweep or a manual operator
# reconcile trigger. Mirrors `_ACTIVE_CHASE_REFCOUNT`'s plain dict/lock
# pattern (not a set — same reasoning doesn't strictly apply here since an
# algo_order_id can't be ambiguously-aborted twice concurrently in
# practice, but a plain set is simplest and sufficient).
_CH_AMBIGUOUS_ABORT_IDS: set[int] = set()
_CH_AMBIGUOUS_ABORT_LOCK = __import__("threading").Lock()


def _ch_mark_ambiguous_abort(algo_order_id: "int | None") -> None:
    """Flag `algo_order_id` for the one-shot backstop reconcile run by
    `chase_order()`'s wrapper after this chase_order() call returns.
    No-op for None (legacy callers with no row to reconcile)."""
    if algo_order_id is None:
        return
    with _CH_AMBIGUOUS_ABORT_LOCK:
        _CH_AMBIGUOUS_ABORT_IDS.add(int(algo_order_id))


def _ch_pop_ambiguous_abort(algo_order_id: "int | None") -> bool:
    """True + clears the flag when `algo_order_id` was marked ambiguous
    by this same chase_order() call. False (no-op) otherwise."""
    if algo_order_id is None:
        return False
    with _CH_AMBIGUOUS_ABORT_LOCK:
        key = int(algo_order_id)
        if key in _CH_AMBIGUOUS_ABORT_IDS:
            _CH_AMBIGUOUS_ABORT_IDS.discard(key)
            return True
        return False


async def _ch_backstop_reconcile_one(algo_order_id: int) -> None:
    """One-shot broker-truth reconcile for a single row, run immediately
    after an ambiguous abort.

    2026-10-23 (coordinator redirect) — uses the canonical single-
    order-view accessor (`orders_helpers.py:get_broker_order_truth` /
    `apply_order_truth`), the SAME one chase's own periodic in-loop
    check (`_ch_periodic_broker_truth_check`) calls, instead of reusing
    `orders.py:list_active_chases`'s own inline reconcile logic as an
    interim measure. Both of chase's item-8 auto-reconcile triggers now
    go through one shared accessor rather than two divergent ones.
    `list_active_chases` itself, the admin bulk/per-card reconcile
    routes, and `background.py:_task_open_order_watchdog` have since
    ALSO migrated onto this same accessor (the "Order poll" follow-up
    plan referenced above) — see `orders_helpers.py`'s module-level
    note next to `OrderTruth`, and `backend/api/algo/order_poll.py`
    for the shared wrapper the watchdog + the new periodic sweep +
    postback's follow-up verification use.

    2026-10-23 audit fix (O5) — now routes the actual reconcile through
    `order_poll.py:reconcile_row_and_followup` (this function already
    runs AFTER `chase_order()`'s `finally` calls `_ch_mark_chase_inactive`,
    so `is_chase_active(algo_order_id)` is already False by the time
    this runs — no `owning_chase` bypass needed here, unlike the
    periodic in-loop check below), so a fill this ambiguous-abort
    backstop resolves gets the same template-attach / ws-resubscribe
    treatment the watchdog and order-poll sweep already get, closing
    the scope gap the previous direct `apply_order_truth` call left
    (that call deliberately omits those side effects — see its own
    docstring).

    Best-effort: any exception is logged and swallowed; this is a
    backstop on top of an already-written CANCEL_FAILED/chase_unfilled
    row, never a new way for the chase itself to fail.
    """
    try:
        from backend.api.cache import invalidate as _cache_invalidate
        # Invalidate the 15s-cached "orders" broker snapshot so the
        # reconcile below reads fresh broker state instead of a
        # pre-ambiguity-abort cache entry.
        _cache_invalidate("orders")
    except Exception:
        pass
    try:
        async with _async_session() as _s:
            row = (await _s.execute(
                _sql_select(_AlgoOrder)
                .where(
                    _AlgoOrder.id == int(algo_order_id),
                    _AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"]),
                )
            )).scalar_one_or_none()
            if row is None:
                return   # nothing left to reconcile (already final elsewhere)
        from backend.api.algo.order_poll import reconcile_row_and_followup
        truth = await reconcile_row_and_followup(row, source="chase-backstop")
        if truth is not None:
            logger.info(
                "[CHASE] backstop reconcile for AlgoOrder #%s applied "
                "broker truth (%s) after an ambiguous abort.",
                algo_order_id, truth.algo_status,
            )
    except Exception as _e:
        logger.debug(
            "[CHASE] backstop reconcile for AlgoOrder #%s failed "
            "(non-fatal — leaves the row for the periodic/manual sweep): %s",
            algo_order_id, _e,
        )


async def _ch_append_superseded_id(
    algo_order_id: "int | None", old_broker_order_id: "str | None",
) -> None:
    """Append `old_broker_order_id` to this row's
    `superseded_broker_order_ids_json` (fix #6) — called at cancel-confirm
    time, BEFORE the replacement order is placed, so the frontend's
    duplicate-open-order-card dedup has an authoritative, race-free
    answer by the time the new order becomes visible broker-side. A
    direct column write (not derived from algo_order_events, which is a
    batched `on_full="drop"` queue and misses the rejected_continue /
    cancelled_continue replacement paths entirely). Best-effort — never
    raises; a failure here must never block placing the replacement
    order.
    """
    if algo_order_id is None or not old_broker_order_id:
        return
    try:
        import json as _json
        async with _async_session() as _s:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return
            try:
                ids = _json.loads(row.superseded_broker_order_ids_json or "[]")
                if not isinstance(ids, list):
                    ids = []
            except Exception:
                ids = []
            old_id = str(old_broker_order_id)
            if old_id not in ids:
                ids.append(old_id)
                row.superseded_broker_order_ids_json = _json.dumps(ids)
                await _s.commit()
    except Exception as _e:
        logger.debug(
            "[CHASE] _ch_append_superseded_id failed for #%s (old id %s): %s",
            algo_order_id, old_broker_order_id, _e,
        )


class ChaseStatus(str, Enum):
    PENDING   = "pending"
    CHASING   = "chasing"
    FILLED    = "filled"
    PARTIAL   = "partial"
    FAILED    = "failed"
    CANCELLED = "cancelled"
    HELD      = "held"


@dataclass
class ChaseResult:
    order_id: str = ""
    account: str = ""
    symbol: str = ""
    transaction_type: str = ""
    quantity: int = 0
    initial_price: float = 0.0
    fill_price: float = 0.0
    attempts: int = 0
    slippage: float = 0.0
    status: ChaseStatus = ChaseStatus.PENDING
    detail: str = ""


@dataclass
class ChaseConfig:
    interval_seconds: int = 20       # time between price adjustments
    aggression_step: float = 0.10    # spread fraction increase per attempt
    max_attempts: int = 20           # before giving up
    exchange: str = "NFO"
    product: str = "NRML"
    variety: str = "regular"
    validity: str = "DAY"
    rejection_backoff_seconds: int = 0
    # extra pause when an attempt comes back REJECTED / CANCELLED. 0 →
    # use cfg.interval_seconds (keeps spacing identical to a normal
    # re-quote cycle). A structural rejection (margin shortfall, tick
    # violation, permission gap) won't fix itself in milliseconds, so
    # waiting before the next place_order avoids hammering Kite's
    # 1-order/sec rate limit and gives the operator time to react.
    intent: str | None = None
    # forwarded to broker.place_order so the adapter ceiling in kite.py
    # treats close orders correctly (intent="close" bypasses the 50-lot cap).
    level: str | None = None
    # P1 fix (2026-10): the operator-facing L/M/H aggressiveness tag this
    # cfg was built from (set by `orders_helpers._live_chase_config`). Pure
    # metadata — nothing in the chase loop itself reads this to change
    # behaviour. Its only consumer today is `_ch_hold_on_repeated_rejection`,
    # which stamps it into the held row's `hold_json["price_policy"]`
    # (as `f"CHASE_{level.upper()}"`) so `order_release.py` can resume the
    # chase at the SAME tier the order was originally placed with instead
    # of a hardcoded MED tuple. None on any cfg built without going through
    # `_live_chase_config` (e.g. expiry-close's own fixed engine cfg) —
    # `order_release.py` treats a missing/unrecognised tag as MED, matching
    # the hardcoded behaviour that existed before this fix.


def _get_broker(account: str):
    """Return the `Broker` adapter for `account`. Routes through the
    registry so the chase engine doesn't know or care whether the
    backing broker is Kite, Groww, Dhan, or anything we add later —
    every method called below is on the `Broker` ABC."""
    return _get_broker_registry(account)


def _get_depth(account: str, exchange: str, symbol: str) -> dict:
    """Fetch market depth for a symbol. Returns {buy: [...], sell: [...]}."""
    broker = _get_broker(account)
    key = f"{exchange}:{symbol}"
    data = broker.quote([key])
    if key not in data:
        raise ValueError(f"No quote data for {key}")
    return data[key].get("depth", {})


def _ch_snap_sell_price(mid: float, spread: float, aggression: float,
                        tick: float, best_bid: float) -> float:
    """Compute tick-snapped SELL limit price: mid → best_bid as aggression grows."""
    return max(_snap_to_tick(mid - spread * aggression * 0.5, tick), best_bid)


def _ch_snap_buy_price(mid: float, spread: float, aggression: float,
                       tick: float, best_ask: float) -> float:
    """Compute tick-snapped BUY limit price: mid → best_ask as aggression grows."""
    return min(_snap_to_tick(mid + spread * aggression * 0.5, tick), best_ask)


def _ch_extract_best_bid_ask(depth: dict) -> "tuple[float, float]":
    """Extract best bid and ask from depth dict. Returns (0, 0) on missing / zero price."""
    buy_side  = depth.get("buy",  [])
    sell_side = depth.get("sell", [])
    bid = buy_side[0]["price"]  if buy_side  and buy_side[0]["price"]  > 0 else 0.0
    ask = sell_side[0]["price"] if sell_side and sell_side[0]["price"] > 0 else 0.0
    return bid, ask


def _calc_limit_price(depth: dict, transaction_type: str, attempt: int,
                      aggression_step: float,
                      exchange: str = "", symbol: str = "") -> float:
    """
    Calculate limit price from market depth, snapped to the
    instrument's actual tick.

    For SELL: start at mid, move toward best_bid with each attempt.
    For BUY:  start at mid, move toward best_ask with each attempt.

    The result is rounded to the contract's real tick_size (₹0.05 on
    NFO/NSE, ₹1 on MCX commodities, …). Without this, the chase would
    happily send ₹437.55 on a ₹1-tick contract and Kite rejects with
    "the entered price is not as per the ticker price".

    Tick-quantization limitation: when the bid-ask spread is 1–2 ticks
    (e.g. spread=0.05, tick=0.05), ``spread × aggression_step × 0.5``
    is sub-tick and successive calls may snap to the same price for
    multiple consecutive attempts. The caller (``chase_order``) enforces
    a minimum 1-tick progression per attempt — do not add that logic here.
    """
    best_bid, best_ask = _ch_extract_best_bid_ask(depth)

    if best_bid == 0 or best_ask == 0:
        return best_bid or best_ask or 0

    spread     = best_ask - best_bid
    mid        = (best_bid + best_ask) / 2
    aggression = min(attempt * aggression_step, 0.95)
    tick       = _tick_size_sync(exchange, symbol) if exchange and symbol else 0.05

    if transaction_type == "SELL":
        return _ch_snap_sell_price(mid, spread, aggression, tick, best_bid)
    return _ch_snap_buy_price(mid, spread, aggression, tick, best_ask)


# O(1) (exchange, symbol) → (lot_size, tick_size) index built on top
# of the instruments cache. The cache itself is a 75k-row list — the
# legacy `for inst in resp.items` scan in `_lot_size_sync` and
# `_tick_size_sync` cost ~75k comparisons per chase attempt. A
# 5-attempt chase = 10 full scans (lot + tick lookups); a 20-attempt
# MCX chase = 40. This dict is built once per cache generation
# (lazily on first lookup; rebuilt when the underlying response id
# changes — detected by the `id(resp)` guard) so subsequent reads
# are O(1) regardless of how many chase loops are concurrent.
_INSTRUMENT_INDEX: dict[tuple[str, str], tuple[int, float]] = {}
_INSTRUMENT_INDEX_FOR: int = 0   # id(resp) the current index was built from


def _ensure_instrument_index(resp) -> None:
    """Build (or rebuild) `_INSTRUMENT_INDEX` from the given instruments
    response when the cache rotated since the last build."""
    global _INSTRUMENT_INDEX, _INSTRUMENT_INDEX_FOR
    rid = id(resp)
    if rid == _INSTRUMENT_INDEX_FOR and _INSTRUMENT_INDEX:
        return
    new_index: dict[tuple[str, str], tuple[int, float]] = {}
    for inst in resp.items:
        new_index[(inst.e, inst.s)] = (
            int(inst.ls) if inst.ls > 0 else 1,
            float(inst.ts) if inst.ts > 0 else 0.05,
        )
    _INSTRUMENT_INDEX = new_index
    _INSTRUMENT_INDEX_FOR = rid


def _instrument_specs(exchange: str, symbol: str) -> tuple[int, float]:
    """Return (lot_size, tick_size) for a contract in O(1).

    On cache miss:
      - MCX/NCO: returns (0, 0.05). lot_size=0 is the "unknown" sentinel;
        callers (especially to_kite_qty via normalise_qty) must check for
        0 and refuse to place the order rather than dividing by 1 and
        sending raw_qty as LOTS — the root cause of the CRUDEOIL 100×
        oversize incident (lot_size cached as 1 → 100 ÷ 1 = 100 LOTS).
      - All other exchanges: returns (1, 0.05) — the NSE default; no
        translation is applied in to_kite_qty for non-MCX symbols, so
        lot_size=1 is a safe no-op there.
    """
    _mcx = exchange in ("MCX", "NCO")
    _miss = (0 if _mcx else 1, 0.05)
    try:
        entry = _cache_store.get("instruments")
        if entry is None:
            return _miss
        _expires, resp = entry
        if resp is None or not hasattr(resp, "items"):
            return _miss
        _ensure_instrument_index(resp)
        return _INSTRUMENT_INDEX.get((exchange, symbol), _miss)
    except Exception:
        return _miss


def _lot_size_sync(exchange: str, symbol: str) -> int:
    """O(1) lot_size lookup. See `_instrument_specs` for cache shape.

    Returns 0 for MCX/NCO on a cache miss — the sentinel that tells
    normalise_qty (→ to_kite_qty) to raise rather than silently divide
    by 1 and send raw_qty as lots (100× oversize on CRUDEOIL).
    Returns 1 for non-MCX misses (safe no-op — to_kite_qty doesn't
    translate non-MCX symbols)."""
    return _instrument_specs(exchange, symbol)[0]


def _tick_size_sync(exchange: str, symbol: str) -> float:
    """O(1) tick_size lookup. See `_instrument_specs` for cache shape.

    Kite rejects orders whose LIMIT price isn't a multiple of the
    contract's tick — NFO/NSE = ₹0.05, MCX commodities like CRUDEOIL =
    ₹1, bullion = ₹1, etc. _calc_limit_price's plain `round(px, 2)`
    handles paise-tick correctly but lets through ₹437.55 on a ₹1-tick
    contract, which the exchange then rejects. We round to the actual
    tick before returning the chase price.

    Falls through to 0.05 on a miss — the NSE default that's been
    the implicit assumption everywhere else in this engine."""
    return _instrument_specs(exchange, symbol)[1]


def _snap_to_tick(price: float, tick: float) -> float:
    """Round `price` to the nearest valid `tick` multiple.

    Uses scaled-integer rounding when tick < 1 (0.05 in binary float
    misbehaves) and plain `round(px/tick)*tick` otherwise.
    """
    if price <= 0 or tick <= 0:
        return price
    if tick < 1:
        scale = round(1.0 / tick)
        return round(round(price * scale) / scale, 4)
    return round(round(price / tick) * tick, 4)


def _ch_apply_min_tick_progression(
    price: float, last_placed_price: float, attempt: int,
    transaction_type: str, cfg: "ChaseConfig", symbol: str, depth: dict,
) -> float:
    """Force at least 1-tick price movement when `_calc_limit_price`
    returns the same snapped price as the previous attempt — happens
    when the bid-ask spread is 1–2 ticks wide, making
    spread × aggression_step × 0.5 sub-tick. Without this the chase
    loop cancels and re-places at an IDENTICAL price, visible as a
    frozen price in the chase panel. Returns `price` unchanged when no
    correction is needed. Extracted out of `chase_order`'s main loop
    (2026-10) purely to keep that function's cyclomatic complexity
    within the project's radon gate — no behavior change.
    """
    if not (attempt > 1 and price == last_placed_price):
        return price
    tick = _tick_size_sync(cfg.exchange, symbol)
    best_bid, best_ask = _ch_extract_best_bid_ask(depth)
    if transaction_type == "BUY":
        forced = _snap_to_tick(last_placed_price + tick, tick)
        price = min(forced, best_ask) if best_ask > 0 else forced
    else:  # SELL
        forced = _snap_to_tick(last_placed_price - tick, tick)
        price = max(forced, best_bid) if best_bid > 0 else forced
    logger.debug(
        "[CHASE] %s attempt %d: sub-tick formula, forced %s price %.4f→%.4f",
        symbol, attempt, transaction_type, last_placed_price, price,
    )
    return price


def _place_order(account: str, symbol: str, transaction_type: str,
                 quantity: int, price: float, cfg: ChaseConfig) -> str:
    """Place a limit order. Returns order_id."""
    broker    = _get_broker(account)
    lot_size  = _lot_size_sync(cfg.exchange, symbol)
    # C4 — explicit lot_size=0 guard: instruments cache miss returns 0.
    # Sending qty with lot_size=0 to normalise_qty causes a ZeroDivisionError
    # or silently passes raw contract qty as lots → multi-lakh order bloat.
    # Raise here so the chase loop's exception handler catches it and aborts.
    if lot_size == 0:
        raise ValueError(
            f"[CHASE-GUARD] lot_size=0 for {cfg.exchange}/{symbol} — "
            "instruments cache miss; cannot normalise qty. "
            "Retry after cache warms."
        )
    # normalise_qty handles per-broker contract↔lot translation
    # (Kite needs lots for MCX/NCO, qty for everything else). Default
    # is a no-op for brokers that always accept contracts.
    broker_qty = broker.normalise_qty(cfg.exchange, quantity, lot_size)
    order_id = broker.place_order(
        variety=cfg.variety,
        exchange=cfg.exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=broker_qty,
        product=cfg.product,
        order_type="LIMIT",
        price=price,
        validity=cfg.validity,
        intent=cfg.intent,
    )
    return str(order_id)


def _cancel_order(account: str, order_id: str, variety: str = "regular",
                  exchange: str = ""):
    """Cancel an open order.
    Slice Q — `exchange` is forwarded to broker.cancel_order so Groww's
    segment resolver uses the correct segment instead of defaulting to NSE
    and silently failing for MCX/NFO cancels."""
    broker = _get_broker(account)
    if exchange:
        broker.cancel_order(order_id, variety=variety, exchange=exchange)
    else:
        broker.cancel_order(order_id, variety=variety)


def _order_status(account: str, order_id: str) -> dict:
    """Get order status. Returns dict with status, filled_quantity, etc.

    Calls `broker.order_status(order_id)` — the Broker ABC's
    targeted single-order endpoint. Kite implements this via
    `order_history(order_id)` so we pay for one order's lifecycle
    instead of the entire day's order book on every 20-s chase
    status poll. Dhan / Groww fall back to the default
    `orders()`-filter implementation until their SDKs expose a
    targeted endpoint."""
    broker = _get_broker(account)
    return broker.order_status(order_id)


# 2026-10-23 chase robustness (fix #3) — ceiling for any single broker
# call made via `_run`. Without this, one hung SDK call (dead socket,
# broker-side stall) parks the awaiting chase-loop coroutine forever —
# `_chase_order_impl`'s `for` loop never reaches its next iteration, so
# neither the normal max_attempts bound nor the stuck-wall-clock check
# below ever get a chance to run.
#
# Honest limitation (do not oversell this in docs/alerts): `asyncio.
# wait_for` only unblocks the AWAITING COROUTINE — it does not, and
# cannot, forcibly kill the underlying `ThreadPoolExecutor` worker
# thread (Python threads can't be pre-empted). A genuinely hung SDK call
# may still occupy that worker until it eventually returns or errors at
# the socket layer. Real socket-level timeouts belong in
# `backend/brokers/`. The value here is narrower and still real: the
# CHASE LOOP ITSELF can never be stuck awaiting one dead call forever.
_CH_BROKER_CALL_TIMEOUT_S = 30


async def _run(fn, *args):
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, fn, *args),
            timeout=_CH_BROKER_CALL_TIMEOUT_S,
        )
    except asyncio.TimeoutError as _te:
        raise TimeoutError(
            f"broker call {getattr(fn, '__name__', fn)!r} did not return "
            f"within {_CH_BROKER_CALL_TIMEOUT_S}s"
        ) from _te


async def _sync_algo_order_id(algo_order_id: int | None,
                              new_broker_order_id: str,
                              current_limit: float | None = None,
                              interval_seconds: int | None = None,
                              attempt: int | None = None) -> bool:
    """Update AlgoOrder.broker_order_id to the latest one the chase
    just placed. Best-effort — never raises. Phase 0.5 — without this
    every chase cancel-and-replace orphaned the row from its broker
    id, so the terminal lookup in _emit_chase_terminal (and the
    postback handler) couldn't match the row by broker_order_id.

    2026-10 fix — also writes `attempts` (the current cancel-and-replace
    attempt number) on every call, same as the other per-attempt sync
    fields below. Before this, `attempts` was only written once, at the
    very end, by `_chase_apply_terminal_mutation` — so the operator-facing
    "Attempts" column (ChaseCard.svelte / OrderCard.svelte) showed a
    stale count while a chase was actively running.

    Audit fix (M-6) — also writes `current_limit` so the chase panel
    shows the LIVE re-quoted limit price instead of the FIRST
    attempt's initial_price. Pre-fix the UI rendered initial_price
    on every chase row regardless of how many cancel-and-replaces
    had moved the broker order's limit; after 3+ iterations the
    operator saw a stale entry price.

    Chase timing fix — writes `last_attempt_at` (epoch seconds, now())
    and `next_attempt_at` (last_attempt_at + interval_seconds) on
    every cancel-and-replace so the chase panel can display a live
    countdown to the next re-quote. Without these columns the UI had
    no way to distinguish "active" from "stalled" chases mid-flight.
    `interval_seconds` is forwarded from ChaseConfig so the next-attempt
    display matches the actual configured cadence.

    2026-10 audit fix — final-status race: a postback for a PRIOR order
    under the same AlgoOrder row can land and finalize the row (e.g. to
    REJECTED) in the exact window between the chase loop deciding to
    retry and this function writing the freshly-placed order's
    broker_order_id onto that row. Writing the new id onto an already-
    final row would silently orphan the new (live, possibly later
    filled) broker order from any DB tracking — see
    `_chase_terminal_update_db`'s identical `ALGO_ORDER_FINAL_STATUSES`
    guard for the sibling fix. The SELECT uses `.with_for_update()` (same
    as `_chase_terminal_update_db`) so a concurrent postback transaction
    that has started (but not yet committed) a status flip is waited on
    rather than read-past under READ COMMITTED — a plain unlocked SELECT
    would still race even with the status check in place. Returns
    `False` (and performs NO write) when the row is already final, so
    the caller (the main chase loop) can abort instead of continuing to
    place yet more untracked orders. Returns `True` on a normal sync
    (row missing/non-final) — the common case — unchanged from before
    this fix.
    """
    if algo_order_id is None or not new_broker_order_id:
        return True
    _race_snap: dict | None = None
    try:
        async with _async_session() as _s:
            row = (await _s.execute(
                _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                .with_for_update()
            )).scalar_one_or_none()
            if row is not None:
                from backend.api.models import ALGO_ORDER_FINAL_STATUSES
                if row.status in ALGO_ORDER_FINAL_STATUSES:
                    # Snapshot what's needed for the alert and exit the
                    # session (releasing the row lock) BEFORE doing any
                    # alert I/O — never hold a DB row lock across a
                    # network call.
                    _race_snap = {
                        "status": row.status,
                        "account": str(getattr(row, "account", "") or ""),
                        "symbol": str(getattr(row, "symbol", "") or ""),
                        "exchange": str(getattr(row, "exchange", "") or ""),
                        "transaction_type": str(getattr(row, "transaction_type", "") or ""),
                        "quantity": int(getattr(row, "quantity", 0) or 0),
                        "agent_id": getattr(row, "agent_id", None),
                    }
                else:
                    if _ch_apply_broker_sync_fields(
                        row, new_broker_order_id, current_limit, interval_seconds,
                        attempt,
                    ):
                        await _s.commit()
    except Exception as _e:
        # Deliberately NOT `return True` here — if `_race_snap` was
        # already populated before the session raised on teardown (e.g.
        # __aexit__ failing after the status check), falling through to
        # the check below still correctly reports the detected race
        # instead of silently discarding it.
        logger.debug(f"_sync_algo_order_id failed: {_e}")
    if _race_snap is not None:
        _ch_alert_sync_final_status_race(algo_order_id, new_broker_order_id, _race_snap)
        return False
    return True


def _ch_apply_broker_sync_fields(
    row, new_broker_order_id: str, current_limit: float | None,
    interval_seconds: int | None, attempt: int | None = None,
) -> bool:
    """Apply broker_order_id/current_limit/timing/attempts field writes
    onto a non-final AlgoOrder row (the normal cancel-and-replace sync
    path). Returns True when any field actually changed, so the caller
    knows whether a commit is needed. Extracted out of
    `_sync_algo_order_id` (2026-10) purely to keep that function's
    cyclomatic complexity within the project's radon gate — no behavior
    change.

    2026-10 fix — also writes `attempts` on every call (same pattern as
    the timing fields below), so the live attempt count the operator
    sees in ChaseCard/OrderCard tracks the chase loop in real time
    instead of only updating once at the terminal mutation."""
    _dirty = False
    if row.broker_order_id != str(new_broker_order_id):
        row.broker_order_id = str(new_broker_order_id)
        _dirty = True
    if current_limit is not None and float(current_limit) > 0:
        if row.current_limit != float(current_limit):
            row.current_limit = float(current_limit)
            _dirty = True
    if attempt is not None:
        try:
            _attempt_int = int(attempt)
        except (TypeError, ValueError):
            _attempt_int = None
        if _attempt_int is not None and row.attempts != _attempt_int:
            row.attempts = _attempt_int
            _dirty = True
    # Write timing fields so the chase panel shows a live countdown.
    # Both columns are nullable; skip on models that predate the
    # migration (hasattr guard).
    _now = _time.time()
    if hasattr(row, "last_attempt_at"):
        row.last_attempt_at = _now
        _dirty = True
    if hasattr(row, "next_attempt_at") and interval_seconds is not None:
        row.next_attempt_at = _now + float(interval_seconds)
        _dirty = True
    if hasattr(row, "interval_seconds") and interval_seconds is not None:
        row.interval_seconds = int(interval_seconds)
        _dirty = True
    return _dirty


def _ch_alert_sync_final_status_race(
    algo_order_id: int, new_broker_order_id: str, row_snap: dict,
) -> None:
    """Log + alert helper for `_sync_algo_order_id`'s final-status race
    guard — split out so the CRITICAL log + alert I/O never runs while
    the AlgoOrder row's DB lock is held."""
    logger.critical(
        "[CHASE] AlgoOrder #%s already finalized to status %s — "
        "refusing to overwrite broker_order_id with newly-placed "
        "order %s, which is now UNTRACKED (live at the broker, "
        "nothing in the DB pointing at it). Manual reconciliation "
        "required.", algo_order_id, row_snap["status"], new_broker_order_id,
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=row_snap["account"], symbol=row_snap["symbol"],
            exchange=row_snap["exchange"], side=row_snap["transaction_type"],
            qty=row_snap["quantity"], mode="live", source="chase",
            agent_id=row_snap.get("agent_id"),
            error=(
                f"Chase retry placed order {new_broker_order_id} but "
                f"AlgoOrder #{algo_order_id} already finalized to "
                f"{row_snap['status']} (likely a racing postback) — the "
                f"new order is now untracked; verify and reconcile "
                f"manually at the broker."
            ),
        )
    except Exception as _alert_exc:
        logger.warning(
            "[CHASE] final-status race alert failed: %s", _alert_exc,
        )


def _ch_compute_new_filled(prior_filled: int, cumulative_filled: int,
                           total_qty: int) -> int:
    """MAX-clamp cumulative broker fill count: monotonic + bounded by total."""
    new_filled = max(prior_filled, int(cumulative_filled))
    # Defensive cap: prevent template attach from over-sizing exit GTTs.
    return min(new_filled, int(total_qty))


def _ch_partial_fill_no_change(
    new_filled: int, prior_filled: int,
    new_avg_price: "float | None", prior_fill_price: float,
) -> bool:
    """Return True when both qty and price are unchanged — skip the write."""
    price_unchanged = (
        new_avg_price is None or abs(new_avg_price - prior_fill_price) < 0.001
    )
    return new_filled == prior_filled and price_unchanged


async def _ch_write_partial_fill_row(
    algo_order_id: int, cumulative_filled: int, avg_price: float, total_qty: int,
) -> None:
    """DB write for one partial-fill update (session body extracted from _record_partial_fill)."""
    async with _async_session() as _s:
        row = (await _s.execute(
            _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
        )).scalar_one_or_none()
        if row is None:
            return
        prior_filled     = int(row.filled_quantity or 0)
        new_filled       = _ch_compute_new_filled(prior_filled, cumulative_filled, total_qty)
        new_avg_price    = float(avg_price) if avg_price and float(avg_price) > 0 else None
        prior_fill_price = float(row.fill_price or 0)
        if _ch_partial_fill_no_change(new_filled, prior_filled, new_avg_price, prior_fill_price):
            return
        row.filled_quantity = new_filled
        if new_avg_price is not None:
            row.fill_price = new_avg_price
        new_avg_display = float(row.fill_price or 0)
        row.detail = (
            f"PARTIAL {new_filled}/{total_qty} @ ₹{new_avg_display:,.2f} "
            f"(chasing residual {int(total_qty) - new_filled})"
        )[:240]
        await _s.commit()


async def _record_partial_fill(algo_order_id: int | None,
                                cumulative_filled: int,
                                avg_price: float,
                                total_qty: int) -> None:
    """Sprint B (#4) — persist partial-fill state on the AlgoOrder row.

    Audit fix (C-1): the `cumulative_filled` argument is the BROKER'S
    cumulative `filled_quantity` from its status poll — NOT a per-call
    delta. Kite reports cumulative (so does Dhan + Groww post-status-
    map). Pre-fix this function added the cumulative value to the
    prior DB value every call, so a chase recovered from DB after a
    restart would inflate `filled_quantity` past `total_qty` (prior
    30 + cumulative 30 = 60 even though only 30 actually traded). The
    template-attach path then sized exit GTTs against the inflated
    value, over-hedging.

    `avg_price` is the broker's running cumulative average fill price
    (most SDKs return cumulative average, not delta-weighted average).
    We store it directly — the broker's number is authoritative.

    Accumulates `filled_quantity` across partials so:
      - the order log shows live `filled/total` progress instead of
        appearing stuck at 0,
      - the UNFILLED give-up path reports the actual unfilled remainder
        instead of restating the original qty,
      - the template attach path sees the accurate `quantity` and
        sizes exit GTTs against the residual, not the original lot.

    Best-effort. Never raises — partial-fill telemetry is
    informational, the chase loop continues whether the write
    succeeds or not.
    """
    if algo_order_id is None or cumulative_filled <= 0:
        return
    try:
        await _ch_write_partial_fill_row(
            algo_order_id, cumulative_filled, avg_price, total_qty,
        )
    except Exception as _e:
        logger.debug(f"_record_partial_fill failed: {_e}")


# ── Poll-status terminal handlers (extracted to reduce CC) ──────────────

def _ch_mcx_lots_broker(account: str) -> bool:
    """C3 — True when `account`'s broker ships MCX/NCO order-status
    `filled_quantity` in LOTS (not contracts).

    Reuses `orders.py:_MCX_LOTS_CONVENTION_BROKERS` — the SAME
    allow-list already used to gate the equivalent postback lots→
    contracts conversion — rather than duplicating the broker-id list
    here. Groww is CONFIRMED to report MCX fills in contracts already
    (`groww.py:492`, `translate_qty` no-op at `:1367`); applying the
    Kite/Dhan lots→contracts reverse-translate to a Groww fill count
    would inflate it by lot_size×.

    "zerodha_kite" (canonical `broker_accounts.broker_id`) and "kite"
    (legacy YAML alias) both mean Kite — normalised the same way
    `registry.py`'s own `_KITE_IDS` set does before the allow-list
    membership check.
    """
    try:
        from backend.api.routes.orders import _MCX_LOTS_CONVENTION_BROKERS
        from backend.brokers.registry import _broker_id_for
        bid = str(_broker_id_for(account) or "").lower()
        if bid == "zerodha_kite":
            bid = "kite"
        return bid in _MCX_LOTS_CONVENTION_BROKERS
    except Exception:
        # Conservative default on resolution failure: assume the
        # lots-convention (matches pre-fix behaviour / the majority
        # broker, Kite) rather than silently under-counting a Kite/Dhan
        # fill by skipping the reverse-translate.
        return True


def _ch_reverse_translate_mcx_filled(
    cfg: "ChaseConfig", symbol: str, account: str, raw_filled: int,
) -> int:
    """C3 — reverse-translate a broker's raw `filled_quantity` from LOTS
    to CONTRACTS for MCX/NCO, gated by broker identity (see
    `_ch_mcx_lots_broker`) rather than by exchange alone.

    Non-MCX/NCO exchanges and non-lots-convention brokers pass through
    unchanged.
    """
    if cfg.exchange not in ("MCX", "NCO") or raw_filled <= 0:
        return raw_filled
    if not _ch_mcx_lots_broker(account):
        return raw_filled
    from backend.brokers.adapters.kite import from_kite_qty
    lot = _lot_size_sync(cfg.exchange, symbol)
    return from_kite_qty(cfg.exchange, raw_filled, lot)


async def _ch_poll_handle_complete(
    result: "ChaseResult", avg_price: float, filled_qty: int,
    quantity: int, attempt: int, remaining_qty: int,
    current_order_id: str, symbol: str, transaction_type: str,
    algo_order_id: "int | None", emit: Callable,
    cumulative_filled: int = 0,
) -> "tuple[str, int]":
    """Handle COMPLETE status: update result + schedule terminal event."""
    result.status    = ChaseStatus.FILLED
    result.fill_price = avg_price
    result.slippage   = abs(avg_price - result.initial_price) * (filled_qty or quantity)
    result.detail     = f"Filled at {avg_price} in {attempt} attempts"
    emit("order_filled", {
        "order_id": current_order_id, "fill_price": avg_price,
        "attempts": attempt, "slippage": result.slippage,
    })
    logger.info(f"Chase {symbol}: FILLED @ {avg_price} "
                f"(attempt {attempt}, slippage ₹{result.slippage:.2f})")
    # C2 (adjacent fix) — persist the FINAL cumulative filled_quantity so
    # downstream template-attach exit GTTs size against the TRUE total
    # (prior partials + this order's own completion), not whatever the
    # last partial-fill write happened to leave in the row. Gated on
    # `cumulative_filled > 0` (prior partials existed on EARLIER orders
    # this chase cycle) so a normal single-shot fill (no prior partials)
    # doesn't get a synthetic "PARTIAL n/n" detail string written.
    # Awaited (not fire-and-forget) so the write commits BEFORE the
    # terminal-event task below snapshots the row for template attach.
    if cumulative_filled > 0:
        await _record_partial_fill(algo_order_id, quantity, avg_price, quantity)
    import asyncio as _asyncio
    _asyncio.create_task(_emit_chase_terminal(
        current_order_id, "chase_fill",
        symbol, transaction_type, quantity,
        final_price=avg_price, attempts=attempt,
        slippage=result.slippage, algo_order_id=algo_order_id,
    ))
    return "filled", remaining_qty


def _ch_poll_handle_rejected(
    result: "ChaseResult", status: dict, attempt: int, remaining_qty: int,
    account: str, symbol: str, transaction_type: str, quantity: int,
    current_order_id: str, cfg: "ChaseConfig",
    algo_order_id: "int | None", emit: Callable,
    agent_id: "int | None" = None,
) -> "tuple[str, int]":
    """Handle REJECTED status: retry with a fresh price when the
    rejection looks price-related and attempts remain; otherwise abort
    (log, alert, schedule terminal event) same as before.
    """
    status_msg = status.get("status_message", "") or "rejected by broker"
    if _ch_rejection_is_recoverable(status_msg) and attempt < cfg.max_attempts:
        logger.warning(
            f"Chase {symbol}: REJECTED (recoverable — {status_msg}) on "
            f"attempt {attempt}/{cfg.max_attempts} — repricing and retrying."
        )
        emit("chase_reprice", {
            "attempt": attempt, "reason": "rejected_recoverable",
            "status_message": status_msg,
        })
        return "rejected_continue", remaining_qty

    abort_msg  = f"Order rejected by broker: {status_msg}"
    logger.error(f"Chase {symbol}: REJECTED — {status_msg}. Aborting chase.")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {
        "attempts": attempt, "error": abort_msg,
        "reason": "broker_rejected", "status_message": status_msg,
    })
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            error=abort_msg, agent_id=agent_id,
        )
    except Exception:
        pass
    import asyncio as _asyncio
    _asyncio.create_task(_emit_chase_terminal(
        current_order_id, "chase_failed",
        symbol, transaction_type, quantity,
        attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
    ))
    return "rejected", remaining_qty


def _ch_poll_handle_cancelled(
    result: "ChaseResult", status: dict, attempt: int, remaining_qty: int,
    current_order_id: str, symbol: str, transaction_type: str, quantity: int,
    algo_order_id: "int | None", emit: Callable,
) -> "tuple[str, int]":
    """Handle CANCELLED status: distinguish operator-kill from broker auto-cancel."""
    # 2026-10-23 Kill-lifecycle fix — also check the algo-order-id-keyed
    # signal, not just the broker_order_id-keyed one: a broker-side
    # CANCELLED landing for an id the operator's Kill click never even
    # read (because chase had already cancelled-and-replaced past it)
    # must still be recognized as an operator kill, not misclassified
    # as a broker auto-cancel that triggers `cancelled_continue` (which
    # would silently re-place the order the operator just tried to stop).
    killed_by_op = is_killed(current_order_id) or (
        algo_order_id is not None and is_order_killed(algo_order_id)
    )
    logger.warning(
        f"Chase {symbol}: order CANCELLED "
        f"(operator_kill={killed_by_op}) — "
        f"{status.get('status_message', '')}"
    )
    if killed_by_op:
        result.status = ChaseStatus.CANCELLED
        result.detail = "Chase cancelled by operator"
        emit("chase_cancelled", {"attempts": attempt})
        import asyncio as _asyncio
        _asyncio.create_task(_emit_chase_terminal(
            current_order_id, "chase_cancelled",
            symbol, transaction_type, quantity,
            attempts=attempt, algo_order_id=algo_order_id,
        ))
        return "killed", remaining_qty
    # Non-operator cancel (broker auto-cancel, circuit, session-end, Kite-app).
    return "cancelled_continue", remaining_qty


async def _chase_poll_status(
    account: str,
    current_order_id: str,
    cfg: ChaseConfig,
    symbol: str,
    transaction_type: str,
    quantity: int,
    result: ChaseResult,
    attempt: int,
    remaining_qty: int,
    algo_order_id: int | None,
    emit: Callable,
    cumulative_filled: int = 0,
    current_order_filled: int = 0,
    agent_id: "int | None" = None,
) -> tuple[str | None, int, int, int]:
    """Check order status after the per-attempt sleep. Mutates result in-place.

    `cumulative_filled` is the chase-WIDE running total (across every
    order placed so far this chase cycle) — NOT this specific order's
    own fill count. `current_order_filled` is how much of THIS order's
    own (from-zero) fill has already been folded into
    `cumulative_filled` — it lets repeated polls of the same still-
    resting order detect only the NEW delta instead of re-adding the
    whole amount (C2 fix; see module-level notes in `chase_order`).

    Returns (signal, new_remaining_qty, new_cumulative_filled,
    new_current_order_filled) where signal is one of:
      'filled'            — order fully complete; caller should return result
      'killed'            — operator cancelled; caller should return result
      'rejected'          — broker rejected, non-recoverable; caller should return result
      'rejected_continue' — broker rejected but the reason looks price-
                            related and attempts remain; caller resets
                            current_order_id and retries with a fresh price
      'cancelled_continue'— broker/external cancel, NOT operator; caller resets
                            current_order_id and sleeps backoff before continuing
      None                — partial fill or no notable status; caller continues loop

    The consecutive_errors counter is NOT managed here — it lives in
    chase_order's outer except block so broker exceptions (raised before
    this helper is called) are still caught correctly.
    """
    # Check status
    status = await _run(_order_status, account, current_order_id)
    order_status = status.get("status", "").upper()
    # Sprint D / C3 — Kite AND Dhan report `filled_quantity` in
    # WHATEVER units `place_order` was given. For MCX/NCO those two
    # brokers were placed in LOTS (translate_qty divides by lot_size),
    # so the status filled_quantity is also in lots — but our
    # `remaining_qty` / `quantity` track CONTRACTS. Groww already
    # reports MCX fills in contracts, so it must NOT go through this
    # reverse-translate (broker-gated via `_ch_mcx_lots_broker`).
    _raw_filled = int(status.get("filled_quantity", 0) or 0)
    filled_qty = _ch_reverse_translate_mcx_filled(cfg, symbol, account, _raw_filled)
    avg_price = status.get("average_price", 0)

    if order_status == "COMPLETE":
        signal, remaining_qty = await _ch_poll_handle_complete(
            result, avg_price, filled_qty, quantity, attempt, remaining_qty,
            current_order_id, symbol, transaction_type, algo_order_id, emit,
            cumulative_filled,
        )
        return signal, remaining_qty, quantity, filled_qty

    # C2 fix — `filled_qty` is THIS order's own from-zero cumulative
    # fill (every SDK reports cumulative-per-order, never a delta). The
    # pre-fix code subtracted `quantity - remaining_qty` (the chase-WIDE
    # total filled by PRIOR orders) from `filled_qty` as if `filled_qty`
    # were itself a chase-wide total — it isn't, so two orders each
    # filling half the quantity independently summed to double-counted
    # `remaining_qty` and a full duplicate re-order. Comparing against
    # `current_order_filled` (this order's own previously-observed fill)
    # instead correctly isolates the NEW delta regardless of how much
    # any earlier order already contributed.
    if filled_qty > current_order_filled:
        _new_delta = filled_qty - current_order_filled
        cumulative_filled += _new_delta
        current_order_filled = filled_qty
        remaining_qty = max(0, quantity - cumulative_filled)
        if cumulative_filled < quantity:
            result.status = ChaseStatus.PARTIAL
            logger.info(
                f"Chase {symbol}: partial fill +{_new_delta} "
                f"(total {cumulative_filled}/{quantity}, remaining {remaining_qty})"
            )
            emit("partial_fill", {"filled": cumulative_filled, "delta": _new_delta,
                                  "remaining": remaining_qty})
        await _record_partial_fill(algo_order_id, cumulative_filled, avg_price, quantity)

    if order_status == "REJECTED":
        signal, remaining_qty = _ch_poll_handle_rejected(
            result, status, attempt, remaining_qty,
            account, symbol, transaction_type, quantity,
            current_order_id, cfg, algo_order_id, emit,
            agent_id=agent_id,
        )
        return signal, remaining_qty, cumulative_filled, current_order_filled

    if order_status in ("CANCELLED", "EXPIRED"):
        signal, remaining_qty = _ch_poll_handle_cancelled(
            result, status, attempt, remaining_qty,
            current_order_id, symbol, transaction_type, quantity,
            algo_order_id, emit,
        )
        return signal, remaining_qty, cumulative_filled, current_order_filled

    # No terminal status — partial fill or benign (e.g. OPEN/TRIGGER_PENDING).
    return None, remaining_qty, cumulative_filled, current_order_filled


def _chase_default_cfg() -> "ChaseConfig":
    """Build a ChaseConfig from /admin/settings (DB → YAML fallback).

    Extracted from chase_order to keep that function's branch count low.
    Lazy-imports settings helpers to avoid circular imports at module load.
    """
    from backend.shared.helpers.settings import get_float, get_int
    return ChaseConfig(
        interval_seconds=get_int("algo.chase_interval_seconds", 20),
        aggression_step=get_float("algo.aggression_step", 0.10),
        max_attempts=get_int("algo.max_attempts", 20),
        rejection_backoff_seconds=get_int(
            "algo.chase_rejection_backoff_seconds", 0),
    )


async def _chase_abort_on_consecutive_errors(
    consecutive_errors: int,
    attempt: int,
    last_error: Exception,
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    current_order_id: str | None,
    cfg: "ChaseConfig",
    result: "ChaseResult",
    emit: Callable,
    algo_order_id: int | None,
    agent_id: "int | None" = None,
    cumulative_filled: int = 0,
    current_order_filled: int = 0,
) -> "ChaseResult":
    """Handle consecutive-error abort in the chase loop.

    Fires an alert, cancels the live order (best-effort, with full
    confirm-read retries), writes the terminal status, and returns the
    mutated result ready for `return`. Only called when
    `consecutive_errors >= _MAX_CHASE_ERRORS`.

    2026-10-23 chase robustness: the client-side errors that triggered
    this abort may themselves have corrupted our view of what actually
    happened broker-side on recent attempts — repeated network/SDK
    errors mean we can't fully trust even a clean-looking cancel read.
    So this ALWAYS writes `CANCEL_FAILED` (not `REJECTED`), regardless of
    whether `_ch_final_cancel_and_confirm` reports the cancel as
    confirmed or not — "consecutive client-side errors" is itself the
    ambiguity, not just whether the final cancel landed. Terminal write
    is now `await`ed (not fire-and-forget) and the row is marked for the
    item-8b one-shot backstop reconcile. The "write some marker" gap is
    also fixed here: the terminal write now fires whenever EITHER
    `current_order_id` OR `algo_order_id` is available (previously it
    required `current_order_id` truthy, silently leaving the row at
    OPEN with no trace whenever all 3 consecutive errors happened before
    any order was ever successfully placed this chase cycle).
    """
    cancel_confirmed, cumulative_filled, current_order_filled, _avg = (
        await _ch_final_cancel_and_confirm(
            account, current_order_id, cfg, symbol, quantity,
            cumulative_filled, current_order_filled, algo_order_id,
        )
    )
    abort_msg = (
        f"Chase abandoned after {consecutive_errors} consecutive errors "
        f"({attempt} total attempts, {cumulative_filled}/{quantity} filled) "
        f"— last error: {last_error}"
    )
    if current_order_id and not cancel_confirmed:
        abort_msg += (
            f" — cancel of order {current_order_id} could NOT be confirmed; "
            f"it may still be resting live at the broker."
        )
    logger.error(f"Chase {symbol}: {abort_msg}")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {"attempts": attempt, "error": str(last_error),
                          "reason": "consecutive_errors",
                          "cancel_confirmed": cancel_confirmed})
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol,
            exchange=cfg.exchange, side=transaction_type,
            qty=quantity, mode="live", source="chase",
            agent_id=agent_id,
            error=abort_msg,
            # 2026-10-23 fix #7 — an abandonment alert must never be
            # swallowed by the 10-minute dedup window or the
            # market-closed suppression; reconciliation matters
            # regardless of market hours, and a second genuinely-new
            # abandonment must not be swallowed by the first one's dedup.
            bypass_gates=True,
        )
    except Exception:
        pass
    if current_order_id or algo_order_id is not None:
        if algo_order_id is not None:
            _ch_mark_ambiguous_abort(algo_order_id)
        try:
            await _emit_chase_terminal(
                current_order_id or "", "chase_cancel_failed",
                symbol, transaction_type, quantity,
                attempts=attempt, error=abort_msg,
                algo_order_id=algo_order_id,
            )
        except Exception:
            pass
    return result


# ── chase_order inner-loop helpers (extracted to reduce CC) ─────────────

async def _ch_hold_on_repeated_rejection(
    result: "ChaseResult",
    account: str, symbol: str, transaction_type: str, quantity: int,
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    algo_order_id: "int | None",
    emit: Callable,
    agent_id: "int | None" = None,
) -> "ChaseResult":
    """Operator instruction (2026-10): a SECOND consecutive price-shaped
    REJECTED outcome for the SAME chase must stop auto-retrying and hand
    the row to the operator instead of placing a third attempt.

    Reuses the EXISTING held-order mechanism verbatim — `AlgoOrder.status
    == "HELD"` + `hold_json` (`order_hold.py`), the same fields
    `order_hold_gate.py:record_held_order` already writes for expiry-
    close holds, surfaced today by `GET /api/orders/held` and released
    via `POST /api/orders/held/{id}/release` — no new status, column, or
    UI surface. Category `AGENT_ORDER` (defined in `order_hold.py`,
    unused until now) tags the reason without touching the
    `EXPIRY_CLOSE`/`TEMPLATE_EXIT` release-path semantics.

    Fires the same `send_order_failure_alert` convention every other
    chase abort path in this module uses, writes a `held` timeline
    event, and pops `_CH_PRE_FILL_NET_QTY` so this chase's pre-fill
    snapshot never leaks (mirrors every other terminal path).

    P1 fix (2026-10): stamps the ORIGINAL chase's aggressiveness level
    (`cfg.level`, set by `orders_helpers._live_chase_config`) into
    `hold_json["price_policy"]` as `f"CHASE_{level.upper()}"` — the same
    field `record_held_order` already uses for expiry-close holds
    (default `"CHASE_MED"`). Without this, `order_release.py` had no way
    to know the row was originally chased at HIGH/LOW and silently
    resumed every release at a hardcoded MED tuple.

    2026-10-23 chase robustness (item 5, deviation from the literal plan
    text) — `expiry.py`'s expiry-close chase call site has NO AlgoOrder
    row to pass as `algo_order_id` (an `OptionPosition` carries no row
    id; expiry-close chases have never created one). Pre-persisting a
    row just for this would make `recover_live_chases()` start resuming
    expiry-close chases on restart — a materially bigger change than
    this fix, and not requested. Instead, the alert text below is now
    conditional on `algo_order_id`: when `None`, it does NOT promise an
    `/orders/held` entry that will never exist.
    """
    from backend.api.algo.order_hold import HoldCategory, hold_record

    _level = getattr(cfg, "level", None)
    price_policy = (
        f"CHASE_{_level.upper()}" if _level in ("low", "med", "high") else "CHASE_MED"
    )

    reason = (
        "repeated price rejection — two consecutive price-shaped REJECTED "
        "outcomes while chasing; held for operator review"
    )
    result.status = ChaseStatus.HELD
    result.order_id = current_order_id or ""
    result.detail = reason
    emit("chase_held", {"reason": reason, "order_id": current_order_id})
    logger.warning(
        "Chase %s: held after two consecutive price-shaped REJECTED "
        "outcomes — awaiting operator review (no further auto-retry).",
        symbol,
    )

    if algo_order_id is not None:
        _CH_PRE_FILL_NET_QTY.pop(algo_order_id, None)
        try:
            from backend.api.models import ALGO_ORDER_FINAL_STATUSES
            async with _async_session() as _s:
                row = (await _s.execute(
                    _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                    .with_for_update()
                )).scalar_one_or_none()
                if row is not None and row.status not in ALGO_ORDER_FINAL_STATUSES:
                    held_at = datetime.now(timezone.utc)
                    row.status = "HELD"
                    row.hold_json = hold_record(
                        HoldCategory.AGENT_ORDER, reason, price_policy, None, held_at,
                    )
                    row.detail = (row.detail or "")[:200] + f" · {reason}"
                    await _s.commit()
        except Exception as _e:
            logger.warning(f"[CHASE] hold-on-repeated-rejection DB write failed: {_e}")
        try:
            await _ch_write_order_event(
                algo_order_id, "held", reason, {"order_id": current_order_id},
            )
        except Exception:
            pass

    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        if algo_order_id is not None:
            _hold_msg = (
                f"{reason}. No further auto-retry will be attempted — "
                f"review and release via /orders/held."
            )
        else:
            # No AlgoOrder row exists for this chase (e.g. expiry-close)
            # — don't promise an operator-review row that will never
            # appear there.
            _hold_msg = (
                f"{reason}. No further auto-retry will be attempted. "
                f"No order row exists for this symbol/account to release "
                f"via /orders/held — manually re-trigger the expiry-close "
                f"scan (or equivalent) if this position still needs closing."
            )
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            agent_id=agent_id,
            error=_hold_msg,
            # HELD is action-required by definition (fix #7).
            bypass_gates=True,
        )
    except Exception:
        pass
    return result


async def _ch_handle_poll_signal(
    signal: "str | None",
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    symbol: str,
    *,
    consecutive_price_rejections: int = 0,
    account: str = "",
    transaction_type: str = "",
    quantity: int = 0,
    algo_order_id: "int | None" = None,
    result: "ChaseResult | None" = None,
    emit: "Callable | None" = None,
    agent_id: "int | None" = None,
) -> "tuple[bool, str | None, int]":
    """Interpret the poll signal, apply the cancelled/rejected-continue
    backoff, and track consecutive price-shaped rejections.

    Operator instruction (2026-10): a repeated price-shaped REJECTED
    (signal == 'rejected_continue' while `consecutive_price_rejections`
    is already >= 1 — i.e. this is the SECOND one in a row) stops
    auto-retry and transitions the row to HELD instead of placing a
    third attempt (see `_ch_hold_on_repeated_rejection`). A broker/
    external cancel (`cancelled_continue`) or any other non-reject
    signal resets the counter — only BACK-TO-BACK price rejections
    count as "consecutive".

    Returns (done, new_current_order_id, new_consecutive_price_rejections).
    done=True means the caller should immediately return `result`
    (terminal outcome — `result` has already been mutated by this call
    when the hold path fired).
    """
    if signal in ("filled", "killed", "rejected"):
        return True, current_order_id, 0
    if signal == "rejected_continue":
        if consecutive_price_rejections >= 1 and result is not None:
            await _ch_hold_on_repeated_rejection(
                result, account, symbol, transaction_type, quantity,
                current_order_id, cfg, algo_order_id, emit or (lambda *a, **k: None),
                agent_id=agent_id,
            )
            return True, current_order_id, 0
        backoff = cfg.rejection_backoff_seconds or cfg.interval_seconds
        logger.info(f"Chase {symbol}: backing off {backoff}s before next place_order")
        await asyncio.sleep(backoff)
        return False, None, consecutive_price_rejections + 1
    if signal == "cancelled_continue":
        backoff = cfg.rejection_backoff_seconds or cfg.interval_seconds
        logger.info(f"Chase {symbol}: backing off {backoff}s before next place_order")
        await asyncio.sleep(backoff)
        return False, None, 0   # reset current_order_id; not a rejection chain
    return False, current_order_id, 0


async def _ch_handle_attempt_error(
    exc: Exception,
    consecutive_errors: int,
    attempt: int,
    symbol: str,
    account: str,
    transaction_type: str,
    quantity: int,
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    result: "ChaseResult",
    emit: Callable,
    algo_order_id: "int | None",
    agent_id: "int | None" = None,
    cumulative_filled: int = 0,
    current_order_filled: int = 0,
) -> "tuple[ChaseResult | None, int]":
    """Handle a broker exception in the chase loop.

    Classifies `exc` as recoverable or not (see `_ch_is_recoverable_error`)
    before deciding whether to retry. Non-recoverable errors (bad input,
    unsupported capability, invalid session) terminate the chase
    immediately — retrying them would just resend the same doomed
    request. Recoverable errors (network blips, rate limits, anything
    untyped) count toward `_MAX_CHASE_ERRORS` and retry with a fresh
    depth-derived price on the next loop iteration.

    Returns (abort_result, new_consecutive_errors).
    abort_result is non-None when the caller should immediately return it.
    """
    if not _ch_is_recoverable_error(exc):
        _reason = (
            "auth_invalid" if isinstance(exc, _BrokerAuthError) else
            "capability_unsupported" if isinstance(exc, _BrokerCapabilityError) else
            "input_rejected"
        )
        # 2026-10-23 fix #4b — broker fate here IS known (the broker
        # explicitly refused the request, or the session/capability is
        # invalid — nothing was ever accepted), so this stays
        # "chase_failed" -> REJECTED. But a still-live PREVIOUS order
        # (already cancel-confirmed earlier this iteration in the
        # common case) may be the one this exception is actually about
        # in a rarer case — attempt a best-effort cancel of it before
        # alerting, so the alert can state accurately whether anything
        # was left resting.
        _cancel_attempted = False
        _cancel_failed = False
        if current_order_id:
            _cancel_attempted = True
            try:
                await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
            except Exception:
                _cancel_failed = True
        abort_msg = f"Order rejected by broker ({_reason}): {exc}"
        if _cancel_attempted:
            abort_msg += (
                f" — cancel of order {current_order_id} "
                + ("FAILED; it may still be resting live." if _cancel_failed
                   else "succeeded.")
            )
        logger.error(
            f"Chase {symbol}: non-recoverable {type(exc).__name__} — "
            f"terminating immediately. {exc}"
        )
        result.status = ChaseStatus.FAILED
        result.detail = abort_msg
        emit("chase_failed", {"attempts": attempt, "error": str(exc), "reason": _reason})
        try:
            from backend.shared.helpers.alert_utils import send_order_failure_alert
            send_order_failure_alert(
                account=account, symbol=symbol, exchange=cfg.exchange,
                side=transaction_type, qty=quantity, mode="live", source="chase",
                agent_id=agent_id,
                error=abort_msg,
            )
        except Exception:
            pass
        # 2026-10-23 fix #2 — "write some marker" gap: fire the terminal
        # write whenever EITHER current_order_id OR algo_order_id is
        # available, not only when current_order_id is truthy. A
        # non-recoverable exception on attempt 1 (before any order was
        # ever placed) used to leave the row at OPEN with no trace.
        if current_order_id or algo_order_id is not None:
            try:
                await _emit_chase_terminal(
                    current_order_id or "", "chase_failed",
                    symbol, transaction_type, quantity,
                    attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
                )
            except Exception:
                pass
        return result, consecutive_errors

    consecutive_errors += 1
    # 2026-10-23 audit fix (O9) — this is a routine, auto-retried
    # recoverable error (network blip, rate limit, etc.), not a real
    # failure: the loop reprices and retries on the very next
    # iteration, and nothing here fires an operator alert of its own.
    # Logging it at ERROR made EVERY such retry match ERROR_AGENT's
    # blanket catch-all condition (`tag="error"` is auto-assigned to
    # any ERROR-or-above record by log_store.py, regardless of logger
    # name), firing a full telegram+ntfy alert for completely normal
    # repricing — a single low-tier chase could generate dozens of
    # these. Only genuine escalation (`_chase_abort_on_consecutive_errors`,
    # once `_MAX_CHASE_ERRORS` is reached) is actually action-required
    # and alerts explicitly. Downgraded to WARNING so it stays visible
    # in logs without false urgency.
    logger.warning(
        f"Chase {symbol}: attempt {attempt} recoverable error "
        f"({consecutive_errors}/{_MAX_CHASE_ERRORS} consecutive): {exc}"
    )
    emit("error", {"attempt": attempt, "error": str(exc)})
    if consecutive_errors >= _MAX_CHASE_ERRORS:
        abort = await _chase_abort_on_consecutive_errors(
            consecutive_errors, attempt, exc,
            account, symbol, transaction_type, quantity,
            current_order_id, cfg, result, emit, algo_order_id,
            agent_id=agent_id,
            cumulative_filled=cumulative_filled,
            current_order_filled=current_order_filled,
        )
        return abort, consecutive_errors
    # Rate-limit responses get a longer cool-off than a normal re-quote
    # interval so a burst of retries doesn't just re-trip the same
    # broker-side cooldown (mirrors registry.py's connection-layer
    # convention for the same situation).
    _backoff = max(cfg.interval_seconds, _CH_RATE_LIMIT_BACKOFF_SECONDS) \
        if isinstance(exc, _BrokerRateLimitError) else cfg.interval_seconds
    await asyncio.sleep(_backoff)
    return None, consecutive_errors


async def _ch_exhaust_max_attempts(
    result: "ChaseResult",
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    account: str, symbol: str, transaction_type: str,
    quantity: int, algo_order_id: "int | None", emit: Callable,
    agent_id: "int | None" = None,
    cumulative_filled: int = 0,
    current_order_filled: int = 0,
) -> "ChaseResult":
    """Handle max-attempts exhaustion: cancel live order, set FAILED/UNFILLED, schedule terminal.

    2026-09 council audit fix (risk lens) — this path previously fired
    NO operator-visible alert at all (only a log line + an internal
    panel `emit`), unlike its sibling `_chase_abort_on_consecutive_errors`
    which does call `send_order_failure_alert`. A chase exhausting its
    max attempts is exactly as actionable as a consecutive-error abort —
    the operator needs to know a chase gave up AND whether the final
    cancel attempt (also best-effort/swallowed here) may have left an
    order resting live with nothing watching it anymore.

    2026-10-23 chase robustness (fix #4) — the bare best-effort cancel is
    replaced with `_ch_final_cancel_and_confirm` (the same multi-read
    confirm loop the normal per-attempt cancel-and-replace uses), so the
    exhaustion alert can report the REAL cumulative filled quantity
    instead of always claiming "(no fill)", and so the final
    `AlgoOrder.filled_quantity`-adjacent bookkeeping reflects reality.
    """
    cancel_confirmed, cumulative_filled, current_order_filled, _avg = (
        await _ch_final_cancel_and_confirm(
            account, current_order_id, cfg, symbol, quantity,
            cumulative_filled, current_order_filled, algo_order_id,
        )
    )
    # 2026-10-23 fix (audit O2) — the final cancel-confirm read above can
    # reveal the order actually reached full fill in the race window
    # before exhaustion fired. The in-loop path
    # (`_ch_cancel_and_capture` -> `_ch_zero_remaining_fill`) already
    # applies this exact check; apply it here too so a late full fill is
    # recorded as FILLED (firing template-attach/auto-TP), not
    # UNFILLED — critical for Dhan/Groww, which lack reliable postback
    # self-healing.
    if quantity > 0 and cumulative_filled >= quantity:
        logger.info(
            f"Chase {symbol}: late full fill ({cumulative_filled}/{quantity}) "
            f"detected at max-attempts exhaustion — recording FILLED, not UNFILLED."
        )
        return _ch_zero_remaining_fill(
            result, symbol, transaction_type, quantity, cfg.max_attempts,
            current_order_id, algo_order_id, emit, _avg,
        )
    _cancel_failed = bool(current_order_id) and not cancel_confirmed
    if _cancel_failed:
        logger.warning(
            "Chase %s: final cancel of order %s could not be confirmed at "
            "max-attempts exhaustion — order may still be resting live.",
            symbol, current_order_id,
        )
    result.status = ChaseStatus.FAILED
    result.detail = f"Failed after {cfg.max_attempts} attempts ({cumulative_filled}/{quantity} filled)"
    if _cancel_failed:
        result.detail += (
            f" — final cancel of order {current_order_id} may have failed; "
            f"it could still be resting live."
        )
    emit("chase_failed", {"attempts": cfg.max_attempts, "cancel_failed": _cancel_failed,
                          "cumulative_filled": cumulative_filled})
    # 2026-10-23 audit fix (O9) — `send_order_failure_alert` below already
    # fires this abort's own operator alert. Without `self_alerted=True`,
    # this ERROR-level log line ALSO matches ERROR_AGENT's blanket
    # catch-all condition (any ERROR-or-above record, regardless of
    # logger/tag — see log_store.py:tags_for), double-alerting the same
    # exhaustion event. See event_agents.py:_error_agent_self_alerted.
    logger.error(
        f"Chase {symbol}: FAILED after {cfg.max_attempts} attempts "
        f"({cumulative_filled}/{quantity} filled)",
        extra={"self_alerted": True},
    )
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        _msg = (
            f"Chase failed after {cfg.max_attempts} attempts "
            f"({cumulative_filled}/{quantity} filled)"
        )
        if _cancel_failed:
            _msg += (
                f" — final cancel of order {current_order_id} may ALSO have "
                f"failed; manually verify the broker's order book."
            )
        send_order_failure_alert(
            account=account, symbol=symbol,
            exchange=cfg.exchange, side=transaction_type,
            qty=quantity, mode="live", source="chase",
            agent_id=agent_id,
            error=_msg,
            # 2026-10-23 fix #7 — exhaustion is an abandonment-class
            # alert; never let it be swallowed by dedup/market-hours gate.
            bypass_gates=True,
        )
    except Exception as _alert_exc:
        logger.warning(f"Chase {symbol}: max-attempts alert failed: {_alert_exc}")
    # Sprint 1b-i — chase_exhausted timeline event. Independent of the
    # `result.order_id` gate below (which only governs the terminal
    # chase_unfilled AlgoOrder-status transition) — exhaustion itself is
    # always worth recording on the row when one exists.
    await _ch_write_order_event(
        algo_order_id, "chase_exhausted",
        f"Chase exhausted after {cfg.max_attempts} attempts "
        f"(cancel_failed={_cancel_failed})",
        {"max_attempts": cfg.max_attempts, "cancel_failed": _cancel_failed},
    )
    # 2026-10-23 fix #2 — "write some marker" gap: fire even when
    # `result.order_id` was never set (e.g. every attempt hit the
    # `price <= 0: continue` branch without ever successfully placing an
    # order) as long as there's an AlgoOrder row to mark. Previously this
    # left such a row at OPEN forever, silently resurrectable by
    # `recover_live_chases()` on the next service restart.
    if result.order_id or algo_order_id is not None:
        try:
            await _emit_chase_terminal(
                result.order_id or "", "chase_unfilled",
                symbol, transaction_type, quantity,
                attempts=cfg.max_attempts, algo_order_id=algo_order_id,
            )
        except Exception:
            pass
    return result


async def _ch_abort_on_sync_race(
    result: "ChaseResult",
    account: str,
    current_order_id: "str | None",
    cfg: "ChaseConfig",
    algo_order_id: "int | None",
    symbol: str,
    transaction_type: str,
    quantity: int,
    emit: Callable,
) -> "ChaseResult":
    """Abort path for `_sync_algo_order_id`'s final-status race (2026-10
    audit fix). Mirrors `_ch_exhaust_max_attempts`'s cancel-then-FAILED
    shape: the order just placed can no longer be tracked by its
    AlgoOrder row (already finalized by a racing postback), so attempt a
    best-effort cancel of it before reporting FAILED — leaving it
    resting live would let it still fill with nothing watching it.
    `_sync_algo_order_id` has already logged CRITICAL and fired the
    operator alert naming the untracked order; this only adds the
    cancel attempt and the chase-loop-level result/emit/terminal.

    Also schedules the SAME `_ch_write_order_event` + `_emit_chase_terminal`
    fire-and-forget pair every other abort path in this module fires
    (`_ch_exhaust_max_attempts`, `_chase_abort_on_consecutive_errors`).
    Without this, `_CH_PRE_FILL_NET_QTY[algo_order_id]` (seeded at
    `chase_order()` entry) is never popped — a leaked entry — and
    `record_chase_terminal` never sees this chase end. The terminal
    write itself will hit `_chase_terminal_update_db`'s own final-status
    guard (the row is already REJECTED/FILLED) and correctly no-op the
    status mutation while still running the pre-fill-qty pop + agent
    bookkeeping that don't depend on the status write succeeding.
    """
    _cancel_failed = False
    if current_order_id:
        try:
            await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
        except Exception as _cancel_exc:
            _cancel_failed = True
            logger.warning(
                "Chase %s: best-effort cancel of untracked order %s failed "
                "after final-status race: %s — order may still be resting live.",
                symbol, current_order_id, _cancel_exc,
            )
    result.status = ChaseStatus.FAILED
    result.detail = (
        f"AlgoOrder #{algo_order_id} finalized mid-chase (racing postback) — "
        f"order {current_order_id} is untracked; aborting without further retries."
    )
    if _cancel_failed:
        result.detail += (
            f" Best-effort cancel of {current_order_id} may have ALSO failed; "
            f"manually verify the broker's order book."
        )
    emit("chase_failed", {
        "reason": "final_status_race",
        "order_id": current_order_id,
        "cancel_failed": _cancel_failed,
    })
    await _ch_write_order_event(
        algo_order_id, "error", result.detail,
        {"reason": "final_status_race", "order_id": current_order_id,
         "cancel_failed": _cancel_failed},
    )
    if current_order_id:
        import asyncio as _asyncio
        _asyncio.create_task(_emit_chase_terminal(
            current_order_id, "chase_failed",
            symbol, transaction_type, quantity,
            attempts=result.attempts, error=result.detail,
            algo_order_id=algo_order_id,
        ))
    return result


def _ch_make_emit(
    on_event: "Callable | None",
    account: str, symbol: str, transaction_type: str, quantity: int,
) -> Callable:
    """Return a fire-and-forget event emitter bound to the current chase context."""
    def emit(event_type: str, detail: dict = None) -> None:
        if on_event:
            try:
                on_event(event_type, {
                    "account": account, "symbol": symbol,
                    "transaction_type": transaction_type,
                    "quantity": quantity, **(detail or {}),
                })
            except Exception:
                pass
    return emit


# ── Sprint 1b-i: per-order timeline events (algo_order_events) ─────────
#
# `algo_order_id` is Optional throughout chase.py (legacy callers and
# some recovery paths never set it) — every write here is guarded so a
# None id never reaches `write_event` (which would otherwise become
# exactly the kind of poison row the event_queue per-row fallback exists
# to handle). Mirrors paper.py's `_paper_write_terminal_event` pattern.

async def _ch_write_order_event(
    algo_order_id: "int | None", kind: str, message: str,
    payload: dict | None = None,
) -> None:
    """Write one algo_order_events row for a chase lifecycle event.

    No-op when there's no AlgoOrder row to attach the event to.
    """
    if algo_order_id is None:
        return
    from backend.api.algo.order_events import write_event
    await write_event(algo_order_id, kind, message, payload)


async def _ch_write_chase_modify_event(
    algo_order_id: "int | None", attempt: int, transaction_type: str,
    remaining_qty: int, price: float, current_order_id: "str | None",
) -> None:
    """Write a chase_modify event for a cancel-and-replace attempt.

    Attempt 1 is the initial placement, not a replace of a prior resting
    order, so it's skipped — only attempt 2+ represents an actual
    cancel-and-replace.
    """
    if attempt <= 1:
        return
    await _ch_write_order_event(
        algo_order_id, "chase_modify",
        f"Chase attempt {attempt}: {transaction_type} {remaining_qty} @ {price}",
        {"order_id": current_order_id, "price": price, "attempt": attempt,
         "remaining_qty": remaining_qty},
    )


async def _ch_write_cancel_confirmed_event(
    algo_order_id: "int | None", current_order_id: "str | None",
    attempt: int, remaining_qty: int,
) -> None:
    """Write chase_cancel_confirmed once a cancel-and-replace's cancel is
    confirmed gone at the broker, just before the caller places the
    replacement order.
    """
    await _ch_write_order_event(
        algo_order_id, "chase_cancel_confirmed",
        f"Cancel confirmed for order {current_order_id} before replace "
        f"(attempt {attempt})",
        {"order_id": current_order_id, "attempt": attempt,
         "remaining_qty": remaining_qty},
    )


async def _ch_cancel_previous(
    account: str, current_order_id: "str | None",
    cfg: "ChaseConfig", symbol: str, attempt: int, emit: Callable,
) -> None:
    """Cancel the previous broker order (best-effort, never raises)."""
    if not current_order_id:
        return
    try:
        await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
        emit("order_cancelled", {"order_id": current_order_id, "attempt": attempt})
    except Exception as e:
        logger.warning(f"Chase {symbol}: cancel failed: {e}")


# 2026-09 council audit fix (risk lens) — statuses that mean the
# just-cancelled order is definitively no longer live/resting at the
# broker, whether or not our own cancel_order call is what put it there.
# COMPLETE/REJECTED are included alongside CANCELLED/EXPIRED because in
# either case the order is equally gone — the only status that means
# "may still be resting" is anything NOT in this set (most commonly
# OPEN/TRIGGER PENDING, or an empty/unreadable status dict).
_CH_CONFIRMED_GONE_STATUSES = frozenset({"CANCELLED", "EXPIRED", "COMPLETE", "REJECTED"})
_CH_CANCEL_CONFIRM_READS = 3
_CH_CANCEL_CONFIRM_WAIT_S = 1.0


async def _ch_capture_late_fill(
    account: str, order_id: str, cfg: "ChaseConfig", symbol: str,
    quantity: int, cumulative_filled: int, current_order_filled: int,
    algo_order_id: "int | None",
) -> "tuple[int, int, int, float, bool]":
    """C2 — poll the just-cancelled order's FINAL status and fold any
    fill that happened between the LAST regular poll and the cancel
    into `cumulative_filled`, before the next attempt sizes its
    replacement order off a stale `remaining_qty`.

    Deliberately called AFTER `_ch_cancel_previous` (not immediately
    before it, as literally specified) — a limit order's
    `filled_quantity` freezes the instant a cancel is confirmed (no
    further quantity can trade against a cancelled order), so reading
    status post-cancel closes the read/cancel race window entirely
    instead of merely narrowing it. This achieves the spec's stated
    goal ("a fill racing the cancel isn't lost") more robustly than a
    pre-cancel read would.

    2026-09 council audit fix (risk lens) — this same post-cancel status
    read is now ALSO used to verify the cancel actually landed, closing
    a real duplicate-order gap: `_ch_cancel_previous` swallows any
    `broker.cancel_order` exception with only a warning log, with no
    signal of whether the order is genuinely gone. Without this check,
    the caller would proceed to place a FRESH replacement order for the
    full `remaining_qty` regardless — if the old order was in fact still
    resting live at the broker (a silently-failed cancel), that produces
    TWO live orders for the same leg, each capable of filling
    independently, with the old one never polled or reconciled again
    once `current_order_id` moves to the new order. `cancel_confirmed`
    (the new 5th return value) is True only when the post-cancel status
    is genuinely terminal (`_CH_CONFIRMED_GONE_STATUSES`); False for any
    other status OR when the status read itself fails — fail-SAFE
    (assume it might still be resting) rather than fail-open, since a
    false "confirmed" here is exactly the duplicate-order risk being
    closed.

    Best-effort on the fill-capture side — never raises for that part; a
    failed poll just means the fill (if any) is picked up by whatever
    reconciliation already exists downstream (next `_task_performance`
    sweep). The confirmation signal degrades safely to False on the
    same failure rather than being silently skipped.

    Returns (cumulative_filled, current_order_filled, remaining_qty,
    avg_price, cancel_confirmed).
    """
    avg_price = 0.0
    cancel_confirmed = False
    try:
        status = await _run(_order_status, account, order_id)
        _raw = int((status or {}).get("filled_quantity", 0) or 0)
        filled_qty = _ch_reverse_translate_mcx_filled(cfg, symbol, account, _raw)
        avg_price = float((status or {}).get("average_price", 0) or 0)
        order_status_str = str((status or {}).get("status", "")).upper()
        cancel_confirmed = order_status_str in _CH_CONFIRMED_GONE_STATUSES
        if not cancel_confirmed:
            logger.warning(
                "Chase %s: cancel NOT confirmed for order %s — broker "
                "status is %r (not one of %s). Order may still be "
                "resting live.",
                symbol, order_id, order_status_str or "<unreadable>",
                sorted(_CH_CONFIRMED_GONE_STATUSES),
            )
        if filled_qty > current_order_filled:
            delta = filled_qty - current_order_filled
            cumulative_filled += delta
            current_order_filled = filled_qty
            logger.info(
                f"Chase {symbol}: captured late fill +{delta} on order {order_id} "
                f"after cancel-and-replace (cumulative {cumulative_filled}/{quantity})"
            )
            await _record_partial_fill(algo_order_id, cumulative_filled, avg_price, quantity)
    except Exception as e:
        logger.warning(
            "Chase %s: late-fill/cancel-confirmation capture failed for "
            "order %s: %s — treating cancel as UNCONFIRMED (fail-safe)",
            symbol, order_id, e,
        )
        cancel_confirmed = False
    remaining_qty = max(0, quantity - cumulative_filled)
    return cumulative_filled, current_order_filled, remaining_qty, avg_price, cancel_confirmed


async def _ch_final_cancel_and_confirm(
    account: str, order_id: "str | None", cfg: "ChaseConfig", symbol: str,
    quantity: int, cumulative_filled: int, current_order_filled: int,
    algo_order_id: "int | None",
) -> "tuple[bool, int, int, float]":
    """Best-effort cancel + multi-read confirm for an ABORT context
    OUTSIDE the normal per-attempt cancel-and-replace flow (exhaustion,
    consecutive-errors, stuck — fix #1/#2/#3/#4). Reuses the exact same
    cancel-previous + multi-read confirm loop the normal mid-chase path
    already runs (`_ch_cancel_previous` + `_ch_capture_late_fill`, the
    same constants `_CH_CANCEL_CONFIRM_READS` / `_CH_CANCEL_CONFIRM_WAIT_S`)
    instead of a bare fire-and-forget `_cancel_order` call that swallows
    its own exception and never tells the caller whether anything is
    still resting live.

    No-op (vacuously "confirmed" — nothing was resting, so there's
    nothing ambiguous) when `order_id` is falsy.

    Returns (cancel_confirmed, cumulative_filled, current_order_filled,
    avg_price).
    """
    if not order_id:
        return True, cumulative_filled, current_order_filled, 0.0
    _noop_emit: Callable = lambda *_a, **_k: None
    await _ch_cancel_previous(account, order_id, cfg, symbol, 0, _noop_emit)
    avg_price = 0.0
    cancel_confirmed = False
    for _confirm_try in range(_CH_CANCEL_CONFIRM_READS):
        cumulative_filled, current_order_filled, _remaining, avg_price, cancel_confirmed = (
            await _ch_capture_late_fill(
                account, order_id, cfg, symbol, quantity,
                cumulative_filled, current_order_filled, algo_order_id,
            )
        )
        if cancel_confirmed:
            break
        if _confirm_try + 1 < _CH_CANCEL_CONFIRM_READS:
            await asyncio.sleep(_CH_CANCEL_CONFIRM_WAIT_S)
    return cancel_confirmed, cumulative_filled, current_order_filled, avg_price


def _ch_already_filled_at_start(
    quantity: int, cumulative_filled: int, remaining_qty: int,
    result: "ChaseResult", symbol: str, transaction_type: str,
    algo_order_id: "int | None", emit: Callable,
    avg_price: float = 0.0,
) -> "ChaseResult | None":
    """C4 — pre-loop guard: True when recovery already started fully
    filled (nothing left to chase). Returns a finalized FILLED
    ChaseResult when so, else None (caller proceeds into the loop).

    `avg_price` must be the REAL recovered fill price (from
    `chase_order`'s `already_filled_price` param), not a placeholder —
    it gates whether `_emit_chase_terminal` fires the auto-TP/
    template-attach hooks (see `_ch_zero_remaining_fill`'s docstring)."""
    if remaining_qty > 0 or quantity <= 0:
        return None
    logger.info(
        f"Chase {symbol}: already fully filled ({cumulative_filled}/{quantity}) "
        f"at start — skipping chase."
    )
    return _ch_zero_remaining_fill(
        result, symbol, transaction_type, quantity,
        0, None, algo_order_id, emit, avg_price,
    )


async def _ch_build_cancel_unconfirmed_abort(
    result: "ChaseResult", symbol: str, account: str, transaction_type: str,
    quantity: int, remaining_qty: int, attempt: int,
    current_order_id: "str | None", algo_order_id: "int | None", emit: Callable,
    agent_id: "int | None" = None,
) -> "ChaseResult":
    """2026-09 council audit fix (risk lens) — abort path for a cancel
    that could not be confirmed. Fail-SAFE: never place a replacement
    order in this state, since the old order may still be live at the
    broker and a replacement would risk two simultaneous live orders for
    the same leg (up to 2x the intended position, silently). Fires a
    CRITICAL ntfy alert — this is exactly the scenario that needs a
    human to manually check the broker's order book, and prior to this
    fix neither this path nor its sibling (_chase_abort_on_consecutive_
    errors, _ch_exhaust_max_attempts) fired any operator-visible alert
    for a cancel failure.

    2026-10-23 chase robustness: (1) now `async` — the terminal write
    below is `await`ed (not fire-and-forget) so the CANCEL_FAILED status
    is guaranteed committed before `chase_order()`'s wrapper runs its
    item-8b backstop reconcile; a late fire-and-forget write landing
    AFTER that backstop could otherwise stomp a correctly-reconciled
    CANCELLED/FILLED status back to CANCEL_FAILED (CANCEL_FAILED is
    deliberately not in ALGO_ORDER_FINAL_STATUSES, so nothing else would
    block that overwrite). (2) outcome is the new `chase_cancel_failed`
    (-> AlgoOrder.status = CANCEL_FAILED), not `chase_failed` (->
    REJECTED) — the broker-side fate here is genuinely UNKNOWN, so
    REJECTED (which permanently blocks a later real fill from ever being
    recorded) is wrong. (3) marks this algo_order_id for the one-shot
    backstop reconcile.
    """
    result.status = ChaseStatus.FAILED
    result.order_id = current_order_id or ""
    result.detail = (
        f"Chase ABORTED — cancel not confirmed for order {current_order_id} "
        f"after attempt {attempt}. It may still be resting live at the "
        f"broker; no replacement order was placed to avoid a possible "
        f"duplicate. Manually check {account}'s order book for {symbol}."
    )
    logger.critical(
        "Chase %s: %s", symbol, result.detail,
        extra={
            "tags": ["chase"], "alert_event": "cancel_unconfirmed",
            # 2026-10-23 fix #4 — binary ntfy gate: this is an
            # action-required outcome (operator must manually verify the
            # broker's order book), so it forces urgent ntfy regardless
            # of the dispatching agent's own stored channel config. See
            # event_agents.py:_dispatch_agent.
            "action_required": True,
            "transaction_type": transaction_type, "symbol": symbol, "account": account,
            "order_id": current_order_id, "attempt": attempt,
            "quantity": quantity, "remaining_qty": remaining_qty,
            "agent_id": agent_id,
        },
    )
    emit("chase_cancel_unconfirmed", {
        "order_id": current_order_id, "attempt": attempt,
        "remaining_qty": remaining_qty,
    })
    if algo_order_id is not None:
        _ch_mark_ambiguous_abort(algo_order_id)
        try:
            await _emit_chase_terminal(
                current_order_id or "", "chase_cancel_failed",
                symbol, transaction_type, quantity,
                attempts=attempt, algo_order_id=algo_order_id,
                error="cancel_unconfirmed — possible resting duplicate order",
            )
        except Exception:
            pass
    return result


async def _ch_cancel_and_capture(
    account: str, current_order_id: "str | None", cfg: "ChaseConfig",
    symbol: str, attempt: int, emit: Callable,
    quantity: int, remaining_qty: int,
    cumulative_filled: int, current_order_filled: int,
    algo_order_id: "int | None", result: "ChaseResult", transaction_type: str,
    agent_id: "int | None" = None,
) -> "tuple[int, int, int, ChaseResult | None]":
    """Cancel the previous resting order (if any) and capture any fill
    that landed in the race window before the cancel confirmed (C2).

    Returns (cumulative_filled, current_order_filled, remaining_qty,
    early_result). `early_result` is non-None when the caller must
    immediately `return early_result` — either because the late-fill
    capture revealed `cumulative_filled` already reached `quantity` (so
    there's nothing left to place a replacement order for), or (2026-09
    council audit fix) because the cancel itself could not be confirmed
    and the chase has been safely aborted rather than risk placing a
    duplicate live order.
    """
    await _ch_cancel_previous(account, current_order_id, cfg, symbol, attempt, emit)
    if not current_order_id:
        return cumulative_filled, current_order_filled, remaining_qty, None
    # A cancel can land at the broker a moment after the cancel call returns,
    # so a single status read may not show the final state yet. Re-read a few
    # times before treating the cancel as unconfirmed. Re-reading is safe: the
    # late-fill capture only adds fills it has not already counted.
    for _confirm_try in range(_CH_CANCEL_CONFIRM_READS):
        cumulative_filled, current_order_filled, remaining_qty, late_avg_price, cancel_confirmed = (
            await _ch_capture_late_fill(
                account, current_order_id, cfg, symbol, quantity,
                cumulative_filled, current_order_filled, algo_order_id,
            )
        )
        if cancel_confirmed or remaining_qty <= 0:
            break
        if _confirm_try + 1 < _CH_CANCEL_CONFIRM_READS:
            await asyncio.sleep(_CH_CANCEL_CONFIRM_WAIT_S)
    if remaining_qty > 0 and not cancel_confirmed:
        early = await _ch_build_cancel_unconfirmed_abort(
            result, symbol, account, transaction_type, quantity,
            remaining_qty, attempt, current_order_id, algo_order_id, emit,
            agent_id=agent_id,
        )
        return cumulative_filled, current_order_filled, remaining_qty, early
    if remaining_qty <= 0:
        early = _ch_zero_remaining_fill(
            result, symbol, transaction_type, quantity, attempt,
            current_order_id, algo_order_id, emit, late_avg_price,
        )
        return cumulative_filled, current_order_filled, remaining_qty, early
    # Reaching here means current_order_id was set AND cancel_confirmed
    # was True (both abort branches above already returned otherwise) —
    # i.e. the cancel genuinely landed and the caller is about to place
    # the replacement order.
    #
    # 2026-10-23 chase robustness (fix #6) — flag the just-superseded id
    # BEFORE the caller places the replacement order (the exact point
    # that closes the original duplicate-open-order-card race: by the
    # time the new order is visible broker-side, the old id is already
    # recorded here).
    await _ch_append_superseded_id(algo_order_id, current_order_id)
    await _ch_write_cancel_confirmed_event(
        algo_order_id, current_order_id, attempt, remaining_qty,
    )
    return cumulative_filled, current_order_filled, remaining_qty, None


def _ch_zero_remaining_fill(
    result: "ChaseResult", symbol: str, transaction_type: str, quantity: int,
    attempt: int, current_order_id: "str | None", algo_order_id: "int | None",
    emit: Callable, avg_price: float,
) -> "ChaseResult":
    """C2/C4 — finalize the chase as FILLED when `cumulative_filled` has
    reached `quantity` OUTSIDE the normal broker COMPLETE-status path:
    either the post-cancel late-fill capture revealed the previous
    order actually finished in the race window before the cancel
    landed, or (C4) a service-restart recovery started already fully
    filled (no order needs to be — or safely CAN be — placed for a
    zero/negative remaining quantity).

    Mirrors `_ch_poll_handle_complete` so the AlgoOrder row + terminal
    event + downstream Auto-TP / template-attach hooks fire identically
    regardless of which code path detected the fill.
    """
    result.status = ChaseStatus.FILLED
    result.fill_price = avg_price
    result.slippage = abs(avg_price - result.initial_price) * quantity
    result.detail = f"Filled at {avg_price} in {attempt} attempts"
    emit("order_filled", {
        "order_id": current_order_id, "fill_price": avg_price,
        "attempts": attempt, "slippage": result.slippage,
    })
    logger.info(
        f"Chase {symbol}: FILLED @ {avg_price} (attempt {attempt}, "
        f"detected via post-cancel/recovery reconciliation — no new order placed)"
    )
    asyncio.create_task(_emit_chase_terminal(
        current_order_id or "", "chase_fill",
        symbol, transaction_type, quantity,
        final_price=avg_price, attempts=attempt,
        slippage=result.slippage, algo_order_id=algo_order_id,
    ))
    return result


async def _ch_post_replace_kill_check(
    account: str, current_order_id: "str | None",
    cfg: "ChaseConfig", result: "ChaseResult",
    symbol: str, transaction_type: str, quantity: int,
    attempt: int, algo_order_id: "int | None", emit: Callable,
) -> bool:
    """Check and handle an operator-kill race, both immediately after a
    cancel-and-replace (called right after `place_order`, `current_order_id`
    always set) AND at the top of every loop iteration via `_ch_loop_guards`
    (`current_order_id` may be `None`/empty on attempt 1, before anything
    has been placed yet).

    2026-10-23 Kill-lifecycle fix — checks BOTH the broker_order_id-keyed
    signal (`is_killed`, the pre-existing one-shot check) and the new
    algo-order-id-keyed signal (`is_order_killed`). The broker_order_id
    check alone misses the case where the operator's Kill click read a
    STALE broker_order_id (chase had already replaced it, concurrently,
    by the time the broker cancel call landed) — `is_order_killed` is set
    by `kill_chase` regardless of which broker_order_id it read, so it
    still fires here even when the id-keyed check can't match.

    Returns True when the kill was detected and the caller should
    `return result`. Returns False (normal path) when no kill is pending.
    """
    if not (
        is_killed(current_order_id or "")
        or (algo_order_id is not None and is_order_killed(algo_order_id))
    ):
        return False
    logger.info(
        f"Chase {symbol}: operator-kill detected for "
        f"broker_order_id={current_order_id or '(none placed yet)'}. "
        f"Cancelling + terminating."
    )
    _cancel_confirmed = True
    if current_order_id:
        try:
            await _run(_cancel_order, account, current_order_id, cfg.variety, cfg.exchange)
        except Exception as _ke:
            logger.warning(f"Chase {symbol}: post-replace kill cancel failed: {_ke}")
            _cancel_confirmed = False
    if not _cancel_confirmed:
        # 2026-10-23 Kill-lifecycle fix — a failed cancel here is the
        # SAME ambiguity class as every other unconfirmed-cancel abort
        # in this file (see `_ch_cancel_and_capture`'s identical
        # pattern): the order may still be resting live at the broker.
        # Reporting CANCELLED unconditionally would recreate, inside
        # the chase loop itself, the exact false-success bug this fix
        # closed at the `kill_chase` HTTP response layer. Write
        # CANCEL_FAILED (non-final — a late fill still applies) and
        # mark for the item-8b backstop reconcile instead.
        result.status = ChaseStatus.FAILED
        result.detail = (
            f"Kill requested but broker cancel for {current_order_id} could "
            f"not be confirmed — it may still be resting live. Manually "
            f"check {account}'s order book for {symbol}."
        )
        emit("chase_cancel_unconfirmed", {
            "order_id": current_order_id, "attempt": attempt,
            "reason": "operator-kill-cancel-failed",
        })
        if algo_order_id is not None:
            _ch_mark_ambiguous_abort(algo_order_id)
            try:
                await _emit_chase_terminal(
                    current_order_id or "", "chase_cancel_failed",
                    symbol, transaction_type, quantity,
                    attempts=attempt, algo_order_id=algo_order_id,
                    error="Killed by operator — cancel unconfirmed",
                )
            except Exception:
                pass
        return True
    result.status = ChaseStatus.CANCELLED
    result.detail = "Killed by operator (post-replace race)"
    emit("order_cancelled", {
        "order_id": current_order_id, "attempt": attempt,
        "reason": "operator-kill-post-replace",
    })
    import asyncio as _asyncio
    _asyncio.create_task(_emit_chase_terminal(
        current_order_id or "", "chase_cancelled",
        symbol, transaction_type, quantity,
        attempts=attempt, algo_order_id=algo_order_id,
        error="Killed by operator",
    ))
    return True


# ── 2026-10-23 chase robustness: stuck/timeout detection (fix #3) + ─────
# external-final-status drift check (fix #8a). Kept as small, standalone
# `_ch_*` helpers composed through ONE combined call site
# (`_ch_loop_guards`) so `_chase_order_impl`'s own branch count grows by
# exactly one `if` for both checks together (CC gate).

def _ch_stuck_ceiling_seconds(cfg: "ChaseConfig") -> float:
    """Wall-clock ceiling for the stuck-detector, derived from `cfg` (not
    a fixed constant) so it is always strictly LESS than the full
    `max_attempts x interval_seconds` budget `_ch_exhaust_max_attempts`
    would eventually hit on its own — this fires FIRST, as an earlier,
    more specific "stuck" signal for a chase making no real progress,
    instead of silently grinding through recoverable-rejection retries /
    cancelled_continue / price<=0-continue loops for the chase's entire
    attempt budget before anyone is told anything. Floor of 60s so a
    fast/aggressive HIGH-tier config (e.g. interval=10s) doesn't trip
    this on perfectly normal early-chase latency.
    """
    return max(60.0, cfg.max_attempts * cfg.interval_seconds * 0.5)


async def _ch_check_external_final_status(algo_order_id: "int | None") -> "str | None":
    """Fix #8a — cheap, read-only, UNLOCKED single-column re-read of the
    AlgoOrder row's OWN status, run at the top of every attempt
    iteration. Catches drift an external writer (a postback that landed
    ahead of this chase's own next poll, or an admin reconcile) may have
    introduced since the chase loop's local state was last synced —
    closing the gap where the chase would otherwise place a FRESH
    cancel-and-replace order AFTER the row was already finalized
    elsewhere, instead of only detecting that race post-hoc at the next
    `_sync_algo_order_id` call (`_ch_abort_on_sync_race`).

    Returns the row's current status string when it's ALREADY in
    `ALGO_ORDER_FINAL_STATUSES`; `None` otherwise (the common case —
    nothing to react to). Fails open (returns `None`) on any DB error —
    a check that can't run must never itself abort a healthy chase.
    """
    if algo_order_id is None:
        return None
    try:
        from backend.api.models import ALGO_ORDER_FINAL_STATUSES
        async with _async_session() as _s:
            st = (await _s.execute(
                _sql_select(_AlgoOrder.status).where(_AlgoOrder.id == int(algo_order_id))
            )).scalar_one_or_none()
        return st if st in ALGO_ORDER_FINAL_STATUSES else None
    except Exception as _e:
        logger.debug("[CHASE] external-final-status check failed: %s", _e)
        return None


# Cadence for the DEEPER broker-truth check below — a real broker round
# trip (cache-backed, but still real work), so it runs less often than
# the zero-broker-call plain DB-status check above.
_CH_RECONCILE_EVERY_N_ATTEMPTS = 3


async def _ch_periodic_broker_truth_check(
    account: str, current_order_id: "str | None", algo_order_id: "int | None",
    attempt: int, symbol: str, cfg: "ChaseConfig", agent_id: "int | None" = None,
) -> "str | None":
    """Fix #8a, "during an active chase" trigger — every
    `_CH_RECONCILE_EVERY_N_ATTEMPTS` attempts, re-verify the CURRENTLY-
    tracked resting order against real broker truth via the canonical
    single-order-view accessor (`orders_helpers.py:get_broker_order_truth`
    / `apply_order_truth` — 2026-10-23 coordinator redirect: the SAME
    accessor the item-8b backstop below uses, not a second divergent
    implementation), beyond the normal per-attempt single-order poll
    (`_chase_poll_status`'s own `_order_status` call). Catches drift a
    missed postback or an external admin action introduced that the
    plain DB-status check (`_ch_check_external_final_status`) wouldn't
    see if nothing has written a new status yet.

    2026-10-23 audit fix (O5) — now routes through the shared
    `order_poll.py:reconcile_row_and_followup` wrapper (via its
    `owning_chase=True` kwarg — this check runs WHILE the chase is
    marked active for this very row via `_ch_mark_chase_active`, which
    spans the whole `chase_order()` retry loop, so the wrapper's normal
    `is_chase_active` no-op gate would otherwise ALWAYS short-circuit
    it; `owning_chase=True` skips that gate for exactly this one
    caller), instead of calling `apply_order_truth` directly, so a fill
    this check catches gets the same template-attach / ws-resubscribe
    treatment the watchdog and order-poll sweep already get.
    `fire_alert=False` is deliberate: an `order_correction` alert here
    would be a false alarm for what is actually a completely normal,
    self-resolving chase fill — chase's own `_emit_chase_terminal`/
    alert path (for the normal in-loop fill) is the correct
    notification channel for this outcome, not the generic corrector
    alert.

    Returns the resolved `AlgoOrder.status` string when
    `apply_order_truth` actually wrote one this call; `None` otherwise
    (not this attempt's turn to check, nothing resolved yet, or the
    check itself failed — fails open, same as its sibling).
    """
    if algo_order_id is None or not current_order_id:
        return None
    if attempt % _CH_RECONCILE_EVERY_N_ATTEMPTS != 0:
        return None
    try:
        from types import SimpleNamespace
        from backend.api.algo.order_poll import reconcile_row_and_followup
        _row = SimpleNamespace(
            id=algo_order_id, broker_order_id=current_order_id,
            status="OPEN", account=account, symbol=symbol,
            exchange=cfg.exchange, agent_id=agent_id,
        )
        truth = await reconcile_row_and_followup(
            _row, source="chase-periodic", fire_alert=False, owning_chase=True,
        )
        return truth.algo_status if truth is not None else None
    except Exception as _e:
        logger.debug("[CHASE] periodic broker-truth check failed: %s", _e)
        return None


async def _ch_external_final_status_result(
    ext_status: str, algo_order_id: int, symbol: str, transaction_type: str,
    quantity: int, attempt: int, result: "ChaseResult", emit: Callable,
) -> "ChaseResult":
    """Build a `ChaseResult` reflecting a row ALREADY finalized — either
    by some OTHER writer entirely (fix #8a's plain DB-status check: a
    postback or admin reconcile raced ahead of this chase's own poll —
    no write happens here, that writer already wrote it including its
    own ledger pipeline), or by THIS chase's own periodic broker-truth
    check (`_ch_periodic_broker_truth_check`, which just wrote the
    status itself via `apply_order_truth`). Either way, no NEW write or
    alert happens in this function — it only builds the in-memory
    result so the loop can stop cleanly before placing another order
    against an already-decided row.
    """
    if ext_status == "FILLED":
        fill_price = 0.0
        try:
            async with _async_session() as _s:
                row = (await _s.execute(
                    _sql_select(_AlgoOrder).where(_AlgoOrder.id == int(algo_order_id))
                )).scalar_one_or_none()
                if row is not None:
                    fill_price = float(row.fill_price or 0)
        except Exception:
            pass
        result.status = ChaseStatus.FILLED
        result.fill_price = fill_price
        result.detail = (
            "Chase stopped — AlgoOrder row already FILLED by another "
            "writer (postback or admin reconcile raced ahead of this "
            "chase's own poll)."
        )
        emit("order_filled", {"fill_price": fill_price, "attempts": attempt,
                              "reason": "external_final_status"})
        logger.info(
            "[CHASE] %s: stopping — AlgoOrder #%s already FILLED by "
            "another writer.", symbol, algo_order_id,
        )
        return result
    result.status = ChaseStatus.FAILED
    result.detail = f"Chase stopped — AlgoOrder row already {ext_status} by another writer"
    emit("chase_failed", {"attempts": attempt, "reason": "external_final_status",
                          "external_status": ext_status})
    logger.warning(
        "[CHASE] %s: stopping — AlgoOrder #%s already %s by another writer.",
        symbol, algo_order_id, ext_status,
    )
    return result


async def _ch_abort_stuck(
    result: "ChaseResult", account: str, current_order_id: "str | None",
    cfg: "ChaseConfig", symbol: str, transaction_type: str, quantity: int,
    cumulative_filled: int, current_order_filled: int,
    algo_order_id: "int | None", emit: Callable, attempt: int,
    elapsed: float, ceiling: float, agent_id: "int | None" = None,
) -> "ChaseResult":
    """Fix #3B — abort when the chase has made NO real progress (no
    fill, no confirmed cancel-and-replace cycle) for longer than
    `_ch_stuck_ceiling_seconds(cfg)`. Reuses `_ch_final_cancel_and_confirm`
    so a resting PREVIOUS order (e.g. a run of `price<=0: continue`
    iterations never even reaches the normal cancel-and-replace step) is
    actually cancelled and verified, not just silently abandoned.

    Outcome: `CANCEL_FAILED` (ambiguous, marked for the item-8b backstop)
    when a resting order couldn't be confirmed gone; `UNFILLED` (not
    ambiguous — confirmed-gone, matches the exhaustion path's own
    convention) otherwise.
    """
    cancel_confirmed, cumulative_filled, current_order_filled, _avg = (
        await _ch_final_cancel_and_confirm(
            account, current_order_id, cfg, symbol, quantity,
            cumulative_filled, current_order_filled, algo_order_id,
        )
    )
    # 2026-10-23 fix (audit O2) — same late-full-fill check as
    # `_ch_exhaust_max_attempts` (see its comment for the full rationale):
    # the final cancel-confirm read above can reveal the order actually
    # reached full fill before the stuck-abort fired.
    if quantity > 0 and cumulative_filled >= quantity:
        logger.info(
            f"Chase {symbol}: late full fill ({cumulative_filled}/{quantity}) "
            f"detected at stuck-abort — recording FILLED, not UNFILLED."
        )
        return _ch_zero_remaining_fill(
            result, symbol, transaction_type, quantity, attempt,
            current_order_id, algo_order_id, emit, _avg,
        )
    abort_msg = (
        f"Chase stuck — no fill/confirmed-cancel progress for "
        f"{elapsed:.0f}s (ceiling {ceiling:.0f}s) across {attempt} "
        f"attempts so far ({cumulative_filled}/{quantity} filled)."
    )
    _ambiguous = bool(current_order_id) and not cancel_confirmed
    if _ambiguous:
        abort_msg += (
            f" Cancel of order {current_order_id} could NOT be "
            f"confirmed; it may still be resting live at the broker."
        )
    logger.error(f"Chase {symbol}: {abort_msg}")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_stuck", {"attempts": attempt, "elapsed_s": elapsed, "ceiling_s": ceiling,
                        "cancel_confirmed": cancel_confirmed})
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            agent_id=agent_id, error=abort_msg,
            # Fix #7 — stuck is an abandonment-class alert; never let it
            # be swallowed by dedup/market-hours suppression.
            bypass_gates=True,
        )
    except Exception:
        pass
    _outcome = "chase_cancel_failed" if _ambiguous else "chase_unfilled"
    if current_order_id or algo_order_id is not None:
        if _ambiguous and algo_order_id is not None:
            _ch_mark_ambiguous_abort(algo_order_id)
        try:
            await _emit_chase_terminal(
                current_order_id or "", _outcome,
                symbol, transaction_type, quantity,
                attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
            )
        except Exception:
            pass
    return result


async def _ch_abort_ambiguous_place_timeout(
    result: "ChaseResult", account: str, symbol: str, transaction_type: str,
    quantity: int, cfg: "ChaseConfig", algo_order_id: "int | None",
    emit: Callable, attempt: int, agent_id: "int | None" = None,
) -> "ChaseResult":
    """Fix #3A-2 — a `_place_order` call that timed out (see `_run`'s
    wrap) is NOT a generic recoverable error: the underlying worker
    thread may still be running and the order may actually land, with
    its real `order_id` discarded by the timeout. Retrying via the
    normal recoverable-error path would risk a SECOND live order on top
    of a possibly-already-landed first one. Hard stop — no retry,
    always `CANCEL_FAILED` (genuinely unknowable), always marked for the
    item-8b backstop (which itself no-ops safely here since there is no
    broker_order_id to check against).
    """
    # 2026-10-23 audit fix (O11) — this message flows unmasked into
    # `send_order_failure_alert(error=abort_msg, ...)` below, which
    # (via `order_failure_messages`'s `error_short`) reaches the
    # operator-facing telegram/ntfy/email alert text verbatim —
    # `alert_utils.py` never passes `error` through
    # `mask_account_in_text`, unlike `_render_chase_cancel`'s own body,
    # which does. Mask here at the source.
    from backend.shared.helpers.utils import mask_account
    _masked_acct = mask_account(account)
    abort_msg = (
        f"Chase ABORTED — placing order (attempt {attempt}) timed out "
        f"after {_CH_BROKER_CALL_TIMEOUT_S}s. The broker call MAY have "
        f"landed with an order_id we never received. No retry was "
        f"attempted, to avoid risking a second live order. Manually "
        f"check {_masked_acct}'s order book for {symbol} for an untracked "
        f"fresh order."
    )
    logger.critical("Chase %s: %s", symbol, abort_msg)
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {"attempts": attempt, "reason": "place_timeout"})
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            agent_id=agent_id, error=abort_msg, bypass_gates=True,
        )
    except Exception:
        pass
    if algo_order_id is not None:
        _ch_mark_ambiguous_abort(algo_order_id)
        try:
            await _emit_chase_terminal(
                "", "chase_cancel_failed",
                symbol, transaction_type, quantity,
                attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
            )
        except Exception:
            pass
    return result


async def _ch_loop_guards(
    *,
    algo_order_id: "int | None", attempt: int, last_progress_mono: float,
    cfg: "ChaseConfig", account: str, symbol: str, transaction_type: str,
    quantity: int, current_order_id: "str | None",
    cumulative_filled: int, current_order_filled: int,
    result: "ChaseResult", emit: Callable, agent_id: "int | None" = None,
) -> "ChaseResult | None":
    """Combined per-iteration guard (fix #3B stuck check + fix #8a's two
    reconcile checks), run at the TOP of every attempt iteration, kept
    in ONE function/ONE call site so `_chase_order_impl`'s own branch
    count grows by exactly one `if` for three otherwise-unrelated safety
    checks (CC gate).

    Returns a finalized `ChaseResult` when any check decides the chase
    must stop now; `None` (the common case) to proceed normally.
    """
    # 2026-10-23 Kill-lifecycle fix — check the operator-kill signal
    # FIRST, before any other guard, so a kill that lands while the
    # chase is sleeping between attempts (no cancel-and-replace in
    # flight right now) is honored at the very next iteration instead
    # of only being caught by the narrower post-replace check. Reuses
    # `_ch_post_replace_kill_check` (handles both the `current_order_id`
    # present and `None`/not-yet-placed cases, and the cancel-failed
    # ambiguous-abort path) rather than duplicating its cancel/emit/
    # terminal logic here.
    if await _ch_post_replace_kill_check(
        account, current_order_id, cfg, result, symbol, transaction_type,
        quantity, attempt, algo_order_id, emit,
    ):
        return result
    ext_status = await _ch_check_external_final_status(algo_order_id)
    if ext_status is None:
        # Deeper, less-frequent broker-truth check (every
        # _CH_RECONCILE_EVERY_N_ATTEMPTS attempts) — catches drift the
        # cheap DB-status check above can't see because nothing has
        # written a new status yet.
        ext_status = await _ch_periodic_broker_truth_check(
            account, current_order_id, algo_order_id, attempt,
            symbol, cfg, agent_id=agent_id,
        )
    if ext_status is not None:
        return await _ch_external_final_status_result(
            ext_status, algo_order_id, symbol, transaction_type, quantity,
            attempt, result, emit,
        )
    ceiling = _ch_stuck_ceiling_seconds(cfg)
    elapsed = _time.monotonic() - last_progress_mono
    if elapsed > ceiling:
        return await _ch_abort_stuck(
            result, account, current_order_id, cfg, symbol, transaction_type,
            quantity, cumulative_filled, current_order_filled, algo_order_id,
            emit, attempt, elapsed, ceiling, agent_id=agent_id,
        )
    return None


async def chase_order(
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    cfg: ChaseConfig | None = None,
    on_event: Callable | None = None,
    algo_order_id: int | None = None,
    already_filled: int = 0,
    already_filled_price: float = 0.0,
    agent_id: int | None = None,
) -> ChaseResult:
    """Chase a limit order until filled — public entry point.

    Thin wrapper around `_chase_order_impl` (the actual loop, unchanged
    below) that marks `algo_order_id` as "actively chasing" in this
    process for the duration of the call (`is_chase_active()`), so the
    postback handlers (`orders_postback.py`) and the `/chases/active`
    reconcile sweep (`orders.py`) know to withhold a non-FILLED terminal
    status write for this row while it's still in here. See the
    `_ACTIVE_CHASE_REFCOUNT` module-level docstring above for the full
    rationale (2026-10 fix). The `finally` guarantees the marker clears
    on every return/exception path, including every early-return branch
    inside `_chase_order_impl`.

    `agent_id` — the originating AlgoOrder row's `agent_id`, when the
    caller already has it in scope (e.g. an agent-fired place_order
    action passes `agent.id`). Threaded straight through to every
    failure alert fired inside the retry loop so it's labeled
    "Agent"/"Agent Bracket" instead of defaulting to "Manual" — see
    `alert_utils._classify_order_origin_label`. Callers with no agent
    context (ticket/basket placement, template-wing chase) omit it,
    correctly defaulting to None.
    """
    _ch_mark_chase_active(algo_order_id)
    try:
        return await _chase_order_impl(
            account, symbol, transaction_type, quantity,
            cfg=cfg, on_event=on_event, algo_order_id=algo_order_id,
            already_filled=already_filled, already_filled_price=already_filled_price,
            agent_id=agent_id,
        )
    finally:
        _ch_mark_chase_inactive(algo_order_id)
        # 2026-10-23 chase robustness (fix #8b) — immediately after an
        # ambiguous abort (cancel-unconfirmed / consecutive-errors /
        # stuck / place-timeout — every path that just wrote
        # CANCEL_FAILED because broker-side fate couldn't be confirmed),
        # make one follow-up broker-truth check to try to resolve the
        # ambiguity on the spot, instead of always leaving it for a
        # later periodic sweep or a manual operator reconcile trigger.
        # MUST run strictly after `_ch_mark_chase_inactive` above (the
        # reused reconcile logic withholds non-FILLED writes while
        # `is_chase_active()` is true) and after the ambiguous abort's
        # own terminal write, which is now `await`ed (not fire-and-
        # forget) by every path that marks `_CH_AMBIGUOUS_ABORT_IDS` —
        # otherwise a late fire-and-forget CANCEL_FAILED write could
        # stomp a correctly-reconciled CANCELLED/FILLED status back to
        # CANCEL_FAILED (CANCEL_FAILED is deliberately non-final).
        if _ch_pop_ambiguous_abort(algo_order_id):
            try:
                await _ch_backstop_reconcile_one(algo_order_id)
            except Exception as _bs_e:
                logger.debug(
                    "[CHASE] item-8b backstop reconcile raised for #%s "
                    "(swallowed — non-fatal): %s", algo_order_id, _bs_e,
                )


async def _chase_order_impl(
    account: str,
    symbol: str,
    transaction_type: str,
    quantity: int,
    cfg: ChaseConfig | None = None,
    on_event: Callable | None = None,
    algo_order_id: int | None = None,
    already_filled: int = 0,
    already_filled_price: float = 0.0,
    agent_id: int | None = None,
) -> ChaseResult:
    """
    Chase a limit order until filled.

    Args:
        account: Kite account ID (e.g. 'ZG0790')
        symbol: Trading symbol (e.g. 'NIFTY24APR25000CE')
        transaction_type: 'BUY' or 'SELL'
        quantity: Number of lots/shares — the TRUE ORIGINAL total size of
                  the chase. This never changes meaning regardless of
                  `already_filled`; it's what downstream template-attach
                  sizing, logging, and G1-style checks key off of.
        cfg: Chase configuration (defaults used if None)
        on_event: Optional callback(event_type: str, detail: dict) for real-time updates
        algo_order_id: Optional AlgoOrder row id — when set, the chase
                       loop syncs broker_order_id on every successful
                       re-place and passes the id into all
                       _emit_chase_terminal calls. Required for any
                       chased order that has a template attached
                       (Phase 0.5).
        already_filled: C4 — quantity already filled by a PRIOR chase
                       cycle before this call started (service-restart
                       recovery). Seeds the chase-wide cumulative-fill
                       counter so the FIRST attempt sizes its order at
                       `quantity - already_filled` instead of the full
                       original `quantity` — recovering a chase always
                       started fresh at full size pre-fix, risking a
                       full duplicate order alongside whatever was still
                       resting at the broker from before the restart.
        already_filled_price: C4 — average fill price for `already_filled`
                       (from the broker's live order-status read at
                       recovery time). Used ONLY when recovery starts
                       already fully filled (`already_filled >= quantity`)
                       — that path finalizes the chase immediately via
                       `_ch_already_filled_at_start`, and the price feeds
                       `_emit_chase_terminal`'s `final_price`, which gates
                       whether auto-TP/template-attach fire. A 0.0 here
                       silently skips attaching exits for an already-
                       filled recovery.

    Returns:
        ChaseResult with fill details
    """
    # FUTURE (not yet implemented): each call to chase_order() is its own
    # independent coroutine/task — multiple orders chase concurrently with
    # no coordination between them (see actions_live.py's
    # chase_close_positions, which explicitly fires one asyncio task per
    # position "so multiple positions close concurrently"). Nothing today
    # caps the AGGREGATE rate of cancel/replace actions this function's
    # concurrent instances send to the broker. A shared rate limiter
    # (token bucket, not a semaphore — the constraint is actions-per-SECOND,
    # not max-concurrent) acquired once per actual broker action (around
    # the cancel + replace calls below) would bound that aggregate rate
    # without touching each chase's own independent pricing/retry logic.
    # Relevant for two reasons: Kite's own API rate limits, and SEBI's
    # retail-algo framework auto-tagging any order stream exceeding ~10
    # orders/second as "algo" (see docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md
    # §9). Deliberately out of scope for Sprint 1b — logging only, no
    # behavior change — flagged here so it isn't lost.
    from backend.shared.helpers.utils import is_prod_branch
    if not is_prod_branch():
        logger.warning("[CHASE] chase_order called on non-prod branch — aborting")
        return ChaseResult(status=ChaseStatus.CANCELLED, detail="non-prod branch")

    if cfg is None:
        cfg = _chase_default_cfg()

    # C3 — Market-hours pre-flight: chase places LIMIT orders which the
    # broker rejects instantly when the exchange is closed. Fail fast here
    # to avoid burning all max_attempts against a closed market.
    # Uses _symbol_exchange_open (not is_any_segment_open) so an NFO chase
    # is blocked during the 09:00–09:15 MCX-only window. Lazy import avoids
    # the known circular dep with agent_engine.
    try:
        from backend.api.algo.agent_engine import (  # circular: lazy OK
            _symbol_exchange_open, _build_now_ctx, _prewarm_special_sessions_for_exchange,
        )
        # Pre-warm from this async context before the sync _build_now_ctx()
        # call below (2026-10-20 fix) — closes the cold-cache gap for this
        # call site specifically.
        await _prewarm_special_sessions_for_exchange(cfg.exchange)
        if not _symbol_exchange_open(cfg.exchange, _build_now_ctx()):
            logger.warning(
                "[CHASE] %s closed — chase not started for %s on %s",
                cfg.exchange, symbol, account,
            )
            return ChaseResult(
                account=account, symbol=symbol,
                transaction_type=transaction_type, quantity=quantity,
                status=ChaseStatus.FAILED,
                detail=f"{cfg.exchange} closed — chase not started",
            )
    except Exception as _mh_e:
        logger.warning("[CHASE] market-hours check failed (proceeding): %s", _mh_e)

    # Bug 1 fix (2026-09-30) — capture the PRE-fill net position now,
    # before the first order of this chase is placed, so the downstream
    # offsetting-position check (fired after a fill) can compare against
    # genuine pre-fill state instead of re-reading a post-fill snapshot.
    # No-op when algo_order_id is None (see _ch_seed_pre_fill_net_qty).
    await _ch_seed_pre_fill_net_qty(algo_order_id, account, cfg.exchange, symbol)

    result = ChaseResult(
        account=account, symbol=symbol,
        transaction_type=transaction_type, quantity=quantity,
    )

    emit = _ch_make_emit(on_event, account, symbol, transaction_type, quantity)
    current_order_id = None
    consecutive_errors = 0        # reset on any successful broker call
    # Operator instruction (2026-10): count BACK-TO-BACK price-shaped
    # REJECTED outcomes — a second one in a row holds the row instead of
    # placing a third attempt. Reset on any non-reject poll signal (see
    # `_ch_handle_poll_signal`).
    consecutive_price_rejections = 0
    last_placed_price: float = 0.0  # enforces minimum 1-tick movement per attempt

    # C2/C4 — cumulative_filled is the chase-WIDE running total across
    # every order placed this cycle (seeded from `already_filled` on
    # restart recovery). current_order_filled tracks how much of the
    # CURRENTLY resting order's own fill has already been folded into
    # cumulative_filled — reset to 0 every time a fresh order is placed.
    cumulative_filled    = max(0, int(already_filled or 0))
    current_order_filled = 0
    remaining_qty         = max(0, quantity - cumulative_filled)

    # C4 — recovery may have started already fully filled (the resting
    # order from before the restart finished in the gap between the
    # crash and this recovery running). Nothing to chase in that case;
    # finalize immediately rather than placing a 0-qty order
    # (BrokerInputError) or a full duplicate at `quantity`.
    _already_done = _ch_already_filled_at_start(
        quantity, cumulative_filled, remaining_qty,
        result, symbol, transaction_type, algo_order_id, emit,
        already_filled_price,
    )
    if _already_done is not None:
        return _already_done

    # 2026-10-23 chase robustness (fix #3B) — "real progress" = a fill
    # (full or partial) or a confirmed cancel-and-replace cycle
    # completing for a previously-resting order. Deliberately NOT
    # "_early is None", which is also true in the cancelled_continue/
    # nothing-was-resting case and would never fire. Checked against
    # `_ch_stuck_ceiling_seconds(cfg)` at the top of every iteration.
    _last_progress_mono = _time.monotonic()

    for attempt in range(1, cfg.max_attempts + 1):
        result.attempts = attempt
        result.status = ChaseStatus.CHASING

        # 2026-10-23 chase robustness (fix #3B + #8a) — one combined
        # per-iteration guard call (kept as ONE call site so this
        # function's own branch count doesn't grow by more than one
        # `if` for two unrelated safety checks — see the CC-gate note on
        # `_ch_loop_guards`'s own docstring).
        _guard_result = await _ch_loop_guards(
            algo_order_id=algo_order_id, attempt=attempt,
            last_progress_mono=_last_progress_mono, cfg=cfg,
            account=account, symbol=symbol, transaction_type=transaction_type,
            quantity=quantity, current_order_id=current_order_id,
            cumulative_filled=cumulative_filled, current_order_filled=current_order_filled,
            result=result, emit=emit, agent_id=agent_id,
        )
        if _guard_result is not None:
            return _guard_result

        _cum_before_iteration = cumulative_filled

        try:
            # Get market depth
            depth = await _run(_get_depth, account, cfg.exchange, symbol)
            price = _calc_limit_price(depth, transaction_type, attempt,
                                       cfg.aggression_step,
                                       exchange=cfg.exchange, symbol=symbol)

            if price <= 0:
                logger.warning(f"Chase {symbol}: no valid price from depth at attempt {attempt}")
                await asyncio.sleep(cfg.interval_seconds)
                continue

            # Minimum 1-tick progression guard — when the bid-ask spread is
            # 1–2 ticks wide, spread × aggression_step × 0.5 is sub-tick and
            # _calc_limit_price can return the same snapped price for several
            # consecutive attempts. Force at least 1-tick movement so the
            # operator sees real price progression in the chase panel.
            # Extracted to `_ch_apply_min_tick_progression` (2026-10) to
            # keep chase_order's own cyclomatic complexity within the
            # project's radon gate — no behavior change.
            price = _ch_apply_min_tick_progression(
                price, last_placed_price, attempt, transaction_type, cfg, symbol, depth,
            )

            if attempt == 1:
                result.initial_price = price

            # Cancel previous order + capture any fill that landed on it
            # between its last regular poll and the cancel (C2) — a
            # cancelled limit order's filled_quantity freezes once the
            # cancel confirms, so reading post-cancel closes the race
            # window rather than merely narrowing it.
            cumulative_filled, current_order_filled, remaining_qty, _early = (
                await _ch_cancel_and_capture(
                    account, current_order_id, cfg, symbol, attempt, emit,
                    quantity, remaining_qty, cumulative_filled, current_order_filled,
                    algo_order_id, result, transaction_type, agent_id=agent_id,
                )
            )
            if _early is not None:
                # The just-cancelled order actually finished in the race
                # window before our cancel landed — nothing left to
                # chase. Placing a 0-qty order here would trip a
                # BrokerInputError and incorrectly mark the chase FAILED
                # even though it's already complete.
                return _early

            # 2026-10-23 fix #3B — a confirmed cancel-and-replace cycle
            # completing for a PREVIOUSLY-resting order counts as real
            # progress (captured before `current_order_id` is overwritten
            # by the fresh placement below).
            _confirmed_cancel_this_iter = bool(current_order_id)

            # Place new order
            try:
                current_order_id = await _run(
                    _place_order, account, symbol, transaction_type, remaining_qty, price, cfg
                )
            except TimeoutError:
                # 2026-10-23 fix #3A-2 — a place-order call that timed out
                # is NOT a generic recoverable error: `_run`'s underlying
                # worker thread may still be running and the order may
                # actually land, with its real order_id discarded by the
                # timeout. Caught HERE (before the outer `except
                # Exception` below, which would otherwise treat an
                # untyped TimeoutError as recoverable-and-retry) so this
                # never places a second live order on top of a possibly-
                # already-landed first one. Hard stop, no retry.
                return await _ch_abort_ambiguous_place_timeout(
                    result, account, symbol, transaction_type, quantity, cfg,
                    algo_order_id, emit, attempt, agent_id=agent_id,
                )
            current_order_filled = 0  # fresh order — starts unfilled
            consecutive_errors = 0   # successful placement resets the error streak
            last_placed_price = price  # track for minimum-tick progression check
            result.order_id = current_order_id
            logger.info(f"Chase {symbol}: attempt {attempt}/{cfg.max_attempts} "
                        f"— {transaction_type} {remaining_qty} @ {price} (order {current_order_id})")
            emit("order_placed", {"order_id": current_order_id, "price": price, "attempt": attempt})
            # Phase 0.5 — keep the AlgoOrder row's broker_order_id in
            # lockstep with the chase loop's current order so the
            # terminal handler + postback handler + chase panel all
            # see the LATEST broker id, not the FIRST one.
            _synced = await _sync_algo_order_id(
                algo_order_id, current_order_id,
                current_limit=price,
                interval_seconds=cfg.interval_seconds,
                attempt=attempt,
            )
            if not _synced:
                # 2026-10 audit fix — the AlgoOrder row this chase is
                # driving already finalized to a terminal status (a
                # racing postback), so the order we JUST placed can no
                # longer be tracked by this row. Stop immediately —
                # placing further orders would only compound an
                # already-untracked situation. _sync_algo_order_id has
                # already logged CRITICAL and fired the operator alert.
                return await _ch_abort_on_sync_race(
                    result, account, current_order_id, cfg,
                    algo_order_id, symbol, transaction_type, quantity, emit,
                )
            # Sprint 1b-i — chase_modify timeline event, mirroring
            # paper.py's write_event pattern. No-op for attempt 1 (the
            # initial placement) and for a None algo_order_id.
            await _ch_write_chase_modify_event(
                algo_order_id, attempt, transaction_type,
                remaining_qty, price, current_order_id,
            )

            # Audit fix (C-2) — operator-kill race: re-check the NEW id
            # immediately after place_order so a kill in the replace window
            # takes effect before the sleep, not one iteration later.
            if await _ch_post_replace_kill_check(
                account, current_order_id, cfg, result,
                symbol, transaction_type, quantity, attempt, algo_order_id, emit,
            ):
                return result

            # Wait for fill
            await asyncio.sleep(cfg.interval_seconds)

            signal, remaining_qty, cumulative_filled, current_order_filled = await _chase_poll_status(
                account, current_order_id, cfg, symbol, transaction_type,
                quantity, result, attempt, remaining_qty, algo_order_id, emit,
                cumulative_filled, current_order_filled, agent_id=agent_id,
            )
            done, current_order_id, consecutive_price_rejections = await _ch_handle_poll_signal(
                signal, current_order_id, cfg, symbol,
                consecutive_price_rejections=consecutive_price_rejections,
                account=account, transaction_type=transaction_type, quantity=quantity,
                algo_order_id=algo_order_id, result=result, emit=emit,
                agent_id=agent_id,
            )
            if done:
                return result

            # 2026-10-23 fix #3B — update the stuck-detector's "last real
            # progress" clock when THIS iteration made any: a confirmed
            # cancel-and-replace cycle for a previously-resting order, or
            # a fill (full or partial) increasing `cumulative_filled`.
            if _confirmed_cancel_this_iter or cumulative_filled > _cum_before_iteration:
                _last_progress_mono = _time.monotonic()

        except Exception as e:
            abort, consecutive_errors = await _ch_handle_attempt_error(
                e, consecutive_errors, attempt, symbol,
                account, transaction_type, quantity, current_order_id,
                cfg, result, emit, algo_order_id, agent_id=agent_id,
                cumulative_filled=cumulative_filled,
                current_order_filled=current_order_filled,
            )
            if abort is not None:
                return abort

    return await _ch_exhaust_max_attempts(
        result, current_order_id, cfg,
        account, symbol, transaction_type, quantity, algo_order_id, emit,
        agent_id=agent_id,
        cumulative_filled=cumulative_filled,
        current_order_filled=current_order_filled,
    )
