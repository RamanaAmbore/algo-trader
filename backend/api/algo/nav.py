"""
NAV calculation — firm-level daily aggregate.

NAV (v3) = Σ (cash + used_margin)        across all funded accounts
         + Σ position.unrealised         across open positions
         + Σ holding.cur_val             across all holdings

Operator framework (v3 replaces v2):

  • Collateral has zero impact on NAV. Pledged stock is the SAME
    stock already counted in holdings.cur_val — including
    funds.collateral would double-count it.
  • Used margin has zero impact on NAV. Margin currently locked
    behind open positions is still YOUR cash; it just isn't free.
    Subtracting it (as funds.net does) drops it from NAV; the fix
    is to add it back to cash so total_cash_owned = free_cash +
    locked_cash.
  • Only M2M unrealized gains/losses move NAV. Positions
    contribute their unrealised field (LTP-avg)×qty — the broker's
    pre-computed open-position P&L. Holdings contribute cur_val
    (qty × LTP). Neither term double-counts the cash spent on
    them; that cash converted into the position/holding at cost,
    and cur_val / unrealised captures the M2M re-valuation.
  • Cash spent on options or stocks counts the same as cash. The
    cost basis is automatically captured: for stocks via cur_val
    (= cost + M2M), for options via the LTP-vs-avg unrealised
    delta. No separate cash-equivalence term is needed.

v2 used funds.net + cur_val + unrealised which expanded to:
  (cash + collateral − used_margin) + cur_val + unrealised
This over-counted pledged stock (+collateral) and dropped locked
margin (−used_margin). Net error per account ≈ collateral − 2 ×
used_margin. v3 drops the collateral term and adds used_margin
back, restoring both errors.

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
    from backend.brokers.broker_apis import (
        fetch_holdings, fetch_positions, fetch_margins,
    )
    from backend.brokers.connections import Connections
    from backend.brokers.kite_ticker import _ticker

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
    # Operator framework: total cash owned = free cash + locked-as-
    # margin cash. The Kite margins payload (flattened by
    # broker_apis.fetch_margins) prefixes the keys with a space:
    #     `avail opening_balance` = SOD cash (frozen, doesn't decay
    #         intraday as the operator spends on option premium /
    #         stock buys — those debits move into holdings.cur_val
    #         and positions.unrealised respectively, so SOD cash
    #         remains the right baseline for `total_cash_owned`)
    #     `util debits` = total margin currently locked behind open
    #         positions (cash that's yours but unavailable to deploy
    #         until you close the positions). Added back to cash
    #         because it didn't leave your account; it's just
    #         reserved.
    # NOT used:
    #     `avail collateral` — the haircut value of pledged stock.
    #         The underlying stock is already in holdings.cur_val at
    #         full LTP. Including collateral here would double-count
    #         a second copy of the same stock.
    try:
        funds_dfs = await asyncio.to_thread(fetch_margins)
        for df in funds_dfs or []:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                # The adapter ships keys with literal spaces. Fall
                # back to the renamed schema names ("cash",
                # "used_margin") used by the polars layer downstream
                # so this code works against either source.
                cash_sod = float(
                    row.get("avail opening_balance")
                    or row.get("cash")
                    or 0.0
                )
                used_margin = float(
                    row.get("util debits")
                    or row.get("used_margin")
                    or 0.0
                )
                cash_total += cash_sod + used_margin
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
                cv = float(row.get("cur_val") or 0.0)
                # Skip only when the row has neither qty nor value.
                # Pledged stocks often show quantity=0 (the shares
                # are reclassified to the collateral bucket) but
                # keep their cur_val populated — operator owns them
                # and they belong in holdings_mtm.
                if qty == 0 and cv == 0:
                    continue
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
