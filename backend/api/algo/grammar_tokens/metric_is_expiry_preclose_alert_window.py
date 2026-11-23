"""Metric resolver factory — `_metric_factory_is_expiry_preclose_window
(lead_minutes)`: 1.0/0.0, True when ANY exchange (MCX or NSE/NFO) is both
expiring today AND within `lead_minutes` of its own close.

The lead time is a genuine PARAMETER of the factory function (operator,
2026-11-22: "the time should be a parameter in the function in the DSL
grammar to make it generic... it could be mcx and nfo") — not a
hardcoded constant baked into a single-purpose function. The alert
token (`is_expiry_preclose_alert_window`, bound to `ALERT_LEAD_MINUTES`
below) and the close-action token (`is_expiry_preclose_close_window`,
`metric_is_expiry_preclose_close_window.py`, bound to
`CLOSE_LEAD_MINUTES`) are both produced by calling this ONE factory with
a different minute value — exactly the same binding pattern already
used elsewhere in this file set for a parameterized metric (see
`metric_max_drawdown_pnl.py`'s `_metric_factory_max_drawdown_pnl(minutes)`
+ its `_30m`/`_1h`/`_4h` bindings). A future third lead time (if ever
needed) is one more one-line binding, never a new copy of the
exchange-loop logic.

Single flat boolean covering both exchanges, so the agent that reads it
needs only one simple leaf condition — no per-exchange compound `all[]`/
`any[]` tree. The exchange-loop and the lead-time check live here, in
Python, not in the DSL. Retimed 2026-11-22 from 30/25 to 65/60 minutes
(operator: "I want the unnetted in the money positions to be closed by
the agents one hour before market close") — the alert still leads the
close-action sibling by 5 minutes, same stagger as before, just moved to
the hour mark. The actual position identification itself happens inside
`_v2_expiry_position_breakdown()` (agent_engine.py), which re-derives
the same per-exchange window check and then runs the real ExpiryEngine
scan — this token is deliberately cheap (no broker call) so it is safe
to evaluate every cycle tick.

Reuses the existing per-exchange metric resolvers directly (never a
second, possibly-drifting day-boundary or close-time definition):
`metric_is_mcx_expiry_today`/`metric_is_nse_expiry_today` for "is today an
expiry day for this exchange", `metric_minutes_until_mcx_close`/
`metric_minutes_until_nse_close` for the close countdown (None while that
exchange is closed — treated as "not in window", not as unknown-is-true).
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_is_mcx_expiry_today import (
    _metric_is_mcx_expiry_today,
)
from backend.api.algo.grammar_tokens.metric_is_nse_expiry_today import (
    _metric_is_nse_expiry_today,
)
from backend.api.algo.grammar_tokens.metric_minutes_until_mcx_close import (
    _metric_minutes_until_mcx_close,
)
from backend.api.algo.grammar_tokens.metric_minutes_until_nse_close import (
    _metric_minutes_until_nse_close,
)

# Shared by this token and its close-window sibling — kept here as the one
# place the lead time belongs, imported rather than re-typed.
ALERT_LEAD_MINUTES = 65


def _expiry_preclose_exchanges(ctx, row, lead_minutes: float) -> set[str]:
    """Which of {"MCX", "NFO"} are expiring today AND within
    `lead_minutes` of their own close, right now. Shared by both
    preclose-window tokens AND by the alert/action render paths
    (`agent_engine.py:_v2_expiry_position_breakdown`,
    `actions_live.py:_action_live_expiry_auto_close`) so there is
    exactly one exchange-loop implementation to drift.

    2026-10-29 audit fix: the condition token (`_metric_is_expiry_
    preclose_*_window`) is a single flat `funds.total`-scoped boolean —
    it tells the agent engine THAT something qualifies, but a scalar
    match carries no exchange identity, so the render/action side
    cannot recover which exchange fired from the match row alone (same
    class of problem the 180-min-cooldown design already works around
    for the condition's OWN re-fire timing). Before this fix, the
    render and action paths silently processed BOTH exchanges
    unconditionally whenever a MATCH existed, regardless of which
    exchange(s) actually caused it — e.g. an NFO-only fire at ~15:05
    would also render/close whatever MCX residual happened to exist at
    that moment, using MCX's moneyness from 8 hours before its own
    close. Callers must re-filter `ExpiryEngine` results to exactly
    this returned set."""
    exchanges: set[str] = set()
    if _metric_is_mcx_expiry_today(ctx, row) == 1.0:
        mins = _metric_minutes_until_mcx_close(ctx, row)
        if mins is not None and mins <= lead_minutes:
            exchanges.add("MCX")
    if _metric_is_nse_expiry_today(ctx, row) == 1.0:
        mins = _metric_minutes_until_nse_close(ctx, row)
        if mins is not None and mins <= lead_minutes:
            exchanges.add("NFO")
    return exchanges


def _expiry_preclose_within(ctx, row, lead_minutes: float) -> bool:
    """True when MCX or NSE is expiring today AND within `lead_minutes`
    of its own close. Thin wrapper over `_expiry_preclose_exchanges` —
    the condition token only needs the boolean, never the set."""
    return bool(_expiry_preclose_exchanges(ctx, row, lead_minutes))


def _metric_factory_is_expiry_preclose_window(lead_minutes: float):
    """Returns a metric resolver closure: 1.0 when either exchange is
    expiring today and within `lead_minutes` of its own close, else 0.0.
    Never None — a missing/closed segment reads as "not in window", not
    "unknown". `lead_minutes` is captured once at binding time (plain
    closure, no per-call DSL parameter passing — grammar-token
    resolvers in this codebase are called as `fn(ctx, row)` with no
    third argument, so a value that must vary by binding is parameterized
    at FACTORY-call time, matching `metric_max_drawdown_pnl.py`'s
    identical precedent)."""
    def _resolver(ctx, row):
        return 1.0 if _expiry_preclose_within(ctx, row, lead_minutes) else 0.0
    return _resolver


_metric_is_expiry_preclose_alert_window = _metric_factory_is_expiry_preclose_window(
    ALERT_LEAD_MINUTES
)
