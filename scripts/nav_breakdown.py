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

    # ── Funds (broker's net account value) ────────────────────────────
    # Use `net` — the broker's authoritative account value (cash +
    # collateral_haircut + realized_pnl − utilized_margin). v1 read
    # `live_cash` and `cash` which don't exist in the flattened
    # margins DataFrame; they're `avail cash` / `avail live_balance`
    # with a space prefix. Either way `net` is more meaningful for NAV.
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
            cash_by_acct[acct] += float(row.get("net") or 0.0)

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
    print("Fetching holdings…", file=sys.stderr)
    hold_dfs = await asyncio.to_thread(fetch_holdings)
    for df in hold_dfs or []:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            qty = int(row.get("quantity") or row.get("opening_qty") or 0)
            if qty == 0:
                continue
            sym = str(row.get("tradingsymbol") or "")
            if not sym:
                continue
            lp = _ticker.get_ltp_by_sym(sym) or 0.0
            if lp <= 0:
                lp = float(row.get("last_price") or 0.0)
            if lp <= 0:
                continue
            acct = str(row.get("account") or "")
            if not acct:
                continue
            accounts.add(acct)
            holdings_mtm_by_acct[acct] += qty * lp
            hold_rows_by_acct[acct] += 1

    # ── Print per-account table ────────────────────────────────────────
    def fmt(v: float) -> str:
        return f"{v:>15,.2f}"

    print()
    print(f"{'Account':<10}{'Net':>17}{'Pos M2M':>17}{'Hold MTM':>17}{'NAV':>17}{'  Pos#':>7}{'  Hold#':>7}")
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
    print(f"firm_nav = Σ funds.net  +  Σ position.unrealised  +  Σ qty × LTP (holdings)")
    print(f"         = {firm_cash:,.2f} + {firm_pos:,.2f} + {firm_hold:,.2f}")
    print(f"         = ₹{firm_nav:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
