"""Scope resolver — `positions.expiring_today.mcx_unhedged`: subset of
positions.expiring_today restricted to MCX contracts whose CE/PE net
quantity across the underlying does NOT balance (unhedged legs)."""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _scope_positions_expiring_today_mcx_unhedged(ctx):
    """Subset of positions.expiring_today restricted to MCX
    contracts whose CE/PE net qty across the underlying does NOT
    balance — i.e. unhedged legs that will face cash settlement.

    Mirrors the legacy ExpiryEngine grouping: group MCX expiring
    rows by `(underlying, expiry)`; if the sum of CE quantities +
    sum of PE quantities is 0, the pair is perfectly hedged and the
    broker nets them against each other (no close needed). Anything
    else is unhedged and returned.

    Why "underlying + expiry": a long-CE-short-PE collar on the
    same strike + expiry is the typical hedge structure; the net
    qty test catches both single-strike hedges and asymmetric
    multi-strike combos (e.g. long 2 CE, short 2 PE → net 0).
    """
    rows = _scope_positions_expiring_today(ctx)
    mcx = [r for r in rows if (r.get('exchange') or '').upper() == 'MCX']
    if not mcx:
        return []
    groups: dict = {}
    for r in mcx:
        parsed = _parsed_or_none(r.get('tradingsymbol') or '')
        if not parsed:
            continue
        key = f"{parsed.get('root', '')}_{parsed.get('expiry', '')}"
        groups.setdefault(key, []).append((r, parsed))
    out = []
    for entries in groups.values():
        ce_qty = sum(int(r.get('quantity', 0) or 0)
                     for r, p in entries if p.get('opt_type') == 'CE')
        pe_qty = sum(int(r.get('quantity', 0) or 0)
                     for r, p in entries if p.get('opt_type') == 'PE')
        if ce_qty + pe_qty == 0:
            # Perfectly hedged group — broker nets settlement; skip.
            continue
        for r, _p in entries:
            out.append(r)
    return out
