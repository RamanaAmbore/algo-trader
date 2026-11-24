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

2026-11-24 fix (audit finding #14) — this scope now OWNS the ITM/
future membership test itself, instead of leaving it to the generic
`is_itm`/`is_future` metrics applied by `expiry-nfo-risk-alert`'s own
condition. Before this, a row whose underlying spot price could not
be resolved made the generic `is_itm` metric return `None` (no match,
no log) — the row silently dropped out of the alert with no trace,
while MCX's sibling scope (`scope_positions_expiring_today_mcx_
unhedged.py`) fails LOUD (CRITICAL + returns every expiring row as
unconfirmed-risk) on the identical data gap. `expiry-nfo-risk-alert`'s
NFO leaf is now a tautological membership check against this scope
(mirroring MCX's own `pnl >= -999999999` sentinel pattern) — see
`_EXPIRY_RISK_AGENTS` in `agent_engine.py`. The generic `is_itm`/
`is_future` tokens themselves are UNCHANGED — they are generic DSL
tokens used by other agents too; only this one scope's own internal
classification changed.
"""

from __future__ import annotations

from backend.api.algo.grammar import _option_moneyness, _parsed_or_none
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _nfo_row_qty(r: dict) -> int:
    """Coerce a row's quantity to int, treating anything unparseable
    as 0 — same fail-safe convention `_opt_from_row`/`_fut_from_row`
    (expiry.py) use for the identical purpose."""
    try:
        return int(float(r.get('quantity', 0) or 0))
    except (TypeError, ValueError):
        return 0


def _nfo_classify_row(ctx, r: dict) -> str:
    """Classify one qty!=0 NFO row for membership in this scope's
    result. Returns one of:

      - "include"    — a future (always included — a future has no
                        moneyness ambiguity), or an option whose
                        moneyness resolved to ITM/NTM
                        (`_option_moneyness`'s intrinsic > 0, the exact
                        same definition the generic `is_itm` metric
                        uses).
      - "exclude"     — an unparseable symbol (fail-safe, matching
                        `_nfo_row_qty`'s own convention), a non-F&O
                        row, or an option whose moneyness resolved OTM
                        with a KNOWN spot price.
      - "unresolved"  — an option whose underlying spot price could
                        not be resolved. The row's real moneyness is
                        genuinely unknown — never silently excluded.
    """
    sym = r.get('tradingsymbol') or ''
    parsed = _parsed_or_none(sym)
    if not parsed:
        return "exclude"
    kind = parsed.get('kind')
    if kind == 'fut':
        return "include"
    if kind != 'opt':
        return "exclude"
    spots = getattr(ctx, 'spot_prices', None) or {}
    spot = spots.get(parsed.get('root') or '')
    if spot is None or spot <= 0:
        return "unresolved"
    _opt_kind, intrinsic = _option_moneyness(ctx, r)
    if _opt_kind is None:
        return "unresolved"
    return "include" if intrinsic > 0 else "exclude"


def _scope_positions_expiring_today_nfo(ctx):
    """Subset of positions.expiring_today restricted to NFO (equity
    F&O), excluding flat (qty==0) rows, and further restricted to the
    rows that genuinely need expiry-day attention: futures, and ITM/
    NTM options — or, on an unresolved spot price, every qty!=0 NFO
    row (fail loud; see the module docstring above).

    Used by the equity-only auto-close agent, which fires at T-30min
    before the 15:30 IST equity close and applies its OWN moneyness
    check via `ExpiryEngine` (unaffected by this function — this
    scope's membership narrowing only matters to `expiry-nfo-risk-
    alert`'s tautological leaf). Kite's `exchange` field is the source
    of truth — NSE for cash equity, NFO for equity F&O contracts.
    """
    rows = _scope_positions_expiring_today(ctx)
    nfo_rows = [
        r for r in rows
        if (r.get('exchange') or '').upper() == 'NFO' and _nfo_row_qty(r) != 0
    ]
    if not nfo_rows:
        return []
    try:
        classified = [(r, _nfo_classify_row(ctx, r)) for r in nfo_rows]
    except Exception as e:
        logger.critical(
            "positions.expiring_today.nfo: classification failed (%s) — "
            "returning all %d expiring NFO position(s) as unconfirmed-risk "
            "rather than silently reporting none",
            e, len(nfo_rows), exc_info=True,
        )
        return nfo_rows
    unresolved = [r for r, c in classified if c == "unresolved"]
    if unresolved:
        logger.critical(
            "positions.expiring_today.nfo: %d of %d expiring NFO option(s) "
            "have an unresolved spot price — returning ALL expiring NFO "
            "position(s) as unconfirmed-risk rather than silently excluding "
            "them: %s",
            len(unresolved), len(nfo_rows),
            [r.get('tradingsymbol') for r in unresolved],
        )
        return nfo_rows
    return [r for r, c in classified if c == "include"]
