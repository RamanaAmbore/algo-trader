"""Metric resolver factory — `_metric_factory_is_expiry_morning_preview_
window(exchange, open_buffer_minutes)`: 1.0/0.0, True when `exchange`
is expiring today AND its own session has been open for at least
`open_buffer_minutes` (so the first live LTP tick has landed —
`ExpiryEngine.scan_positions()` needs a real underlying price to
classify moneyness, not a stale pre-open one).

Operator: "the morning mcx or nfo message, instead of just showing it
is market close day, it should show the positions that need to be
closed at that time" / "the time should be a parameter in the function
in the DSL grammar to make it generic."

Deliberately PER-EXCHANGE, two bound tokens (`is_mcx_expiry_morning_
preview_window`, `is_nse_expiry_morning_preview_window`) from day one —
the pre-close pair (`metric_is_expiry_preclose_alert_window.py`) used a
single shared flat boolean until 2026-11-26, when it was ALSO split
per-exchange, for a different reason (cross-exchange conflation + a
day-early false trigger, not a cooldown-collision risk). This module's
own per-exchange design was always necessary regardless of that later
fix, because of the specific cooldown-collision risk below — MCX opens
at 09:00 and NSE at 09:15, only ~15 minutes apart, nowhere near enough separation for a
same-agent design to be safe. Worse: `_cycle_in_cooldown` gates at the
AGENT level, not the per-leaf/per-metric level — once ANY leaf of a
single agent's condition fires (shared flat boolean OR a compound
`any[]` of two per-exchange leaves, it makes no difference),
`_ae_cycle_eval_and_buffer` returns before evaluating anything else for
that agent until its own cooldown clears. On a day both exchanges
expire, MCX firing at ~09:05 with any cooldown long enough to mean
"once per day" would silence the SAME agent's own evaluation of NSE's
branch at ~09:20 too. Two independent agents, each with its own
cooldown and its own `last_fired` timestamp, is the only shape that is
safe here — the same reasoning `market-preclose-mcx-v2`/
`market-preclose-nonmcx` already use as two separate agents rather than
one merged one.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
    _is_exchange_expiring_exactly_today,
)
from backend.api.algo.grammar_tokens.metric_minutes_since_mcx_open import (
    _metric_minutes_since_mcx_open,
)
from backend.api.algo.grammar_tokens.metric_minutes_since_nse_open import (
    _metric_minutes_since_nse_open,
)

# How long after the session opens before the morning preview is
# allowed to fire — long enough for a real first LTP tick to land, not
# a pre-open stale/zero quote. Shared by both bindings below.
OPEN_BUFFER_MINUTES = 5

_MINUTES_SINCE_OPEN_RESOLVERS = {
    "MCX": _metric_minutes_since_mcx_open,
    "NFO": _metric_minutes_since_nse_open,
}


def _metric_factory_is_expiry_morning_preview_window(
    exchange: str, open_buffer_minutes: float,
):
    """Returns a metric resolver closure: 1.0 when `exchange` is
    expiring today and has been open at least `open_buffer_minutes`,
    else 0.0. Never None. `exchange` must be "MCX" or "NFO".

    2026-11-26 fix (day-early false trigger, same root cause as the
    pre-close window's own fix — see
    `metric_is_expiry_preclose_alert_window.py`'s module docstring for
    the live incident): gates on `_is_exchange_expiring_exactly_today()`
    (exact today) instead of `is_mcx_expiry_today`/`is_nse_expiry_today`
    (the ≤1.5-day lookahead, correct for THEIR other callers but wrong
    here) — without this, a position expiring TOMORROW could trip this
    morning-preview window a day early, then `ExpiryEngine.scan_
    positions()`'s own exact-today filter finds nothing, producing the
    same self-contradicting "a position expires today" / "No positions
    expiring today" message this agent family is already exchange-pure
    against, but was not yet day-boundary-safe against."""
    minutes_since_open = _MINUTES_SINCE_OPEN_RESOLVERS[exchange]

    def _resolver(ctx, row):
        if not _is_exchange_expiring_exactly_today(ctx, exchange):
            return 0.0
        mins = minutes_since_open(ctx, row)
        if mins is None or mins < open_buffer_minutes:
            return 0.0
        return 1.0

    return _resolver


_metric_is_mcx_expiry_morning_preview_window = _metric_factory_is_expiry_morning_preview_window(
    "MCX", OPEN_BUFFER_MINUTES
)
_metric_is_nse_expiry_morning_preview_window = _metric_factory_is_expiry_morning_preview_window(
    "NFO", OPEN_BUFFER_MINUTES
)
