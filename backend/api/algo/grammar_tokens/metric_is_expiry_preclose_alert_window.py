"""Metric resolver factory — `_metric_factory_is_expiry_preclose_window
(lead_minutes)`: 1.0/0.0, True when ANY exchange (MCX or NSE/NFO) is both
expiring today AND within `lead_minutes` of its own close.

2026-11-26 fix (live incident — operator: "this is alert is confusing. id
nfo position and nfo expiry, no need of this alert", then broadened to
"check all alerts for the issue") — two compounding defects, both fixed
here:

1. **Day-early false trigger.** This factory used to gate each exchange
   on `_metric_is_mcx_expiry_today`/`_metric_is_nse_expiry_today`, which
   read a ≤1.5-DAY lookahead buffer (`_scope_positions_expiring_today`),
   not an exact "expires today" check — deliberately, for OTHER callers
   that want a T-1 heads-up. For this window check, that buffer let the
   gate go true for MCX purely because a position expires TOMORROW,
   while the renderer (`agent_engine.py:_v2_expiry_position_breakdown`,
   now split — see below) correctly filtered to exact `expiry == today`
   and found nothing, producing a self-contradicting alert: "a position
   expires today, within 65 minutes of close" + "No MCX F&O positions
   are expiring today — nothing to close." New
   `_is_exchange_expiring_exactly_today()` below requires
   `days_to_expiry < 1.0`, reusing the same parsing/close-time
   primitives `_scope_positions_expiring_today` uses, but deliberately
   NOT calling that function — its ≤1.5-day semantic is correct for its
   OTHER callers (`is_mcx_expiry_today`/`is_nse_expiry_today` themselves
   are UNCHANGED) and is left alone.
2. **Cross-exchange conflation.** `expiry-day-preclose-alert` and
   `expiry-day-itm-auto-close` used to be ONE agent each, spanning both
   exchanges via this flat, no-exchange-identity boolean — the same
   failure class `expiry-nfo-risk-alert`/`expiry-mcx-risk-alert` were
   split for earlier the same day (merged 2026-11-19, split back
   2026-11-26 after an identical live incident: "i don't have any nfo
   positions. i got nfo expiration alerts with mcx positions"). Both
   expiry-day agents are now split into 4 exchange-pure agents
   (`expiry-day-{mcx,nse}-preclose-alert`,
   `expiry-day-{mcx,nse}-itm-auto-close`), each driven by a new
   per-exchange token from `_metric_factory_is_expiry_preclose_window_
   for_exchange(exchange, lead_minutes)` below — same factory-reuse
   style as `metric_is_expiry_morning_preview_window.py`'s own
   per-exchange bindings.

   **Verification pass done, catalog entries removed (2026-11-27).**
   The OLD combined tokens (`is_expiry_preclose_alert_window`/
   `is_expiry_preclose_close_window`) were confirmed dead — a direct
   read-only query against BOTH prod (`ramboq`) and dev (`ramboq_dev`)
   `agents` tables found zero rows (active or inactive) whose
   `conditions`/`preamble` JSON mentions either token name — and have
   been removed from `agent_grammar.yaml` / the golden baseline, so no
   NEW custom agent can author a condition against the combined,
   cross-exchange-conflating shape by name. The Python resolver
   functions below (`_metric_is_expiry_preclose_alert_window`, and its
   sibling in `metric_is_expiry_preclose_close_window.py`) are
   DELIBERATELY still defined and still exported — `seed_grammar_
   tokens()`'s own docstring states it never prunes a system token that
   disappears from `SYSTEM_TOKENS` between releases ("left in the table
   as is_active=True until manually cleaned — safer than auto-deleting
   something an agent might still reference"), so an already-seeded
   `grammar_tokens` row on an existing prod/dev database still points at
   these dotted paths and must keep resolving. Removing the YAML
   catalog entry stops it being authored FRESH; it does NOT retroactively
   deactivate an already-seeded row — that would need a manual DB
   action (`UPDATE grammar_tokens SET is_active=false WHERE token IN
   (...)`), a deliberate choice outside the scope of a code change.

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
`_v2_expiry_exchange_breakdown_or_empty()`/`_v2_expiry_preclose_alert_
section()` (`agent_engine.py`, since the 2026-11-26 exchange-pure split —
the combined `_v2_expiry_position_breakdown()` this paragraph used to
name no longer exists), which runs the real `ExpiryEngine` scan for ONE
exchange, never re-deriving this token's own per-exchange window check
itself. This token is deliberately cheap (no broker call) so it is safe
to evaluate every cycle tick.

Reuses the existing per-exchange metric resolvers directly (never a
second, possibly-drifting day-boundary or close-time definition):
`metric_is_mcx_expiry_today`/`metric_is_nse_expiry_today` for "is today an
expiry day for this exchange", `metric_minutes_until_mcx_close`/
`metric_minutes_until_nse_close` for the close countdown (None while that
exchange is closed — treated as "not in window", not as unknown-is-true).

Perf (P3-4, 2026-11-27): `_expiry_preclose_exchanges()` checks the cheap
minutes-until-close value FIRST, short-circuiting before the expensive
`_is_exchange_expiring_exactly_today()` row-walk — for nearly the whole
trading day (everywhere outside the `lead_minutes` window), the minutes
check alone resolves "not in window" and the row-walk never runs at all.
`_is_exchange_expiring_exactly_today()` also memoizes its own per-
exchange result on `ctx` itself (a plain, non-slotted `@dataclass`
instance the agent engine already constructs fresh once per cycle — see
`agent_evaluator.Context`), never a module-level cache keyed by
`id(ctx)`: a module-level dict would risk serving a stale value for a
RECYCLED id once an old `Context` is garbage-collected and a new one
happens to get the same id, and would need its own date-keyed
invalidation; attaching the memo directly to the per-cycle `ctx` instance
needs neither — a fresh `ctx` means a fresh (empty) memo, by
construction, with no possibility of leaking across cycles or days. The
attribute probe guards against a non-dict value (e.g. a bare `MagicMock`
test double auto-vivifying a truthy, non-dict attribute on first access)
by checking `isinstance(memo, dict)`, never bare truthiness.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.metric_minutes_until_mcx_close import (
    _metric_minutes_until_mcx_close,
)
from backend.api.algo.grammar_tokens.metric_minutes_until_nse_close import (
    _metric_minutes_until_nse_close,
)

# Shared by this token and its close-window sibling — kept here as the one
# place the lead time belongs, imported rather than re-typed.
ALERT_LEAD_MINUTES = 65

_MINUTES_UNTIL_CLOSE_RESOLVERS = {
    "MCX": _metric_minutes_until_mcx_close,
    "NFO": _metric_minutes_until_nse_close,
}


def _is_exchange_expiring_exactly_today(ctx, exchange: str) -> bool:
    """True when any held `exchange` F&O position's contract expiry is
    exactly today (`days_to_expiry < 1.0`) — NOT the wider ≤1.5-day
    lookahead `_scope_positions_expiring_today()`/`is_{mcx,nse}_expiry_
    today` use for their other callers (a deliberate T-1-warning-
    friendly buffer, left unchanged there). Purpose-built for the
    pre-close/morning-preview window checks, which must agree with
    `ExpiryEngine.scan_positions()`'s own exact `expiry == today` filter
    — see this module's own docstring for the live incident this closes
    (a position expiring TOMORROW tripped the window a day early, then
    the renderer correctly found nothing, producing a self-contradicting
    "a position expires today" / "No positions expiring today" message).

    Re-walks `ctx.position_rows` with the same parsing/close-time
    resolution `scope_positions_expiring_today.py` uses
    (`_parsed_or_none`, `days_to_expiry`, `close_time_for_exchange`) —
    deliberately a separate, narrower function rather than a parameter
    added to that one, so its ≤1.5-day semantic for every OTHER caller
    is provably untouched.

    Perf (P3-4, 2026-11-27): memoized per `(ctx, exchange)` — see this
    module's own docstring for why the memo lives on `ctx` itself
    rather than a module-level, id()-keyed cache. `_expiry_preclose_
    exchanges()` below already runs the cheap minutes-until-close check
    first and only calls this at all when that check has already
    passed, so in steady state this row-walk runs only inside the
    actual pre-close window, not on every cycle tick."""
    memo = getattr(ctx, '_expiry_exact_today_memo', None)
    if not isinstance(memo, dict):
        memo = {}
        try:
            ctx._expiry_exact_today_memo = memo
        except Exception:
            pass  # best-effort — correctness doesn't depend on this sticking
    if exchange in memo:
        return memo[exchange]

    rows_src = getattr(ctx, 'position_rows', None) or []
    from backend.api.algo.grammar import _parsed_or_none
    from backend.api.algo.derivatives import days_to_expiry
    from backend.shared.helpers.date_time_utils import close_time_for_exchange

    result = False
    for r in rows_src:
        if (r.get('exchange') or '').upper() != exchange:
            continue
        sym = r.get('tradingsymbol') or ''
        parsed = _parsed_or_none(sym)
        if not parsed or not parsed.get('expiry'):
            continue
        try:
            close_time = close_time_for_exchange(r.get('exchange'), parsed['expiry'])
            if close_time is None:
                close_time = (15, 30)
            d = float(days_to_expiry(parsed['expiry'], ref=ctx.now, close_time=close_time))
        except Exception:
            continue
        if d < 1.0:
            result = True
            break

    memo[exchange] = result
    return result


def _expiry_preclose_exchanges(ctx, row, lead_minutes: float) -> set[str]:
    """Which of {"MCX", "NFO"} are expiring EXACTLY today AND within
    `lead_minutes` of their own close, right now. Shared by both
    preclose-window token families (the combined legacy tokens and the
    per-exchange 2026-11-26 split tokens below) AND by the alert/action
    render paths (`agent_engine.py`'s per-exchange preclose renderer,
    `actions_live.py:_action_live_expiry_auto_close`) so there is
    exactly one exchange-loop implementation to drift.

    2026-10-29 audit fix (exchange identity): the combined condition
    token is a single flat `funds.total`-scoped boolean — it tells the
    agent engine THAT something qualifies, but a scalar match carries
    no exchange identity, so a caller resolving "which exchange fired"
    from the match row alone cannot recover it (same class of problem
    the 180-min-cooldown design already works around for the
    condition's OWN re-fire timing). Callers must re-filter
    `ExpiryEngine` results to exactly this returned set.

    2026-11-26 fix (day-early false trigger): now gates each exchange on
    `_is_exchange_expiring_exactly_today()` (exact today), not the old
    ≤1.5-day `is_mcx_expiry_today`/`is_nse_expiry_today` — see this
    module's own docstring.

    Perf (P3-4, 2026-11-27): the cheap `minutes_until_close` check runs
    FIRST, short-circuiting before the expensive `_is_exchange_
    expiring_exactly_today()` position-row walk — both conditions are
    ANDed, so swapping their order changes nothing about the result,
    only how often the expensive branch actually runs. Outside the
    `lead_minutes` window (nearly the entire trading day) this means
    the row-walk never runs at all."""
    exchanges: set[str] = set()
    for exchange, minutes_until_close in _MINUTES_UNTIL_CLOSE_RESOLVERS.items():
        mins = minutes_until_close(ctx, row)
        if mins is None or mins > lead_minutes:
            continue
        if _is_exchange_expiring_exactly_today(ctx, exchange):
            exchanges.add(exchange)
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


def _metric_factory_is_expiry_preclose_window_for_exchange(exchange: str, lead_minutes: float):
    """Per-exchange variant of `_metric_factory_is_expiry_preclose_window`
    above — 1.0 when `exchange`, and only `exchange`, is expiring exactly
    today and within `lead_minutes` of ITS OWN close; 0.0 otherwise
    (including when the OTHER exchange is in-window — this token must
    never go true for a different exchange's breach). Never None.

    Powers the exchange-pure `expiry-day-{mcx,nse}-preclose-alert` /
    `expiry-day-{mcx,nse}-itm-auto-close` agent pairs (2026-11-26 split
    — see this module's own docstring for the live incident this
    fixes). Calls the SAME `_expiry_preclose_exchanges()` every other
    binding in this file set uses, never a second exchange-loop
    implementation — only the membership check differs from the
    combined `_metric_factory_is_expiry_preclose_window` above."""
    def _resolver(ctx, row):
        return 1.0 if exchange in _expiry_preclose_exchanges(ctx, row, lead_minutes) else 0.0
    return _resolver


_metric_is_mcx_expiry_preclose_alert_window = _metric_factory_is_expiry_preclose_window_for_exchange(
    "MCX", ALERT_LEAD_MINUTES
)
_metric_is_nse_expiry_preclose_alert_window = _metric_factory_is_expiry_preclose_window_for_exchange(
    "NFO", ALERT_LEAD_MINUTES
)
