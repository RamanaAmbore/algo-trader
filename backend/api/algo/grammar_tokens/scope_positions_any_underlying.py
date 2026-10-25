"""Scope resolver — `positions.any_underlying`: one row per distinct F&O
underlying currently held, grouped from per-symbol position rows."""

from __future__ import annotations


def _scope_positions_any_underlying(ctx):
    """One row per distinct F&O underlying currently held, grouped from
    `ctx.position_rows`. Cash-equity / unparseable rows (no `underlying`)
    are skipped — same skip rule `_scope_positions_expiring_today` already
    uses for the same reason. Leaf is OR-combined per underlying.

    `accounts` / `pnl` / `day_change_val` are cheap roll-ups useful for
    alert-message enrichment — the metric resolvers below don't read them
    off this summary row; they re-walk `ctx.position_rows` themselves for
    full per-leg detail (qty, strike, expiry, ...) this summary row
    doesn't carry.

    See grammar.py's module comment above the old location of this
    resolver (the "Underlying-scoped theoretical-vs-market loss
    divergence" block) for the full design rationale — scope
    membership here and the three underlying_* metrics in
    grammar_tokens/ all read `ctx.position_rows` filtered by the SAME
    `row['underlying']` grouping key, deliberately not
    `pnl_math.breakdown_positions_by_underlying()`.
    """
    rows = getattr(ctx, 'position_rows', None) or []
    groups: dict = {}
    for r in rows:
        u = r.get('underlying')
        if not u:
            continue
        groups.setdefault(u, []).append(r)
    out = []
    for u, group in groups.items():
        out.append({
            'underlying': u,
            'accounts': sorted({r.get('account') for r in group if r.get('account')}),
            'pnl': sum(float(r.get('pnl', 0) or 0) for r in group),
            'day_change_val': sum(float(r.get('day_change_val', 0) or 0) for r in group),
        })
    return out
