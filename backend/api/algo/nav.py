"""
NAV calculation — firm-level daily aggregate.

NAV (v2) =  Σ funds.net                  across all funded accounts
          + Σ position.unrealised        across open positions
          + Σ holding.quantity × LTP     across all holdings

Why these three terms (v2 corrects v1, which had two bugs):

1. funds.net — the broker's authoritative "net account value"
   = cash + collateral_haircut + realized_pnl − utilized_margin
   It already accounts for margin currently locked in open positions,
   stock collateral haircuts, and intraday realized P&L. v1 read
   `row.get("live_cash")` / `row.get("cash")` — neither column exists
   in the flattened margins DataFrame (the adapter prefixes
   available.cash → `avail cash` and available.live_balance →
   `avail live_balance` with a space). Net result: cash_total was
   always 0. Using `net` gives the broker's single-source-of-truth
   for cash-equivalent value.

2. position.unrealised — the broker's already-computed unrealized
   P&L per position. v1 used `qty × LTP` which is the NOTIONAL value
   of an F&O contract (your obligation), NOT what the position is
   worth to you. For futures and short options, qty × LTP is the
   total contract value (lakhs); your actual exposure is just the
   M2M change since entry. v2 uses the `unrealised` field that
   broker_apis.fetch_positions surfaces (Kite computes it natively).

3. holdings qty × LTP — kept as v1. You DO own the shares, so the
   full mark-to-market value is your wealth. Pledged shares are
   already counted via funds.net (haircut collateral), so non-pledged
   holdings are what fetch_holdings returns.

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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


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
    """
    from backend.shared.helpers.broker_apis import (
        fetch_holdings, fetch_positions, fetch_margins,
    )
    from backend.shared.helpers.connections import Connections
    from backend.shared.helpers.kite_ticker import _ticker

    cash_total = 0.0
    positions_mtm = 0.0
    holdings_mtm = 0.0
    accounts_in: list[str] = []
    errors: list[str] = []

    conn_keys = list(Connections().conn.keys())

    # ── Funds (net account value per broker) ──────────────────────────
    # `net` is the broker's authoritative "your account is worth this":
    #   net = cash + collateral_haircut + realized_pnl − utilized_margin
    # Already nets out margin currently locked in open positions and
    # stock-collateral haircuts. Open-position unrealized P&L is added
    # separately below via positions.unrealised.
    try:
        funds_dfs = await asyncio.to_thread(fetch_margins)
        for df in funds_dfs or []:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                net_val = float(row.get("net") or 0.0)
                cash_total += net_val
                acct = str(row.get("account") or "")
                if acct and acct != "TOTAL" and acct not in accounts_in:
                    accounts_in.append(acct)
    except Exception as e:
        errors.append(f"funds: {e}")

    # ── Positions unrealized P&L ─────────────────────────────────────
    # Broker fills `unrealised` per position natively (Kite) — same
    # value our chase / agent engine reads. Using it directly avoids
    # the F&O notional-vs-value bug: `qty × LTP` would treat a
    # 50-contract NIFTY future as worth its full notional (₹12L+) when
    # the actual exposure is just (LTP − avg_price) × qty.
    try:
        pos_dfs = await asyncio.to_thread(fetch_positions)
        for df in pos_dfs or []:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                qty = int(row.get("quantity") or 0)
                if qty == 0:
                    continue
                positions_mtm += float(row.get("unrealised") or 0.0)
                acct = str(row.get("account") or "")
                if acct and acct not in accounts_in:
                    accounts_in.append(acct)
    except Exception as e:
        errors.append(f"positions: {e}")

    # ── Holdings MTM ──────────────────────────────────────────────────
    # Use `cur_val` (broker's pre-computed qty × LTP, post lot-size
    # multiplier for MCX) so the NAV total reconciles against summing
    # the Holdings detail grid's Value column on /performance.
    try:
        hold_dfs = await asyncio.to_thread(fetch_holdings)
        for df in hold_dfs or []:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                qty = int(row.get("quantity") or row.get("opening_qty") or 0)
                if qty == 0:
                    continue
                cv = float(row.get("cur_val") or 0.0)
                if cv == 0:
                    # Fallback to qty × LTP for adapters that don't
                    # populate cur_val. Same chain as v1.
                    sym = str(row.get("tradingsymbol") or "")
                    if not sym:
                        continue
                    lp = _ticker.get_ltp_by_sym(sym) or 0.0
                    if lp <= 0:
                        lp = float(row.get("last_price") or 0.0)
                    if lp <= 0:
                        continue
                    cv = qty * lp
                holdings_mtm += cv
                acct = str(row.get("account") or "")
                if acct and acct not in accounts_in:
                    accounts_in.append(acct)
    except Exception as e:
        errors.append(f"holdings: {e}")

    nav = cash_total + positions_mtm + holdings_mtm
    return {
        "nav": round(nav, 2),
        "cash_total": round(cash_total, 2),           # = Σ funds.net (v2)
        "positions_mtm": round(positions_mtm, 2),     # = Σ position.unrealised (v2)
        "holdings_mtm": round(holdings_mtm, 2),       # = Σ qty × LTP per holding
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
