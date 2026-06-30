"""
NAV calculation — firm-level daily aggregate.

NAV (v4) = Σ (cash_sod + option_premium)  across all funded accounts
         + Σ position.unrealised          across open positions
         + Σ holding.cur_val              across all holdings

Why v4 replaces v3:

  • v3 added `used_margin` back to cash to undo broker_funds.net's
    subtraction. That double-counted any futures SPAN margin that's
    *already* embedded in position.unrealised (broker's M2M reflects
    the funded margin requirement; adding the cash form of it again
    inflates NAV). Audit Sprint E confirmed via account-level
    reconciliation against `kite.profile().net`.
  • v4 uses `option_premium` only — the sum of long-option premiums
    paid (operator-verified spec). Cash side becomes
    `cash_sod + option_premium`, leaving futures margin to flow
    purely through position.unrealised.

Operator framework (unchanged across v3 → v4):

  • Collateral has zero impact on NAV. Pledged stock is the SAME
    stock already counted in holdings.cur_val — including
    funds.collateral would double-count it.
  • Cash spent on options is captured by adding `option_premium`
    back: the broker debits cash when you buy a long option, then
    surfaces the premium under `util option_premium`. Adding it
    back means the long-option leg is reflected via
    position.unrealised (M2M re-valuation) without losing the cost
    basis.
  • Only M2M unrealized gains/losses move NAV. Positions
    contribute their unrealised field (LTP-avg)×qty — the broker's
    pre-computed open-position P&L. Holdings contribute cur_val
    (qty × LTP). Neither term double-counts the cash spent on
    them; that cash converted into the position/holding at cost,
    and cur_val / unrealised captures the M2M re-valuation.
  • Holdings qty × LTP: you DO own the shares, so the full
    mark-to-market value is your wealth. Pledged shares are
    already counted via funds.net (haircut collateral), so
    non-pledged holdings are what fetch_holdings returns.

LTPs come from the same fallback chain the strategy unrealised
calc uses: KiteTicker tick_map (zero broker quota for subscribed
symbols) → row.last_price. Symbols with no LTP available contribute
0 to the MTM (under-estimate is safer than refusing to compute).

Caller responsibility:
- Pass an active asyncio session (not running on the chase loop
  thread).
- The daily background task calls this once at 16:00 IST; the
  operator can also trigger via the admin endpoint for ad-hoc
  recompute / backfill.

Frontend equivalent: `frontend/src/lib/data/nav.js` (navByAccount).
Both surfaces share the same v4 formula; any future revision must
update both files together.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

import polars as pl

logger = logging.getLogger(__name__)


def _funds_from_df(df) -> tuple[float, list[str]]:
    """Vectorized extraction of (cash_total, accounts) from a margins DataFrame.

    NAV cash term (v4): SOD cash (avail opening_balance) + long-option
    premium paid (util option_premium). See module docstring for why.

    Returns (cash_sum, list_of_account_strings).
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    # Resolve cash column — prefer "avail opening_balance", fall back to "cash".
    if "avail opening_balance" in lf.columns:
        cash_col = pl.col("avail opening_balance").cast(pl.Float64, strict=False).fill_null(0.0)
    elif "cash" in lf.columns:
        cash_col = pl.col("cash").cast(pl.Float64, strict=False).fill_null(0.0)
    else:
        cash_col = pl.lit(0.0)

    # Resolve premium column — prefer "util option_premium", fall back to "option_premium".
    if "util option_premium" in lf.columns:
        prem_col = pl.col("util option_premium").cast(pl.Float64, strict=False).fill_null(0.0)
    elif "option_premium" in lf.columns:
        prem_col = pl.col("option_premium").cast(pl.Float64, strict=False).fill_null(0.0)
    else:
        prem_col = pl.lit(0.0)

    cash_sum = float(
        lf.select((cash_col + prem_col).sum()).to_series()[0] or 0.0
    )

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "TOTAL")
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return cash_sum, accounts


def _positions_from_df(df) -> tuple[float, list[str]]:
    """Vectorized extraction of (positions_mtm, accounts) from a positions DataFrame.

    Sums unrealised only for rows where quantity != 0 (broker-computed
    M2M — avoids the F&O notional-vs-value bug).
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    qty_col = (
        pl.col("quantity").cast(pl.Float64, strict=False).fill_null(0.0)
        if "quantity" in lf.columns
        else pl.lit(0.0)
    )
    unr_col = (
        pl.col("unrealised").cast(pl.Float64, strict=False).fill_null(0.0)
        if "unrealised" in lf.columns
        else pl.lit(0.0)
    )

    # Only sum unrealised where qty != 0 (matches original per-row guard).
    mtm = float(
        lf.select(
            pl.when(qty_col != 0.0).then(unr_col).otherwise(pl.lit(0.0)).sum()
        ).to_series()[0] or 0.0
    )

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return mtm, accounts


def _holdings_from_df(df, ticker) -> tuple[float, list[str]]:
    """Vectorized extraction of (holdings_mtm, accounts) from a holdings DataFrame.

    Uses cur_val when populated. Falls back to qty × LTP for rows where
    cur_val == 0 but qty > 0 (same logic as the original iterrows path).
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    qty_col = pl.lit(0.0)
    for c in ("quantity", "opening_qty", "opening_quantity"):
        if c in lf.columns:
            qty_col = pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0)
            break

    cv_col = (
        pl.col("cur_val").cast(pl.Float64, strict=False).fill_null(0.0)
        if "cur_val" in lf.columns
        else pl.lit(0.0)
    )

    # Rows where both qty and cur_val are zero — skip (same as original).
    # For rows with cv == 0 but qty != 0 we fall back to LTP below.
    lf = lf.with_columns(
        qty_col.alias("_qty"),
        cv_col.alias("_cv"),
    ).filter(~((pl.col("_qty") == 0.0) & (pl.col("_cv") == 0.0)))

    if lf.is_empty():
        return 0.0, []

    # Rows that already have cur_val — sum them immediately.
    lf_have_cv = lf.filter(pl.col("_cv") != 0.0)
    cv_sum = float(lf_have_cv.select(pl.col("_cv").sum()).to_series()[0] or 0.0)

    # Rows that need LTP fallback — resolve per-symbol then sum.
    lf_need_ltp = lf.filter(pl.col("_cv") == 0.0)
    ltp_sum = 0.0
    if not lf_need_ltp.is_empty() and "tradingsymbol" in lf_need_ltp.columns:
        # Collect to Python for per-symbol ticker lookup (N is small —
        # these are the adapter-missing-cur_val rows, rarely > handful).
        for row in lf_need_ltp.select(["tradingsymbol", "_qty"]).to_dicts():
            sym = str(row.get("tradingsymbol") or "")
            qty = float(row.get("_qty") or 0.0)
            if not sym or qty == 0.0:
                continue
            lp = ticker.get_ltp_by_sym(sym) or 0.0
            if lp <= 0 and "last_price" in lf_need_ltp.columns:
                # Try the last_price column as secondary fallback.
                last_prices = lf_need_ltp.filter(
                    pl.col("tradingsymbol") == sym
                ).select(
                    pl.col("last_price").cast(pl.Float64, strict=False).fill_null(0.0)
                ).to_series()
                lp = float(last_prices[0]) if not last_prices.is_empty() else 0.0
            if lp > 0:
                ltp_sum += qty * lp

    mtm = cv_sum + ltp_sum

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return mtm, accounts


async def compute_firm_nav() -> dict:
    """Return today's NAV plus the breakdown components.

    Shape:
        {
          "nav":             float,
          "cash_total":      float,
          "positions_mtm":   float,
          "holdings_mtm":    float,
          "accounts":        list[str],   # which broker codes contributed
          "errors":          list[str],   # per-account failures (non-blocking)
        }

    Each broker-account call is wrapped in its own try/except so a
    single offline broker doesn't break the whole snapshot. The
    `errors` list surfaces what was excluded; the `accounts` list
    is the inverse (what WAS included).

    Vectorized via Polars — each per-account DataFrame is converted
    once with pl.from_pandas() and aggregated with .sum() expressions
    instead of .iterrows() (~50-100× faster on typical 5-account frames).
    """
    from backend.brokers.broker_apis import (
        fetch_holdings, fetch_positions, fetch_margins,
    )
    from backend.brokers.connections import Connections
    from backend.brokers.kite_ticker import get_ticker as _get_ticker

    _ticker = _get_ticker()

    cash_total = 0.0
    positions_mtm = 0.0
    holdings_mtm = 0.0
    accounts_in: list[str] = []
    errors: list[str] = []

    conn_keys = list(Connections().conn.keys())
    if not conn_keys:
        # Cutover branch — local Connections is empty when conn_service
        # owns the sessions; fall back to the canonical account list.
        from backend.brokers.client import is_cutover_on
        if is_cutover_on():
            from backend.brokers.client.remote_broker import list_remote_accounts
            conn_keys = [r["account"] for r in list_remote_accounts() if r.get("account")]

    # ── Funds (cash + locked margin per broker) ───────────────────────
    try:
        funds_dfs = await asyncio.to_thread(fetch_margins)
        for df in funds_dfs or []:
            cash_chunk, accts = _funds_from_df(df)
            cash_total += cash_chunk
            for a in accts:
                if a and a not in accounts_in:
                    accounts_in.append(a)
    except Exception as e:
        errors.append(f"funds: {e}")

    # ── Positions unrealized P&L ─────────────────────────────────────
    try:
        pos_dfs = await asyncio.to_thread(fetch_positions)
        for df in pos_dfs or []:
            mtm_chunk, accts = _positions_from_df(df)
            positions_mtm += mtm_chunk
            for a in accts:
                if a and a not in accounts_in:
                    accounts_in.append(a)
    except Exception as e:
        errors.append(f"positions: {e}")

    # ── Holdings MTM ──────────────────────────────────────────────────
    try:
        hold_dfs = await asyncio.to_thread(fetch_holdings)
        for df in hold_dfs or []:
            hold_chunk, accts = _holdings_from_df(df, _ticker)
            holdings_mtm += hold_chunk
            for a in accts:
                if a and a not in accounts_in:
                    accounts_in.append(a)
    except Exception as e:
        errors.append(f"holdings: {e}")

    nav = cash_total + positions_mtm + holdings_mtm
    return {
        "nav": round(nav, 2),
        "cash_total": round(cash_total, 2),           # = Σ (cash_sod + option_premium)
        "positions_mtm": round(positions_mtm, 2),     # = Σ position.unrealised
        "holdings_mtm": round(holdings_mtm, 2),       # = Σ holding.cur_val
        "accounts": sorted(accounts_in),
        "errors": errors,
    }


async def write_nav_snapshot(target_date: Optional[date] = None) -> dict:
    """Compute today's NAV and write it to `nav_daily` (upsert).
    Returns the snapshot dict + the row id.

    Idempotent — same `as_of_date` re-writes the existing row (e.g.
    operator triggers a recompute mid-day after an outage clears).
    """
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.database import async_session
    from backend.api.models import NavDaily
    from backend.shared.helpers.date_time_utils import timestamp_indian

    snap = await compute_firm_nav()
    target = target_date or timestamp_indian().date()

    note = None
    if snap["errors"]:
        note = "errors: " + " | ".join(snap["errors"])[:500]

    async with async_session() as s:
        stmt = pg_insert(NavDaily).values(
            as_of_date=target,
            nav=snap["nav"],
            cash_total=snap["cash_total"],
            positions_mtm=snap["positions_mtm"],
            holdings_mtm=snap["holdings_mtm"],
            accounts_snapshot=snap["accounts"],
            note=note,
        ).on_conflict_do_update(
            index_elements=["as_of_date"],
            set_=dict(
                nav=snap["nav"],
                cash_total=snap["cash_total"],
                positions_mtm=snap["positions_mtm"],
                holdings_mtm=snap["holdings_mtm"],
                accounts_snapshot=snap["accounts"],
                note=note,
            ),
        )
        await s.execute(stmt)
        await s.commit()
    logger.info(
        f"nav_daily: wrote NAV ₹{snap['nav']:,.0f} for {target.isoformat()} "
        f"(cash ₹{snap['cash_total']:,.0f} + pos ₹{snap['positions_mtm']:,.0f} "
        f"+ hold ₹{snap['holdings_mtm']:,.0f}, accts={len(snap['accounts'])}, "
        f"errors={len(snap['errors'])})"
    )
    return snap
