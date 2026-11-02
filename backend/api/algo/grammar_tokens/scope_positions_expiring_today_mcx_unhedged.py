"""Scope resolver — `positions.expiring_today.mcx_unhedged`: subset of
positions.expiring_today restricted to MCX contracts whose CE/PE net
quantity across the underlying does NOT balance (unhedged legs).

`compute_mcx_ce_pe_net_groups()` below is the SHARED grouping/netting
helper — lifted out of this module (not duplicated) so
`background.py`'s `position_rows` construction can broadcast the SAME
`net_qty`/`is_unhedged` facts onto every row in a group (DSL
dataframe-access gaps, item 1 — group-derived columns) and guarantee
parity with this scope token by construction: both read the identical
grouping logic.
"""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)


def _mcx_ce_pe_group_key(parsed: dict) -> tuple:
    """`(root, expiry)` key MCX CE/PE netting groups by.

    Why "root + expiry": a long-CE-short-PE collar on the same strike +
    expiry is the typical hedge structure; the net qty test catches both
    single-strike hedges and asymmetric multi-strike combos (e.g. long 2
    CE, short 2 PE -> net 0). `exchange` is NOT part of the key — every
    caller of this function already restricts its input rows to MCX
    before calling (see both call sites below and
    `background.py`'s broadcast site), so including it would be
    redundant, not a safety requirement."""
    return (parsed.get('root', ''), parsed.get('expiry', ''))


def compute_mcx_ce_pe_net_groups(rows: list) -> dict:
    """Group MCX F&O `rows` (the caller must already restrict to
    `exchange == 'MCX'` — this function does not re-check) by
    `(root, expiry)` and net CE/PE quantity within each group.

    Returns `{group_key: {"net_qty": int | None, "rows": [...]}}`.

    `net_qty` is `None` (never a coerced 0) when ANY row's `quantity`
    in that group can't be coerced to a number — degrades that ONE
    group only, NEVER raises. This is a deliberate resilience
    improvement over the pre-lift code (which let a bad `int(...)`
    cast propagate uncaught): the old scope-resolver call site was
    already protected by `agent_evaluator._eval_leaf`'s own
    try/except around `scope_fn(ctx)` (a raise there degrades the
    WHOLE leaf to no-match, not just the one bad group), but the NEW
    `background.py` broadcast call site runs inside the
    `_task_performance`-style outer try that wraps the entire agent
    cycle — an uncaught raise there would skip EVERY agent this tick,
    not just the MCX-unhedged one. Catching per-group here is strictly
    safer for both callers and never produces a different result on
    well-formed data (the only data any existing golden/regression test
    exercises).

    Rows whose symbol doesn't parse, or has no resolvable `root`, are
    silently excluded from grouping (same as the pre-lift code)."""
    groups: dict = {}
    for r in rows:
        parsed = _parsed_or_none(r.get('tradingsymbol') or '')
        if not parsed or not parsed.get('root'):
            continue
        key = _mcx_ce_pe_group_key(parsed)
        bucket = groups.setdefault(key, {"rows": [], "parsed": []})
        bucket["rows"].append(r)
        bucket["parsed"].append(parsed)

    out: dict = {}
    for key, g in groups.items():
        net = 0
        ok = True
        for r, p in zip(g["rows"], g["parsed"]):
            if p.get('opt_type') not in ('CE', 'PE'):
                continue
            try:
                net += int(float(r.get('quantity', 0) or 0))
            except (TypeError, ValueError):
                ok = False
                break
        out[key] = {"net_qty": net if ok else None, "rows": g["rows"]}
    return out


def _scope_positions_expiring_today_mcx_unhedged(ctx):
    """Subset of positions.expiring_today restricted to MCX
    contracts whose CE/PE net qty across the underlying does NOT
    balance — i.e. unhedged legs that will face cash settlement.

    Mirrors the legacy ExpiryEngine grouping: group MCX expiring
    rows by `(underlying, expiry)`; if the sum of CE quantities +
    sum of PE quantities is 0, the pair is perfectly hedged and the
    broker nets them against each other (no close needed). Anything
    else is unhedged and returned. A group whose net quantity
    couldn't be computed (malformed quantity data) is treated the
    SAME way as a perfectly-hedged group — excluded — so this token
    and the `is_unhedged` group-derived column (background.py) always
    agree: neither flags a row it can't confirm is genuinely unhedged.
    """
    rows = _scope_positions_expiring_today(ctx)
    mcx = [r for r in rows if (r.get('exchange') or '').upper() == 'MCX']
    if not mcx:
        return []
    groups = compute_mcx_ce_pe_net_groups(mcx)
    out = []
    for g in groups.values():
        net = g["net_qty"]
        if net is None or net == 0:
            # Perfectly hedged, or unconfirmable — broker nets settlement
            # (or we can't confirm it doesn't) — skip.
            continue
        out.extend(g["rows"])
    return out
