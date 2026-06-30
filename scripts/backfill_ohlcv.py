"""
backfill_ohlcv.py — Standalone operator script for immediate prod fix.

Imports the backfill helpers and runs them against the live DB.  Prints a
summary table for each kind.  Designed to be run as www-data via sudo:

    ssh ramboq
    cd /opt/ramboq && sudo -u www-data ./venv/bin/python scripts/backfill_ohlcv.py --daily --intraday

Requires a live DB connection (same DATABASE_URL the API uses).  The script
wires its own async event loop, matching the pattern in scripts/capture_metrics.py.

Arguments:
  --daily     Run backfill_ohlcv_daily (365-day coverage check)
  --intraday  Run backfill_intraday_today (today's 30-min bars)
  (both flags can be combined; omitting both prints help and exits)

Notes:
  - The symbol universe is built from the DB (watchlist_items + holdings +
    positions as seen by the broker at call time) + the mover warm set.
  - Rate-limit cool-off is respected.  If the price broker is in cool-off,
    affected symbols are skipped and reported; run again after the cool-off
    expires (60 s by default).
  - Persistent write-back is handled by the write_queue; allow a few seconds
    after the script exits for the workers to flush remaining batches.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

# Make the script runnable from the repo root without -m.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Universe builder (standalone — no Litestar app, no HTTP server)
# ---------------------------------------------------------------------------

async def _build_universe() -> list[tuple[str, str]]:
    """Build the same 300-symbol universe as _task_warm_backfill in background.py."""
    symbols: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # 1. Watchlist.
    try:
        from backend.api.database import async_session
        from backend.api.models import WatchlistItem
        from sqlalchemy import select as sa_select
        from backend.api.routes.watchlist import _resolve_mcx_commodity, _resolve_cds_currency

        async with async_session() as sess:
            rows = (await sess.execute(
                sa_select(WatchlistItem.tradingsymbol, WatchlistItem.exchange)
            )).all()
        for row in rows:
            sym  = (row.tradingsymbol or "").upper().strip()
            exch = (row.exchange or "NSE").upper().strip()
            if not sym:
                continue
            if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_mcx_commodity(sym)
                if resolved:
                    sym = resolved.upper().strip()
            elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
                resolved = await _resolve_cds_currency(sym)
                if resolved:
                    sym = resolved.upper().strip()
            key = (sym, exch)
            if key not in seen:
                seen.add(key)
                symbols.append(key)
        print(f"  watchlist: {len(symbols)} symbols", flush=True)
    except Exception as exc:
        print(f"  [WARN] watchlist collect failed: {exc}", flush=True)

    # 2. Holdings + Positions (live broker fetch).
    try:
        import pandas as pd
        from backend.brokers import broker_apis

        for fetch_fn, label, default_exch in (
            (broker_apis.fetch_holdings,  "holdings",  "NSE"),
            (broker_apis.fetch_positions, "positions", "NFO"),
        ):
            dfs = fetch_fn()
            df  = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
            before = len(symbols)
            if not df.empty and "tradingsymbol" in df.columns:
                _exch_col = df["exchange"] if "exchange" in df.columns else pd.Series([default_exch] * len(df))
                for s, e in zip(df["tradingsymbol"], _exch_col):
                    sym  = str(s or "").upper().strip()
                    exch = str(e or default_exch).upper().strip()
                    if sym:
                        k = (sym, exch)
                        if k not in seen:
                            seen.add(k)
                            symbols.append(k)
            print(f"  {label}: +{len(symbols) - before} symbols (total {len(symbols)})", flush=True)
    except Exception as exc:
        print(f"  [WARN] holdings/positions collect failed: {exc}", flush=True)

    # 3. Mover universe (fills up to 300-symbol cap).
    try:
        from backend.shared.helpers.mover_universe import mover_warm_pairs
        _mover_set  = set(mover_warm_pairs())
        book_pairs  = [p for p in symbols if p not in _mover_set]
        mover_pairs_now = [p for p in symbols if p in _mover_set]
        remaining   = max(0, 300 - len(book_pairs))
        symbols     = book_pairs + mover_pairs_now[:remaining]

        before = len(symbols)
        for key in mover_warm_pairs():
            if key not in seen and len(symbols) < 300:
                seen.add(key)
                symbols.append(key)
        print(f"  movers: +{len(symbols) - before} symbols (total {len(symbols)})", flush=True)
    except Exception as exc:
        print(f"  [WARN] mover universe collect failed: {exc}", flush=True)
        symbols = symbols[:300]

    return symbols[:300]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_summary(kind: str, result: dict) -> None:
    print(f"\n{'='*56}", flush=True)
    print(f"  {kind.upper()} BACKFILL SUMMARY", flush=True)
    print(f"{'='*56}", flush=True)
    print(f"  Requested      : {result['requested']}", flush=True)
    print(f"  Filled         : {result['filled']}", flush=True)
    print(f"  Skipped cooloff: {result['skipped_cooloff']}", flush=True)
    print(f"  Errors         : {len(result['errors'])}", flush=True)
    if result["errors"]:
        print("  Error details:", flush=True)
        for sym, exch, msg in result["errors"][:10]:
            print(f"    {sym}/{exch}: {msg}", flush=True)
        if len(result["errors"]) > 10:
            print(f"    … and {len(result['errors']) - 10} more (see app log)", flush=True)
    print(f"{'='*56}\n", flush=True)


async def _main(run_daily: bool, run_intraday: bool) -> int:
    from backend.api.persistence.backfill import backfill_ohlcv_daily, backfill_intraday_today
    from backend.shared.helpers.date_time_utils import is_any_segment_open, timestamp_indian

    # Initialise the DB engine so async_session works.
    from backend.api.database import init_db
    await init_db()

    print("\nBuilding symbol universe …", flush=True)
    symbols = await _build_universe()
    print(f"\nFinal universe: {len(symbols)} symbols\n", flush=True)

    if not symbols:
        print("[ERROR] Empty symbol universe — nothing to backfill.", flush=True)
        return 1

    rc = 0

    if run_daily:
        print(f"Running ohlcv_daily backfill (target_days=365) …", flush=True)
        result = await backfill_ohlcv_daily(symbols, target_days=365, max_concurrent=3)
        _print_summary("ohlcv_daily", result)

    if run_intraday:
        now_ist = timestamp_indian()
        if is_any_segment_open(now_ist):
            print(f"Running intraday_today backfill (interval=30minute) …", flush=True)
            result2 = await backfill_intraday_today(symbols, interval="30minute", max_concurrent=3)
            _print_summary("intraday_today", result2)
        else:
            print(
                "[INFO] Skipping intraday_today — no market segment is currently open.\n"
                "       Today's bars accumulate during session; run this flag during market hours\n"
                "       or rely on the startup hook at next session open.",
                flush=True,
            )

    return rc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Operator backfill script for ohlcv_daily and intraday_bars.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Backfill ohlcv_daily — force-fetch 365-day window for under-covered symbols.",
    )
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="Backfill intraday_bars — force-fetch today's 30-min bars (market hours only).",
    )
    args = parser.parse_args()

    if not args.daily and not args.intraday:
        parser.print_help()
        sys.exit(0)

    rc = asyncio.run(_main(run_daily=args.daily, run_intraday=args.intraday))
    sys.exit(rc)


if __name__ == "__main__":
    main()
