"""Shared "is this leg already being worked this session" guard.

Extracted from `expiry.py` (2026-11-24, audit finding #3) so both
`expiry.py`'s `close_positions()` AND `actions_live.py`'s
`_al_maybe_stage_agent_close_hold` (the exit-tp-short-option-90 staging
path) can call the SAME check, instead of `actions_live.py` growing its
own byte-identical copy — exactly the duplication-drift pattern audit
finding #18 flags for the sibling context shims. Neither call site needs
a new import cycle: this module imports nothing from `expiry.py` or
`actions_live.py`.

Database-backed (not pure) — deliberately NOT part of `order_hold.py`,
whose own docstring commits to "no database, no broker calls".
"""
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_EXP_IN_FLIGHT_STATUSES = ("HELD", "OPEN", "CANCEL_FAILED")


async def _exp_leg_in_flight_this_session(
    account: str, symbol: str, exchange: str, side: str,
) -> bool:
    """True when this exact (account, symbol, exchange, side) already
    has an unresolved `AlgoOrder` from the CURRENT trading session (see
    `expiry_freeze.session_date_of`) — status `HELD` (staged, awaiting
    operator release), `OPEN` (released and actively chasing, or a
    live chase placed directly when hold is off), or `CANCEL_FAILED`
    (a cancel attempt that may still leave the broker order resting).

    2026-11-22 fix (see `expiry.py:close_positions()`'s own docstring
    for the full incident this closes): `record_held_order()`'s own
    dedup guard only matches `status == 'HELD'`, so it alone cannot see
    a row the operator already released and is still chasing. This
    check runs BEFORE that guard, for BOTH the held-staging branch and
    the live-chase branch, so a re-scan (an agent's own periodic
    re-fire) can never stage or place a second order for a leg that
    already has one outstanding this session — regardless of which
    branch created the first one.

    2026-11-24 fix (audit finding #3): generalized beyond EXPIRY_CLOSE
    — `_al_maybe_stage_agent_close_hold` (AGENT_CLOSE, e.g.
    exit-tp-short-option-90) now calls this SAME check too. Before this,
    a released row that was `OPEN` and still chasing was invisible to
    AGENT_CLOSE's own dedup (which only sees `status=='HELD'` rows), so
    a second HELD row could stage and auto-release while the first
    chase was live — `position_matches` still passes (net still short/
    long), so the result was a second order that flips the leg. This
    check is identity-only, with no `HoldCategory` filter, so it also
    correctly blocks an AGENT_CLOSE re-stage attempt for a leg an
    EXPIRY_CLOSE hold is already working, and vice versa — the same
    real-world hazard `record_held_order`'s own dedup is deliberately
    identity-only for (see that function's own docstring).

    Deliberately existence-based across EVERY row from today's session
    for this identity, not just the most recent one — an account could
    in principle have more than one row for the same leg this session
    (e.g. a CANCEL_FAILED row followed by a fresh attempt), and any one
    of them being unresolved is reason enough to skip.

    On any lookup failure, fails TOWARD SKIPPING (returns True) — the
    opposite failure direction from the sibling cancel-suppression
    check in `actions_live.py`, and deliberately so: that check guards
    an alert-only re-stage (failing open just means "the operator sees
    the reminder again", harmless), while this one guards whether a
    SECOND REAL ORDER gets placed or staged (failing open there risks
    exactly the double-close incident this function exists to prevent).
    A transient DB error here costs one skipped re-scan tick, which the
    next tick (or the operator's own manual review) recovers from.

    2026-11-23 fix (Part C): a `CANCEL_FAILED` row alone no longer
    blocks forever. `HELD` and `OPEN` rows are unconditionally
    blocking, as before (they are, respectively, still awaiting
    operator action, or actively being worked by a chase — neither
    needs re-verification). When EVERY unresolved row from today's
    session is `CANCEL_FAILED`, each is checked against real broker
    truth via `_exp_verify_cancel_failed_leg` rather than assumed to
    be forever resting: confirmed genuinely gone (cancelled/rejected/
    expired, or filled and reconciled) → no longer blocking; confirmed
    still resting at the broker, or truth unavailable → stays
    blocking, and an alert fires either way (never a silent guess).
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.api.algo.expiry_freeze import session_date_of
    try:
        async with async_session() as s:
            rows = (await s.execute(
                select(AlgoOrder).where(
                    AlgoOrder.account == account,
                    AlgoOrder.symbol == symbol,
                    AlgoOrder.exchange == exchange,
                    AlgoOrder.transaction_type == side,
                    AlgoOrder.status.in_(_EXP_IN_FLIGHT_STATUSES),
                )
            )).scalars().all()
        if not rows:
            return False
        now = datetime.now(timezone.utc)
        today_session = session_date_of(now)
        todays_rows = [r for r in rows if session_date_of(r.created_at) == today_session]
        if not todays_rows:
            return False
        unconditionally_blocking = [r for r in todays_rows if r.status != "CANCEL_FAILED"]
        if unconditionally_blocking:
            return True
        # Every one of today's rows is CANCEL_FAILED — verify each
        # against real broker truth rather than assuming it's still
        # resting forever (see `_exp_verify_cancel_failed_leg`).
        for r in todays_rows:
            if await _exp_verify_cancel_failed_leg(r):
                return True
        return False
    except Exception as e:
        logger.warning(
            f"In-flight check failed for {symbol} ({account}), "
            f"skipping this leg rather than risking a duplicate order: {e}"
        )
        return True


async def _exp_verify_cancel_failed_leg(row) -> bool:
    """Re-verify ONE `CANCEL_FAILED` `AlgoOrder` row against real broker
    truth. Returns True when the row must STILL be treated as
    in-flight (blocking); False when it is confirmed resolved and safe
    to allow a fresh close to be staged/placed for this leg.

    Three outcomes, matching `get_broker_order_truth`'s own contract —
    never a silent guess:
      - genuinely terminal (cancelled/rejected/expired, or filled) →
        not blocking. A genuine fill is reconciled onto the row via
        `apply_order_truth` (writes the FIFO ledger on FILLED) so the
        row doesn't sit at CANCEL_FAILED forever once the real outcome
        is known.
      - confirmed still resting at the broker → stays blocking. Alerts
        as stale UNLESS a live chase is already working this exact
        `algo_order_id` (`chase.is_chase_active`) — a chase genuinely
        in progress is not "silently unmanaged", so no alert.
      - truth unavailable (lookup failed, or no usable broker id) →
        stays blocking, and always alerts (never silently guessed).
    """
    from backend.api.routes.orders_helpers import get_broker_order_truth, apply_order_truth
    from backend.api.algo.chase import is_chase_active

    try:
        truth = await get_broker_order_truth(row.broker_order_id)
    except Exception as e:
        logger.error(
            f"CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
            f"broker truth lookup failed: {e}. Staying blocked; needs manual review."
        )
        _exp_alert_cancel_failed_stale(row, f"broker truth lookup failed: {e}")
        return True

    if not truth.available:
        logger.error(
            f"CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
            f"broker truth unavailable. Staying blocked; needs manual review."
        )
        _exp_alert_cancel_failed_stale(row, "broker truth unavailable")
        return True

    if truth.algo_status is None:
        # Confirmed still resting at the broker (not yet terminal).
        if not is_chase_active(row.id):
            logger.error(
                f"CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
                f"confirmed still resting at the broker with no active chase. "
                f"Staying blocked; needs manual review."
            )
            _exp_alert_cancel_failed_stale(
                row, "confirmed still resting at the broker with no active chase")
        return True

    # Genuinely terminal — reconcile the row so it no longer sits at
    # CANCEL_FAILED forever, then it's safe (no longer in-flight).
    try:
        await apply_order_truth(row.id, truth)
    except Exception as e:
        logger.warning(f"Reconcile of CANCEL_FAILED order {row.id} failed: {e}")
    return False


# In-process dedup for the alert below — keyed by AlgoOrder.id, same
# convention as `background.py:_AUTO_RELEASE_FAILED_ALERTED`. A stale
# CANCEL_FAILED leg is re-checked on every re-scan; without this it
# would page the operator again every ~30 minutes for the same row.
_CANCEL_FAILED_ALERTED: set[int] = set()


def _exp_alert_cancel_failed_stale(row, reason: str) -> None:
    """Action-required alert for a CANCEL_FAILED leg that could not be
    confirmed resolved — never a silent `logger.error` alone, which
    only reaches the generic ERROR_AGENT catch-all (Telegram only, with
    its own repeat-gate). `bypass_gates=True` so a closed-market gate
    never suppresses a genuine "this leg may still need manual
    attention" warning on expiry day. One alert per row for the process
    lifetime (ids are never reused)."""
    if row.id in _CANCEL_FAILED_ALERTED:
        return
    _CANCEL_FAILED_ALERTED.add(row.id)
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=row.account, symbol=row.symbol, exchange=row.exchange,
            side=row.transaction_type, qty=int(row.quantity), mode="live",
            source="expiry-cancel-failed-reverify",
            error=(
                f"CANCEL_FAILED order {row.id} could not be confirmed resolved "
                f"at settlement: {reason}. It may still need a manual close "
                f"before expiry."
            ),
            bypass_gates=True,
        )
    except Exception as e:
        logger.warning(f"CANCEL_FAILED alert itself failed for order {row.id}: {e}")
