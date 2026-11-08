"""Metric resolver — `short_option_gain_from_entry_pct`: percent of
premium captured on a SHORT OPTION position, direction- and
instrument-type-aware, with the qty<0 and CE/PE checks folded INSIDE
the metric itself.

Sprint 3 of the auto-close-short-option plan. This is a deliberately
NARROW variant of `gain_from_entry_pct` (Sprint 1), built to close a
confirmed blocking finding from Sprint 1's own spike testing (a real
`evaluate()` call, not a theory): `agent_evaluator._eval_all`'s join
over a fan-out scope (`positions.any_symbol`) only recognises an
`'acct'`/`'und'` join-key shape and otherwise cross-joins by ACCOUNT,
not by row identity. Expressing "short AND option AND gain>=90%" as
THREE separate leaves (`qty < 0`, an instrument-type check, `gain_from_
entry_pct >= 90`) joined with `all[]` over `positions.any_symbol` would
therefore wrongly include a position that satisfies only ONE of those
leaves, whenever another position in the SAME account satisfies a
different leaf. Fixing `_eval_all` itself is explicitly out of scope
(shared engine code, wider blast radius) — instead, this metric folds
both checks inside itself so the planned agent's whole condition is a
SINGLE leaf (`short_option_gain_from_entry_pct >= 90`), with no `all[]`
and no cross-join exposure at all.
"""

from __future__ import annotations

import math

from backend.api.algo.grammar_tokens.metric_gain_from_entry_pct import (
    _DEGRADED_PRICE_SOURCES,
    _coerce_positive_finite_float,
)


def _metric_short_option_gain_from_entry_pct(ctx, row):
    """Percent of premium captured from entry — SHORT OPTIONS ONLY.

    `(avg_price - ltp) / avg_price * 100` — identical formula to
    `gain_from_entry_pct`'s own short-side branch (Sprint 1), reused
    verbatim rather than re-derived.

    Returns None (never a coerced 0) when:
      - `quantity` is missing, non-finite, or >= 0 (this metric is
        meaningless/undefined for a long position or a flat row — a
        LONG leaf must never be silently evaluated by this metric), or
      - the row's `tradingsymbol` does not parse as a CE/PE option
        (digit-anchored suffix check — `_option_type()`, imported
        lazily from `routes.positions` to avoid a module-load-time
        import cycle between the grammar-token package and the routes
        package; same lazy-import convention this codebase already
        uses elsewhere for the identical reason), or
      - `average_price` is missing, <= 0, or non-finite, or
      - `last_price` (ltp) is missing, <= 0, or non-finite, or
      - the row's own `price_source` is one of the confirmed degraded
        tags `gain_from_entry_pct` already guards against
        (`snapshot_settled` / `snapshot_unsettled` / `pending_open`) —
        reused from that module's own frozenset, not re-derived, so
        the two metrics can never silently drift apart on this list.
    """
    try:
        qty = float(row.get('quantity', 0) or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty >= 0:
        return None

    from backend.api.routes.positions import _option_type
    symbol = row.get('tradingsymbol') or ''
    if _option_type(str(symbol)) not in ('CE', 'PE'):
        return None

    if row.get('price_source') in _DEGRADED_PRICE_SOURCES:
        return None

    avg_price = _coerce_positive_finite_float(row.get('average_price'))
    if avg_price is None:
        return None

    ltp = _coerce_positive_finite_float(row.get('last_price'))
    if ltp is None:
        return None

    return (avg_price - ltp) / avg_price * 100.0
