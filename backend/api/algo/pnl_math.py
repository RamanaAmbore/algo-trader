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


# ---------------------------------------------------------------------------
# Day P&L / Exp P&L redesign (2026-09) — baseline-diff SSOT
# ---------------------------------------------------------------------------
#
# Day P&L for any position, regardless of state (new entry, full exit,
# partial exit, re-entry, flip, or any combination), reduces to one
# formula:
#
#     day_pnl = current_total_profit(realised, unrealised) - base_pnl
#
# where `base_pnl` is that position's total profit frozen at the most
# recent trading day's close-reset snapshot (0 when no prior snapshot
# exists — e.g. a brand-new position). This is proven algebraically
# correct and needs no branching by position-state, unlike the legacy
# `decomposed_intraday_pnl` / `apply_day_change_backstop` machinery
# above, which is kept for diagnostic purposes only and must not feed
# new rollups or displays.
#
# Per-broker sourcing of `realised` / `unrealised` is owned by the
# broker layer (`backend/brokers/broker_apis.py`) — by the time a
# DataFrame reaches this module, `realised` + `unrealised` are assumed
# to sum to the correct combined total-profit for every broker (Kite,
# Dhan, Groww). Callers here never re-derive per-broker logic.


def current_total_profit(realised: float, unrealised: float) -> float:
    """Canonical current total profit for one position: realised + unrealised.

    Never use a broker's raw combined `pnl` field directly here — the
    broker layer is responsible for making `realised`/`unrealised` sum to
    the correct total for every broker before the DataFrame reaches this
    module (see module docstring).
    """
    return float(realised or 0.0) + float(unrealised or 0.0)


def baseline_diff_day_pnl(realised: float, unrealised: float, base_pnl: float) -> float:
    """Canonical Day P&L for one position: current total profit − base_pnl.

    `base_pnl` is the position's `current_total_profit` frozen at the most
    recent trading day's close-reset snapshot (0 when no prior snapshot
    exists, e.g. a position opened today). Correct for every position
    state — new entry, full exit, partial exit, re-entry, and flip —
    without branching, because it is a pure difference of two point-in-time
    totals.
    """
    return current_total_profit(realised, unrealised) - float(base_pnl or 0.0)


def current_total_profit_series(realised: "pd.Series", unrealised: "pd.Series") -> "pd.Series":
    """Vectorised pandas wrapper over `current_total_profit`."""
    _r = pd.to_numeric(realised, errors="coerce").fillna(0.0)
    _u = pd.to_numeric(unrealised, errors="coerce").fillna(0.0)
    return _r + _u


def baseline_diff_day_pnl_series(
    realised: "pd.Series", unrealised: "pd.Series", base_pnl: "pd.Series | float"
) -> "pd.Series":
    """Vectorised pandas wrapper over `baseline_diff_day_pnl`.

    `base_pnl` may be a scalar (broadcast) or a per-row Series (e.g. the
    `prev_settlement_pnl` column backfilled from `daily_book.total_pnl`).
    """
    _total = current_total_profit_series(realised, unrealised)
    if isinstance(base_pnl, pd.Series):
        _base = pd.to_numeric(base_pnl, errors="coerce").fillna(0.0)
    else:
        _base = float(base_pnl or 0.0)
    return _total - _base


def current_total_profit_expr(
    realised_col: str = "realised", unrealised_col: str = "unrealised"
):
    """Polars expression wrapper over `current_total_profit`.

    Lazy-imports polars so this module stays importable in pandas-only
    test contexts. Returns a `pl.Expr` summing the two (null-safe) columns.
    """
    import polars as pl
    return (
        pl.col(realised_col).cast(pl.Float64, strict=False).fill_null(0.0)
        + pl.col(unrealised_col).cast(pl.Float64, strict=False).fill_null(0.0)
    )


def baseline_diff_day_pnl_expr(
    realised_col: str = "realised",
    unrealised_col: str = "unrealised",
    base_pnl_col: str = "prev_settlement_pnl",
):
    """Polars expression wrapper over `baseline_diff_day_pnl`."""
    import polars as pl
    return current_total_profit_expr(realised_col, unrealised_col) - (
        pl.col(base_pnl_col).cast(pl.Float64, strict=False).fill_null(0.0)
    )


# ---------------------------------------------------------------------------
# realised/unrealised "not populated" fallback — SSOT
# ---------------------------------------------------------------------------
#
# Some producers (closed-hours snapshot rows built before this redesign,
# paper-trade synthetic rows, any pre-deploy window) only carry the
# broker-combined `pnl` field and leave `realised`/`unrealised` at their
# struct default of 0.0 each. A naive `realised + unrealised` on such a row
# silently evaluates to 0 instead of falling back to `pnl` — this is exactly
# the class of bug the 2026-09 Day P&L redesign audit caught (frontend and
# backend disagreeing on the same row because they used different fallback
# triggers). The rule below is the SINGLE fallback trigger: both `realised`
# and `unrealised` being exactly 0 together means "not populated, use pnl
# as the realised leg" — every summary builder (row-level, polars-vectorised,
# frontend `currentTotalProfit`) must use this exact trigger.

def resolve_realised_unrealised(
    realised: float, unrealised: float, pnl: float
) -> tuple[float, float]:
    """Return the (realised, unrealised) pair to feed `current_total_profit`.

    When `realised` and `unrealised` are BOTH exactly 0 (the "not split /
    not populated" case), fall back to `(pnl, 0.0)` — Kite's `pnl` is
    confirmed to equal `realised + unrealised`, so this is algebraically
    equivalent to a fully-populated row with all its profit on the
    realised leg. Otherwise returns `(realised, unrealised)` unchanged,
    even when one of the two is legitimately 0 (e.g. a fresh open
    position has realised=0, unrealised>0 — must NOT trigger the
    pnl fallback).
    """
    r = float(realised or 0.0)
    u = float(unrealised or 0.0)
    if r or u:
        return r, u
    return float(pnl or 0.0), 0.0


def baseline_diff_day_pnl_with_fallback(
    realised: float, unrealised: float, pnl: float, base_pnl: float
) -> float:
    """`baseline_diff_day_pnl`, applying `resolve_realised_unrealised`'s
    pnl-fallback first. SSOT for any per-row Day P&L computation that may
    see an unpopulated realised/unrealised pair (snapshot rows, paper
    rows, pre-deploy rows)."""
    r, u = resolve_realised_unrealised(realised, unrealised, pnl)
    return baseline_diff_day_pnl(r, u, base_pnl)


def baseline_diff_day_pnl_series_with_fallback(
    realised: "pd.Series", unrealised: "pd.Series", pnl: "pd.Series", base_pnl: "pd.Series | float"
) -> "pd.Series":
    """Vectorised pandas wrapper over `baseline_diff_day_pnl_with_fallback` —
    the pnl-fallback-aware SSOT variant of `baseline_diff_day_pnl_series`.

    Falls back to `pnl` as the realised leg (unrealised=0) on rows where
    `realised` AND `unrealised` are both exactly 0 (the "not populated"
    case — same trigger as `resolve_realised_unrealised` /
    `baseline_diff_day_pnl_expr_with_fallback`, used by positions.py's
    polars route-summary path `_with_baseline_diff_day_change`). Use this
    (not the plain `baseline_diff_day_pnl_series`) for ANY pandas-path
    summary rebuild that consumes broker rows where realised/unrealised
    may be unpopulated (e.g. Groww rows missing native realised_pnl /
    unrealised_pnl) — otherwise that rebuild disagrees with the polars
    route-summary path on the exact same underlying row (2026-09 Day P&L
    audit round 3, item #4)."""
    _r = pd.to_numeric(realised, errors="coerce").fillna(0.0)
    _u = pd.to_numeric(unrealised, errors="coerce").fillna(0.0)
    _p = pd.to_numeric(pnl, errors="coerce").fillna(0.0)
    _populated = (_r != 0.0) | (_u != 0.0)
    _total = (_r + _u).where(_populated, _p)
    if isinstance(base_pnl, pd.Series):
        _base = pd.to_numeric(base_pnl, errors="coerce").fillna(0.0)
    else:
        _base = float(base_pnl or 0.0)
    return _total - _base


def baseline_diff_day_pnl_expr_with_fallback(
    realised_col: str = "realised",
    unrealised_col: str = "unrealised",
    pnl_col: str = "pnl",
    base_pnl_col: str = "prev_settlement_pnl",
):
    """Polars expression wrapper over `baseline_diff_day_pnl_with_fallback`.

    Falls back to `pnl_col` as the realised leg (unrealised=0) only on
    rows where `realised_col` AND `unrealised_col` are both exactly 0 —
    same trigger as the scalar helper, vectorised via `pl.when`."""
    import polars as pl
    _r = pl.col(realised_col).cast(pl.Float64, strict=False).fill_null(0.0)
    _u = pl.col(unrealised_col).cast(pl.Float64, strict=False).fill_null(0.0)
    _p = pl.col(pnl_col).cast(pl.Float64, strict=False).fill_null(0.0)
    _base = pl.col(base_pnl_col).cast(pl.Float64, strict=False).fill_null(0.0)
    _total = pl.when((_r != 0.0) | (_u != 0.0)).then(_r + _u).otherwise(_p)
    return _total - _base


def _recompute_day_change_pct(
    df: pd.DataFrame, sel_mask: "pd.Index", qty: "pd.Series"
) -> None:
    """Recompute day_change_percentage in-place. Primary denom: |prev_close × qty|;
    fallback: |avg × qty| for opened-today rows where prev_close == 0."""
    if "day_change_percentage" not in df.columns or "day_change_val" not in df.columns:
        return
    _dcv = pd.to_numeric(df.loc[sel_mask, "day_change_val"], errors="coerce").fillna(0)
    _cls = (
        pd.to_numeric(df.loc[sel_mask, "prev_close"], errors="coerce").fillna(0)
        if "prev_close" in df.columns
        else pd.Series(0.0, index=sel_mask)
    )
    _avg = (
        pd.to_numeric(df.loc[sel_mask, "average_price"], errors="coerce").fillna(0)
        if "average_price" in df.columns
        else pd.Series(0.0, index=sel_mask)
    )
    _close_denom = (_cls * qty).abs()
    _avg_denom = (_avg * qty).abs()
    _denom = _close_denom.where(_close_denom > 0, _avg_denom)
    df.loc[sel_mask, "day_change_percentage"] = (
        _dcv / _denom.replace(0, pd.NA) * 100
    ).fillna(0)


def _recompute_pnl_pct(
    df: pd.DataFrame, sel_mask: "pd.Index", qty: "pd.Series"
) -> None:
    """Recompute pnl_percentage in-place: pnl / |avg × qty| × 100."""
    if "pnl_percentage" not in df.columns or "pnl" not in df.columns:
        return
    _pnl = pd.to_numeric(df.loc[sel_mask, "pnl"], errors="coerce").fillna(0)
    _avg = (
        pd.to_numeric(df.loc[sel_mask, "average_price"], errors="coerce").fillna(0)
        if "average_price" in df.columns
        else pd.Series(0.0, index=sel_mask)
    )
    _cost_basis = (_avg * qty).abs()
    df.loc[sel_mask, "pnl_percentage"] = (
        _pnl / _cost_basis.replace(0, pd.NA) * 100
    ).fillna(0)


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

    qty column: always `quantity` (remaining shares); `opening_quantity` only as
    last-resort fallback. For partial-sell holdings DataFrames `opening_quantity`
    is the original lot count (pre-sell) which overstates the P&L denominator.

    No-op when the required columns are absent (safe to call unconditionally).
    """
    if df is None or df.empty or len(sel_mask) == 0:
        return

    _qty_col = "quantity" if "quantity" in df.columns else "opening_quantity"
    if _qty_col not in df.columns:
        return

    _qty = pd.to_numeric(df.loc[sel_mask, _qty_col], errors="coerce").fillna(0)
    _recompute_day_change_pct(df, sel_mask, _qty)
    _recompute_pnl_pct(df, sel_mask, _qty)


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
        cls: prev_close           — prior session's authoritative close
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
    `day_change_val` whenever Kite ships `last_price = 0`. Three shapes hit
    this path:

      Case 1 — new position (overnight_quantity == 0, ltp == 0 pre-first-tick):
        Kite's `pnl` field carries the correct value (Kite computes on their
        side with their own quote, usually non-zero). Fall back to `pnl`.

      Case 2 — overnight position where the LTP gate zeroed dcv but broker
        pnl is valid (overnight_quantity > 0, day_change_val == 0, pnl != 0,
        prev_close > 0, average_price > 0). Recovery mirrors the frontend
        SSOT `baseDayPnlForPosition` formula:
            day_pnl = pnl − (close − avg) × oq
        This strips out the overnight carry so only today's session P&L
        remains.

      Case 3 — fully closed intraday round-trip (quantity == 0,
        overnight_quantity == 0, realised != 0):
        For a flat intraday row `unrealised = 0` and `pnl = realised`.
        Fall back to `pnl` (covers MCX round-trip quirks). Requires
        overnight_quantity == 0 — closed overnight positions (qty=0, oq>0)
        must use Case 2 so only today's component is returned, not the
        full gain from entry price.

    Cases 1 and 3 write `pnl` directly; Case 2 writes the decomposed value.
    The rescue mirrors the frontend SSOT `baseDayPnlForPosition` in
    `frontend/src/lib/data/nav.js` so route, background task, NAV math,
    snapshot writers, and alerts all agree.

    Column name: the DataFrame is expected to have `prev_close` (not
    `close_price`); the fallback `raw.get('close_price', ...)` is kept as
    a backward-compat shim for any caller that hasn't migrated yet.

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
        raw.get('overnight_quantity', pd.Series(0.0, index=raw.index)), errors='coerce'
    ).fillna(0)
    _dcv = pd.to_numeric(
        raw.get('day_change_val', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)
    _pnl = pd.to_numeric(
        raw.get('pnl', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)
    _cls = pd.to_numeric(
        raw.get('prev_close', raw.get('close_price', pd.Series(dtype=float))), errors='coerce'
    ).fillna(0)
    _avg = pd.to_numeric(
        raw.get('average_price', pd.Series(dtype=float)), errors='coerce'
    ).fillna(0)

    # Case 1: new position (oq=0, dcv zeroed by gate, pnl non-zero)
    _case1 = (_oq == 0) & (_dcv == 0) & (_pnl != 0)
    # Case 2: overnight position, LTP gate zeroed dcv, broker pnl is valid
    _case2 = (_oq != 0) & (_dcv == 0) & (_pnl != 0) & (_cls > 0) & (_avg > 0)
    _case2_val = _pnl - (_cls - _avg) * _oq
    # Case 3: fully closed intraday round-trip (qty=0, oq=0, dcv zeroed, pnl non-zero).
    # Requires oq=0 — closed OVERNIGHT positions (qty=0, oq>0) must NOT fall through
    # here, because pnl for those rows = total gain since entry price, not today's
    # session gain. Case 2 handles the overnight-closed path when prev_close is
    # available; when it isn't (e.g. Dhan adapter), dcv stays 0 (conservative).
    _case3 = (_qty == 0) & (_oq == 0) & (_dcv == 0) & (_pnl != 0)

    _mask_pnl = _case1 | _case3
    _mask = _mask_pnl | _case2
    if _mask.any() and 'day_change_val' in raw.columns:
        if _mask_pnl.any():
            raw.loc[_mask_pnl, 'day_change_val'] = _pnl[_mask_pnl]
        if _case2.any():
            raw.loc[_case2, 'day_change_val'] = _case2_val[_case2]
    return raw
