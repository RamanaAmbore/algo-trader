"""Gate for automated closes: record a held order instead of sending it."""
from datetime import datetime, timezone

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import get_bool, get_int

from backend.api.algo.order_hold import HoldCategory, effective_hold, hold_record

logger = get_logger(__name__)

EXPIRY_CLOSE_RELEASED_KEY = "hold.expiry_close_released"
# `TEMPLATE_EXIT_RELEASED_KEY` is kept (not in the brief's named-function
# removal list) — `backend/api/routes/admin.py`'s `/admin/settings`
# toggle still reads/writes this settings key by name. It is now
# functionally inert: nothing in this module checks it anymore, since
# `template_exit_held()` (the only reader) was removed along with
# `HoldCategory.TEMPLATE_EXIT` (Bracket/Template-GTT removal, T3.1,
# 2026-11). Removing the admin-settings toggle itself is frontend/admin
# cleanup, out of scope for this sub-sprint.
TEMPLATE_EXIT_RELEASED_KEY = "hold.template_exit_released"


def held_for(category: HoldCategory, override: bool | None = None) -> bool:
    """Generic hold-check for any `HoldCategory`.

    A per-order override wins when given. Otherwise the category's own
    global release switch (`hold.<category>_released`) decides, and the
    default is held. `expiry_close_held()` is a thin wrapper over this
    for the one category that existed before this was generalised —
    kept so existing callers/tests that monkeypatch it by name keep
    working unchanged.
    """
    released = get_bool(f"hold.{category.value}_released", False)
    return effective_hold(category, override, {category.value: released})


def expiry_close_held() -> bool:
    """True when expiry closes must be held (global default is held)."""
    return held_for(HoldCategory.EXPIRY_CLOSE, None)


async def record_held_order(category: HoldCategory, *, account: str, symbol: str,
                            exchange: str, side: str, qty: int, product: str,
                            reason: str, price_policy: str = "CHASE_MED",
                            agent_id: "int | None" = None,
                            auto_release_at: "datetime | None" = None) -> int | None:
    """Persist one HELD AlgoOrder for any hold category. Returns the row id.

    `auto_release_at` (Sprint 3, generic auto-release infra, reused by
    any future `Agent.require_release` category — not only `AGENT_CLOSE`)
    — an absolute UTC timestamp, already computed by the CALLER (e.g.
    `held_at_for_deadline + timedelta(minutes=agent.release_timeout_
    minutes)`), snapshotted onto `hold_json` via `hold_record()`. `None`
    (the default) means "never auto-releases, stays held until an
    explicit operator action". This function never derives the value
    itself and never re-reads a live `Agent.release_timeout_minutes` —
    see `hold_record()`'s own docstring for why that matters.

    Generic form of the old `record_held_close` (which held one and only one
    category, `EXPIRY_CLOSE`, hardcoded). See the module docstring in
    `order_release.py` for how to wire up a new category end to end.

    `agent_id` — the firing agent's row id, when the caller is an agent
    action (e.g. ExpiryEngine, fired by expiry-day-*-itm-auto-close). Persisted
    on the row so a later release's resumed chase correctly labels its
    failure alerts "Agent"/"Agent Bracket" instead of defaulting to "Manual"
    — see `alert_utils._classify_order_origin_label`.

    Dedup guard (2026-10-29 audit fix): on the expiry-close path alone,
    up to three independent sources can legitimately try to hold the
    SAME leg — the legacy prod-only `bg-expiry` background task, its own
    30-min re-scan loop, and the agent-engine's `expiry-day-itm-auto-
    close`. Without a check here each one wrote its own HELD row for the
    same (account, symbol, exchange, side); `release_held_order` only
    verifies the live position still matches the HELD qty before
    chasing, which a SECOND duplicate row also still passes (the
    position hasn't moved yet, since the first chase hasn't filled) —
    releasing both placed two real closes, the second of which flips the
    position. Before inserting, this now locks and returns the id of an
    existing not-yet-resolved HELD row for the same (account, symbol,
    exchange, side) instead of creating a second one. Category is
    deliberately NOT part of the match — two different categories
    holding the exact same leg+side is the same real-world hazard this
    guard exists to prevent, not a legitimate distinct case.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder

    held_at = datetime.now(timezone.utc)
    try:
        async with async_session() as s:
            existing = (await s.execute(
                select(AlgoOrder)
                .where(
                    AlgoOrder.account == account,
                    AlgoOrder.symbol == symbol,
                    AlgoOrder.exchange == exchange,
                    AlgoOrder.transaction_type == side,
                    AlgoOrder.status == "HELD",
                )
                .with_for_update()
            )).scalars().first()
            if existing is not None:
                logger.info(
                    f"[HOLD] {category.value} held: {side} {qty} {symbol} "
                    f"acct={account} already has a HELD row id={existing.id} "
                    f"({existing.detail!r}) — not creating a duplicate"
                )
                return existing.id
            row = AlgoOrder(
                account=account, symbol=symbol, exchange=exchange,
                transaction_type=side, quantity=qty, product=product,
                initial_price=None, status="HELD", engine="live", mode="live",
                broker_order_id="", detail=f"Pending: {reason}", agent_id=agent_id,
                hold_json=hold_record(category, reason, price_policy, None, held_at,
                                      auto_release_at=auto_release_at),
            )
            s.add(row)
            await s.commit()
            row_id = row.id
        logger.info(f"[HOLD] {category.value} held: {side} {qty} {symbol} acct={account} id={row_id}")
        from backend.api.algo.order_events import write_event
        await write_event(row_id, "held", f"Pending: {side} {qty} {symbol} ({reason})", {"reason": reason})
        return row_id
    except Exception as e:
        logger.error(f"[HOLD] could not record held order ({category.value}) for {symbol}: {e}")
        return None


# `template_exit_override`/`hold_template_exit` removed (Bracket/
# Template-GTT removal, T3.1, 2026-11) — both served only the deleted
# fill-triggered template-attach pipeline
# (`backend/api/algo/template_attach.py`) and `order_release.py`'s
# now-deleted `release_template_exit`. `AlgoOrder.template_hold_override`
# stays in the DB schema (schema drop is a later sub-sprint).


_CLOSE_HM = {"NFO": (15, 30), "BFO": (15, 30), "NSE": (15, 30), "MCX": (23, 30), "NCO": (23, 30)}


def cutoff_for(exchange: str, now_ist: datetime | None = None) -> datetime:
    """Cut-off for today's expiry close: session close minus the lead time (IST)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    ex = (exchange or "").upper()
    now = now_ist or datetime.now(ZoneInfo("Asia/Kolkata"))
    hh, mm = _CLOSE_HM.get(ex, (15, 30))
    default_lead = 30 if ex in ("MCX", "NCO") else 15
    lead = get_int(f"hold.lead_minutes_{ex.lower()}", default_lead)
    close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return close - timedelta(minutes=lead)


def before_cutoff(exchange: str, now_ist: datetime | None = None) -> bool:
    """True while the expiry scan must wait for the cut-off."""
    from zoneinfo import ZoneInfo
    now = now_ist or datetime.now(ZoneInfo("Asia/Kolkata"))
    return now < cutoff_for(exchange, now)
