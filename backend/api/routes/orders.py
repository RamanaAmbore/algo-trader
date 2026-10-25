"""
Orders endpoints.

GET  /api/orders/             — list all orders across all accounts (cached 15s)
POST /api/orders/ticket       — primary order-placement entry point. Routes by
                                mode (paper / live), records an AlgoOrder row,
                                supports chase + chase_aggressiveness. Every
                                frontend surface flows through this endpoint.
POST /api/orders/preflight    — pre-validate an order before it hits the broker.
                                Returns structured blockers (MARGIN_SHORTFALL,
                                SEGMENT_INACTIVE, QTY_FREEZE, ACCOUNT_UNKNOWN)
                                with actionable fix text. Does not place an order.
PUT  /api/orders/{order_id}   — modify an open order
DELETE /api/orders/{order_id} — cancel an open order
POST /api/orders/postback     — Kite postback: real-time order status updates
GET  /api/accounts/           — list accounts (masked display + unmasked ID for order form)
"""

import asyncio
import json
from datetime import datetime, timezone

from typing import Optional

import msgspec
from litestar import Controller, Request, delete, get, patch, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK

from backend.api.auth_guard import jwt_guard, auth_or_demo_guard, admin_guard, is_admin_request
from backend.api.cache import get_or_fetch, invalidate
from backend.api.routes.ws import broadcast
from backend.api.schemas import (
    AccountInfo,
    AccountsResponse,
    BasketMarginResponse,
    BasketOrderRequest,
    BasketOrderResponse,
    CancelAllOrdersRequest,
    CancelAllOrdersResponse,
    CancelOrderResponse,
    DraftOrderCreateResponse,
    DraftOrderPatchRequest,
    DraftOrderRequest,
    ModifyOrderRequest,
    ModifyOrderResponse,
    OrderRow,
    OrdersResponse,
    ReconcileSingleRequest,
    SpreadCheckResponse,
    TicketOrderRequest,
    TicketOrderResponse,
    TicketPreviewRequest,
    TicketPreviewResponse,
)
from backend.brokers.connections import Connections
from backend.shared.helpers.date_time_utils import timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, mask_account_in_text, secrets

logger = get_logger(__name__)


# ── Re-exports from split sub-modules ─────────────────────────────────────────
# Symbols below were extracted in the RED-zone split (orders.py 4322→<1500 LOC).
# They are imported here so that:
#   1. All existing callers of `from backend.api.routes.orders import X` keep
#      working without change (backward-compat shim).
#   2. The controller methods that reference these names in their bodies find
#      them in the module namespace as before.
#
# DO NOT remove these re-exports until all external callers have been updated.
from backend.brokers.adapters.kite import ensure_lot_index  # noqa: E402
from backend.api.routes.orders_helpers import (  # noqa: E402
    _VARIETIES,
    _ORDER_TYPES,
    _PRODUCTS,
    _TXN_TYPES,
    _EXCHANGES,
    _ORDERS_TTL,
    _REJECTION_TRACKER,
    _REJECTION_WINDOW_S,
    _REJECTION_THRESHOLD,
    _BREAKER_ALERT_COOLDOWN_S,
    _BREAKER_LAST_ALERT,
    _rejection_key,
    _prune_rejection_window,
    _rejection_count,
    _record_rejection,
    _clear_rejections,
    _maybe_send_breaker_alert,
    _TICK_INDEX,
    _TICK_INDEX_STAMP,
    _rebuild_tick_index,
    _align_price_to_tick,
    _broker_for,
    _live_chase_config,
    _start_live_chase,
    _row_from_dict,
    _fetch_orders,
    AlgoOrderEventInfo,
    AlgoOrderInfo,
    _fetch_child_order_ids,
    _resolve_target_pct,
    _ticket_overrides_dict,
    _build_overrides_json,
)
from backend.api.routes.orders_place import (  # noqa: E402
    _enforce_capacity_guard,
    _TEMPLATE_ATTACH_LOCKS,
    _TEMPLATE_ATTACH_META_LOCK,
    _TPL_LOCK_TTL_S,
    _get_template_attach_lock,
    _maybe_fire_template_attach_for_reconcile,
    _fire_template_attach_on_fill,
    _maybe_attach_template_to_ticket,
    _attach_basket_leg_template,
    _arm_take_profit,
)
from backend.api.routes.orders_postback import (  # noqa: E402
    _process_broker_postback,
)

import time as _time  # used by _postback_broadcast_fanout for ts fields


# ── Chase-reconcile helpers (extracted from list_active_chases, reconcile_algo_orders, retry_template) ─
#
# 2026-10-23 (order-poll consolidation) — the "has a broker_order_id"
# branch that used to live here (`_chase_process_live_row`'s broker-id
# path, plus the standalone bulk/per-card reconcile writers
# `_rco_reconcile_one_row` / `_rco_reconcile_account` /
# `_rco_apply_reconcile_status` / `_rco_reconcile_apply_target` /
# `_rco_fetch_broker_order`) is DELETED. All four callers
# (`list_active_chases`, the admin bulk `/algo/reconcile` sweep, the
# admin per-card `/{broker_order_id}/reconcile`, and
# `background.py:_task_open_order_watchdog`) now resolve broker truth
# via the single canonical accessor
# (`orders_helpers.py:get_broker_order_truth` / `apply_order_truth`),
# which is SAFE by construction on a missing/empty broker response
# ("unavailable, don't guess" — never a guessed UNFILLED), takes the
# row lock only for a row that actually needs writing, and writes the
# FIFO ledger on a confirmed fill. See `_rco_reconcile_live_row` /
# `_rco_reconcile_no_id_row` / `_rco_reconcile_paper_row` below for the
# per-mode wrappers `list_active_chases` composes; the sweep/watchdog
# use the shared `backend/api/algo/order_poll.py:reconcile_row_and_
# followup` wrapper instead (they also need template-attach + ws
# resubscribe + an alert on a real correction, which `list_active_
# chases` deliberately does NOT fire — see that module's docstring).
#
# `_chase_process_paper_row` is UNCHANGED — paper-mode rows are not
# broker-truth (no Kite/Dhan/Groww round-trip involved), so they stay
# outside the canonical accessor's scope; its own pre-existing tests in
# test_orders_chase_helpers.py keep passing unmodified.
#
# The "no broker_order_id at all" cleanup (`_rco_reconcile_no_id_row`
# below) is likewise NOT broker truth — it's a distinct, pre-existing
# business rule (placement itself never returned an id, so there's
# nothing a canonical accessor could ever resolve) kept local to
# `list_active_chases` only, not extended to the sweep/watchdog/admin
# routes, none of which had an equivalent concept before this change.


def _chase_snapshot_paper_open_ids() -> Optional[set]:
    """Snapshot paper engine's in-flight AlgoOrder ids, once per request.
    Returns None when the engine can't be reached — callers use that
    sentinel to skip the paper-row drop path.
    """
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        _pe = get_prod_paper_engine()
        return {o.get("algo_order_id") for o in _pe.open_order_details()}
    except Exception:
        return None


async def _chase_snapshot_broker_status_by_id() -> dict[str, dict]:
    """Snapshot the cached live broker order book keyed by order_id.
    Values are `{"status": <UPPER>, "average_price": <float>}`. On
    cache miss returns an empty dict — callers treat that as "broker
    snapshot unavailable, keep row as OPEN".
    """
    out: dict[str, dict] = {}
    try:
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
    except asyncio.TimeoutError:
        logger.warning("chases/active broker snapshot timed out after 10s")
    except Exception as _oe:
        logger.debug(f"chases/active broker snapshot failed: {_oe}")
    return out


def _chase_process_paper_row(r, _paper_open_ids: set, dry_run: bool = False) -> tuple[bool, int]:
    """Handle one paper-mode row. Returns `(drop, dropped_paper_delta)`.
    Only counts as `dropped_paper` when this call actually flipped the
    row from OPEN → UNFILLED (concurrent step() may have raced us to a
    terminal state — do NOT overcount those).

    `dry_run=True` computes the SAME decision without mutating `r` —
    used by `list_active_chases`'s unlocked classification pass so it
    can determine which rows need a row lock without writing to any
    row that hasn't been locked yet.
    """
    if r.id in _paper_open_ids:
        return False, 0
    if r.status == "OPEN":
        if not dry_run:
            r.status = "UNFILLED"
            r.detail = ((r.detail or "")[:200]
                        + " · paper engine no longer tracking")
        return True, 1
    return True, 0


async def _single_row_locked_update(row_id: int, allowed_statuses: set, mutator) -> bool:
    """Lock exactly one AlgoOrder row by id (`.with_for_update()`,
    scoped to that single row — never the full unlocked batch) and
    apply `mutator(row)` + commit ONLY if the row's CURRENT status
    (re-read fresh under the lock, guarding against a race since the
    caller's own classification read was unlocked) is still in
    `allowed_statuses`. Returns True iff a write actually happened.

    Shared by the "no broker_order_id" and paper-engine-drop cleanups
    below — neither is broker-truth, so neither goes through
    `apply_order_truth`, but both need the identical lock-only-when-
    needed shape `list_active_chases`'s callers depend on (see
    test_chases_active_lock_scoping.py).
    """
    from sqlalchemy import select as _sru_select
    from backend.api.database import async_session as _sru_async_session
    from backend.api.models import AlgoOrder as _SruAlgoOrder
    async with _sru_async_session() as s:
        _rows = (await s.execute(
            _sru_select(_SruAlgoOrder).where(_SruAlgoOrder.id == row_id)
            .with_for_update()
        )).scalars().all()
        row = _rows[0] if _rows else None
        if row is None or row.status not in allowed_statuses:
            return False
        mutator(row)
        await s.commit()
        return True


def _rco_mark_no_id_rejected(row) -> None:
    row.status = "REJECTED"
    row.detail = ((row.detail or "")[:200]
                  + " · live placement never returned broker_order_id")


def _rco_mark_paper_dropped(row) -> None:
    row.status = "UNFILLED"
    row.detail = ((row.detail or "")[:200] + " · paper engine no longer tracking")


async def _rco_reconcile_no_id_row(r) -> bool:
    """A live-mode row with NO broker_order_id at all — placement
    itself never succeeded. Deliberately NOT broker truth (the
    canonical accessor treats a falsy id as 'unavailable, don't guess'
    and leaves the row alone) — this is a distinct, pre-existing
    business rule kept local to `list_active_chases` (see the
    module-level note above this block). Reuses
    `orders_postback._pb_should_withhold_status`'s HELD/active-chase
    check for consistency with every other writer instead of a
    standalone `is_chase_active` call. Returns True iff REJECTED was
    actually written (row dropped from `kept`).
    """
    from backend.api.routes.orders_postback import _pb_should_withhold_status
    if _pb_should_withhold_status(r, "REJECTED"):
        return False
    return await _single_row_locked_update(
        r.id, {"OPEN", "CANCEL_FAILED"}, _rco_mark_no_id_rejected,
    )


async def _rco_reconcile_paper_row(r, paper_open_ids: "set | None") -> bool:
    """Returns True iff dropped from `kept`. Classification reuses the
    UNCHANGED `_chase_process_paper_row(..., dry_run=True)` (pure,
    no mutation) so the drop/write decision stays the single source of
    truth that function's own tests already cover; the write itself (only
    when actually needed) goes through `_single_row_locked_update` so a
    row that doesn't need a write never takes a lock.
    `paper_open_ids=None` (paper engine unreachable) means 'unknown,
    keep as-is' — never drop, matching the pre-existing behavior.
    """
    if paper_open_ids is None:
        return False
    drop, write_needed = _chase_process_paper_row(r, paper_open_ids, dry_run=True)
    if not drop:
        return False
    if write_needed:
        await _single_row_locked_update(r.id, {"OPEN"}, _rco_mark_paper_dropped)
    return True


async def _rco_reconcile_live_row(r, broker_status_by_id: dict) -> "tuple[bool, bool]":
    """Reconcile one live-mode row that IS known to carry a
    broker_order_id, via the canonical single-order-view accessor
    (`orders_helpers.py:get_broker_order_truth` / `apply_order_truth`).
    Returns `(dropped, filled)` — `dropped=True` means the row actually
    transitioned this call and must not appear in `kept`.

    `withhold_if_active=True` reuses `_pb_should_withhold_status`
    internally (same HELD/active-chase-retry guard the old inline
    `_chase_process_live_row` enforced) — a mid-retry row's just-
    rejected broker_order_id can't be finalized to a non-FILLED
    terminal status ahead of the retry's own `_sync_algo_order_id`
    write. A withheld write, OR a write lost to a race with another
    writer (apply_order_truth's own idempotency/final-status guards),
    both return `dropped=False` — the stale in-memory `r` keeps
    showing for at most one more ~3s poll cycle, a display-only
    staleness, never a safety issue (the authoritative DB state is
    correct either way).
    """
    from backend.api.routes.orders_helpers import get_broker_order_truth, apply_order_truth
    truth = await get_broker_order_truth(r.broker_order_id, broker_status_by_id)
    if not truth.available or truth.algo_status is None:
        return False, False
    changed = await apply_order_truth(r.id, truth, withhold_if_active=True)
    if not changed:
        return False, False
    return True, truth.algo_status == "FILLED"


def _retry_parse_overrides(overrides_json) -> dict:
    """Best-effort parse of the row's persisted per-submit overrides.
    Returns `{}` on any failure — the retry pipeline just re-runs
    without the overrides in that case.
    """
    if not overrides_json:
        return {}
    try:
        import json as _json_parse
        _parsed = _json_parse.loads(overrides_json)
        return _parsed if isinstance(_parsed, dict) else {}
    except Exception:
        return {}


def _retry_build_gtt_entry(spec, gid: str, plan, product: str) -> dict:
    """Build one `attached_gtts_json` GTT entry mirroring the exact
    shape `_fire_template_attach_on_fill` writes. Split out so both
    surfaces can't drift.

    Populates the trail-stop scaffolding (`sl_trail_pct`,
    `current_trigger`, `highest_ltp`, `lowest_ltp`, `tp_trigger`) and
    the parent metadata block downstream pollers (`trail-stop poller`,
    OCO pair-watcher) rely on.
    """
    _entry: dict = {
        "kind":           "gtt",
        "label":          spec.label,
        "id":             gid,
        "trigger_values": list(spec.trigger_values or []),
        "trigger_type":   str(spec.trigger_type),
    }
    _is_two_leg = (str(spec.trigger_type) == "two-leg"
                   and len(spec.trigger_values or []) >= 2)
    if spec.sl_trail_pct is not None and spec.trigger_values:
        _entry["sl_trail_pct"]    = float(spec.sl_trail_pct)
        _entry["current_trigger"] = float(spec.trigger_values[-1])
        if _is_two_leg:
            _entry["tp_trigger"]  = float(spec.trigger_values[0])
        _entry["highest_ltp"]     = float(plan.parent_fill_price)
        _entry["lowest_ltp"]      = float(plan.parent_fill_price)
    elif _is_two_leg:
        # Non-trail two-leg still wants tp_trigger for any modify_gtt
        # round-trip the operator might trigger later.
        _entry["tp_trigger"]      = float(spec.trigger_values[0])
    # Parent metadata — needed by both trail-stop poller
    # (rebuild orders_payload) and OCO pair-watcher (sibling
    # cancel routing).
    _entry["parent_side"]     = str(plan.parent_side)
    _entry["parent_symbol"]   = str(plan.parent_symbol)
    _entry["parent_exchange"] = str(plan.parent_exchange)
    _entry["parent_account"]  = str(plan.parent_account)
    _entry["parent_qty"]      = int(plan.parent_qty)
    _entry["parent_product"]  = str(product or "NRML")
    return _entry


def _retry_build_attached_payload(result, product: str) -> list:
    """Build the full `attached_gtts_json` list from an attach result.
    One dict per GTT (via `_retry_build_gtt_entry`) plus a `wing`
    entry when the plan issued a wing order.

    Keyed off each spec's own `placed_id` (mirrors
    `_opp_build_attach_entries` in orders_place.py) rather than
    positionally zipping `plan.gtts` with `result.gtt_ids` — a GTT
    that `_ta_live_place_one_gtt` placed but the broker rejected (or
    one that raised before `gtt_ids` was appended) never gets
    `placed_id` set, so it's skipped here too, instead of letting the
    positional zip silently pair a *different* spec's id/trigger/label
    onto this one.

    T20 fix (2026-10-23) — this function's own docstring claimed
    field-for-field parity with `_opp_build_attach_entries`
    (orders_place.py) but never actually wrote `sibling_id` /
    `sibling_ids` at all. On Groww (no native broker-side OCO),
    `_task_oco_pair_watcher` has no pointer to cancel the surviving
    leg when one of a pair/group fires via THIS path (retry-attach),
    so both TP and SL — or an entire scale-ladder-plus-SL group — stay
    live. Reuses the SAME map builders `_opp_build_attach_entries`
    uses (`_opl_build_sibling_map` / `_opl_build_group_sibling_map`)
    so the two paths can never represent a pair/group differently.
    `parent_symbol`/`parent_account`/`parent_exchange` need no extra
    work here — `_retry_build_gtt_entry` already writes them
    unconditionally on every entry.
    """
    from backend.api.routes.orders_place import (
        _opl_build_sibling_map, _opl_build_group_sibling_map,
    )
    sibling_by_id = _opl_build_sibling_map(getattr(result, "sibling_pairs", None))
    group_by_id = _opl_build_group_sibling_map(getattr(result, "sibling_groups", None))
    payload: list = []
    if result.plan:
        for _spec in result.plan.gtts:
            if not _spec.placed_id:
                continue
            _entry = _retry_build_gtt_entry(
                _spec, _spec.placed_id, result.plan, product,
            )
            _sib = sibling_by_id.get(str(_spec.placed_id))
            _group_tps = group_by_id.get(str(_spec.placed_id))
            if _sib:
                _entry["sibling_id"] = _sib
            elif _group_tps:
                _entry["sibling_ids"] = _group_tps
            payload.append(_entry)
    if result.wing_order_id:
        # Mirror orders_place.py:_opp_build_attach_entries field-for-field —
        # `chased=True` means `result.wing_order_id` is the sentinel string
        # "chase" (the wing was handed to chase, not placed directly) and
        # must never be treated as a real, cancellable broker order id.
        payload.append({
            "kind":   "wing",
            "label":  "Wing",
            "id":     result.wing_order_id,
            "chased": bool(getattr(result, "wing_chased", False)),
        })
    return payload


async def _retry_effective_parent_qty(row) -> int:
    """Prefer accumulated `filled_quantity` when non-zero so a partial
    fill doesn't oversize the exit GTT (mirrors the postback path).
    Falls back to original quantity when no partial fill was captured.

    M7: re-fetch `filled_quantity` from the DB rather than relying on
    the in-memory row snapshot, which may be stale if the postback
    arrived after the reconcile loaded the row but before it applied
    the fill count. The extra SELECT is cheap (PK lookup) and prevents
    an exit GTT sized off an unupdated row.
    """
    try:
        from sqlalchemy import select as _sel_rq
        from backend.api.database import async_session as _ars
        from backend.api.models import AlgoOrder as _AO_rq
        async with _ars() as _s_rq:
            _fresh_filled = (await _s_rq.execute(
                _sel_rq(_AO_rq.filled_quantity).where(_AO_rq.id == row.id)
            )).scalar_one_or_none()
        if _fresh_filled is not None and int(_fresh_filled or 0) > 0:
            return int(_fresh_filled)
    except Exception as _rq_err:
        logger.debug(
            "[RETRY-QTY] DB re-fetch failed for #%s, falling back "
            "to in-memory row: %s", getattr(row, "id", None), _rq_err,
        )
    # Fall back to the in-memory row value.
    filled = int(row.filled_quantity or 0)
    if filled > 0:
        return filled
    return int(row.quantity or 0)


def _retry_build_result_response(result, attached_now: bool) -> dict:
    """Materialise the retry outcome into the operator-facing dict.
    When the plan resolved cleanly but produced neither GTTs nor a wing,
    treat that as a failure with a clear reason (Sc.5c) rather than a
    silent "ok: true, attached: false".
    """
    _notes = result.plan.notes if result.plan else []
    _errors = result.errors or []
    if not attached_now and not _errors:
        return {
            "ok":            False,
            "reason":        "Template produced no GTTs and no wing — nothing to attach (check overrides + chain-scan filters)",
            "wing_order_id": result.wing_order_id,
            "gtt_ids":       result.gtt_ids,
            "notes":         _notes,
            "errors":        result.errors,
            "attached":      False,
        }
    return {
        "ok":            True,
        "wing_order_id": result.wing_order_id,
        "gtt_ids":       result.gtt_ids,
        "notes":         _notes,
        "errors":        result.errors,
        "attached":      attached_now,
    }


def _retry_precheck_row(row) -> Optional[dict]:
    """Return an early-exit response dict when the row shouldn't be
    retried; `None` when the retry may proceed.
    """
    if row.template_id is None:
        return {"ok": False, "reason": "no template attached to this order"}
    # A child row (parent_order_id set) is never itself a templated PARENT
    # order, even if it carries a `template_id` for traceability — e.g. a
    # chase-routed wing's own AlgoOrder row (`_chase_wing` in
    # template_attach.py) is stamped with both `parent_order_id` and
    # `template_id` so it's traceable back to its parent fill. Without this
    # guard, that wing row — once chase fills it (status FILLED,
    # attached_gtts_json never populated on a wing's OWN row, only on the
    # parent's) — looks identical to a stalled/failed template attach and
    # would run apply_template_to_order AGAINST THE WING ITSELF, arming a
    # second full template (GTTs + another wing) recursively.
    if getattr(row, "parent_order_id", None) is not None:
        return {"ok": False, "reason":
                "this is a child order (wing/GTT leg) — template retry "
                "only applies to the parent order"}
    if row.attached_gtts_json:
        return {"ok": False, "reason":
                "template already attached — nothing to retry"}
    # T16 fix (2026-10-23) — a row whose exits placed at the broker but
    # whose DB persist failed (see orders_place.py:
    # _TPL_ATTACH_PERSIST_PENDING) has NO attached_gtts_json yet, so the
    # check above doesn't catch it. Without this, Retry-attach would
    # read "nothing attached" and place a SECOND live set of exits on
    # top of the first, now-untracked, one.
    from backend.api.routes.orders_place import _TPL_ATTACH_PERSIST_PENDING
    if getattr(row, "id", None) in _TPL_ATTACH_PERSIST_PENDING:
        return {"ok": False, "reason":
                "exits already placed at the broker but not recorded — "
                "do not Re-attach; reconcile the broker GTT book manually"}
    if (row.status or "").upper() != "FILLED":
        return {"ok": False,
                "reason": f"parent must be FILLED to attach (status={row.status})"}
    # 2026-09-30 fix — companion to _fire_template_attach_on_fill's mode
    # gate: since that fix, a FILLED paper/replay/shadow row with a
    # template_id and no attached_gtts_json is now its PERMANENT resting
    # state (real broker attach is correctly skipped for non-live fills),
    # which looks identical to "attach silently failed, needs retry".
    # _rco_run_template_attach only special-cases mode=='sim' (routes to
    # apply_plan_sim, no real broker call) — every OTHER non-live mode
    # falls through to its 'live' branch and would place a REAL broker
    # GTT/wing order for a paper/replay/shadow fill. Refuse here instead.
    _row_mode = (row.mode or "").lower()
    if _row_mode not in ("live", "sim"):
        return {"ok": False, "reason":
                f"template retry-attach is not supported for mode={row.mode!r} "
                "orders — only 'live' fills place real broker GTTs"}
    return None


def _chase_row_superseded_ids(r) -> list[str]:
    """Parse `AlgoOrder.superseded_broker_order_ids_json` into a plain
    `list[str]` for the API response (fix #6). `[]` on null/empty/parse
    failure — never raises.
    """
    raw = getattr(r, "superseded_broker_order_ids_json", None)
    if not raw:
        return []
    try:
        import json as _json
        parsed = _json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _chase_row_to_info(r, masked_acct, child_map: dict) -> "AlgoOrderInfo":
    """Materialise an AlgoOrder ORM row into the API response Struct.
    Extracted so the list_active_chases + list_orders response builders
    can share the same mapping.
    """
    return AlgoOrderInfo(
        id=r.id, account=masked_acct(r.account), symbol=r.symbol,
        exchange=r.exchange,
        transaction_type=r.transaction_type, quantity=r.quantity,
        initial_price=(float(r.initial_price) if r.initial_price is not None else None),
        current_limit=(float(r.current_limit) if r.current_limit is not None else None),
        fill_price=(float(r.fill_price) if r.fill_price is not None else None),
        attempts=int(r.attempts or 0),
        status=r.status, engine=r.engine, mode=r.mode,
        detail=r.detail,
        created_at=r.created_at.isoformat() if r.created_at else "",
        target_pct=(float(r.target_pct) if r.target_pct is not None else None),
        target_abs=(float(r.target_abs) if r.target_abs is not None else None),
        parent_order_id=r.parent_order_id,
        basket_tag=r.basket_tag,
        template_id=r.template_id,
        attached_gtts_json=r.attached_gtts_json,
        hold_json=getattr(r, "hold_json", None),
        filled_quantity=(int(r.filled_quantity) if r.filled_quantity is not None else None),
        child_order_ids=child_map.get(r.id, []),
        interval_seconds=(int(r.interval_seconds) if getattr(r, "interval_seconds", None) is not None else None),
        last_attempt_at=(float(r.last_attempt_at) if getattr(r, "last_attempt_at", None) is not None else None),
        next_attempt_at=(float(r.next_attempt_at) if getattr(r, "next_attempt_at", None) is not None else None),
        broker_order_id=r.broker_order_id,
        source=getattr(r, "source", None),
        agent_id=getattr(r, "agent_id", None),
        superseded_broker_order_ids=_chase_row_superseded_ids(r),
    )


# ── Draft orders (2026-10) ────────────────────────────────────────────────────
# Server-side persistence for the OrderTicket "Add to Payoff" draft flow.
# Drafts are real AlgoOrder rows (mode='draft', status='OPEN') but NEVER
# reach a broker — no place_order / translate_qty / preflight call exists
# anywhere in this section. See DraftOrderRequest's docstring for the
# quantity-unit convention (contracts-equivalent, NOT lots).

def _draft_validate_input(data) -> None:
    """Raise HTTPException(400) on an obviously-invalid draft payload.
    Deliberately lightweight — unlike _ticket_validate_input there is no
    lot_size resolution or broker-facing check, since a draft never reaches
    a broker."""
    sym = (data.symbol or "").strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol is required")
    side = (data.transaction_type or "").upper()
    if side not in _TXN_TYPES:
        raise HTTPException(status_code=400, detail="transaction_type must be BUY or SELL")
    if int(data.quantity or 0) <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    exch = (data.exchange or "NFO").upper()
    if exch not in _EXCHANGES:
        raise HTTPException(status_code=400, detail=f"exchange must be one of {sorted(_EXCHANGES)}")


async def _draft_delete_by_id(draft_id: int) -> bool:
    """Delete an AlgoOrder row by id, ONLY when mode=='draft'. Returns True
    when a row was actually deleted; False (no-op) when the id doesn't
    exist or exists but isn't a draft — NEVER raises. This is the guard
    that keeps this a drafts-only deletion path, not an accidental
    delete-any-order endpoint: a `mode='live'`/`'paper'`/etc. row passed
    here is left completely untouched.

    Also called (best-effort, swallowed) from
    orders_place.py:ticket_order_handler after a CONFIRMED successful
    /ticket submission that carried a `draft_id` — see that function's
    docstring for why a delete failure there must never raise.
    """
    from sqlalchemy import select as _sel
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    try:
        async with async_session() as s:
            row = (await s.execute(
                _sel(AlgoOrder).where(AlgoOrder.id == draft_id)
            )).scalar_one_or_none()
            if row is None or (row.mode or "").lower() != "draft":
                return False
            await s.delete(row)
            await s.commit()
            return True
    except Exception as e:
        logger.warning(f"[DRAFT] delete failed for id={draft_id}: {e}")
        return False


def _rco_invalidate_terminal_caches(status: str) -> None:
    """Invalidate API + raw-DF caches on terminal order status.

    positions/holdings caches are only busted on COMPLETE — they don't
    change on CANCELLED/REJECTED/EXPIRED, so busting them there just causes
    a cold-cache broker round-trip on the next NavStrip poll.

    funds/margins are busted on every terminal status: a fill releases margin
    and a cancel may release reserved funds.
    """
    _is_complete = str(status).upper() == "COMPLETE"
    if _is_complete:
        for _key in ("positions", "holdings"):
            try:
                invalidate(_key)
            except Exception:
                pass
    try:
        invalidate("funds")
    except Exception:
        pass
    try:
        from backend.brokers.broker_apis import _raw_cache_invalidate
        if _is_complete:
            _raw_cache_invalidate("positions")
            _raw_cache_invalidate("holdings")
        _raw_cache_invalidate("margins")
    except Exception:
        pass


_MCX_LOTS_CONVENTION_BROKERS = frozenset({"kite", "dhan"})


def _mcx_postback_qty_to_contracts(exchange: str, symbol: str, qty, broker: str) -> int:
    """Convert a provisional MCX/NCO postback fill quantity from LOTS to
    CONTRACTS before it's used downstream (position_filled broadcast,
    post-fill refresh poll). Mirrors the lots→contracts pattern already
    used for the enriched positions DataFrame in
    `backend/brokers/broker_apis.py:_annotate_lot_size` — Kite/Dhan ship
    MCX order/postback quantity in lots, same as their intraday position
    fields (CLAUDE.md "Option qty vs lot_size"); NFO/CDS/BFO/equity qty is
    already in contracts and passes through unchanged.

    `broker` gates the conversion — INVERTED allow-list design (fixed
    2026-09, was a Groww-only deny-list): only convert for brokers
    CONFIRMED to ship MCX quantity in lots (`_MCX_LOTS_CONVENTION_BROKERS`
    = kite, dhan). Every other broker identifier — Groww (confirmed
    CONTRACTS for all exchanges), paper/simulated fanout (`broker="paper"`,
    AlgoOrder quantities are already in contracts — see
    `orders_place.py:_ticket_validate_input`), or any future/unknown
    broker string — passes through unconverted by default. This is the
    safer general shape: a new caller that forgets to pass a real broker
    id gets correct (unconverted) behaviour instead of a silent double
    conversion. Previously the deny-list ("skip only for groww") meant
    paper-mode's hardcoded `broker="kite"` (used to reuse Kite's postback
    fanout wiring) incorrectly triggered a lots→contracts multiply on
    quantities that were never in lots to begin with, doubling MCX
    quantities in paper mode.

    Returns `qty` unchanged (best-effort) when the lot-size cache is cold,
    the broker isn't in the lots-convention allow-list, or the exchange
    isn't MCX/NCO — never raises.
    """
    try:
        _qty_int = int(qty or 0)
    except (TypeError, ValueError):
        return 0
    if exchange not in ("MCX", "NCO"):
        return _qty_int
    if str(broker or "").lower() not in _MCX_LOTS_CONVENTION_BROKERS:
        return _qty_int
    try:
        from backend.brokers.adapters.kite import _LOT_INDEX
        lot_size = _LOT_INDEX.get((exchange, symbol), 0)
        if lot_size > 1:
            return _qty_int * lot_size
        logger.warning(
            f"[MCX-POSTBACK-QTY] cold/missing lot_size for {exchange}/{symbol} — "
            f"passing provisional qty={_qty_int} through unconverted"
        )
    except Exception as _lex:
        logger.warning(f"[MCX-POSTBACK-QTY] lot_size lookup failed for {exchange}/{symbol}: {_lex}")
    return _qty_int


def _rco_broadcast_position_filled(
    masked: str, exchange: str, symbol: str, txn: str, qty, price, order_id
) -> None:
    """Broadcast position_filled WS event on COMPLETE. No-op when qty is zero.

    `qty` must already be in CONTRACTS — see `_mcx_postback_qty_to_contracts`,
    applied by the caller (`_postback_broadcast_fanout`) before this runs.
    """
    try:
        _qty_int = int(qty or 0)
        if _qty_int > 0:
            _side_sign = 1 if (txn or "").upper() == "BUY" else -1
            broadcast(json.dumps({
                "event": "position_filled",
                "account": masked, "exchange": exchange,
                "tradingsymbol": symbol,
                "qty": _qty_int * _side_sign,
                "fill_price": float(price or 0),
                "ts": int(_time.time() * 1000),
                "order_id": order_id,
            }))
    except Exception as _pe:
        logger.debug(f"position_filled broadcast skipped: {_pe}")


async def _positions_refresh_after_fill(
    account: str, tradingsymbol: str, qty_delta: int
) -> None:
    """Fire-and-forget: poll broker positions for up to 5 s after a fill,
    then invalidate the raw cache + broadcast positions_refreshed once the
    fill is reflected (or qty changes). The initial cache bust in
    _rco_invalidate_terminal_caches() clears stale data immediately; this
    task handles the broker-propagation lag so the UI eventually sees the
    new position without waiting for the 5-min performance poll.

    Scoped to `account` — `fetch_positions(account=..., force_refresh=True)`
    falls straight through to `_fetch_positions_local` for just that one
    account (no `@for_all_accounts` fan-out), so a multi-leg/multi-account
    fill doesn't force every OTHER account's broker session on every poll
    attempt. That single-account branch also sits entirely outside the
    30s-TTL SSOT cache that wraps the zero-arg all-accounts entry — every
    read here is already a direct, uncached broker round-trip, so
    `force_refresh=True` has no additional effect on this call shape; it's
    kept for signature parity/documentation with the all-accounts call. A
    genuinely empty `account` (Groww's postback carries none at all — see
    `order_postback_groww` below) falls back to the all-accounts scan via
    the SAME `@for_all_accounts` falsy-account check every other
    single-account caller in this codebase relies on — not a crash, just
    the pre-existing (less targeted) behaviour, preserved for that broker.
    """
    try:
        import asyncio as _aio
        from backend.brokers.broker_apis import fetch_positions, _raw_cache_invalidate

        async def _read_qty() -> int:
            dfs = await _aio.to_thread(
                fetch_positions, account=account, force_refresh=True,
            )
            rows = [
                r for df in (dfs or [])
                for r in df.to_dict(orient="records")
                if r.get("tradingsymbol") == tradingsymbol
            ]
            return sum(int(r.get("quantity", 0)) for r in rows)

        # Baseline reading BEFORE the propagation sleep — the OLD bug:
        # this read used to happen at t+3s (after the sleep below), by
        # which point the broker had USUALLY ALREADY applied the fill, so
        # `cur_qty` could never differ from that late "baseline" on any
        # later poll and the loop reliably timed out with
        # positions_refreshed essentially never firing in practice.
        # Reading now (t+0, genuinely pre-fill) and comparing against the
        # algebraically EXPECTED post-fill quantity (baseline + qty_delta —
        # the parameter the old comparison never used) makes every later
        # poll attempt a real test instead of a guaranteed no-op.
        try:
            initial_qty = await _read_qty()
        except Exception as _base_err:
            logger.debug("[FILL-POLL] baseline read failed: %s", _base_err)
            initial_qty = 0
        expected_qty = initial_qty + qty_delta

        # Give the broker 2 s to propagate the fill before the first poll.
        await _aio.sleep(2)
        for _attempt in range(5):
            await _aio.sleep(1)
            try:
                cur_qty = await _read_qty()
                # Fire once quantity hits the algebraically expected
                # post-fill value, OR on any other change from the
                # (now-genuinely-pre-fill) baseline — the latter is a
                # robustness fallback for lot-size rounding or a
                # concurrent second leg moving the same symbol further
                # than this call's own delta.
                changed = (cur_qty == expected_qty) or (cur_qty != initial_qty)
                if changed:
                    _raw_cache_invalidate("positions")
                    # Bug fix (2026-09-30): only the raw broker-DataFrame
                    # cache was busted here, not the 30s route-level cache
                    # (get_or_fetch("positions", ...) in positions.py). The
                    # frontend's own loadPositions({fresh:true}) call fires
                    # ~0-1s after the fill event — before the broker has
                    # propagated it — and repopulates the route cache with
                    # a stale pre-fill frame under a fresh 30s TTL. Busting
                    # only the raw cache at that point does nothing for the
                    # route cache, so Payoff/Legs stayed stale for up to
                    # 30s. Also invalidate "holdings" the same way — a CNC
                    # sell fill moves qty from holdings to positions, and
                    # _rco_invalidate_terminal_caches() already treats the
                    # two as a paired invalidation set for every COMPLETE
                    # fill (see that function, same file), so this is the
                    # genuine parallel case, not a guess. Busts BOTH cache
                    # layers for holdings (raw + route), matching the two
                    # existing precedents in this codebase
                    # (_rco_invalidate_terminal_caches, lines above, and
                    # holdings.py's own fresh-read path) — a route-only
                    # clear would still serve a stale raw-cache frame on
                    # the very next /api/holdings request.
                    invalidate("positions")
                    invalidate("holdings")
                    _raw_cache_invalidate("holdings")
                    broadcast(json.dumps({
                        "event": "positions_refreshed",
                        "tradingsymbol": tradingsymbol,
                        "account": account,
                        "ts": int(_time.time() * 1000),
                    }))
                    logger.debug(
                        "[FILL-POLL] positions_refreshed after %d attempt(s) "
                        "symbol=%s qty_delta=%+d",
                        _attempt + 1, tradingsymbol, qty_delta,
                    )
                    return
            except Exception as _poll_err:
                logger.debug("[FILL-POLL] attempt %d error: %s", _attempt + 1, _poll_err)
        logger.warning(
            "[FILL-POLL] positions_refreshed timed out after 5 attempts "
            "symbol=%s qty_delta=%+d",
            tradingsymbol, qty_delta,
        )
    except Exception as _outer:
        logger.debug("[FILL-POLL] outer error: %s", _outer)


async def _opp_cancel_is_own_chase_requote(order_id: "str | int | None") -> bool:
    """True when `order_id` (a broker_order_id) belongs to an AlgoOrder
    row that `chase.is_chase_active()` reports as currently inside an
    active `chase_order()` retry loop in THIS process.

    2026-10-23 audit fix (O4) — the signal used here is the SAME
    in-process marker `orders_postback.py:_pb_should_withhold_status`
    already relies on to decide whether a CANCELLED postback is chase's
    own cancel-and-replace cycle. A CANCELLED event for a row chase is
    actively managing is, for all practical purposes, always chase's
    own routine re-quote cancel — not an operator action or a genuinely
    unexpected broker-side cancel. Fail-safe: any lookup/import error
    returns False (treat as action-required, never silently swallow a
    genuine cancel alert because of a DB hiccup).
    """
    if not order_id:
        return False
    try:
        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session as _async_s
        from backend.api.models import AlgoOrder as _AO
        from backend.api.algo.chase import is_chase_active
        async with _async_s() as _s:
            row_id = (await _s.execute(
                _sql_select(_AO.id).where(_AO.broker_order_id == str(order_id))
            )).scalars().first()
        return is_chase_active(row_id)
    except Exception:
        return False


async def _opp_send_cancel_alert(
    account: str, symbol: str, txn: str, qty: object, status_message: str,
    order_id: "str | int | None" = None,
) -> None:
    # 2026-10 audit fix: `send_ntfy_alert(title, message, priority=None)`
    # was called with a single positional argument (`msg` bound to
    # `title`), raising a `TypeError` inside the fire-and-forget
    # `create_task` this is scheduled from (orders.py's postback
    # fan-out) — every postback CANCELLED alert silently never sent.
    # Fixed: proper title/message split, routed through the shared
    # origin-first header (this call bypasses the event-agent pipeline
    # entirely, so it previously carried no agent info at all;
    # `format_notification_header(None, None, ...)` degrades to
    # "Agent: manual" — there is no agent context threaded to this
    # fan-out helper today).
    #
    # 2026-10-23 audit fix (O4) — this fired a direct, unconditional
    # `send_ntfy_alert`, bypassing the binary action_required ntfy gate
    # every other chase alert now uses. Worse, it fired for EVERY
    # CANCELLED postback including each of chase's own routine
    # cancel-and-replace re-quotes — a single low-tier chase (30 max
    # attempts) could generate ~87 urgent pushes for completely normal
    # repricing. Fix: when the row behind this broker_order_id is
    # currently under an active chase retry loop, the cancel is not
    # action-required — skip ntfy entirely (log-only; nothing else
    # needs to fire for a routine re-quote). Otherwise this is an
    # operator-initiated or genuinely unexpected broker-side cancel —
    # still action-required, so it must reach ntfy, forced urgent
    # (not the clock-based day/night default).
    if await _opp_cancel_is_own_chase_requote(order_id):
        logger.debug(
            "[CANCEL-ALERT] suppressed ntfy for chase's own re-quote cancel "
            "order_id=%s symbol=%s", order_id, symbol,
        )
        return

    from backend.shared.helpers.alert_utils import send_ntfy_alert, format_notification_header
    title = f"Order cancelled: {symbol}"
    header = format_notification_header(None, None)
    message = f"{header}\n{mask_account(account)} {txn} {qty} {symbol} — {status_message}"
    await asyncio.to_thread(send_ntfy_alert, title, message, "urgent")


def _postback_broadcast_fanout(
    *,
    status: str,
    order_id,
    account: str,
    masked: str,
    symbol: str,
    txn: str,
    qty,
    price,
    broker: str,
    exchange: str = "",
    status_message: str = "",
) -> None:
    """Cache invalidation + WS broadcast trio shared by every broker
    postback handler (Kite inline, Dhan/Groww via _process_broker_postback).

    `broker` — required, no default. Identifies which broker's postback
    this is ("kite" / "dhan" / "groww" / paper's own "paper") so
    `_mcx_postback_qty_to_contracts` applies the right lots↔contracts
    convention — only "kite"/"dhan" trigger the lots→contracts multiply
    (`_MCX_LOTS_CONVENTION_BROKERS`); every other identifier, including
    "paper" (AlgoOrder quantities are already in contracts), passes
    through unconverted. Deliberately has no default so a new caller
    can't silently inherit Kite's lots-conversion behaviour for a broker
    that doesn't use it.

    Steps:
      1. `invalidate("orders")` always
      2. On terminal status: also invalidate `positions`, `holdings`,
         and the raw-DataFrame caches (positions/holdings/margins).
      3. Broadcast `order_update` with the postback payload.
      4. On COMPLETE: broadcast `position_filled` with signed qty
         delta so the frontend can patch its local positions
         table immediately (no waiting for the 5-min poll).
      5. On terminal: broadcast `book_changed` so subscribers can
         refetch their primary loaders in one coordinated pass.

    Best-effort — every step wrapped in try/except so a single
    broadcast failure can't break the postback ACK. Kite's webhook
    will retry on a non-2xx response, so swallowing fan-out errors
    here is safer than propagating.
    """
    _terminal = str(status or "").upper() in (
        "COMPLETE", "CANCELLED", "REJECTED", "EXPIRED",
    )

    try:
        invalidate("orders")
        if _terminal:
            _rco_invalidate_terminal_caches(status)

        broadcast(json.dumps({
            "event": "order_update",
            "order_id": order_id, "account": masked, "status": status,
            "tradingsymbol": symbol, "transaction_type": txn,
            "quantity": qty, "price": price, "status_message": status_message,
        }))

        if str(status).upper() == "COMPLETE":
            # Provisional fill qty from the postback is in LOTS for MCX/NCO
            # (same convention as Kite's positions intraday fields) — convert
            # to CONTRACTS once, before both downstream consumers, so neither
            # the WS position_filled event nor the post-fill refresh poll
            # under/over-count by lot_size. See _mcx_postback_qty_to_contracts.
            _qty_contracts = _mcx_postback_qty_to_contracts(exchange, symbol, qty, broker)
            _rco_broadcast_position_filled(masked, exchange, symbol, txn, _qty_contracts, price, order_id)
            # 2026-10 fix: paper/sim/replay fills never touch a real
            # broker position — all three modes share PaperTradeEngine
            # and fan out through this SAME helper with the one literal
            # broker="paper" (see paper.py:_paper_fanout_terminal) — so
            # polling the real broker for up to 5 attempts on every
            # simulated fill was pure waste, contending with live
            # chase/postback traffic for the same shared conn-service
            # broker session.
            if broker != "paper":
                try:
                    _side_sign = 1 if (txn or "").upper() == "BUY" else -1
                    asyncio.create_task(
                        _positions_refresh_after_fill(account, symbol, _qty_contracts * _side_sign)
                    )
                except Exception:
                    pass

        if _terminal:
            broadcast(json.dumps({
                "event": "book_changed",
                "account": masked, "exchange": exchange,
                "tradingsymbol": symbol, "reason": status,
                "ts": int(_time.time() * 1000),
            }))

        if str(status).upper() == "CANCELLED":
            asyncio.create_task(
                _opp_send_cancel_alert(account, symbol, txn, qty, status_message or "", order_id)
            )
    except Exception as _be:
        logger.warning(f"postback fan-out failed: {_be}")


# ── Controller-level helpers (_rco_*) ─────────────────────────────────────────
# Extracted from D-grade controller methods to reduce cyclomatic complexity.

async def _rco_kill_paper_mode(row) -> str:
    """Cancel a paper-mode chase row via the engine's canonical cancel path.
    Returns an error string on failure, empty string on success."""
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        eng = get_prod_paper_engine()
        cancelled = eng.cancel_paper_order(row.id)
        if not cancelled:
            return "paper engine no longer tracking"
        return ""
    except Exception as e:
        return f"paper cancel failed: {e}"


async def _rco_kill_live_mode(row) -> str:
    """Cancel a live-mode chase row at the broker. Returns error string on
    failure, empty string on success. Marks the broker_order_id as killed
    before the broker call so the chase loop sees the flag even if a poll
    lands between cancel and DB mark."""
    from backend.brokers import get_broker
    if not str(row.account or "").strip():
        return "no account on row"
    if not (row.broker_order_id or "").strip():
        return "no broker_order_id on row"
    try:
        broker = get_broker(str(row.account))
    except Exception as _ge:
        return f"broker not loaded for {row.account}: {_ge}"
    try:
        from backend.api.algo.chase import mark_killed
        mark_killed(str(row.broker_order_id))
    except Exception:
        pass
    try:
        await asyncio.to_thread(
            broker.cancel_order,
            str(row.broker_order_id),
            variety="regular",
        )
        return ""
    except Exception as e:
        return f"broker cancel failed: {e}"


_PREVIEW_FO_EXCHANGES = frozenset({"NFO", "MCX", "CDS", "BFO", "BCD", "NCO"})


async def _rco_preview_resolve_qty(exch: str, sym: str, input_qty: int) -> int:
    """Convert LOTS to contracts for F&O exchanges in ticket_preview.
    Returns contracts when lot_size is available, raw input_qty otherwise.
    """
    if exch not in _PREVIEW_FO_EXCHANGES or input_qty <= 0:
        return input_qty
    from backend.brokers.adapters.kite import get_lot_size as _prev_lot
    try:
        _lot = int(await _prev_lot(exch, sym) or 0)
    except Exception:
        _lot = 0
    return input_qty * _lot if _lot > 0 else input_qty


def _rco_preview_empty_plan(data) -> dict:
    """Build a stub plan dict when apply_template_to_order returns None."""
    return {
        "template_id":   None,
        "template_name": "(none)",
        "template_slug": None,
        "parent_account":    data.account,
        "parent_symbol":     (data.tradingsymbol or "").upper(),
        "parent_side":       (data.side or "").upper(),
        "parent_qty":        data.quantity,
        "parent_exchange":   data.exchange,
        "parent_fill_price": float(data.reference_price or 0.0),
        "gtts": [],
        "wing": None,
        "notes": [],
    }


async def _preview_check_broker_gates(data, exch: str) -> None:
    """Raise 422 early at preview for MCX GTT capability + wing_premium_pct=0. (#5, #7)"""
    if data.template_id and exch in ("MCX", "NCO"):
        _acct = (data.account or "")
        if _acct:
            from backend.brokers.capabilities import capabilities_for
            if not capabilities_for(_acct).gtt_supports_mcx:
                raise HTTPException(status_code=422, detail="Selected broker does not support MCX GTT — remove template or switch broker")
    _wpp = getattr(data, "wing_premium_pct_override", None)
    if _wpp is not None and _wpp <= 0:
        raise HTTPException(status_code=422, detail="wing_premium_pct must be > 0")
    _wms = getattr(data, "wing_max_spread_pct_override", None)
    if _wms is not None and _wms <= 0:
        raise HTTPException(status_code=422, detail="wing_max_spread_pct must be > 0")


async def _preview_template_meta(result, data, sym: str, exch: str) -> "tuple[bool | None, list[str]]":
    """Compute wing_feasible and gtt_trigger_errors for a preview result. (#28A, #29)"""
    gtt_errors: list[str] = []
    if data.template_id:
        from backend.api.routes.orders_place import _validate_gtt_triggers, _opl_price_from_broker
        ltp = await _opl_price_from_broker(sym) or 0.0
        gtt_errors = _validate_gtt_triggers(result.plan, ltp, exch)

    wing_feasible: "bool | None" = None
    if data.template_id:
        has_wing = any(
            getattr(g, "kind", "") == "wing" or "wing" in (getattr(g, "label", "") or "").lower()
            for g in (result.plan.gtts or [])
        ) or bool(result.wing_skipped_reason)
        if has_wing or result.wing_skipped_reason:
            wing_feasible = result.wing_skipped_reason is None

    return wing_feasible, gtt_errors


_PB_RAW_PAYLOAD_SENSITIVE_KEYS = ("dhanClientId",)


def _pb_redact_payload_for_log(body: dict) -> dict:
    """Shallow-copy a raw broker postback payload with sensitive id
    fields redacted, for logging only — `body` itself is returned
    untouched to every real caller (`_rco_parse_dhan_postback_body`,
    `_groww_parse_payload`), which need the raw value for account
    routing.

    2026-10-24 Dhan-numeric-id gap: Dhan's `dhanClientId` is purely
    numeric, a shape the centralized `_AccountMaskFilter` (log_store.py)
    can't catch inside a dumped dict's `repr()` — that filter's regex
    only matches `[A-Z]{2}[A-Z0-9]{4,8}`-shaped codes, and `mask_account()`
    itself is a scalar-string mask, not a find-and-replace-inside-a-blob
    tool.

    2026-10-24, amended same day: now that `register_accounts()` aliases
    each account's raw numeric `client_id` to that account's own
    already-computed mask (`backend/shared/helpers/utils.py`), masking
    this value in place produces a genuinely useful, distinguishable
    string (e.g. `D1####`) instead of the useless all-`#` the scalar
    fallback would have produced — so this masks in place rather than
    redacting the key outright, keeping the payload dump structurally
    complete. `mask_account()` falls back to the scalar (all-digits-
    masked) mask on its own if the alias hasn't been registered yet
    (e.g. before the first `Connections.rebuild_from_db()` call), so
    this is safe either way. Groww's payload carries no account
    identifier at all (per this route's own docstring) — a no-op for it
    today, kept generic so a future same-shaped Groww field is covered
    too.
    """
    if not isinstance(body, dict):
        return body
    return {
        k: (mask_account(str(v)) if k in _PB_RAW_PAYLOAD_SENSITIVE_KEYS and v is not None else v)
        for k, v in body.items()
    }


def _rco_parse_dhan_postback_body(body: dict) -> "tuple[str, str, str, str, str, object, object, str, str]":
    """Extract canonical (order_id, account, kite_status, kite_symbol, txn,
    qty, price, kite_exchange, status_message) from a Dhan postback payload.
    """
    order_id = str(body.get("orderId") or body.get("order_id") or "")
    status   = str(body.get("orderStatus") or body.get("status") or "").upper()
    symbol   = body.get("tradingSymbol") or body.get("tradingsymbol") or ""
    txn      = str(body.get("transactionType") or body.get("transaction_type") or "")
    qty      = body.get("filledQuantity") or body.get("quantity") or 0
    price    = body.get("averageTradedPrice") or body.get("price") or 0
    account  = str(body.get("dhanClientId") or body.get("account") or "")
    raw_seg  = str(body.get("exchangeSegment") or "")
    status_msg = str(body.get("statusMessage") or "")
    try:
        from backend.brokers.adapters.dhan import (
            _DHAN_STATUS_TO_KITE,
            _DHAN_SEGMENT_TO_EXCHANGE,
            _dhan_to_kite_symbol,
        )
        kite_status   = _DHAN_STATUS_TO_KITE.get(status, status)
        kite_symbol   = _dhan_to_kite_symbol(str(symbol)) if symbol else str(symbol)
        kite_exchange = _DHAN_SEGMENT_TO_EXCHANGE.get(raw_seg, raw_seg)
    except Exception:
        kite_status   = status
        kite_symbol   = str(symbol)
        kite_exchange = raw_seg
    return order_id, account, kite_status, kite_symbol, txn, qty, price, kite_exchange, status_msg


def _rco_parse_groww_postback_body(body: dict) -> "tuple[str, str, str, str, object, object, str, str]":
    """Extract canonical (order_id, kite_status, symbol, txn, qty, price,
    exchange, status_message) from a Groww postback payload.
    """
    order_id = str(body.get("groww_order_id") or body.get("order_id") or "")
    status   = str(body.get("order_status") or body.get("status") or "").upper()
    symbol   = str(body.get("trading_symbol") or body.get("symbol") or "")
    txn      = str(body.get("transaction_type") or "")
    qty      = body.get("filled_quantity") or body.get("quantity") or 0
    price    = body.get("average_price") or body.get("price") or 0
    exchange = str(body.get("exchange") or body.get("segment") or "")
    status_msg = str(body.get("status_message") or "")
    try:
        from backend.brokers.adapters.groww import _GROWW_STATUS_TO_KITE
        kite_status = _GROWW_STATUS_TO_KITE.get(status, status)
    except Exception:
        kite_status = status
    return order_id, kite_status, symbol, txn, qty, price, exchange, status_msg


# Module-level, process-lifetime "already warned" set — see
# _pb_verify_shared_token's docstring. One entry per broker_id, so the
# CRITICAL log fires once per process start (not once per postback,
# which could arrive frequently under active trading) if a broker's
# secret is genuinely unconfigured.
_PB_TOKEN_WARNED: set[str] = set()


def _pb_verify_shared_token(request: Request, broker_id: str) -> bool:
    """Verify a shared-secret token query param for brokers with no native
    webhook signature scheme (Dhan, Groww) — see orders_postback.py's
    _pb_verify_signature for the HMAC-based equivalent Kite gets, since
    Kite's postback carries a real per-message checksum and Dhan/Groww's
    do not.

    2026-09 council audit fix (risk + architect council members
    independently found this): /dhan_postback and /groww_postback had
    guards=[] with NO authentication at all — any POST to these public,
    internet-reachable endpoints was trusted. For Groww specifically
    (account is always "" — Groww's payload carries no account
    identifier), the fallback order-matching path in
    _pb_fallback_lookup_row needs no order_id and no account, just a
    guessed symbol+side+qty within a 60s window matched across EVERY
    account on the platform. A forged "COMPLETE" fill flows straight
    into template-attach, which places real GTT/wing orders at the
    broker sized off the attacker-supplied fill price.

    Reads a per-broker shared secret from secrets.yaml (key
    "<broker_id>_postback_token", e.g. "dhan_postback_token") that the
    operator generates once and appends as a query string to the webhook
    URL configured in that broker's own partner dashboard — e.g.
    https://ramboq.com/api/orders/dhan_postback?token=<secret>. Dhan and
    Groww don't sign their webhook payloads, so there's no per-message
    HMAC to verify (unlike Kite); a static shared secret in the URL is
    the standard fallback for a webhook sender that doesn't support
    request signing.

    TRANSITIONAL fail-open: if no secret is configured yet for this
    broker (key absent/empty in secrets.yaml — true for every existing
    deployment until the operator configures it), returns True so
    deploying this fix doesn't immediately break existing Dhan/Groww
    webhook processing that was already relying on the unauthenticated
    endpoint. Logs a CRITICAL line once per process so the gap can never
    be silently missed — configure the secret (and update the broker's
    dashboard URL to include it) to actually close it.
    """
    import hmac as _hmac
    from backend.shared.helpers.utils import secrets as _secrets

    key = f"{broker_id}_postback_token"
    expected = str(_secrets.get(key) or "").strip()
    if not expected:
        if broker_id not in _PB_TOKEN_WARNED:
            _PB_TOKEN_WARNED.add(broker_id)
            logger.critical(
                "[POSTBACK-AUTH] %s: no '%s' configured in secrets.yaml — "
                "this webhook endpoint is UNAUTHENTICATED and internet-"
                "reachable. Generate a secret, add '%s: <secret>' to "
                "secrets.yaml, and append '?token=<secret>' to the webhook "
                "URL configured in %s's partner dashboard to close this gap.",
                broker_id, key, key, broker_id,
            )
        return True
    supplied = request.query_params.get("token", "")
    return _hmac.compare_digest(expected, str(supplied))


async def _rco_attached_gtts_already_set(row_id: int) -> bool:
    """Fresh DB read of `attached_gtts_json` for *row_id*.

    T13 fix: `_rco_run_template_attach`'s only caller
    (`retry_template_attach`) checks `attached_gtts_json` via
    `_retry_precheck_row` BEFORE acquiring the per-row lock below. A
    concurrent fill-triggered attach (postback / chase terminal, via
    `_fire_template_attach_on_fill`) can win that lock first and persist
    `attached_gtts_json` in the window between that precheck and this
    function actually acquiring the lock — without a re-check taken
    INSIDE the lock, this path would still proceed to call
    `apply_template_to_order` a second time, placing a second live
    GTT/wing set and orphaning the first (now-untracked) one. Mirrors
    `orders_place.py:_opp_load_row_for_attach`'s in-lock idempotency
    read on the fill-triggered path.

    T16 fix (2026-10-23) — also checks `_TPL_ATTACH_PERSIST_PENDING`
    first: a row whose exits already placed at the broker but whose
    DB persist failed carries NO `attached_gtts_json` yet (the write
    that would set it is exactly what failed), so the plain DB read
    below would wrongly say "not attached" and let this path place a
    SECOND live set of exits. Attempts one flush of the pending
    persist (best-effort — the flush outcome doesn't change the
    verdict) and returns True unconditionally when a hold exists.
    """
    from backend.api.routes.orders_place import (
        _TPL_ATTACH_PERSIST_PENDING, _opp_flush_pending_persist,
    )
    if row_id in _TPL_ATTACH_PERSIST_PENDING:
        await _opp_flush_pending_persist(row_id)
        return True

    from sqlalchemy import select as _sel_rco
    from backend.api.database import async_session as _async_s_rco
    from backend.api.models import AlgoOrder as _AO_rco

    async with _async_s_rco() as _s:
        _val = (await _s.execute(
            _sel_rco(_AO_rco.attached_gtts_json).where(_AO_rco.id == row_id)
        )).scalar_one_or_none()
    return bool(_val)


async def _rco_run_template_attach(row) -> "tuple | None":
    """Run apply_template_to_order for *row* and persist attached_gtts_json.

    Returns (result, payload_persisted) where payload_persisted is True
    when attached_gtts_json was written to the row. Returns None when
    apply_template_to_order returned no result, OR when a concurrent
    fill-triggered attach already populated attached_gtts_json while
    this call was waiting for the per-row lock (see
    _rco_attached_gtts_already_set).

    Must be called within an active SQLAlchemy session context (row is
    session-attached). The caller must commit after this returns.
    """
    from backend.api.algo.template_attach import apply_template_to_order

    apply_path = "sim" if (row.mode or "").lower() == "sim" else "live"
    _retry_overrides = _retry_parse_overrides(row.template_overrides_json)
    # Same per-row lock the fill-triggered path (_fire_template_attach_on_fill)
    # uses, now that the live apply_plan_live chain runs off the event loop
    # (asyncio.to_thread) and can genuinely interleave with a concurrent
    # fill-triggered attach on the same row — without this, an operator
    # Retry-attach racing a late postback/chase-terminal attach could place
    # a duplicate live GTT instead of being accidentally serialised by the
    # old loop-blocking behaviour.
    _row_lock = await _get_template_attach_lock(row.id)
    async with _row_lock:
        # T13 fix — re-check attached_gtts_json INSIDE the lock, not just
        # at the call site's precheck (which runs before the lock is
        # acquired and can race a concurrent fill-triggered attach).
        if await _rco_attached_gtts_already_set(row.id):
            logger.info(
                "[TPL-ATTACH] retry-attach for #%s skipped — a concurrent "
                "fill-triggered attach already populated attached_gtts_json "
                "while this retry was waiting for the per-row lock",
                row.id,
            )
            return None
        result = await apply_template_to_order(
            template_id=row.template_id,
            template_slug=None,
            overrides=_retry_overrides,
            parent_account=row.account or "",
            parent_symbol=row.symbol or "",
            parent_side=row.transaction_type or "BUY",
            parent_qty=await _retry_effective_parent_qty(row),
            parent_exchange=row.exchange or "NFO",
            parent_fill_price=float(row.fill_price or row.initial_price or 0),
            parent_product=row.product or "NRML",
            parent_order_id=row.id,
            parent_agent_id=getattr(row, "agent_id", None),
            apply_path=apply_path,
        )
    return result


def _rco_persist_attach_payload(row, result, session) -> bool:
    """Write the attached_gtts_json payload onto *row* from *result*.
    Returns True when a payload was written, False when empty.
    Assumes the session's commit will be called by the caller.
    """
    _attached_payload = _retry_build_attached_payload(result, row.product or "NRML")
    if _attached_payload:
        import json as _json_retry
        row.attached_gtts_json = _json_retry.dumps(_attached_payload)
        if result.errors:
            row.detail = ((row.detail or "")[:200]
                          + " · retry attach: "
                          + "; ".join(result.errors[:2]))[:240]
        return True
    return False


async def _rco_dispatch_kill(row) -> str:
    """Dispatch the mode-appropriate cancel for a chase row.
    Returns an error string on failure, empty string on success.
    """
    mode = (row.mode or "").lower()
    if mode == "paper":
        return await _rco_kill_paper_mode(row)
    if mode == "live":
        return await _rco_kill_live_mode(row)
    return ""


def _rco_apply_kill_status(row, err_msg: str) -> None:
    """Stamp CANCELLED or CANCEL_FAILED status + detail onto the row."""
    if err_msg:
        row.status = "CANCEL_FAILED"
        row.detail = ((row.detail or "")[:200] + f" · cancel failed: {err_msg}")
    else:
        row.status = "CANCELLED"
        row.detail = ((row.detail or "")[:200] + " · killed by operator")


# ── 2026-10-23 Kill / CANCEL_FAILED lifecycle fix ───────────────────────
# Three linked bugs in the kill path (see .claude/PLAN.md cluster 1A):
#   1. Kill was keyed only by a `broker_order_id` snapshot read at click
#      time — if chase had just cancelled-and-replaced under a NEW id in
#      the window around the click, the broker cancel targeted a dead
#      id, failed, and the row was wrongly stamped CANCEL_FAILED while
#      chase kept chasing under the new id, unaware. Fixed via
#      `chase.mark_order_killed()` (algo-order-id-keyed, survives a
#      broker_order_id swap — see its own docstring) plus a fresh
#      re-read of the row's current state right before dispatching the
#      broker cancel.
#   2. The HTTP response always returned `ok: true` even when the
#      broker cancel genuinely failed. Fixed below — `ok` now reflects
#      whether the row actually ended up CANCELLED (or was already
#      resolved).
#   3. A CANCEL_FAILED row could never be re-killed (gate required
#      status == "OPEN"), and never aged out on its own. Fixed: the
#      gate now also accepts CANCEL_FAILED, a retry first checks real
#      broker truth (the earlier failed attempt may have actually
#      landed), and a retry whose broker call ALSO fails force-resolves
#      a row that predates the current trading session — a day-validity
#      order genuinely cannot still be resting from a prior session.

_KILL_RETRYABLE_STATUSES = frozenset({"OPEN", "CANCEL_FAILED"})


def _rco_kill_already_terminal_response(status: str) -> dict:
    """Early-return response for a row this endpoint will not act on.
    FILLED/REJECTED are genuinely final — nothing could still be
    cancelled, so `ok: false` (the kill did not, and could not, achieve
    anything — never read this as a successful kill). Every other
    non-retryable status (already CANCELLED, UNFILLED, HELD, ...) is
    the desired end-state already reached — `ok: true`.
    """
    ok = status not in ("FILLED", "REJECTED")
    return {
        "ok": ok, "already_terminal": True, "status": status,
        "err": None if ok else f"order already {status.lower()} — nothing to kill",
    }


async def _rco_kill_resolve_cancel_failed(row) -> "str | None":
    """For a CANCEL_FAILED retry, consult the canonical single-order-view
    accessor (`orders_helpers.get_broker_order_truth`) BEFORE attempting
    another broker cancel call — the earlier failed attempt may have
    actually landed at the broker (or the order may have since filled),
    just never reflected onto this row. Returns the resolved terminal
    status ('CANCELLED'/'FILLED'/'REJECTED') when broker truth already
    answers the question; `None` when the order is genuinely still
    resting at the broker, or when broker truth is unavailable — either
    way a real cancel attempt is still required.
    """
    broker_order_id = str(getattr(row, "broker_order_id", "") or "")
    if not broker_order_id:
        return None
    try:
        from backend.api.routes.orders_helpers import get_broker_order_truth
        truth = await get_broker_order_truth(broker_order_id)
    except Exception:
        return None
    if not truth.available or truth.algo_status is None:
        return None
    if truth.algo_status in ("CANCELLED", "FILLED", "REJECTED"):
        return truth.algo_status
    return None


def _rco_kill_predates_current_session(row) -> bool:
    """True when *row* was created in a PRIOR trading session (the
    [08:00 IST, next 08:00 IST) boundary this codebase uses everywhere
    else — see `expiry_freeze.session_date_of`). Every order this app
    places is day-validity — a row from a prior session cannot still be
    genuinely resting live at the broker, no matter what our own stale
    CANCEL_FAILED status says or what the broker's cancel call just
    returned (almost certainly "order not found" — the broker's own
    book has long since rolled over)."""
    from backend.api.algo.expiry_freeze import session_date_of
    created_at = getattr(row, "created_at", None)
    if created_at is None:
        return False
    return session_date_of(created_at) < session_date_of(datetime.now(timezone.utc))


def _rco_kill_force_resolve_stale(row, err_msg: str) -> None:
    """Force-resolve a CANCEL_FAILED retry whose broker cancel ALSO
    failed, for a row that predates the current trading session (see
    `_rco_kill_predates_current_session`) — the operator's ONLY path to
    clear a permanently-stuck stale CANCEL_FAILED row, reusing the
    existing Kill button rather than adding new UI/endpoints. Resolves
    to CANCELLED (deliberately non-final — a genuinely-confused late
    fill postback can still apply) with an explicit note distinguishing
    this from a real broker-confirmed cancel."""
    row.status = "CANCELLED"
    row.detail = ((row.detail or "")[:200]
                  + f" · force-resolved by operator: order predates current "
                  f"trading session, broker has no record ({err_msg}) — "
                  f"verify positions/order book manually")


# Broker error text that genuinely means "this order no longer exists at
# the broker" — distinct from e.g. a wrong `variety` parameter (AMO rows
# use variety='amo', but `_rco_kill_live_mode` always sends 'regular' —
# a pre-existing, separately-tracked gap, deferred per .claude/PLAN.md).
# A variety mismatch raises a DIFFERENT kind of broker error than "not
# found", so this list deliberately does NOT widen to match every
# failure — the stale force-resolve path below must never fire for an
# AMO order that's still genuinely resting live just because its cancel
# call used the wrong variety.
_RCO_KILL_GONE_HINTS = (
    "not found", "does not exist", "no such order", "unable to find",
    "invalid order id", "order id does not exist",
)


def _rco_kill_error_looks_gone(err_msg: str) -> bool:
    """True when a broker cancel-failure message itself indicates the
    order genuinely no longer exists — the narrowing condition for the
    stale-CANCEL_FAILED force-resolve path (see `_rco_kill_force_resolve_stale`
    and `_RCO_KILL_GONE_HINTS` above)."""
    msg = (err_msg or "").lower()
    return any(h in msg for h in _RCO_KILL_GONE_HINTS)


async def _rco_kill_resolve_dispatch_race(s, row, row_id: int) -> "tuple[str, object, bool]":
    """Dispatch the broker-level cancel for *row*; if it fails WHILE
    chase is still actively running this exact row, re-resolve the
    row's CURRENT broker_order_id and retry once against the fresh id
    before giving up — closing the race where the operator's earlier
    read and this dispatch straddle a chase cancel-and-replace cycle
    that swapped in a brand-new broker_order_id.

    Returns (err_msg, row_to_use, pending):
      - `pending=True` means chase is STILL actively resolving this row
        after the retry also failed — the caller must NOT write any
        terminal status here. Chase's own loop (already aware of the
        algo-order-level kill intent `kill_chase` marked earlier) owns
        finalizing this row; a blind write here could stomp chase's
        own correct terminal write with a stale derived guess.
    """
    from backend.api.algo.chase import is_chase_active, mark_killed

    old_id = str(getattr(row, "broker_order_id", "") or "")
    err_msg = await _rco_dispatch_kill(row)
    if not err_msg or not is_chase_active(row_id):
        return err_msg, row, False

    # `row` is already identity-mapped in session `s` — a plain re-SELECT
    # by primary key within the SAME session returns the SAME cached
    # Python object (SQLAlchemy identity map), NOT a fresh read from the
    # DB. `session.refresh()` is the correct idiom to pick up a change
    # chase's OWN (different) session may have committed in the
    # meantime — e.g. its cancel-and-replace writing a new
    # broker_order_id via `_sync_algo_order_id`.
    try:
        await s.refresh(row)
    except Exception:
        return err_msg, row, False
    new_id = str(getattr(row, "broker_order_id", "") or "")
    if not new_id or new_id == old_id:
        # Nothing changed — the failure wasn't caused by a
        # broker_order_id swap, so a blind retry wouldn't help.
        return err_msg, row, False

    mark_killed(new_id)
    retry_err = await _rco_dispatch_kill(row)
    if not retry_err:
        return "", row, False
    if is_chase_active(row_id):
        return retry_err, row, True
    return retry_err, row, False


async def _rco_kill_apply_status_without_stomping(s, row, err_msg: str) -> None:
    """Re-check the row's CURRENT persisted status before writing a
    derived CANCELLED/CANCEL_FAILED guess — a concurrent chase terminal
    write (fire-and-forget, committed via a DIFFERENT session/
    transaction) may have already resolved this row while this request
    was dispatching the broker cancel above. Never overwrite an
    already-final (FILLED/REJECTED) or already-CANCELLED status with a
    stale derived one."""
    from sqlalchemy import select as _sql_select3
    from backend.api.models import AlgoOrder as _AlgoOrder3, ALGO_ORDER_FINAL_STATUSES
    fresh_status = (await s.execute(
        _sql_select3(_AlgoOrder3.status).where(_AlgoOrder3.id == row.id)
    )).scalar_one_or_none()
    if fresh_status in ALGO_ORDER_FINAL_STATUSES or fresh_status == "CANCELLED":
        row.status = fresh_status
        return
    _rco_apply_kill_status(row, err_msg)




def _rco_parse_preflight_identity(body: dict) -> tuple:
    """Extract account / exchange / symbol / qty / order classification fields."""
    account       = str(body.get("account") or "").strip()
    exchange      = str(body.get("exchange") or "NFO").strip().upper()
    tradingsymbol = str(body.get("tradingsymbol") or "").strip().upper()
    quantity      = int(body.get("quantity") or 0)
    order_type    = str(body.get("order_type") or "LIMIT").strip().upper()
    product       = str(body.get("product") or "NRML").strip().upper()
    return account, exchange, tradingsymbol, quantity, order_type, product


def _rco_parse_preflight_scalars(body: dict) -> tuple:
    """Extract and coerce scalar order fields from the preflight JSON body.
    Returns (account, exchange, tradingsymbol, quantity, order_type,
    product, variety, side, price, trigger_price, intent).
    """
    account, exchange, tradingsymbol, quantity, order_type, product = _rco_parse_preflight_identity(body)
    variety       = str(body.get("variety") or "regular").strip().lower()
    side          = str(body.get("side") or body.get("transaction_type") or "BUY").strip().upper()
    price         = float(body.get("price") or 0)
    trigger_price = float(body.get("trigger_price") or 0)
    intent        = body.get("intent") or None
    return account, exchange, tradingsymbol, quantity, order_type, product, variety, side, price, trigger_price, intent


def _rco_parse_preflight_body(body: dict) -> tuple:
    """Parse and coerce the raw JSON body for `preflight_order`.
    Returns (account, exchange, tradingsymbol, quantity, order_type,
    product, variety, side, price, trigger_price, intent, paired_legs)."""
    scalars = _rco_parse_preflight_scalars(body)
    paired_legs = body.get("paired_legs") or []
    if not isinstance(paired_legs, list):
        paired_legs = []
    return scalars + (paired_legs,)


def _rco_validate_preflight_params(
    account: str, tradingsymbol: str, quantity: int, exchange: str, side: str,
) -> None:
    """Validate required fields for preflight_order. Raises HTTPException on failure."""
    if not account:
        raise HTTPException(status_code=400, detail="account is required")
    if not tradingsymbol:
        raise HTTPException(status_code=400, detail="tradingsymbol is required")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    if exchange not in _EXCHANGES:
        raise HTTPException(status_code=400,
            detail=f"exchange must be one of {sorted(_EXCHANGES)}")
    if side not in _TXN_TYPES:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")


_GROWW_EXCHANGE_TO_KITE: dict[str, str] = {
    "NSE": "NSE", "BSE": "BSE", "NFO": "NFO", "BFO": "BFO",
    "MCX": "MCX", "CDS": "CDS",
    "NSE_EQ": "NSE", "BSE_EQ": "BSE",
    "NSE_FNO": "NFO", "BSE_FNO": "BFO", "MCX_COMM": "MCX",
}


def _groww_parse_payload(body: dict) -> tuple[str, str, str, str, object, object]:
    """Extract and normalise core fields from a Groww postback payload.

    Returns (order_id, status, symbol, txn, qty, price).
    All ``or`` chains are concentrated here so the parent method stays
    free of boolean-expression CC inflation.
    """
    order_id = str(body.get("groww_order_id") or body.get("order_id") or "")
    status   = str(body.get("order_status")   or body.get("status")   or "").upper()
    symbol   = body.get("trading_symbol")      or body.get("symbol")   or ""
    txn      = body.get("transaction_type")    or ""
    qty      = body.get("filled_quantity")     or body.get("quantity") or 0
    price    = body.get("average_price")       or body.get("price")    or 0
    return order_id, status, symbol, txn, qty, price


async def _subscribe_filled_pairs(pairs: "list[tuple[str, str]]") -> None:
    """Wake the performance task and subscribe newly-filled instruments'
    ticker tokens so live ticks are addressable by symbol immediately,
    instead of waiting up to 5 minutes for the next `_task_performance`
    cycle's own `subscribe_with_sym()` call.

    Shared by every fill-detection path that can mark an AlgoOrder row
    FILLED outside — or in addition to — the primary Kite postback flow:
    Dhan/Groww postback, the open-order-watchdog reconcile sweep, the
    admin/per-card reconcile endpoints, and chase's own terminal-fill
    update. `pairs` is a list of (tradingsymbol, exchange) in Kite
    canonical form (matches `AlgoOrder.symbol` / `AlgoOrder.exchange`
    and the Dhan/Groww-normalised tradingsymbol).

    Deliberately uses `subscribe_with_sym()`, NOT the bare `subscribe()`.
    Plain `subscribe()` registers the token with the ticker's live socket
    so Kite starts streaming it, but it does NOT populate
    `_token_to_sym` / `_sym_to_token` — every tick is then published to
    the SSE bus with `sym=""`, and every symbol-keyed frontend consumer
    (`symbolStore`'s `getSnapshot(sym)`) silently drops it. The tick only
    becomes usable once the next `_task_performance` cycle's
    `subscribe_with_sym()` backfills the symbol mapping — i.e. the exact
    "lags up to 5 minutes" symptom this function exists to close.

    Best-effort: kick_performance() always fires on a non-empty call;
    token resolution + subscribe is skipped (not raised) on any failure
    or when a pair can't be resolved to a token.
    """
    try:
        from backend.api.background import kick_performance
        kick_performance()
        if not pairs:
            return
        from backend.api.persistence.instruments_store import get_or_fetch_instruments
        from backend.brokers.kite_ticker import get_ticker

        _ticker = get_ticker()
        _tok_cache: dict[str, dict] = {}
        batch: list[tuple[int, str]] = []
        for sym, exch in pairs:
            sym_u  = str(sym or "").upper()
            exch_u = str(exch or "").upper()
            if not sym_u or not exch_u:
                continue
            tok_map = _tok_cache.get(exch_u)
            if tok_map is None:
                tok_map = await get_or_fetch_instruments(exch_u)
                _tok_cache[exch_u] = tok_map
            tok = tok_map.get((sym_u, exch_u))
            if tok:
                batch.append((int(tok), sym_u))
        if batch:
            _ticker.subscribe_with_sym(batch)
            logger.info(
                f"post-fill resubscribe: {[s for _, s in batch]}"
            )
    except Exception as _sub_exc:
        logger.warning(f"post-fill resubscribe failed: {_sub_exc}")


async def _groww_handle_complete_fill(body: dict, symbol: str) -> None:
    """Wake the performance task and subscribe the filled instrument's ticker token.

    Called only when the mapped kite_status is ``COMPLETE``.
    Groww exchange strings are normalised to Kite canonical names via
    ``_GROWW_EXCHANGE_TO_KITE`` before the instrument lookup. Delegates
    to `_subscribe_filled_pairs` (shared with the reconcile/chase gap
    fixes) for the actual kick + resolve + subscribe_with_sym.
    """
    raw_exchange  = str(body.get("exchange") or body.get("segment") or "").upper()
    kite_exchange = _GROWW_EXCHANGE_TO_KITE.get(raw_exchange, raw_exchange)
    await _subscribe_filled_pairs([(symbol, kite_exchange)])


class OrdersController(Controller):
    path = "/api/orders"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_orders(self, request: Request) -> OrdersResponse:
        try:
            await ensure_lot_index()
            resp = await get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL)
            # Mask account codes for everyone who is NOT admin/designated.
            # Copy-not-mutate so the shared cache doesn't keep the masked
            # codes (was the demo→signin lag bug — first masked caller
            # poisoned the cached resp for subsequent admin requests).
            if not is_admin_request(request):
                import msgspec as _ms
                return _ms.structs.replace(
                    resp,
                    rows=[_ms.structs.replace(
                        r, account=mask_account(r.account)
                    ) for r in resp.rows],
                )
            return resp
        except Exception as e:
            logger.error(f"Orders API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @get("/algo/recent")
    async def list_algo_orders(self, request: Request, n: int = 100, mode: str = "all") -> list[AlgoOrderInfo]:
        """
        Recent agent-generated orders from the algo_orders table.

        `mode`:
          - "all"  (default) → every row, newest first. Order-log tab on the
            agents page uses this so operators see both real and simulated
            fires with a single fetch.
          - "live" → mode='live' only
          - "sim"  → mode='sim'  only

        Response includes `initial_price` (the LIMIT price = sim's LTP at
        trigger time, or the broker-submitted price in live mode), so the
        UI can show "SELL 50 NIFTY @ ₹175.50" inline.
        """
        from sqlalchemy import desc, select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        async with async_session() as s:
            q = sql_select(AlgoOrder).order_by(desc(AlgoOrder.id)).limit(max(1, min(n, 500)))
            if mode in ("live", "sim", "paper", "replay", "shadow", "draft"):
                q = q.where(AlgoOrder.mode == mode)
            rows = (await s.execute(q)).scalars().all()
            child_map = await _fetch_child_order_ids(s, [r.id for r in rows])
        # Mask account codes for everyone who is NOT admin/designated.
        # Partner JWTs see masked codes too (audit fix). Same masking
        # the /performance grids apply — turns ZG0790 into ZG####.
        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [_chase_row_to_info(r, masked_acct, child_map) for r in rows]

    @get("/chases/active")
    async def list_active_chases(self, request: Request) -> list[AlgoOrderInfo]:
        """In-flight chase orders — algo_orders rows in OPEN state
        across paper / live / shadow. Sorted newest-first.

        Two cheap inline reconciles before returning so the card
        never surfaces an order that's already dead (operator: "how
        can an order present in chase in flight when they are no
        open orders"):
          1. paper rows whose id isn't in the paper engine's open
             set — marked UNFILLED in DB, dropped from response.
          2. live rows with no broker_order_id (placement never
             succeeded) — marked REJECTED in DB, dropped.
          3. live rows WITH a broker_order_id reconcile via the
             canonical single-order-view accessor (2026-10-23 order-poll
             consolidation — see the module-level note above
             `_single_row_locked_update`).

        Live rows that DO carry a broker_order_id and resolve as
        genuinely in-flight are trusted; the operator-triggered
        reconcile endpoint does a forced-fresh broker round-trip when
        they want a thorough sweep.

        2026-10 fix (lock scoping, preserved by the 2026-10-23
        consolidation above) — this panel is polled every ~3s from
        several simultaneously-mounted UI components (`/orders`,
        `SymbolPanel`, `ActivityLogModal`), so it must never take a row
        lock across the whole unlocked batch on every poll. Each
        per-mode helper below only ever opens a session + takes a lock
        for a row it has ALREADY determined (via a cheap, lock-free
        check) needs a write — `get_broker_order_truth` itself does no
        DB work at all, and `apply_order_truth` scopes its own lock to
        exactly one row. The common case (nothing to reconcile) still
        takes zero locks. No alert fires from this endpoint — see
        `backend/api/algo/order_poll.py`'s module docstring for why
        alerting is reserved for the automatic background correctors
        (the watchdog, the periodic sweep, postback's follow-up) and
        deliberately excluded here (polled every ~3s — would spam).
        """
        from sqlalchemy import desc, select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        # Snapshot paper engine's open-order ids ONCE before the DB
        # query so we don't pay for the lock on every row.
        _paper_open_ids = _chase_snapshot_paper_open_ids()

        # Snapshot live broker orders (cached 15 s in get_or_fetch).
        # Operator: "once chase reconciled and order completed it should
        # not show in chase reconcile card. example GOLDM 146000 strike
        # completed but still listed." Postbacks can drop, leaving the
        # row stuck at OPEN; the panel polled /chases/active every 3 s
        # but never round-tripped to the broker, so a FILLED order kept
        # rendering until the operator clicked Reconcile (which also
        # had the KiteConnection bug — separate fix in this commit).
        # Inline broker lookup uses the existing /api/orders cache so we
        # pay 1 broker.orders() per account per 15 s, not 1 per 3 s poll.
        # Fed into `get_broker_order_truth(..., broker_status_by_id=...)`
        # per row below so a sweep over many rows costs one fetch, not
        # one per row.
        _broker_status_by_id = await _chase_snapshot_broker_status_by_id()

        async with async_session() as s:
            # Audit fix (H-1) — also include CANCEL_FAILED rows so the
            # operator sees orders where the Kill click hit a broker
            # failure. Pre-fix these rows fell off the chase grid
            # (status != OPEN) and the operator had no surface short of
            # browsing /orders. The order is still LIVE at the broker;
            # recovery is via Reconcile or another Kill attempt.
            # 2026-10 fix — exclude mode='draft'. Every mode other than
            # 'paper'/'live' falls through to kept.append(r) below
            # unconditionally, so a draft row (status='OPEN' by
            # construction, never cleaned up by a chase/postback since
            # it never reaches a broker) would otherwise sit in this
            # "in-flight chase" panel forever.
            #
            # UNLOCKED read — no `.with_for_update()` here. Deliberately
            # NOT given the FINAL-status write-refuse guard the
            # postback/chase paths have — reconcile is the repair path
            # that corrects stuck rows from broker truth, so it must be
            # able to write any status (enforced per-row by
            # `apply_order_truth` / `_single_row_locked_update` below,
            # not here).
            unlocked_rows = (await s.execute(
                sql_select(AlgoOrder)
                .where(
                    AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"]),
                    AlgoOrder.mode != "draft",
                )
                .order_by(desc(AlgoOrder.id))
                .limit(500)
            )).scalars().all()

            kept: list = []
            filled_ids: list[int] = []
            for r in unlocked_rows:
                mode = (r.mode or "").lower()
                if mode == "paper":
                    if await _rco_reconcile_paper_row(r, _paper_open_ids):
                        continue
                elif mode == "live":
                    if not (r.broker_order_id or "").strip():
                        if await _rco_reconcile_no_id_row(r):
                            continue
                    else:
                        dropped, filled = await _rco_reconcile_live_row(
                            r, _broker_status_by_id,
                        )
                        if dropped:
                            if filled:
                                filled_ids.append(r.id)
                            continue
                kept.append(r)

            child_map = await _fetch_child_order_ids(s, [r.id for r in kept])

        # Template-attach fires AFTER the lock above releases, same as
        # before — `apply_order_truth` already wrote the FIFO ledger
        # entry and committed for every row in `filled_ids`.
        if filled_ids:
            async with async_session() as _s2:
                _filled_rows = (await _s2.execute(
                    sql_select(AlgoOrder).where(AlgoOrder.id.in_(filled_ids))
                )).scalars().all()
            for _filled_row in _filled_rows:
                _maybe_fire_template_attach_for_reconcile(_filled_row)

        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [_chase_row_to_info(r, masked_acct, child_map) for r in kept]

    @post("/chases/{algo_order_id:int}/kill", guards=[admin_guard])
    async def kill_chase(self, algo_order_id: int, request: Request) -> dict:
        """Cancel an in-flight chase — best-effort across paper, live,
        and shadow modes. Admin-only.

        - Paper: delegate to engine.cancel_paper_order() which writes the
          canonical AlgoOrderEvent(kind='cancel') row and flips DB status
          via _safe_update_algo_order_cancel.
        - Live: call broker.cancel_order(variety, broker_order_id).
        - Shadow: just flip the row's status (nothing real to cancel).
        Sets AlgoOrder.status='CANCELLED' (or CANCEL_FAILED on error) and
        writes a 'killed' event so the timeline reflects the operator action.

        2026-10-23 Kill-lifecycle fix (see `_rco_kill_already_terminal_response`
        and friends above for the per-bug rationale):
        - Re-killable: a row already at CANCEL_FAILED may be re-killed
          (not just OPEN) — it is deliberately non-final; the earlier
          cancel may have failed while the order was still genuinely
          resting, or it may already be resolved at the broker.
        - `ok` in the response reflects whether the kill actually
          succeeded (row ended up CANCELLED/resolved-non-FILLED), never
          a blanket `true`.
        - The algo-order-id-level kill intent (`chase.mark_order_killed`)
          is set BEFORE any broker I/O and BEFORE re-reading the row's
          current broker_order_id, so a concurrently-running chase loop
          sees the kill regardless of which broker_order_id it is
          currently chasing under.
        """
        from sqlalchemy import select as _sql_select
        from datetime import datetime, timezone
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.api.algo.order_events import write_event as _write_event
        from backend.api.algo.chase import mark_order_killed, mark_killed

        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == algo_order_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="order not found")
            if row.status not in _KILL_RETRYABLE_STATUSES:
                return _rco_kill_already_terminal_response(row.status)

            was_cancel_failed = row.status == "CANCEL_FAILED"

            # Mark the kill intent at the ALGO-ORDER level first, before
            # any broker call — a concurrently-running chase loop may be
            # mid cancel-and-replace right now, under a broker_order_id
            # different from the one we're about to read below. Marking
            # this signal first means the chase loop's very next guard
            # check (`_ch_loop_guards` / `_ch_post_replace_kill_check`)
            # sees the kill regardless of timing.
            mark_order_killed(algo_order_id)
            if (row.broker_order_id or "").strip():
                mark_killed(str(row.broker_order_id))

            if was_cancel_failed:
                _resolved = await _rco_kill_resolve_cancel_failed(row)
                if _resolved is not None:
                    row.status = _resolved
                    row.detail = ((row.detail or "")[:200]
                                  + f" · resolved via broker truth: {_resolved}")
                    await s.commit()
                    _row_id = row.id
                    _ok = _resolved != "FILLED"
                    invalidate("orders")
                    await _write_event(
                        _row_id, "killed",
                        f"Chase kill resolved via broker truth: {_resolved}",
                        payload={
                            "err": None if _ok else f"order {_resolved} at broker",
                            "ts": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    return {
                        "ok": _ok, "status": _resolved,
                        "err": None if _ok else f"order already {_resolved.lower()} at broker",
                    }

            _row_id = row.id

        # Re-resolve the row's CURRENT state in a fresh read right
        # before dispatching the broker cancel — narrows the staleness
        # window between the gate check above and the real broker call.
        # The remaining window is closed by the algo-order-level kill
        # intent marked above (a concurrent chase loop self-aborts on
        # its own next check) rather than by this re-read alone.
        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == _row_id)
            )).scalar_one_or_none()
            if row is None:
                return {"ok": False, "err": "order vanished during kill"}
            if row.status not in _KILL_RETRYABLE_STATUSES:
                # Chase's own loop (now aware of the kill intent marked
                # above) may already have finalized the row between the
                # two reads.
                return _rco_kill_already_terminal_response(row.status)

            err_msg, row, pending = await _rco_kill_resolve_dispatch_race(s, row, _row_id)
            if pending:
                # Chase is still actively cancelling its own replacement
                # order for this row right now — do not write a terminal
                # status; chase's loop (aware of the kill intent marked
                # above) owns finalizing it. Writing here could stomp a
                # correct terminal write chase is about to make.
                invalidate("orders")
                return {
                    "ok": False, "pending": True, "status": "OPEN",
                    "err": "chase is still cancelling the replacement "
                           "order — retry shortly",
                }
            if (err_msg and was_cancel_failed
                    and _rco_kill_error_looks_gone(err_msg)
                    and _rco_kill_predates_current_session(row)):
                _rco_kill_force_resolve_stale(row, err_msg)
                err_msg = ""
            else:
                await _rco_kill_apply_status_without_stomping(s, row, err_msg)
            await s.commit()
            _final_status = row.status

        invalidate("orders")
        await _write_event(
            _row_id, "killed",
            f"Chase killed by operator{' (' + err_msg + ')' if err_msg else ''}",
            payload={"err": err_msg or None,
                     "ts": datetime.now(timezone.utc).isoformat()},
        )
        return {"ok": not bool(err_msg), "err": err_msg or None, "status": _final_status}

    @post("/{algo_order_id:int}/retry-template", guards=[admin_guard])
    async def retry_template(self, algo_order_id: int, request: Request) -> dict:
        """Re-run apply_template_to_order against an already-filled
        parent. Useful when the original attach failed silently — e.g.
        a SELL option with wing_premium_pct=10 picked nothing because
        every chain candidate failed the OI gate (fixed forward in
        d826590e, but pre-existing positions need this to catch up).

        Idempotent: bails when `attached_gtts_json` is already set,
        when the order isn't FILLED, or when no template was attached
        in the first place. Operator gets a clear reason string back
        rather than a generic 4xx.
        """
        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder, OrderTemplate, Agent

        async with async_session() as s:
            row = (await s.execute(
                _sql_select(AlgoOrder).where(AlgoOrder.id == algo_order_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="order not found")
            _precheck = _retry_precheck_row(row)
            if _precheck is not None:
                return _precheck

            # Sanity-check the template row still exists before
            # dispatching — apply_template_to_order does its own load via
            # load_template_for_slug_or_id, but we want a clear "deleted"
            # reason for the operator rather than a "no template found"
            # surface from deep inside the attach pipeline.
            tpl = (await s.execute(
                _sql_select(OrderTemplate.id).where(OrderTemplate.id == row.template_id)
            )).scalar_one_or_none()
            if tpl is None:
                return {"ok": False, "reason": f"template #{row.template_id} no longer exists"}
            # Sprint E — `apply_template_to_order` now resolves via the
            # Agent(kind='bracket') mirror, NOT `OrderTemplate` directly
            # (see template_attach.load_template_for_slug_or_id). The
            # OrderTemplate row existing is no longer sufficient on its
            # own — defense-in-depth against the two tables somehow
            # disagreeing (should be structurally impossible, since
            # OrderTemplateController writes both atomically; this
            # catches it with a clear reason instead of a confusing
            # "no result" surface from deep inside the attach pipeline).
            mirror = (await s.execute(
                _sql_select(Agent.id).where(
                    Agent.kind == "bracket",
                    Agent.legacy_template_id == row.template_id,
                )
            )).scalar_one_or_none()
            if mirror is None:
                return {"ok": False, "reason":
                        f"bracket mirror for template #{row.template_id} is "
                        f"missing — the live resolver cannot arm this fill"}

            result = await _rco_run_template_attach(row)
            if result is None:
                return {"ok": False, "reason": "apply_template_to_order returned no result (template missing or empty plan)"}
            if _rco_persist_attach_payload(row, result, s):
                await s.commit()
                await s.refresh(row)

        # Sc.5c — distinguish full success from silent no-op. When the
        # plan resolved but produced no GTTs and no wing (all branches
        # rejected by overrides or chain-scan), the response previously
        # claimed `ok: true, attached: false` which read like a success.
        # Treat the empty-payload case as a failure with a clear reason.
        return _retry_build_result_response(
            result, row.attached_gtts_json is not None,
        )

    @post("/algo/reconcile")
    async def reconcile_algo_orders(self, request: Request) -> dict:
        """Admin sweep — re-syncs stale OPEN algo_orders rows against the
        broker via the canonical single-order-view accessor
        (`orders_helpers.py:get_broker_order_truth` / `apply_order_truth`).
        Operator-triggered after observing rows stuck at OPEN when the
        broker has already moved the order to a terminal status
        (postback miss, network drop, etc.).

        2026-10-23 (order-poll consolidation) — previously this route
        ran its own `_rco_reconcile_account`/`_rco_reconcile_one_row`
        logic, which unconditionally guessed UNFILLED whenever the
        broker no longer carried the order_id — including a transient
        API glitch or an empty response. Now: an unavailable broker
        response leaves the row untouched ("missing" below means
        *unavailable, left untouched*, not "confirmed gone" — see
        `OrderTruth.available`'s own docstring), never a guess.
        `invalidate("orders")` busts the 15s broker-snapshot cache
        FIRST so this operator-triggered sweep always reads fresh
        broker state, not up to 15s of staleness. `withhold_if_active`
        protects against finalizing a row the chase engine is actively
        mid-retry on (same `_pb_should_withhold_status` guard every
        other writer in this codebase now shares).
        """
        if not is_admin_request(request):
            raise HTTPException(status_code=403, detail="admin only")

        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.api.routes.orders_helpers import (
            get_broker_order_truth, apply_order_truth, _broker_order_snapshot,
        )

        # Pull every live OPEN algo_orders row.
        async with async_session() as s:
            rows = (await s.execute(
                _sql_select(AlgoOrder).where(
                    AlgoOrder.mode == "live",
                    AlgoOrder.status == "OPEN",
                )
            )).scalars().all()

        if not rows:
            return {"scanned": 0, "updated": 0, "missing": 0}

        invalidate("orders")
        broker_status_by_id = await _broker_order_snapshot()

        updated = 0
        missing = 0
        filled_ids: list[int] = []
        for r in rows:
            bid = str(r.broker_order_id or "")
            if not bid:
                continue
            truth = await get_broker_order_truth(bid, broker_status_by_id)
            if not truth.available:
                missing += 1
                continue
            if truth.algo_status is None:
                continue
            if await apply_order_truth(r.id, truth, withhold_if_active=True):
                updated += 1
                if truth.algo_status == "FILLED":
                    filled_ids.append(r.id)

        if updated:
            invalidate("orders")

        # Re-fetch the freshly-filled rows for attach/subscribe — the
        # `rows` fetched above predate every mutation above (each made
        # in `apply_order_truth`'s own locked session), so their
        # fill_price/filled_quantity/template fields are stale.
        attach_rows: list = []
        if filled_ids:
            async with async_session() as s2:
                attach_rows = (await s2.execute(
                    _sql_select(AlgoOrder).where(AlgoOrder.id.in_(filled_ids))
                )).scalars().all()
        for _r in attach_rows:
            _maybe_fire_template_attach_for_reconcile(_r)
        # Gap fix (2026-10): this sweep can mark a row FILLED without
        # ever going through the primary postback flow (which already
        # kicks the performance task + resubscribes the ticker on
        # fill) — without this, a newly-filled position's WS tick
        # subscription silently waits for the next _task_performance
        # cycle (up to 5 min).
        if attach_rows:
            await _subscribe_filled_pairs(
                [(_r.symbol, _r.exchange) for _r in attach_rows]
            )

        return {"scanned": len(rows), "updated": updated, "missing": missing}

    @post("/{broker_order_id:str}/reconcile")
    async def reconcile_single_order(
        self,
        broker_order_id: str,
        request: Request,
        data: ReconcileSingleRequest,
    ) -> dict:
        """Per-card reconcile — re-sync ONE order against the broker via
        the canonical single-order-view accessor. Operator-triggered
        from the OrderCard Reconcile button so a stuck row can be
        cleared without running the full sweep across every account.

        2026-10-23 (order-poll consolidation) — a missing/unavailable
        broker response now leaves the row untouched with an explicit
        `note`, instead of the old unconditional UNFILLED guess.
        `invalidate("orders")` forces a fresh broker snapshot first —
        an operator clicking Reconcile wants broker truth NOW, not a
        cached read.
        """
        if not is_admin_request(request):
            raise HTTPException(status_code=403, detail="admin only")

        from sqlalchemy import select as _sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from backend.api.routes.orders_helpers import (
            get_broker_order_truth, apply_order_truth, _broker_order_snapshot,
        )
        from backend.brokers import get_broker

        acct = (data.account or "").strip()
        if not acct:
            raise HTTPException(status_code=400, detail="account required")
        try:
            get_broker(acct)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"account {acct} not loaded ({e})")

        invalidate("orders")
        broker_status_by_id = await _broker_order_snapshot()
        truth = await get_broker_order_truth(broker_order_id, broker_status_by_id)

        async with async_session() as s:
            r = (await s.execute(
                _sql_select(AlgoOrder).where(
                    AlgoOrder.broker_order_id == str(broker_order_id),
                )
            )).scalars().first()

        updated = False
        algo_status = r.status if r is not None else None
        if r is None:
            note = ("broker has no order (no algo row to update)" if not truth.available
                    else f"broker status={truth.raw_broker_status} (no algo row to update)")
        elif not truth.available:
            note = "broker response unavailable for this order_id — left untouched (no guess)"
        elif truth.algo_status is None:
            note = f"broker status={truth.raw_broker_status} (not yet terminal)"
        else:
            updated = await apply_order_truth(r.id, truth, withhold_if_active=True)
            if updated:
                algo_status = truth.algo_status
                note = f"broker status={truth.raw_broker_status} → {truth.algo_status}"
            elif r.status == truth.algo_status:
                note = f"already {truth.algo_status}"
            else:
                note = ("withheld — row is HELD or under an active chase retry, "
                        "or already finalized by another writer")

        if updated:
            invalidate("orders")
            # Gap fix (2026-10): a per-card reconcile can flip a row to
            # FILLED with no postback involved. Re-fetch a fresh row for
            # the attach pipeline — `r` above predates the mutation
            # (made in `apply_order_truth`'s own locked session), so its
            # fill_price/filled_quantity/template fields are stale.
            if algo_status == "FILLED":
                async with async_session() as s2:
                    _fresh = (await s2.execute(
                        _sql_select(AlgoOrder).where(AlgoOrder.id == r.id)
                    )).scalar_one_or_none()
                if _fresh is not None:
                    _maybe_fire_template_attach_for_reconcile(_fresh)
                    await _subscribe_filled_pairs([(_fresh.symbol, _fresh.exchange)])

        return {
            "broker_order_id": str(broker_order_id),
            "broker_status":   truth.raw_broker_status,
            "algo_status":     algo_status,
            "updated":         updated,
            "note":            note,
        }

    @get("/{order_id:int}/events")
    async def order_events(self, order_id: int, request: Request) -> list[AlgoOrderEventInfo]:
        """Per-order event timeline, oldest-first.

        Returns every row in algo_order_events for the given AlgoOrder id.
        account values inside payload_json are masked for non-admin (demo)
        callers so raw account codes are never exposed.
        """
        from sqlalchemy import asc, select as _sql_select
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrderEvent as _AlgoOrderEvent

        async with _async_session() as s:
            rows = (await s.execute(
                _sql_select(_AlgoOrderEvent)
                .where(_AlgoOrderEvent.order_id == order_id)
                .order_by(asc(_AlgoOrderEvent.ts))
            )).scalars().all()

        # Admin/designated only — partners + demo see masked codes.
        do_mask = not is_admin_request(request)
        _mask = mask_account_in_text if do_mask else (lambda raw: raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask(r.payload_json),
            )
            for r in rows
        ]

    @get("/events/recent")
    async def recent_order_events(
        self,
        request: Request,
        limit: int = 50,
        status: str = "open",
    ) -> list[AlgoOrderEventInfo]:
        """Recent events for orders whose current status matches the filter.

        Default: status=open — useful for the navbar chase chip to show what's
        happening on actively-chasing orders without listing the whole history.

        status='open'  → OPEN orders only (default)
        status='all'   → every order regardless of status
        status=<other> → exact case-insensitive status match
        """
        from sqlalchemy import asc, desc, select as _sql_select, and_
        from backend.api.database import async_session as _async_session
        from backend.api.models import AlgoOrder as _AlgoOrder, AlgoOrderEvent as _AlgoOrderEvent

        limit = max(1, min(limit, 500))
        status_filter = (status or "open").strip().upper()

        async with _async_session() as s:
            if status_filter == "ALL":
                # All orders — pull latest N events across the full table.
                rows = (await s.execute(
                    _sql_select(_AlgoOrderEvent)
                    .order_by(desc(_AlgoOrderEvent.ts))
                    .limit(limit)
                )).scalars().all()
                # Return oldest-first within the window.
                rows = list(reversed(rows))
            else:
                # Fetch matching order ids first, then their events.
                kite_status = "OPEN" if status_filter == "OPEN" else status_filter
                order_ids_q = (await s.execute(
                    _sql_select(_AlgoOrder.id).where(
                        _AlgoOrder.status == kite_status
                    ).order_by(desc(_AlgoOrder.created_at)).limit(200)
                )).scalars().all()
                if not order_ids_q:
                    return []
                rows = (await s.execute(
                    _sql_select(_AlgoOrderEvent)
                    .where(_AlgoOrderEvent.order_id.in_(order_ids_q))
                    .order_by(asc(_AlgoOrderEvent.ts))
                    .limit(limit)
                )).scalars().all()

        # Admin/designated only — partners + demo see masked codes.
        do_mask = not is_admin_request(request)
        _mask = mask_account_in_text if do_mask else (lambda raw: raw)

        return [
            AlgoOrderEventInfo(
                id=r.id,
                order_id=r.order_id,
                ts=r.ts.isoformat() if r.ts else "",
                kind=r.kind,
                message=r.message,
                payload_json=_mask(r.payload_json),
            )
            for r in rows
        ]

    @post("/preflight")
    async def preflight_order(self, request: Request) -> dict:
        """
        Pre-validate an order before any broker call.

        Runs four checks: ACCOUNT_UNKNOWN → SEGMENT_INACTIVE → QTY_FREEZE →
        MARGIN_SHORTFALL. Returns ok=true when all pass, or ok=false with a
        structured `blocked[]` list each carrying code / reason / fix / data.

        Gated by jwt_guard (admin) — demo callers get 403 (raised below);
        this endpoint intentionally never proxies to broker on demo
        sessions. Dhan/Groww MARGIN_SHORTFALL is log-only unless their
        `preflight.enforce_margin_*` setting is on; a server-verified
        close is exempt from the margin gate. Kite always enforces.
        """
        from backend.api.algo.actions import run_preflight

        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: preflight requires an authenticated session.")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        (account, exchange, tradingsymbol, quantity, order_type,
         product, variety, side, price, trigger_price, intent,
         paired_legs) = _rco_parse_preflight_body(body)

        _rco_validate_preflight_params(account, tradingsymbol, quantity, exchange, side)

        result = await run_preflight(account, {
            "exchange":         exchange,
            "tradingsymbol":    tradingsymbol,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          product,
            "variety":          variety,
            "transaction_type": side,
            "intent":           intent,
            "price":            price,
            "trigger_price":    trigger_price,
        }, paired_orders=paired_legs)
        return result

    @post("/ticket/preview")
    async def ticket_preview(self, data: TicketPreviewRequest, request: Request) -> TicketPreviewResponse:
        """
        Preview what the ticket WILL place — resolves the chosen
        template + overrides into a concrete TemplatePlan WITHOUT
        making any broker / DB writes. OrderTicket calls this on
        every relevant field change so the operator sees the planned
        TP / SL / Wing artefacts inline before they hit Submit.

        Returns the plan as a plain dict — see template_attach.TemplatePlan
        for the shape. Empty plan (no gtts + no wing) when neither a
        template nor overrides were supplied.
        """
        from backend.api.algo.template_attach import apply_template_to_order

        # v2 API (2026-07-08): `data.quantity` is LOTS for F&O and shares
        # for equity. Convert to contracts here so the template resolver
        # sizes exit GTTs against the same unit convention it always has.
        _sym = (data.tradingsymbol or "").upper()
        _exch = (data.exchange or "NFO")
        await _preview_check_broker_gates(data, _exch)

        _parent_qty = await _rco_preview_resolve_qty(_exch, _sym, int(data.quantity or 0))
        result = await apply_template_to_order(
            template_id=data.template_id,
            template_slug=None,
            overrides=_ticket_overrides_dict(data),
            parent_account=(data.account or ""),
            parent_symbol=_sym,
            parent_side=(data.side or "").upper(),
            parent_qty=_parent_qty,
            parent_exchange=_exch,
            parent_fill_price=float(data.reference_price or 0.0),
            parent_product=(data.product or "NRML"),
            apply_path="preview",
        )
        if result is None:
            return TicketPreviewResponse(plan=_rco_preview_empty_plan(data))

        _wing_feasible, _gtt_errors = await _preview_template_meta(result, data, _sym, _exch)
        return TicketPreviewResponse(
            plan=result.plan.to_dict(),
            wing_feasible=_wing_feasible,
            gtt_trigger_errors=_gtt_errors,
        )

    @get("/spread-check")
    async def spread_check(
        self,
        tradingsymbol: str,
        exchange: str,
        max_spread_pct: Optional[float] = None,
    ) -> SpreadCheckResponse:
        """Reusable bid-ask spread% check for ONE tradingsymbol+exchange.

        Backs the Chain tab's pre-submission spread gate (operator:
        "if there is too much spread on the offset limit side, it
        should warn ... it should check original and offset orders").
        The frontend (`frontend/src/lib/data/spreadGate.js`) calls this
        once per leg — the parent AND the resolved offset/wing leg —
        and polls it in a bounded loop until the operator confirms,
        edits TP%/SL%/Spread%, or disables the template.

        Deliberately NOT template-aware: the caller resolves operator-
        override-vs-template-default client-side (TemplateBar's new
        Spread% field, default-seeded from `OrderTemplateOut.
        wing_max_spread_pct`) and sends the final number as
        `max_spread_pct`; omitting it falls back to the global
        `templates.wing_max_spread_pct` admin setting via
        `backend.api.algo.spread_check.resolve_max_spread_pct`. No
        template load, no wing chain-scan — safe to poll every few
        seconds (quotes are cached 2s to bound broker-call volume
        across the two legs × the gate's own poll cadence).

        Always 200 with a structured result — `backend.api.algo.
        spread_check` never raises, so a quote-fetch failure degrades
        to `status="error"`/`ok=False`, never a 5xx. Reused by any
        future declarative-agent-grammar metric that wants the same
        bid-ask threshold check (see spread_check.py's own docstring).
        """
        sym  = (tradingsymbol or "").strip().upper()
        exch = (exchange or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=422, detail="tradingsymbol is required")
        if not exch:
            raise HTTPException(status_code=422, detail="exchange is required")
        if max_spread_pct is not None and max_spread_pct <= 0:
            raise HTTPException(status_code=422, detail="max_spread_pct must be > 0")

        from backend.api.algo.spread_check import check_spreads, resolve_max_spread_pct

        threshold, source = resolve_max_spread_pct(
            None, {"wing_max_spread_pct": max_spread_pct},
        )

        async def _cached_quote_fn(keys: list[str]):
            # 2s TTL — short enough to track a moving market, long
            # enough that the gate's own poll cadence (4s, two legs)
            # doesn't double every real quote round-trip.
            cache_key = "spread_q:" + ",".join(sorted(keys))

            async def _fetch():
                import asyncio as _aio
                from backend.brokers.registry import get_market_data_broker
                broker = get_market_data_broker()
                return await _aio.to_thread(broker.quote, keys)

            return await get_or_fetch(cache_key, _fetch, ttl_seconds=2)

        results = await check_spreads(
            [{"tradingsymbol": sym, "exchange": exch}],
            threshold,
            quote_fn=_cached_quote_fn,
        )
        r = results[0]
        return SpreadCheckResponse(
            ok=r.ok, tradingsymbol=r.tradingsymbol, exchange=r.exchange,
            threshold_pct=threshold, threshold_source=source,
            status=r.status, spread_pct=r.spread_pct, bid=r.bid, ask=r.ask,
            basis=r.basis, reason=r.reason,
        )

    @post("/ticket")
    async def ticket_order(self, data: TicketOrderRequest, request: Request) -> TicketOrderResponse:
        """Ticket order placement. Delegates full logic to orders_place.ticket_order_handler."""
        from backend.api.routes.orders_place import ticket_order_handler
        return await ticket_order_handler(data, request)

    # ── Draft orders (2026-10) ────────────────────────────────────────────
    # Server-side CRUD for the OrderTicket "Add to Payoff" draft flow.
    # mode='draft' AlgoOrder rows — no broker call anywhere in this block.

    @post("/drafts")
    async def create_draft(self, data: DraftOrderRequest, request: Request) -> DraftOrderCreateResponse:
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        _draft_validate_input(data)
        sym = data.symbol.upper().strip()
        side = data.transaction_type.upper()
        exch = (data.exchange or "NFO").upper()
        acct = (data.account or "").strip()

        async with async_session() as s:
            row = AlgoOrder(
                account=acct, symbol=sym, exchange=exch,
                transaction_type=side, quantity=int(data.quantity),
                initial_price=(float(data.price) if data.price is not None else None),
                status="OPEN", engine="manual", mode="draft",
                detail=f"[DRAFT] {side} {int(data.quantity)} {sym}",
            )
            s.add(row)
            await s.commit()
            return DraftOrderCreateResponse(id=row.id)

    @get("/drafts")
    async def list_drafts(self, request: Request) -> list[AlgoOrderInfo]:
        """All draft rows (mode='draft'), newest first. Account-masked for
        non-admin callers — same convention as /algo/recent and
        /chases/active. NOTE: AlgoOrder has no owner/user column, so this
        returns every draft, not just "the calling operator's own"."""
        from sqlalchemy import desc, select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        async with async_session() as s:
            rows = (await s.execute(
                sql_select(AlgoOrder)
                .where(AlgoOrder.mode == "draft")
                .order_by(desc(AlgoOrder.id))
            )).scalars().all()

        do_mask = not is_admin_request(request)
        masked_acct = mask_account if do_mask else (lambda a: a)
        return [_chase_row_to_info(r, masked_acct, {}) for r in rows]

    @delete("/drafts/{draft_id:int}", status_code=HTTP_200_OK)
    async def delete_draft(self, draft_id: int, request: Request) -> dict:
        """Deletes a draft row. Refuses (404) when the id doesn't exist or
        exists but isn't mode='draft' — same uniform message either way,
        so this can never become an accidental delete-any-order endpoint."""
        deleted = await _draft_delete_by_id(draft_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Draft not found")
        return {"id": draft_id, "deleted": True}

    @patch("/drafts/{draft_id:int}")
    async def update_draft(
        self, draft_id: int, data: DraftOrderPatchRequest, request: Request,
    ) -> AlgoOrderInfo:
        """Updates only the fields supplied (non-None); omitted fields are
        left unchanged — same convention as PUT /{order_id}'s
        ModifyOrderRequest. Same mode='draft' guard as DELETE."""
        from sqlalchemy import select as sql_select
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder

        async with async_session() as s:
            row = (await s.execute(
                sql_select(AlgoOrder).where(AlgoOrder.id == draft_id)
            )).scalar_one_or_none()
            if row is None or (row.mode or "").lower() != "draft":
                raise HTTPException(status_code=404, detail="Draft not found")

            if data.symbol is not None:
                row.symbol = data.symbol.upper().strip()
            if data.exchange is not None:
                exch = data.exchange.upper()
                if exch not in _EXCHANGES:
                    raise HTTPException(status_code=400, detail=f"exchange must be one of {sorted(_EXCHANGES)}")
                row.exchange = exch
            if data.transaction_type is not None:
                side = data.transaction_type.upper()
                if side not in _TXN_TYPES:
                    raise HTTPException(status_code=400, detail="transaction_type must be BUY or SELL")
                row.transaction_type = side
            if data.quantity is not None:
                if int(data.quantity) <= 0:
                    raise HTTPException(status_code=400, detail="quantity must be > 0")
                row.quantity = int(data.quantity)
            if data.price is not None:
                row.initial_price = float(data.price)
            if data.account is not None:
                row.account = data.account.strip()

            await s.commit()
            masked_acct = mask_account if not is_admin_request(request) else (lambda a: a)
            return _chase_row_to_info(row, masked_acct, {})

    @put("/{order_id:str}")
    async def modify_order(self, order_id: str, data: ModifyOrderRequest, request: Request) -> ModifyOrderResponse:
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: cannot modify orders.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required to modify orders.")
        broker = _broker_for(data.account)
        masked = mask_account(data.account)
        # 2026-10 fix: ModifyOrderRequest carries no exchange/tradingsymbol,
        # so this used to send `quantity` straight to broker.modify_order()
        # unconverted — the same "qty vs lot_size" trap as the agent
        # modify_order path (see actions_live.py:_al_modify_resolve_qty's
        # docstring). Resolve the order's own exchange/symbol from its
        # AlgoOrder row and reuse the exact same G1 lot-multiple check +
        # broker.translate_qty() call the agent path already uses, instead
        # of duplicating that logic here.
        from backend.api.algo.actions_live import (
            _al_modify_fetch_order_meta, _al_modify_resolve_qty,
        )
        kwargs = {k: v for k, v in {
            "quantity":      data.quantity,
            "price":         data.price,
            "order_type":    data.order_type,
            "trigger_price": data.trigger_price,
            "validity":      data.validity,
        }.items() if v is not None}
        try:
            if "quantity" in kwargs:
                row_exchange, row_symbol = await _al_modify_fetch_order_meta(order_id)
                kwargs["quantity"] = await _al_modify_resolve_qty(
                    broker, int(kwargs["quantity"]), row_exchange, row_symbol,
                )
            broker.modify_order(order_id, variety=data.variety, **kwargs)
            invalidate("orders")
            logger.info(f"Order modified: {order_id} [{masked}]")
            return ModifyOrderResponse(order_id=order_id)
        except Exception as e:
            logger.error(f"Modify order failed [{masked}] {order_id}: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @post("/postback", guards=[])
    async def order_postback(self, request: Request) -> dict:
        """Kite postback. Delegates to orders_postback.kite_postback_handler."""
        from backend.api.routes.orders_postback import kite_postback_handler
        return await kite_postback_handler(request)
    @post("/dhan_postback", guards=[])
    async def order_postback_dhan(self, request: Request) -> dict:
        """Dhan order-status webhook. Same role as the Kite postback
        but parses Dhan's payload shape. Scaffold ship — caches the
        full payload to the API log on first hit so the operator can
        forward Dhan's actual structure; the parser then maps known
        fields. Operator must configure the webhook URL inside the
        Dhan partner dashboard for this account.

        Best-effort: never 5xx (Dhan retries on non-2xx and will
        rapidly back-pressure us). Always returns 200 OK; failures
        log + drop. No per-message HMAC — Dhan doesn't sign its webhook
        payloads, so authentication is a shared-secret query-string
        token instead (`_pb_verify_shared_token`); see that function's
        docstring for the 2026-09 council audit fix this closes.

        TODO(operator): the shared secret is generated and set in
        secrets.yaml (local + prod + dev) — the token check is live but
        NOT yet enforced end-to-end. Append `?token=<dhan_postback_token>`
        to this webhook's URL in Dhan's own partner dashboard
        (Settings → Postback URL, or equivalent) — until that's done,
        Dhan sends requests with no token and `_pb_verify_shared_token`
        still passes them (fail-open transitional behavior), so the auth
        gap isn't actually closed yet.
        """
        if not _pb_verify_shared_token(request, "dhan"):
            logger.critical(
                "[POSTBACK-AUTH] rejected dhan postback — missing/invalid "
                "'token' query param."
            )
            raise HTTPException(status_code=401, detail="Invalid postback token.")
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"dhan postback non-JSON body: {e}")
            return {"status": "ok"}
        logger.info(f"dhan postback raw payload: {_pb_redact_payload_for_log(body)!r}")

        order_id, account, kite_status, kite_symbol, txn, qty, price, kite_exchange, status_msg = (
            _rco_parse_dhan_postback_body(body)
        )
        # M9: empty account means Dhan didn't send dhanClientId — we can't
        # route the postback to an AlgoOrder. Log CRITICAL for visibility
        # but return 200 OK so Dhan doesn't retry indefinitely.
        if not account:
            logger.critical(
                "[DHAN-POSTBACK] missing account (dhanClientId) in payload "
                "for order_id=%s status=%s sym=%s — postback cannot be "
                "attributed; raw payload logged above.",
                order_id, kite_status, kite_symbol,
            )
            return {"status": "ok"}
        await _process_broker_postback(
            broker_id="dhan",
            order_id=order_id,
            status=kite_status,
            account=account,
            symbol=kite_symbol,
            txn=txn,
            qty=qty,
            price=price,
            exchange=kite_exchange,
            status_message=status_msg,
        )

        # On a completed Dhan fill: wake performance task + subscribe instrument.
        try:
            from backend.api.routes.orders_postback import _broker_is_fill_status  # noqa: PLC0415
            if _broker_is_fill_status("dhan", str(body.get("orderStatus") or "")):
                _pairs = [(kite_symbol, kite_exchange)] if (kite_symbol and kite_exchange) else []
                await _subscribe_filled_pairs(_pairs)
        except Exception as _sub_exc:
            logger.warning(f"dhan postback: post-fill subscribe failed: {_sub_exc}")

        return {"status": "ok"}

    @post("/groww_postback", guards=[])
    async def order_postback_groww(self, request: Request) -> dict:
        """Groww order-status webhook. Same shape as the Dhan
        scaffold — log raw payload, best-effort parse known fields,
        delegate to the shared `_process_broker_postback` so the
        Kite-canonical mapping + invalidation + book_changed
        broadcast all reuse the same code path.

        Groww's postback support is uncertain (per the audit
        broker-API parity matrix). Route exists so we capture
        whatever Groww sends if/when the webhook is configured.

        No per-message signature — Groww doesn't sign its webhook
        payloads (and its payload carries no account identifier at
        all), so authentication is a shared-secret query-string token
        (`_pb_verify_shared_token`); see that function's docstring for
        the 2026-09 council audit fix this closes. This endpoint's
        account="" fallback-matching gap made it the more exploitable
        of the two brokers' postback routes.

        TODO(operator): same as the Dhan postback above — the shared
        secret is generated and set in secrets.yaml (local + prod + dev)
        but NOT yet appended to this webhook's URL in Groww's own
        partner dashboard. Append `?token=<groww_postback_token>` there
        to actually close the auth gap end-to-end (currently fail-open
        since Groww isn't sending the token yet).
        """
        if not _pb_verify_shared_token(request, "groww"):
            logger.critical(
                "[POSTBACK-AUTH] rejected groww postback — missing/invalid "
                "'token' query param."
            )
            raise HTTPException(status_code=401, detail="Invalid postback token.")
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"groww postback non-JSON body: {e}")
            return {"status": "ok"}
        logger.info(f"groww postback raw payload: {_pb_redact_payload_for_log(body)!r}")

        order_id, status, symbol, txn, qty, price = _groww_parse_payload(body)

        try:
            from backend.brokers.adapters.groww import _GROWW_STATUS_TO_KITE
            kite_status = _GROWW_STATUS_TO_KITE.get(status, status)
        except Exception:
            kite_status = status

        await _process_broker_postback(
            broker_id="groww",
            order_id=order_id,
            status=kite_status,
            account="",
            symbol=str(symbol),
            txn=str(txn),
            qty=qty,
            price=price,
            exchange=str(body.get("exchange") or body.get("segment") or ""),
            status_message=str(body.get("status_message") or ""),
        )

        # On a completed Groww fill: wake performance task + subscribe instrument.
        if kite_status == "COMPLETE":
            try:
                await _groww_handle_complete_fill(body, str(symbol))
            except Exception as _sub_exc:
                logger.warning(f"groww postback: post-fill subscribe failed: {_sub_exc}")

        return {"status": "ok"}

    @post("/basket/margin")
    async def basket_margin(self, data: BasketOrderRequest, request: Request) -> BasketMarginResponse:
        """Compute offset-aware margin for a basket WITHOUT placing. Delegates to orders_basket."""
        from backend.api.routes.orders_basket import basket_margin_handler
        return await basket_margin_handler(data, request)

    @post("/basket")
    async def basket_order(self, data: BasketOrderRequest, request: Request) -> BasketOrderResponse:
        """Multi-account basket order dispatch. Delegates to orders_basket."""
        from backend.api.routes.orders_basket import basket_order_handler
        return await basket_order_handler(data, request)

    @delete("/{order_id:str}", status_code=HTTP_200_OK)
    async def cancel_order(
        self,
        order_id: str,
        request:  Request,
        account:  str = Parameter(query="account"),
        variety:  str = Parameter(query="variety", default="regular"),
    ) -> CancelOrderResponse:
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: cannot cancel orders.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required to cancel orders.")
        broker = _broker_for(account)
        masked = mask_account(account)
        try:
            # Slice Q — look up exchange from the persisted AlgoOrder row
            # so Groww's segment resolver gets the right segment instead
            # of silently routing MCX/NFO cancels to the CASH segment.
            _exchange = ""
            try:
                from sqlalchemy import select as _sel
                from backend.api.models import AlgoOrder as _AO
                async with async_session() as _s:
                    _row = (await _s.execute(
                        _sel(_AO).where(_AO.broker_order_id == order_id)
                    )).scalar_one_or_none()
                if _row and _row.exchange:
                    _exchange = _row.exchange
            except Exception:
                pass
            if _exchange:
                broker.cancel_order(order_id, variety=variety, exchange=_exchange)
            else:
                broker.cancel_order(order_id, variety=variety)
            # 2026-10 fix (chase mark_killed gap): on a successful broker
            # cancel, tell chase's poll loop this id was an
            # operator-initiated kill — without this, chase sees the
            # resulting CANCELLED status, is_killed() reports False, and
            # it silently re-places the order at a fresh price. Marking
            # AFTER (not before, unlike the actions_live.py paths) is
            # safe here specifically because broker.cancel_order() above
            # is a plain synchronous call with no `await` between it and
            # this line — no event-loop yield for a chase poll to land in.
            try:
                from backend.api.algo.chase import mark_killed
                mark_killed(order_id)
            except Exception:
                pass
            invalidate("orders")
            logger.info(f"Order cancelled: {order_id} [{masked}]")
            return CancelOrderResponse(order_id=order_id)
        except Exception as e:
            logger.error(f"Cancel order failed [{masked}] {order_id}: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @post("/cancel-all")
    async def cancel_all_orders(
        self,
        data: CancelAllOrdersRequest,
        request: Request,
    ) -> CancelAllOrdersResponse:
        """Fleet/account-wide cancel-all, optionally narrowed by
        `symbol`/`side`. Shares its underlying implementation
        (`actions_live.cancel_all_orders_scoped`) with the
        `cancel_all_orders` agent DO-action — same scope/account
        narrowing, same `scope="account"` without `account` refusal.

        Same RBAC pattern as the single-order `cancel_order` route above
        (demo refused, admin-only) — deliberately NOT a bare/unauthenticated
        endpoint despite its destructive, fleet-wide blast radius.
        """
        if getattr(request.state, "is_demo", False):
            raise HTTPException(status_code=403,
                detail="Demo: cannot cancel orders.")
        if not is_admin_request(request):
            raise HTTPException(status_code=403,
                detail="Admin access required to cancel orders.")

        from backend.api.algo.actions_live import cancel_all_orders_scoped

        scope = str(data.scope or "total").lower()
        try:
            cancelled, errors = await cancel_all_orders_scoped(
                scope=scope, account=data.account,
                symbol=data.symbol, side=data.side,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        invalidate("orders")
        masked_acct = mask_account(data.account) if data.account else None
        logger.info(
            f"cancel-all: {cancelled} cancelled, {errors} errors "
            f"(scope={scope}, account={masked_acct}, symbol={data.symbol}, "
            f"side={data.side})"
        )
        return CancelAllOrdersResponse(
            cancelled=cancelled, errors=errors, scope=scope,
            account=data.account, symbol=data.symbol, side=data.side,
        )

class AccountsController(Controller):
    path = "/api/accounts"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_accounts(self, request: Request) -> AccountsResponse:
        # Raw account codes gated to admin/designated only. Partner JWTs
        # and demo (anonymous) sessions get masked codes (ZG####, D1####, …),
        # symmetric with mask_column() in row endpoints (positions/holdings/funds).
        # auth_or_demo_guard allows anonymous sessions on prod so the demo UI
        # can populate the account dropdown with all broker accounts (incl.
        # Dhan / Groww) even when those brokers have zero positions/holdings.
        conn = Connections().conn
        # Cutover branch — when conn_service owns the sessions, local
        # Connections.conn is empty. Fetch the canonical account list
        # from /internal/accounts so order modals get a populated
        # dropdown. Without this, every order surface (derivatives
        # ticket, /orders ticket, /pulse modal) saw an empty list.
        loaded_accounts: list[str] = list(conn.keys())
        if not loaded_accounts:
            from backend.brokers.client import is_cutover_on
            if is_cutover_on():
                from backend.brokers.client.remote_broker import list_remote_accounts
                loaded_accounts = [
                    r["account"] for r in list_remote_accounts() if r.get("account")
                ]
        do_mask = not is_admin_request(request)
        accounts = [
            AccountInfo(
                # Non-admin (demo / partner) callers get masked codes in BOTH
                # fields so the raw broker ID never surfaces in JS memory.
                # account_id == display for masked callers; downstream order-
                # placement guards (auth_or_demo_guard + broker checks) prevent
                # the masked code from being submitted as a real order target.
                account_id=(mask_account(account) if do_mask else account),
                display=(mask_account(account) if do_mask else account),
            )
            for account in loaded_accounts
        ]
        # Default broker account is operator-configurable from
        # /admin/settings (orders.default_account). When set + the
        # account is loaded, the frontend SymbolPanel pre-selects it.
        # Masked partners get an empty string back — the raw default
        # code would leak the unmasked ID through this surface.
        from backend.shared.helpers.settings import get_string
        default_acct = "" if do_mask else get_string("orders.default_account", "ZG0790")
        # Only return the default if it's actually in the loaded set —
        # otherwise the frontend would try to pre-select an account
        # that doesn't exist (silent fail; operator sees the dropdown
        # stay empty). Stale settings shouldn't break the UI.
        if default_acct and default_acct not in loaded_accounts:
            default_acct = ""
        # orders.default_symbol setting retired — modals now use the
        # operator's recent symbol (localStorage on the FE) or open
        # with empty context. Always return blank so legacy FE code
        # paths don't latch onto a non-existent symbol.
        return AccountsResponse(
            accounts=accounts,
            default_account=default_acct,
            default_symbol="",
        )
