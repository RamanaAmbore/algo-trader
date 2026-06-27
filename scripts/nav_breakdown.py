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
    from backend.shared.helpers.broker_apis import (
        fetch_holdings, fetch_positions, fetch_margins,
    )
    from backend.shared.helpers.kite_ticker import _ticker

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
            cash_sod = float(
                row.get("avail opening_balance")
                or row.get("cash") or 0.0
            )
            used_margin = float(
                row.get("util debits")
                or row.get("used_margin") or 0.0
            )
            cash_by_acct[acct] += cash_sod + used_margin

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
    print(f"firm_nav = Σ (cash + used_margin)  +  Σ position.unrealised  +  Σ holdings.cur_val")
    print(f"         = {firm_cash:,.2f} + {firm_pos:,.2f} + {firm_hold:,.2f}")
    print(f"         = ₹{firm_nav:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
