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

import pandas as pd


def recompute_row_percentages(df: pd.DataFrame, sel_mask: "pd.Index") -> None:
    """Recompute day_change_percentage + pnl_percentage in-place on selected rows.

    Called after any in-place LTP or close_price override that has already
    updated day_change_val and pnl on those rows. Without this step the
    percentage columns still reflect the pre-override values (stale broker
    numbers), causing visible drift vs the absolute columns.

    Formulas (per the broker convention used throughout the codebase):

        day_change_percentage = day_change_val / |close × qty| × 100
            fallback denominator: |avg × qty| when close is zero
            (opened-today rows have close_price = 0; we use avg instead
            so the row still shows a meaningful Day % since entry)

        pnl_percentage = pnl / |avg × qty| × 100

    qty column: `quantity` for positions, `opening_quantity` for holdings.
    The function probes `opening_quantity` first and falls back to `quantity`
    so it works correctly for both route contexts.

    No-op when the required columns are absent (safe to call unconditionally).
    """
    if df is None or df.empty or len(sel_mask) == 0:
        return

    _qty_col = "opening_quantity" if "opening_quantity" in df.columns else "quantity"
    if _qty_col not in df.columns:
        return

    _qty = pd.to_numeric(df.loc[sel_mask, _qty_col], errors="coerce").fillna(0)

    if "day_change_percentage" in df.columns and "day_change_val" in df.columns:
        _dcv = pd.to_numeric(df.loc[sel_mask, "day_change_val"], errors="coerce").fillna(0)
        _cls = (
            pd.to_numeric(df.loc[sel_mask, "close_price"], errors="coerce").fillna(0)
            if "close_price" in df.columns
            else pd.Series(0.0, index=sel_mask)
        )
        _avg = (
            pd.to_numeric(df.loc[sel_mask, "average_price"], errors="coerce").fillna(0)
            if "average_price" in df.columns
            else pd.Series(0.0, index=sel_mask)
        )
        _close_denom = (_cls * _qty).abs()
        _avg_denom = (_avg * _qty).abs()
        # Primary denominator: |close × qty|; fallback: |avg × qty| for opened-today
        _denom = _close_denom.where(_close_denom > 0, _avg_denom)
        _dcp = (_dcv / _denom.replace(0, pd.NA) * 100).fillna(0)
        df.loc[sel_mask, "day_change_percentage"] = _dcp

    if "pnl_percentage" in df.columns and "pnl" in df.columns:
        _pnl = pd.to_numeric(df.loc[sel_mask, "pnl"], errors="coerce").fillna(0)
        _avg = (
            pd.to_numeric(df.loc[sel_mask, "average_price"], errors="coerce").fillna(0)
            if "average_price" in df.columns
            else pd.Series(0.0, index=sel_mask)
        )
        _cost_basis = (_avg * _qty).abs()
        _pct = (_pnl / _cost_basis.replace(0, pd.NA) * 100).fillna(0)
        df.loc[sel_mask, "pnl_percentage"] = _pct


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


def apply_day_change_backstop(raw: pd.DataFrame) -> pd.DataFrame:
    """Restore day_change_val for rows where the broker gate zeroed it.

    The polars gate `pl.when(_ltp > 0)` in `_enrich_positions` zeros
    `day_change_val` whenever Kite ships `last_price = 0`. Two shapes hit
    this path:

      Case 1 — new position (overnight_quantity == 0, ltp == 0 pre-first-tick):
        Kite's `pnl` field carries the correct value (Kite computes on their
        side with their own quote, usually non-zero). Fall back to `pnl`.

      Case 3 — fully closed intraday (quantity == 0, realised != 0):
        For a flat row `unrealised = 0` and `pnl = realised`. Fall back to
        `pnl` (covers MCX round-trip quirks where `realised = 0` but
        `pnl != 0`).

    Both cases share: `overnight_quantity == 0 AND day_change_val == 0
    AND pnl != 0`. The rescue mirrors the frontend SSOT
    `baseDayPnlForPosition` in `frontend/src/lib/data/nav.js` so route,
    background task, NAV math, snapshot writers, and alerts all agree.

    Returns a copy of `raw` with `day_change_val` restored where the mask
    fires. If `raw` is empty or lacks the required columns, returns it
    unchanged (safe to call unconditionally).
    """
    if raw is None or raw.empty:
        return raw
    raw = raw.copy()

    _qty = pd.to_numeric(
        raw.get('quantity', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)
    _oq = pd.to_numeric(
        raw.get('overnight_quantity', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)
    _dcv = pd.to_numeric(
        raw.get('day_change_val', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)
    _pnl = pd.to_numeric(
        raw.get('pnl', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)

    # Case 1: new position (oq=0, dcv zeroed by gate, pnl non-zero)
    _case1 = (_oq == 0) & (_dcv == 0) & (_pnl != 0)
    # Case 3: fully closed intraday (qty=0, dcv zeroed by gate, pnl non-zero)
    _case3 = (_qty == 0) & (_dcv == 0) & (_pnl != 0)

    _mask = _case1 | _case3
    if _mask.any() and 'day_change_val' in raw.columns:
        raw.loc[_mask, 'day_change_val'] = _pnl[_mask]
    return raw
