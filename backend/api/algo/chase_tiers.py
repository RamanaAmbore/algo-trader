"""Chase aggressiveness tiers (low/med/high) — the one shared table of
(interval_seconds, aggression_step, max_attempts) per tier.

SSOT for two independent callers:
  - `backend/api/routes/orders_helpers.py:_live_chase_config()` — builds
    a real `ChaseConfig` for a fresh/resumed chase.
  - `backend/api/algo/order_hold.py:_floor_minutes_for_tier()` — computes
    the worst-case chase duration (minutes) for the generic hold
    auto-release deadline resolver.

Deliberately zero imports (not even stdlib beyond `__future__`) and zero
I/O — `order_hold.py`'s own docstring requires "no database, no broker
calls", and this module must be importable from there with no risk of
ever starting a cycle. `orders_helpers.py` pulls in `litestar`/`msgspec`/
`backend.api.schemas` at module level; routing *that* module's own table
through here, rather than the reverse, is what keeps `order_hold.py`
import-safe.
"""

from __future__ import annotations

# Industry analogue: IBKR Adaptive Algo Patient / Normal / Urgent.
#   low   — patient: long interval, small aggression step, more attempts.
#   med   — balanced.
#   high  — urgent: short interval, big aggression step, fewer attempts.
CHASE_TIERS: dict[str, dict[str, float | int]] = {
    "low":  {"interval_seconds": 30, "aggression_step": 0.05, "max_attempts": 30},
    "med":  {"interval_seconds": 20, "aggression_step": 0.10, "max_attempts": 20},
    "high": {"interval_seconds": 10, "aggression_step": 0.25, "max_attempts": 10},
}


def resolve_chase_tier(aggressiveness: "str | None") -> "tuple[str, dict[str, float | int]]":
    """Normalise a free-form aggressiveness string to one of
    ('low', 'med', 'high') and return `(level, tier_dict)`.

    Unknown, missing, or unrecognised values fall back to 'low' — the
    exact fallback `_live_chase_config` used before this table was
    extracted (its own comment explains why: pinning the *returned*
    level to "low" too, not the raw input string, so a malformed value
    can't run at the low-tier tuple while stamping a non-canonical
    `level` elsewhere).
    """
    a = (aggressiveness or "low").lower()
    level = a if a in CHASE_TIERS else "low"
    return level, CHASE_TIERS[level]
