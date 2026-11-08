"""Metric resolver — `gain_from_entry_pct`: % gain/loss from a position's
average entry price, direction-aware for long vs short.

Sprint 1 foundation (auto-close-short-option-at-90%-profit plan). `avg_
price` is the broker's blended cost basis for the NET position — the
SAME field every other P&L metric in this codebase reads for this
purpose (e.g. Holdings' `inv_val = avg x quantity`; `metric_pnl_pct`'s
own margin-based pnl_pct). No per-tranche/per-entry cost tracking exists
in this codebase and is explicitly out of scope for this metric.
"""

from __future__ import annotations

import math

# Confirmed degraded price_source tags a position row can carry — see
# `backend.api.helpers.price_resolver.resolve_current_price`'s own
# docstring ("live" / "snapshot_settled" / "snapshot_unsettled") and the
# pending-open pin at `routes/positions.py:_apply_pending_open_tag`
# ("pending_open"). "live" is the ONLY non-degraded value emitted by any
# of these call sites today.
#
# A row carrying NO `price_source` key at all is NOT treated as
# degraded — that is the normal shape of `ctx.position_rows` in
# production today (`background.py`'s `_perf_fetch_all_broker_data` ->
# `_perf_run_agent_engine` pipeline builds `position_rows` straight from
# the raw broker DataFrame, which never gets a `price_source` column;
# only the `/api/positions` ROUTE response layer tags PositionRow
# structs with it). Treating a genuinely absent field as degraded would
# make this metric return None on every live tick in production, which
# is the exact missing-vs-zero trap this codebase's convention exists to
# avoid in the other direction.
_DEGRADED_PRICE_SOURCES = frozenset({
    'snapshot_settled', 'snapshot_unsettled', 'pending_open',
})


def _coerce_positive_finite_float(v) -> "float | None":
    """float(v) if v is a genuine positive, finite number — else None
    (never a coerced 0). `math.isfinite` explicitly rejects NaN AND
    +-inf — a plain `fv != fv` NaN check alone would let `inf` through
    (`inf <= 0` is False and `inf == inf` is True)."""
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv) or fv <= 0:
        return None
    return fv


def _metric_gain_from_entry_pct(ctx, row):
    """% gain/loss from this position's average entry price.

    Long  (qty > 0):  (ltp - avg_price) / avg_price * 100
    Short (qty < 0):  (avg_price - ltp) / avg_price * 100

    Returns None (never a coerced 0) when:
      - `average_price` is missing, <= 0, or non-finite, or
      - `last_price` (ltp) is missing, <= 0, or non-finite, or
      - `quantity` is missing, zero, or non-finite (no open position —
        "gain from entry" is undefined with nothing held), or
      - the row's own `price_source` is one of the confirmed degraded
        tags above (a frozen/pending price is not a real market gain).
    """
    if row.get('price_source') in _DEGRADED_PRICE_SOURCES:
        return None

    avg_price = _coerce_positive_finite_float(row.get('average_price'))
    if avg_price is None:
        return None

    ltp = _coerce_positive_finite_float(row.get('last_price'))
    if ltp is None:
        return None

    try:
        qty = float(row.get('quantity', 0) or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty):
        return None

    if qty > 0:
        return (ltp - avg_price) / avg_price * 100.0
    if qty < 0:
        return (avg_price - ltp) / avg_price * 100.0
    return None
