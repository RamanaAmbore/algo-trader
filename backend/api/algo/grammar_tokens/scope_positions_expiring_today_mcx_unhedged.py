"""Scope resolver — `positions.expiring_today.mcx_unhedged`: subset of
positions.expiring_today restricted to MCX contracts the real expiry
engine would flag as needing attention (unhedged after its 4-rule
netting, or UNKNOWN-moneyness — see below).

`compute_mcx_ce_pe_net_groups()` below is a SEPARATE, simpler
sum-of-signed-quantity heuristic — kept here, UNCHANGED, because
`background.py`'s `position_rows` construction still broadcasts its
`net_qty`/`is_unhedged` facts onto every row in a group (DSL
dataframe-access gaps, item 1 — group-derived columns), a generic
display/DSL-column feature any custom operator-authored agent may
already depend on.

2026-11 classifier re-base (operator decision) — IMPORTANT, breaks the
parity this module's docstring used to promise: `_scope_positions_
expiring_today_mcx_unhedged()` below no longer calls
`compute_mcx_ce_pe_net_groups()`. It now classifies via
`expiry.classify_from_context()`, the SAME real 4-rule theta-priority
netting (plus same-underlying futures as delta-offset partners)
`ExpiryEngine.scan_positions()` uses for `expiry-day-preclose-alert`
and `expiry-day-itm-auto-close` — per explicit operator instruction,
so `expiry-mcx-risk-alert` can never disagree with those two about the
same position. The crude sum-based heuristic disagreed with the real
engine on at least two structural cases: (1) a "synthetic" combo
(e.g. long CE + short PE at the same strike — a directional position,
NOT a hedge) sums to zero and was wrongly excluded, while the real
engine correctly flags it (no valid opt-opt pairing rule covers
opposite-type-opposite-sign); (2) a "guts" position (long CE at one
ITM strike + long PE at a DIFFERENT ITM strike, equal qty) sums to a
non-zero total and was wrongly flagged, while the real engine's rule 3
(long CE + long PE, same sign) correctly nets it to zero.

Residual, left broken and documented rather than silently so (per
review — re-basing the DSL `is_unhedged`/`net_qty` display column onto
the real engine as well is a SEPARATE, wider-scoped change: that
column is generic DSL surface, not owned by this one agent, and other
operator-authored agents may already read it expecting the simple
sum-based semantics): `background.py`'s `is_unhedged`/`net_qty`
columns and THIS scope token's own `needs_close` determination can now
genuinely disagree for the same row. A custom agent reading the
`is_unhedged` column directly does NOT get the re-based classifier —
only `positions.expiring_today.mcx_unhedged` (this scope token) does.
"""

from __future__ import annotations

from backend.api.algo.grammar import _parsed_or_none
from backend.api.algo.grammar_tokens.scope_positions_expiring_today import (
    _scope_positions_expiring_today,
)
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# `expiry.classify_from_context` is imported LAZILY inside the function
# below, not at module level — `expiry.py` pulls in `chase.py` and
# `broker_apis.py` at its own module level, and this grammar_tokens
# module is dotted-path-imported by `GrammarRegistry.reload()` at boot
# (see `grammar.py`'s own module docstring). A module-level import here
# would risk a boot-time circular import, and a failed dotted-path
# import leaves `REGISTRY.scope('positions.expiring_today.mcx_unhedged')`
# as `None` for the WHOLE process — silently taking `expiry-mcx-risk-
# alert` dark rather than failing one call. A lazy, per-call import
# keeps any import failure local and loud (see the try/except below).


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
    """Subset of positions.expiring_today restricted to MCX contracts
    the real `ExpiryEngine` classifier would flag as needing attention
    — genuinely unhedged after its 4-rule theta-priority netting (plus
    same-underlying futures as delta-offset partners), OR UNKNOWN-
    moneyness (no resolvable spot price — surfaced explicitly rather
    than silently assumed safe; see below).

    2026-11 classifier re-base (operator decision — see this module's
    own docstring for the full rationale and the residual `is_unhedged`
    parity gap this creates against `background.py`'s simpler DSL
    column): classifies via `expiry.classify_from_context()`, which
    runs the IDENTICAL netting math `ExpiryEngine.scan_positions()`
    uses for `expiry-day-preclose-alert`/`expiry-day-itm-auto-close` —
    never a second, independently-reimplemented copy — but sourced
    from `ctx.position_rows`/`ctx.spot_prices` (already populated once
    per cycle, no broker call) instead of a live `scan_positions()`
    broker fetch, which this sync condition resolver cannot safely make
    (see `expiry.py`'s module comment on the context-driven adapter).

    UNKNOWN-moneyness rows (no spot price for that root in
    `ctx.spot_prices`) are INCLUDED, not excluded — `ExpiryEngine`
    itself treats "can't confirm moneyness" as a loud warning
    (`_v2_expiry_root_unknown_lines`), never a silent all-clear; this
    scope must agree. This is a DELIBERATE change from the old crude
    heuristic's "unconfirmable net qty → excluded" rule: that rule was
    about malformed QUANTITY data (a genuinely different failure, where
    nothing about the position can be known at all); a missing SPOT
    PRICE still has a known, real, possibly-risky position behind it.

    A row that doesn't even parse as a non-flat CE/PE (e.g. qty==0, or
    a lone future with no option leg — futures are only ever netting
    partners here, never a needs_close candidate in their own right,
    same residual gap `classify_from_context()`'s own docstring flags)
    is excluded, matching `ExpiryEngine._fetch_option_positions()`'s
    own skip.
    """
    rows = _scope_positions_expiring_today(ctx)
    mcx = [r for r in rows if (r.get('exchange') or '').upper() == 'MCX']
    if not mcx:
        return []
    try:
        from backend.api.algo.expiry import classify_from_context
        classified = classify_from_context(
            mcx,
            getattr(ctx, 'position_rows', None) or [],
            getattr(ctx, 'spot_prices', None) or {},
            exchange_filter='MCX',
            now=getattr(ctx, 'now', None),
        )
    except Exception as e:
        # Import/classification failure — this is a RISK alert scope,
        # the single highest-stakes resolver in the whole engine (it
        # exists specifically to catch MCX expiry-day exposure the
        # broker will auto-settle with no further warning). The rest
        # of this codebase's design philosophy (see CLAUDE.md: "never
        # a false all-clear") is explicit that an unresolvable spot
        # price is INCLUDED as a warning, not excluded — the OLD
        # behaviour here (returning `[]`, i.e. "nothing confirmed
        # unhedged") was the exact false all-clear that philosophy
        # forbids, on the one resolver where it matters most. Fail
        # LOUD instead: log CRITICAL (picked up by the ERROR_AGENT
        # catch-all — see the "CRITICAL alerts now match ERROR_AGENT"
        # entry in CLAUDE.md, no extra tags/alert_event needed) AND
        # return every currently-expiring MCX position, the safe,
        # conservative "I don't know, so alert on everything expiring"
        # answer — never an empty list, which `_eval_leaf` would read
        # as "nothing to see here".
        logger.critical(
            "positions.expiring_today.mcx_unhedged: classify_from_context "
            "failed (%s) — returning all %d expiring MCX position(s) as "
            "unconfirmed-risk rather than silently reporting none",
            e, len(mcx),
            exc_info=True,
        )
        return mcx

    # Key by (account, tradingsymbol) — a bare tradingsymbol can repeat
    # across accounts, and the crude heuristic's own `(root, expiry)`
    # grouping never needed an account dimension because it summed
    # quantities wholesale; the real engine's own `_classify_mcx_group`
    # IS account-scoped (`(account, underlying, expiry)`), so the
    # mapping back to rows must be too.
    by_key: dict[tuple, "object"] = {
        (c.account, c.tradingsymbol): c for c in classified
    }
    out = []
    for r in mcx:
        c = by_key.get((r.get('account') or '', r.get('tradingsymbol') or ''))
        if c is None:
            continue
        if c.needs_close or c.moneyness == "UNKNOWN":
            out.append(r)
    return out
