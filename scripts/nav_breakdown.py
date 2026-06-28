"""
Per-account NAV breakdown — diagnostic script.

Runs the same broker-fetch path as compute_firm_nav() but groups the
output by ACCOUNT instead of summing across all accounts. Prints a
per-account row showing cash, positions MTM, holdings MTM, and the
per-account NAV total. Closes with the firm-level totals so you can
cross-check against `/api/nav/latest`.

Usage (on prod server):
    ssh ramboq
    cd /opt/ramboq
    sudo -u www-data python scripts/nav_breakdown.py

LTP priority matches compute_firm_nav():
    KiteTicker tick_map → row.last_price → 0  (row skipped if 0)
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict


async def main() -> None:
    from backend.brokers.broker_apis import (
        fetch_holdings, fetch_positions, fetch_margins,
    )
    # Use get_ticker() — routes to MmapTickReader when conn_service
    # owns the WS (RAMBOQ_USE_CONN_SERVICE=1). Direct `_ticker` import
    # would return the empty in-process TickerManager and every LTP
    # lookup would miss → 0 holdings_mtm → wrong NAV.
    from backend.brokers.kite_ticker import get_ticker as _get_ticker
    _ticker = _get_ticker()

    # Per-account accumulators
    cash_by_acct          = defaultdict(float)
    positions_mtm_by_acct = defaultdict(float)
    holdings_mtm_by_acct  = defaultdict(float)
    accounts: set[str] = set()

    # Per-account row counts (handy for spotting missing data)
    pos_rows_by_acct      = defaultdict(int)
    hold_rows_by_acct     = defaultdict(int)

    # ── Funds (cash + locked margin) ──────────────────────────────────
    # Operator framework: NAV's cash term = total cash owned, free
    # plus locked-as-margin. Excludes collateral (that's pledged
    # stock, already counted in holdings.cur_val below). Excludes
    # `net` (which subtracts used_margin from the operator's wealth).
    print("Fetching funds…", file=sys.stderr)
    funds_dfs = await asyncio.to_thread(fetch_margins)
    for df in funds_dfs or []:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            acct = str(row.get("account") or "")
            if not acct or acct == "TOTAL":
                continue
            accounts.add(acct)
            # v4 — SOD cash + option_premium (NOT util debits as a
            # whole). Operator: futures SPAN/exposure margin is not a
            # position COST; only long-option premium needs adding
            # back so NAV doesn't undercount premium-paid positions.
            cash_sod = float(
                row.get("avail opening_balance")
                or row.get("cash") or 0.0
            )
            opt_premium = float(
                row.get("util option_premium")
                or row.get("option_premium") or 0.0
            )
            cash_by_acct[acct] += cash_sod + opt_premium

    # ── Positions (broker's unrealised P&L) ───────────────────────────
    # Use `unrealised` directly — the broker (Kite) computes it natively
    # and surfaces it on every position row. Using `qty × LTP` would
    # mis-value F&O positions at their notional rather than their actual
    # exposure to the operator.
    print("Fetching positions…", file=sys.stderr)
    pos_dfs = await asyncio.to_thread(fetch_positions)
    for df in pos_dfs or []:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            qty = int(row.get("quantity") or 0)
            if qty == 0:
                continue
            acct = str(row.get("account") or "")
            if not acct:
                continue
            accounts.add(acct)
            positions_mtm_by_acct[acct] += float(row.get("unrealised") or 0.0)
            pos_rows_by_acct[acct] += 1

    # ── Holdings ───────────────────────────────────────────────────────
    # Use `cur_val` (broker's pre-computed qty × LTP) — matches the
    # Holdings detail grid's Value column on /performance so NAV
    # reconciles row-by-row. Pledged stock rows often have qty=0
    # but keep cur_val populated; include those too.
    print("Fetching holdings…", file=sys.stderr)
    hold_dfs = await asyncio.to_thread(fetch_holdings)
    for df in hold_dfs or []:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            qty = int(row.get("quantity") or row.get("opening_qty") or 0)
            cv = float(row.get("cur_val") or 0.0)
            if qty == 0 and cv == 0:
                continue
            acct = str(row.get("account") or "")
            if not acct:
                continue
            accounts.add(acct)
            holdings_mtm_by_acct[acct] += cv
            hold_rows_by_acct[acct] += 1

    # ── Print per-account table ────────────────────────────────────────
    def fmt(v: float) -> str:
        return f"{v:>15,.2f}"

    print()
    print(f"{'Account':<10}{'Cash+Lock':>17}{'Pos M2M':>17}{'Hold MTM':>17}{'NAV':>17}{'  Pos#':>7}{'  Hold#':>7}")
    print("-" * 90)
    firm_cash = firm_pos = firm_hold = 0.0
    for acct in sorted(accounts):
        c = cash_by_acct[acct]
        p = positions_mtm_by_acct[acct]
        h = holdings_mtm_by_acct[acct]
        n = c + p + h
        firm_cash += c
        firm_pos  += p
        firm_hold += h
        pn = pos_rows_by_acct[acct]
        hn = hold_rows_by_acct[acct]
        print(f"{acct:<10}{fmt(c)}{fmt(p)}{fmt(h)}{fmt(n)}{pn:>7}{hn:>7}")
    print("-" * 90)
    firm_nav = firm_cash + firm_pos + firm_hold
    print(f"{'TOTAL':<10}{fmt(firm_cash)}{fmt(firm_pos)}{fmt(firm_hold)}{fmt(firm_nav)}")
    print()
    print(f"firm_nav = Σ (cash_sod + option_premium)  +  Σ position.unrealised  +  Σ holdings.cur_val")
    print(f"         = {firm_cash:,.2f} + {firm_pos:,.2f} + {firm_hold:,.2f}")
    print(f"         = ₹{firm_nav:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
