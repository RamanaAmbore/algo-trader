"""Hold and release policy for automated orders.

Pure functions only: no database, no broker calls. The gate, the release
action, and the expiry scan use these rules.
"""
from datetime import datetime, timedelta
from enum import Enum


class HoldCategory(str, Enum):
    EXPIRY_CLOSE = "expiry_close"
    # TEMPLATE_EXIT removed (Bracket/Template-GTT removal, T3.1,
    # 2026-11) — its 3 dedicated functions (`template_exit_held`,
    # `template_exit_override`, `hold_template_exit` in
    # `order_hold_gate.py`; `release_template_exit` in
    # `order_release.py`) are deleted alongside this enum member.
    # Confirmed zero live rows carry `hold_json.category == "template_exit"`
    # on prod or dev (direct DB check) before removing — a historical
    # row with this literal string in `hold_json` would fall through
    # `get_release_handler()`'s dict lookup to the generic
    # `_DEFAULT_RELEASE_HANDLER` (`release_held_order`), same as any
    # other unregistered category.
    AGENT_ORDER = "agent_order"
    # Sprint 3 of the auto-close-short-option plan (generic infra, reused
    # by any future `Agent.require_release` agent). Stages a close that
    # was NEVER attempted at the broker — distinct from `AGENT_ORDER`
    # (chase's own repeated-rejection hold, which RESUMES an
    # already-placed order from its own persisted fields). Deliberately
    # left UNREGISTERED in `order_release.py`'s `_RELEASE_HANDLERS` — it
    # falls through to `_DEFAULT_RELEASE_HANDLER` (`release_held_order`)
    # automatically, which is already exactly the right shape for this
    # category (re-verify the live position, re-quote a fresh price,
    # place a new close order). Do not register a dedicated handler for
    # this category without a real reason to deviate from that default.
    AGENT_CLOSE = "agent_close"


DEFAULT_HELD = True


def effective_hold(category: HoldCategory, override: bool | None,
                   global_switch: dict[str, bool] | None) -> bool:
    """Return True when the order must be held.

    A per-order override (True or False) wins. Otherwise the global switch for
    the category decides. Otherwise the default applies, which is held.
    """
    if override is not None:
        return bool(override)
    if global_switch and category.value in global_switch:
        return not bool(global_switch[category.value])
    return DEFAULT_HELD


def cutoff_time(close_time: datetime, lead_minutes: int) -> datetime:
    """Return the cut-off: close time minus the lead time."""
    if lead_minutes < 0:
        raise ValueError("lead_minutes must be >= 0")
    return close_time - timedelta(minutes=lead_minutes)


def release_price(bid: float | None, ask: float | None, last: float | None,
                  tick: float, band_low: float, band_high: float) -> tuple[bool, float | None, str]:
    """Pick a limit price at release from the live quote.

    Uses the mid of bid and ask when both exist, otherwise the last price.
    Rounds to the nearest tick and checks the exchange price band.
    Returns (ok, price, reason).
    """
    if tick <= 0:
        return False, None, "invalid tick size"
    if bid and ask and bid > 0 and ask > 0:
        raw = (bid + ask) / 2.0
    elif last and last > 0:
        raw = float(last)
    else:
        return False, None, "no live price"
    price = round(round(raw / tick) * tick, 2)
    if price < band_low or price > band_high:
        return False, price, f"price {price} outside band {band_low}-{band_high}"
    return True, price, "ok"


def hold_record(category: HoldCategory, reason: str, price_policy: str,
                override: bool | None, held_at: datetime,
                auto_release_at: "datetime | None" = None) -> str:
    """Serialise the hold state stored on an order (AlgoOrder.hold_json).

    `auto_release_at` (Sprint 3, generic auto-release infra) — an
    absolute UTC timestamp, snapshotted at STAGE time by the caller
    (e.g. `order_hold_gate.record_held_order`'s own caller, who computes
    it as `held_at + timedelta(minutes=agent.release_timeout_minutes)`
    when that setting is non-None, else `None`). Stored as an ISO
    string, or omitted from the JSON entirely when `None` — so every
    pre-existing caller's serialised `hold_json` stays byte-identical
    to before this field existed. A later edit to the agent's own
    `release_timeout_minutes` setting can never retroactively change an
    already-staged hold's deadline: the auto-release scan
    (`background.py`) reads ONLY this snapshotted value, never a live
    re-join back to `Agent.release_timeout_minutes`.
    """
    import json
    out = {
        "category": category.value,
        "reason": reason,
        "price_policy": price_policy,
        "override": override,
        "held_at": held_at.isoformat(),
    }
    if auto_release_at is not None:
        out["auto_release_at"] = auto_release_at.isoformat()
    return json.dumps(out, sort_keys=True)


def parse_hold_record(raw: str | None) -> dict | None:
    """Parse AlgoOrder.hold_json; None when empty or unreadable."""
    import json
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ── Generic hold auto-release policy resolver (2026-11-23, Part C) ────────
#
# Built once, reused by any future `Agent.require_release`-shaped action —
# not expiry-specific. The resolver stays pure (no database, no broker
# calls, matching this module's own docstring) by reading the chase tier
# durations from `chase_tiers.py`, a zero-import sibling module, rather
# than `backend/api/routes/orders_helpers.py` (which pulls in litestar/
# msgspec/schemas at module level and would risk a cycle).

def _floor_minutes_for_tier(tier: "str | None") -> float:
    """Worst-case chase duration (minutes) for an L/M/H tier, plus a
    fixed +1 minute safety margin for the auto-release scan's own poll
    latency (`hold.agent_close_auto_release_poll_seconds`, default 30s
    — this module stays pure, so the margin is a constant, not read
    live from /admin/settings).

    Known, accepted residual: a leg staged in literally the final 1-2
    minutes before `close_time` can, in the worst case, still get a
    resumed chase with too little time to exhaust every attempt before
    close. This margin narrows that window; it does not eliminate it.
    """
    from backend.api.algo.chase_tiers import resolve_chase_tier
    _level, cfg = resolve_chase_tier(tier)
    chase_seconds = float(cfg["interval_seconds"]) * float(cfg["max_attempts"])
    return (chase_seconds / 60.0) + 1.0


def _resolve_hold_auto_release_at(
    policy: "dict | None",
    now: datetime,
    close_time: "datetime | None",
    held_at: datetime,
    default_chase_tier: str = "med",
) -> "datetime | None":
    """Generic, reusable auto-release deadline resolver for any
    `Agent.require_release`-shaped hold — not expiry-specific. Pure: no
    database, no broker calls (see this module's own docstring).

    `policy` is a plain, DSL-expressible dict with exactly one of two
    recognised shapes. An empty/falsy `policy`, or one matching
    neither shape, returns `None` — callers must treat a `None` result
    the same way they treat a missing `agent_id`: refuse to stage an
    unreleasable hold, never silently persist one with no deadline.

      - `{"before_close_min": N}` — a FIXED deadline: `close_time`
        minus `N` minutes. Requires `close_time`; returns `None` when
        `close_time` is unknown (never guess a deadline with no clock
        to anchor it to).
      - `{"after_min": N, "floor_tier": "low"|"med"|"high"}` — a
        ROLLING deadline: `held_at + N` minutes, clamped so it never
        runs later than `close_time` minus the worst-case chase
        duration for `floor_tier` (`floor_tier` defaults to
        `default_chase_tier` when omitted) — see
        `_floor_minutes_for_tier`. The clamp is skipped (the rolling
        deadline is used as-is) when `close_time` is unknown.

    Every datetime argument MUST be timezone-aware — this codebase
    persists `auto_release_at` as an ISO string compared later against
    `datetime.now(timezone.utc)` (`background.py`'s auto-release
    scan); a naive IST value compared against an aware one would
    silently release roughly 5h30m late. This function does not itself
    attach a timezone; that is the caller's responsibility, same as
    every other pure helper in this module.

    The result is never earlier than `now` — a policy that resolves to
    an already-elapsed instant means "release on the very next scan",
    not "never" and not a confusing deadline sitting in the past.

    `now` and `held_at` are kept as independent parameters (not
    collapsed into one), so a future caller recomputing or
    re-validating an ALREADY-staged hold's deadline can pass the
    ORIGINAL `held_at` (unchanged) alongside the CURRENT `now`, without
    this function needing to know which case it's in. A caller staging
    a hold right now (the only case Part C itself uses) passes the
    same wall-clock read for both.
    """
    if not policy:
        return None
    before_close_min = policy.get("before_close_min")
    if before_close_min is not None:
        if close_time is None:
            return None
        deadline = close_time - timedelta(minutes=float(before_close_min))
    else:
        after_min = policy.get("after_min")
        if after_min is None:
            return None
        deadline = held_at + timedelta(minutes=float(after_min))
        if close_time is not None:
            tier = policy.get("floor_tier") or default_chase_tier
            floor_deadline = close_time - timedelta(minutes=_floor_minutes_for_tier(tier))
            deadline = min(deadline, floor_deadline)
    if deadline < now:
        deadline = now
    return deadline
