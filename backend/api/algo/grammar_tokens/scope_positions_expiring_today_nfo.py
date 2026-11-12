"""Scope resolver — `positions.expiring_today.nfo`: subset of
positions.expiring_today restricted to NFO (equity F&O).

2026-11 classifier re-base (operator decision): `expiry-nfo-risk-alert`
used to classify ITM/future via the generic `is_itm`/`is_future`
metrics applied to EVERY row this scope returned, with no quantity
filter — unlike `ExpiryEngine._fetch_option_positions()` /
`_fetch_future_positions()` (the classifier `expiry-day-preclose-alert`
and `expiry-day-itm-auto-close` already use via `scan_positions()`),
which both skip a flat (qty==0) position outright. A squared-off ITM
leg or a closed-out future therefore used to still fire this alert.

Given the SAME `ctx.spot_prices` the generic `is_itm` metric reads,
the engine's own moneyness classification is `intrinsic > 0 → ITM`
PLUS a buffer zone it calls NTM — `_classify_moneyness`'s ITM-or-NTM
union is exactly `intrinsic > 0`, i.e. exactly what the generic
`is_itm` metric already returns. So the real re-base needed here is
NOT a second classification pass (unlike the MCX scope's netting,
see `scope_positions_expiring_today_mcx_unhedged.py`) — it is this
qty!=0 filter, which brings the scope into agreement with
`ExpiryEngine`'s own skip. The generic `is_itm`/`is_future` metrics
applied by `expiry-nfo-risk-alert`'s own condition are left unchanged
(they are generic tokens, not owned by this agent).

Known residual, flagged not fixed (matches `ExpiryEngine`'s own
pre-existing behaviour): this scope's `≤1.5 days-to-expiry` window
(see `_scope_positions_expiring_today`) is slightly wider than
`scan_positions()`'s own `expiry == today` (instrument-master) check
— a T-1 intraday position can appear here but not in a same-moment
`scan_positions()` call. Re-deriving the exact instrument-master
expiry date without a live download is a separate piece of work, not
attempted here.
"""

from __future__ import annotations

from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _nfo_row_qty(r: dict) -> int:
    """Coerce a row's quantity to int, treating anything unparseable
    as 0 — same fail-safe convention `_opt_from_row`/`_fut_from_row`
    (expiry.py) use for the identical purpose."""
    try:
        return int(float(r.get('quantity', 0) or 0))
    except (TypeError, ValueError):
        return 0


def _scope_positions_expiring_today_nfo(ctx):
    """Subset of positions.expiring_today restricted to NFO (equity
    F&O), excluding flat (qty==0) rows. Used by the equity-only
    auto-close agent which fires at T-30min before the 15:30 IST
    equity close, and by `expiry-nfo-risk-alert`'s `is_itm`/`is_future`
    condition (see the module docstring above for the 2026-11
    classifier re-base rationale). Kite's `exchange` field is the
    source of truth — NSE for cash equity, NFO for equity F&O
    contracts.
    """
    rows = _scope_positions_expiring_today(ctx)
    return [
        r for r in rows
        if (r.get('exchange') or '').upper() == 'NFO' and _nfo_row_qty(r) != 0
    ]
