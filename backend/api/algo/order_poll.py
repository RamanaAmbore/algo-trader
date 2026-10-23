"""
order_poll.py — shared "reconcile one order against broker truth, then
handle the side effects of a real correction" wrapper, plus the new
periodic "order poll" sweep.

2026-10-23 (order-poll consolidation plan) — the canonical single-
order-view accessor (`orders_helpers.py:get_broker_order_truth` /
`apply_order_truth`) already resolves broker truth and writes the AlgoOrder
row + FIFO ledger safely. What it deliberately does NOT do (see its own
docstring) is fire the template-attach / ws-resubscribe side effects a
real FILLED correction needs, or alert the operator. This module is that
missing layer, shared by every AUTOMATIC background corrector:

  - `background.py:_task_open_order_watchdog` (existing 60s-default backstop,
    migrated here from its own `_rco_reconcile_account` logic)
  - `_task_order_poll` below (new periodic sweep, independent of chase,
    covering every live OPEN/CANCEL_FAILED order — not just chase-driven
    ones)
  - `orders_postback.py`'s non-blocking follow-up verification (fires a
    few seconds after a postback's own fast direct-apply, to catch a
    wrong/incomplete event payload)

Deliberately NOT used by:
  - `orders.py:list_active_chases` — it calls `get_broker_order_truth` /
    `apply_order_truth` directly (plus its own template-attach fan-out)
    and stays alert-free by design. NOT because alerting would "spam"
    on a fast poll — `apply_order_truth` locks the row and no-ops once
    its status already matches, so one genuine correction produces at
    most one `True` regardless of poll cadence. The real reason: this
    endpoint only runs while some UI tab happens to have `/chases/active`
    open. If it alerted, whether an `order_correction` notification
    fires for a given stuck row would depend on whether an operator's
    browser tab was open at the right moment — an accidental, confusing
    dependency, not a deliberate alerting policy. The automatic
    background correctors below run unconditionally regardless of any
    UI, which is what alerting should depend on.
  - The two admin `/algo/reconcile` routes — operator-triggered;
    transparency is via the response payload (`updated`/`note`/
    `missing`), not an alert. An operator who just clicked Reconcile
    doesn't need a notification about their own action.

This module lives in `backend/api/algo/` (not `orders_helpers.py`, whose
own docstring forbids importing from `orders.py` / its route-layer
siblings — `_maybe_fire_template_attach_for_reconcile` and
`_subscribe_filled_pairs` both live there) and NOT in `orders.py` either,
since `background.py` (no HTTP/route context) needs to import it too.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account

logger = get_logger(__name__)


async def reconcile_row_and_followup(
    row,
    broker_status_by_id: "dict | None" = None,
    *,
    source: str = "order-poll",
    fire_alert: bool = True,
    owning_chase: bool = False,
) -> "object | None":
    """Reconcile one AlgoOrder *row* (any session state — only
    `.id` / `.broker_order_id` / `.status` / `.account` / `.symbol` /
    `.exchange` are read, all BEFORE any mutation) against the canonical
    accessor, with `withhold_if_active=True` (never finalizes a NON-FILL
    terminal status — CANCELLED/REJECTED/UNFILLED — for a row the chase
    engine is actively mid-retry on). On a genuine correction: fires
    template-attach + ws-resubscribe on a FILLED resolution (the side
    effects `apply_order_truth` deliberately omits — see its own
    docstring), and logs an `order_correction` alert record (urgent
    ntfy, action-required — `ORDER_CORRECTION_AGENT` in
    `event_agents.py`) unless `fire_alert=False`.

    Full no-op when `chase.py:is_chase_active(row.id)` is True — checked
    BEFORE touching broker truth at all, not merely deferred to
    `apply_order_truth`'s own withhold. `_pb_should_withhold_status`
    (what `withhold_if_active` delegates to) deliberately never
    withholds a FILLED resolution — "a fill is never ambiguous" is the
    correct rule for postback, which is instantaneous and authoritative.
    It is the WRONG rule for a background corrector that polls on a
    timer: a low-tier chase can run for ~15 minutes, well past the
    5-minute age cutoff every automatic caller here uses, so this
    function could otherwise race ahead of chase's OWN fill detection
    and apply FILLED first — firing a false `order_correction` alert
    (3x-repeat urgent ntfy) for what is actually a completely normal,
    self-resolving chase fill, not a genuine "something needed manual
    reconciliation" event. While a chase owns a row, every EXTERNAL
    caller of this function (the watchdog, the periodic order-poll
    sweep, postback's follow-up verification) defers entirely via this
    check.

    `owning_chase` (2026-10-23, audit fix O5) — set ONLY by chase's own
    periodic in-loop broker-truth check
    (`chase.py:_ch_periodic_broker_truth_check`), which runs WHILE this
    exact row is marked active (`chase.py`'s `_ch_mark_chase_active` /
    `_ch_mark_chase_inactive` pair spans the WHOLE retry loop, not just
    one check) — for that one caller, the row's own active chase IS the
    caller, so the `is_chase_active` deferral above would otherwise
    ALWAYS short-circuit it into a permanent no-op. When True: skips the
    `is_chase_active` early-return, and passes `withhold_if_active=False`
    to `apply_order_truth` — matching `_ch_periodic_broker_truth_check`'s
    own pre-existing call (it never passed `withhold_if_active`, i.e.
    the function's default of False), unchanged by this migration. Every
    other caller keeps `withhold_if_active=True`.

    The registry is in-process only; a service restart clears it, and
    `recover_live_chases()` (background.py) is the real safety net for
    an interrupted chase at that point, not this function.

    Returns the resolved `OrderTruth` when a write actually happened,
    `None` otherwise (unavailable, not yet terminal, withheld, id
    missing, chase-active, or lost a race to another writer) — `None`
    means nothing to report, not an error.
    """
    from backend.api.routes.orders_helpers import get_broker_order_truth, apply_order_truth

    broker_order_id = str(getattr(row, "broker_order_id", "") or "")
    if not broker_order_id:
        return None
    algo_order_id = getattr(row, "id", None)
    if algo_order_id is None:
        return None
    if not owning_chase:
        try:
            from backend.api.algo.chase import is_chase_active
            if is_chase_active(algo_order_id):
                return None
        except Exception:
            pass
    prior_status = str(getattr(row, "status", "") or "")

    truth = await get_broker_order_truth(broker_order_id, broker_status_by_id)
    if not truth.available or truth.algo_status is None:
        return None
    changed = await apply_order_truth(
        algo_order_id, truth, withhold_if_active=not owning_chase,
    )
    if not changed:
        return None

    await _opo_postprocess(
        algo_order_id,
        prior_status=prior_status,
        new_status=truth.algo_status,
        source=source,
        fire_alert=fire_alert,
        static_account=str(getattr(row, "account", "") or ""),
        static_symbol=str(getattr(row, "symbol", "") or ""),
        static_exchange=str(getattr(row, "exchange", "") or "NFO"),
        static_agent_id=getattr(row, "agent_id", None),
    )
    return truth


async def _opo_postprocess(
    algo_order_id: int,
    *,
    prior_status: str,
    new_status: str,
    source: str,
    fire_alert: bool,
    static_account: str,
    static_symbol: str,
    static_exchange: str,
    static_agent_id: "int | None" = None,
) -> None:
    """Side effects for a real correction `reconcile_row_and_followup`
    just applied. Re-fetches a fresh row (the caller's own `row` object
    predates the mutation, made in `apply_order_truth`'s own locked
    session, so its fill_price/filled_quantity/template fields are
    stale) before firing template-attach on a FILLED resolution.
    Best-effort throughout — a side-effect failure never un-does the
    already-committed status write.
    """
    try:
        if new_status == "FILLED":
            from sqlalchemy import select as _sel
            from backend.api.database import async_session
            from backend.api.models import AlgoOrder
            async with async_session() as s:
                fresh = (await s.execute(
                    _sel(AlgoOrder).where(AlgoOrder.id == algo_order_id)
                )).scalar_one_or_none()
            if fresh is not None:
                from backend.api.routes.orders_place import (
                    _maybe_fire_template_attach_for_reconcile,
                )
                _maybe_fire_template_attach_for_reconcile(fresh)
                from backend.api.routes.orders import _subscribe_filled_pairs
                await _subscribe_filled_pairs(
                    [(fresh.symbol, fresh.exchange)]
                )
    except Exception as _se:
        logger.warning(
            "[ORDER-POLL] post-correction side effects failed for "
            "AlgoOrder #%s (%s -> %s): %s",
            algo_order_id, prior_status, new_status, _se,
        )

    if not fire_alert:
        return
    try:
        from backend.shared.helpers.utils import config
        masked = mask_account(static_account) if static_account else ""
        logger.warning(
            "[ORDER-POLL] AlgoOrder #%s corrected %s -> %s (source=%s) "
            "%s %s %s",
            algo_order_id, prior_status or "?", new_status, source,
            masked, static_symbol, static_exchange,
            extra={
                "tags": ["orders"], "alert_event": "order_correction",
                "action_required": True,
                "algo_order_id": algo_order_id,
                "prior_status": prior_status or "?",
                "new_status": new_status, "source": source,
                "masked": masked, "symbol": static_symbol,
                "exchange": static_exchange,
                "agent_id": static_agent_id,
                "branch": config.get("deploy_branch", "main"),
            },
        )
    except Exception as _ae:
        logger.debug(f"[ORDER-POLL] alert log failed for #{algo_order_id}: {_ae}")


# ── Periodic "order poll" sweep ─────────────────────────────────────────

async def _order_poll_candidate_rows(cutoff: datetime) -> list:
    """Every live OPEN/CANCEL_FAILED AlgoOrder row older than `cutoff` —
    the two states that are actually broker-ambiguous (see the plan's
    own rationale: CANCELLED/UNFILLED/HELD are out of scope for this
    first version)."""
    from sqlalchemy import select as _sel
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    async with async_session() as s:
        rows = (await s.execute(
            _sel(AlgoOrder).where(
                AlgoOrder.mode == "live",
                AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"]),
                AlgoOrder.created_at < cutoff,
            )
        )).scalars().all()
    return list(rows)


def _order_poll_interval_seconds() -> int:
    """Cadence matches the existing 5-min "book poll" fill-detection
    backstop (`performance.refresh_interval` minutes — see
    `backend/api/background.py:_task_performance`'s own `_interval()`
    helper), not an invented new number. Mirrors that helper's EXACT
    DB → YAML → in-code-default read chain (`config.get(
    "performance_refresh_interval", 5)` as the YAML fallback, not a
    bare `5`) so a YAML-tuned prod cadence can't silently diverge
    between the two tasks. `backend_config.yaml` ships no
    `performance_refresh_interval` key as of this change, so both
    tasks currently resolve to the same in-code default (5 min) either
    way — but the read chain itself must still match. `orders.
    order_poll_seconds` overrides per-deploy if this sweep specifically
    needs a different cadence. Re-read every iteration so an
    `/admin/settings` change lands on the next tick, not after a
    restart."""
    from backend.shared.helpers.settings import get_int
    from backend.shared.helpers.utils import config
    _default_s = max(60, get_int(
        "performance.refresh_interval",
        config.get("performance_refresh_interval", 5),
    ) * 60)
    return max(60, get_int("orders.order_poll_seconds", _default_s))


async def _task_order_poll() -> None:
    """Periodic, chase-independent broker-truth reconciliation sweep —
    covers EVERY live OPEN/CANCEL_FAILED AlgoOrder, not just
    chase-driven ones (the operator's original ask: a backstop modeled
    on the 5-min "book poll", but broker-wide). Same `_supervised` /
    `on_startup` registration convention as every other periodic task
    (see `background.py`) — this coroutine itself is a bare infinite
    loop; the supervisor wrapper restarts it on an unhandled crash.

    Dev-idle gate mirrors `_task_performance`'s own `is_engine_idle()`
    check — a non-main branch with no operator-selected PAPER/SIM/
    REPLAY session skips the broker round-trip entirely.
    """
    from backend.shared.helpers.utils import is_engine_idle
    from backend.api.routes.orders_helpers import _broker_order_snapshot

    while True:
        interval_s = _order_poll_interval_seconds()
        await asyncio.sleep(interval_s)
        try:
            if is_engine_idle():
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            rows = await _order_poll_candidate_rows(cutoff)
            if not rows:
                continue
            broker_status_by_id = await _broker_order_snapshot()
            corrected = 0
            for r in rows:
                truth = await reconcile_row_and_followup(
                    r, broker_status_by_id, source="order-poll",
                )
                if truth is not None:
                    corrected += 1
            logger.info(
                "[ORDER-POLL] sweep scanned=%s corrected=%s",
                len(rows), corrected,
            )
        except Exception as exc:
            logger.warning("[ORDER-POLL] sweep iteration failed: %s", exc)
