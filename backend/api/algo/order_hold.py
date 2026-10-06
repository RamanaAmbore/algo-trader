"""Hold and release policy for automated orders.

Pure functions only: no database, no broker calls. The gate, the release
action, and the expiry scan use these rules.
"""
from datetime import datetime, timedelta
from enum import Enum


class HoldCategory(str, Enum):
    EXPIRY_CLOSE = "expiry_close"
    TEMPLATE_EXIT = "template_exit"
    AGENT_ORDER = "agent_order"


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
                override: bool | None, held_at: datetime) -> str:
    """Serialise the hold state stored on an order (AlgoOrder.hold_json)."""
    import json
    return json.dumps({
        "category": category.value,
        "reason": reason,
        "price_policy": price_policy,
        "override": override,
        "held_at": held_at.isoformat(),
    }, sort_keys=True)


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
