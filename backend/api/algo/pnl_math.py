"""Pure-Python intraday P&L math — single source of truth.

Two surfaces previously inlined the same decomposed intraday Day P&L
formula:

  • `backend/brokers/broker_apis.py:_enrich_positions`  (Polars expression)
  • `backend/api/routes/positions.py:_compute_day_change_val`  (pandas Series)

Both now route through the canonical helper here. The polars/pandas
adapters keep their vectorised semantics — they only wrap the scalar
math defined in this module.

Formula (positions, full intraday-field set):

    day_pnl = overnight_qty × (LTP − close)        # carried
            + day_buy_qty   × LTP − day_buy_value  # bought today
            + day_sell_value − day_sell_qty × LTP  # sold today

The decomposition matters because Kite's `positions` payload returns
day_buy_value / day_sell_value as the *traded notional* (qty × fill
price), not qty × LTP — so a naive `(LTP − close) × quantity` would
miss the realised leg every time the operator closes a position
mid-session.

The naive fallback `(LTP − close) × quantity` is used only when the
intraday columns aren't all present (Dhan / Groww adapters that don't
ship the buy/sell decomposition).
"""

from __future__ import annotations


def decomposed_intraday_pnl(
    oq: float,
    ltp: float,
    cls: float,
    bq: float,
    bv: float,
    sv: float,
    sq: float,
) -> float:
    """Intraday Day P&L for a single position row.

    Args:
        oq:  overnight_quantity   — qty carried into today's session
        ltp: last_price           — live mark
        cls: close_price          — prior session's authoritative close
        bq:  day_buy_quantity     — qty bought today
        bv:  day_buy_value        — notional spent today (qty × fill)
        sv:  day_sell_value       — notional received today (qty × fill)
        sq:  day_sell_quantity    — qty sold today

    Returns:
        Day P&L in rupees. Sign convention: positive = profit on the
        day for a long position; negative = loss.
    """
    return oq * (ltp - cls) + (bq * ltp - bv) + (sv - sq * ltp)


def naive_day_pnl(ltp: float, cls: float, qty: float) -> float:
    """Naive (LTP − close) × qty — fallback when intraday decomposition
    fields aren't available (Dhan / Groww adapters)."""
    return (ltp - cls) * qty
