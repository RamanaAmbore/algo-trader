"""Metric resolver — `underlying_market_loss`: Σ (day_change_val now −
day_change_val at baseline capture) across every currently-held leg of
one underlying."""

from __future__ import annotations


def _metric_underlying_market_loss(ctx, row):
    """Σ (day_change_val NOW − day_change_val AT BASELINE CAPTURE) across
    every currently-held leg of this underlying that has a theoretical
    baseline entry — i.e. the underlying's day_change_val change SINCE
    `_update_underlying_theo_baseline()` (agent_engine.py) froze that
    leg's reprice baseline this session.

    Why not raw `day_change_val` (the pre-2026-10-22 behaviour): that
    column is measured from **prev_close** (the standard day-change
    baseline, reset at the 08:00 IST session boundary), a DIFFERENT
    reference point than `_underlying_theoretical_loss`'s own baseline
    (each leg's LTP/spot/IV frozen the first tick seen THIS session —
    which can be well after 08:00 on a process restart, or can
    legitimately differ from prev_close on any overnight gap). Diffing
    against the SAME reference point the theoretical side uses means a
    pure gap-day move already reflected in day_change_val by the time
    the baseline was captured contributes ~0 here, not a large false
    "divergence" — see the module comment above
    `grammar_tokens.scope_positions_any_underlying.
    _scope_positions_any_underlying` for the full baseline-reprice
    design this mirrors.

    A leg with no baseline entry yet (first tick, not yet captured by
    `_update_underlying_theo_baseline`) simply does not contribute to
    this sum — same missing-data skip `_underlying_theoretical_loss`
    applies to an un-baselined leg, just scoped per-leg here rather than
    all-or-nothing for the whole underlying, since a leg's raw
    day_change_val carries no theoretical-reprice dependency the way an
    option/future reprice does.

    Returns None (missing-vs-zero convention — never a false 0) when the
    row carries no `underlying` at all (wrong scope), OR when NOT A
    SINGLE leg of this underlying has a baseline yet."""
    underlying = row.get('underlying')
    if not underlying:
        return None
    rows = getattr(ctx, 'position_rows', None) or []
    baseline = ((getattr(ctx, 'alert_state', None) or {}).get('underlying_theo_baseline') or {})
    total = 0.0
    any_baselined = False
    for r in rows:
        if r.get('underlying') != underlying:
            continue
        b = baseline.get(r.get('tradingsymbol') or '')
        if b is None:
            continue
        any_baselined = True
        dcv_now = float(r.get('day_change_val', 0) or 0)
        dcv_base = float(b.get('dcv_base', 0) or 0)
        total += dcv_now - dcv_base
    if not any_baselined:
        return None
    return total
