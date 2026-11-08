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
