"""
Daily book snapshot — captures holdings / positions / trades from every
loaded Kite account and upserts them into the daily_book table.

Called:
  - by _task_daily_snapshot in background.py (at 15:35 IST + once at startup)
  - by POST /api/admin/pnl/snapshot (manual / backfill)

NOTE on trades: kite.trades() only returns TODAY's trades. Snapshotting a
past date will produce holdings + positions rows but ZERO trades rows — the
broker SDK has no historical trades endpoint. Historical trade data must be
imported via the CSV upload path (future).
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import text

from backend.api.database import async_session
from backend.shared.helpers.date_time_utils import timestamp_indian
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Reuse background.py's executor when called from there; create a local one
# for the admin endpoint path.
_local_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ramboq-snap")


# ---------------------------------------------------------------------------
# Segment classifier
# ---------------------------------------------------------------------------

def kite_seg_from_exchange(exchange: str) -> str:
    """Map a Kite exchange string to our segment vocabulary."""
    _MAP = {
        "NSE": "equity",
        "BSE": "equity",
        "NFO": "derivatives",
        "BFO": "derivatives",
        "MCX": "commodity",
        "CDS": "currency",
        "MF":  "equity",
    }
    return _MAP.get((exchange or "").upper(), "equity")


# ---------------------------------------------------------------------------
# Per-account fetch helpers (sync — run in executor)
# ---------------------------------------------------------------------------

def _fetch_account_data(kite, account: str, target_date: date) -> dict:
    """
    Fetch holdings, positions, and (if target_date == today) trades for one
    account. Returns a dict with keys 'holdings', 'positions', 'trades', each
    being a list of raw row dicts.

    Errors are caught per-kind; a failure in one kind does NOT abort the
    others.
    """
    from backend.shared.helpers.date_time_utils import timestamp_indian
    today_ist = timestamp_indian().date()
    is_today = (target_date == today_ist)

    out: dict[str, list[dict]] = {"holdings": [], "positions": [], "trades": []}

    try:
        out["holdings"] = kite.holdings() or []
    except Exception as e:
        logger.warning(f"Snapshot [{account}] holdings fetch failed: {e}")

    try:
        raw_pos = kite.positions() or {}
        out["positions"] = raw_pos.get("net", [])
    except Exception as e:
        logger.warning(f"Snapshot [{account}] positions fetch failed: {e}")

    if is_today:
        try:
            out["trades"] = kite.trades() or []
        except Exception as e:
            logger.warning(f"Snapshot [{account}] trades fetch failed: {e}")
    else:
        logger.debug(f"Snapshot [{account}] skipping trades for past date {target_date}")

    return out


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _holdings_rows(account: str, target_date: date, raw: list[dict]) -> list[dict]:
    rows = []
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NSE")
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      kite_seg_from_exchange(exchange),
            "kind":         "holdings",
            "symbol":       symbol,
            "exchange":     exchange,
            "qty":          int(r.get("opening_quantity") or r.get("quantity") or 0),
            "avg_cost":     float(r["average_price"]) if r.get("average_price") is not None else None,
            "ltp":          float(r["last_price"])    if r.get("last_price")    is not None else None,
            "day_pnl":      float(r["day_change"])    if r.get("day_change")    is not None else None,
            "total_pnl":    float(r["pnl"])           if r.get("pnl")           is not None else None,
            "payload_json": json.dumps(r, default=str),
        })
    return rows


def _positions_rows(account: str, target_date: date, raw: list[dict]) -> list[dict]:
    rows = []
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NFO")
        last_price  = r.get("last_price")
        close_price = r.get("close_price")
        qty         = r.get("quantity") or 0
        # Kite's net positions don't expose a day-only pnl field, but the
        # math is unambiguous: (last - close) × qty captures the move
        # from yesterday's close to now. Same formula broker_apis uses
        # for live data — keeping the snapshot in sync.
        day_pnl = None
        if last_price is not None and close_price is not None:
            day_pnl = float((last_price - close_price) * qty)
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      kite_seg_from_exchange(exchange),
            "kind":         "positions",
            "symbol":       symbol,
            "exchange":     exchange,
            "qty":          int(qty),
            "avg_cost":     float(r["average_price"]) if r.get("average_price") is not None else None,
            "ltp":          float(last_price)         if last_price             is not None else None,
            "day_pnl":      day_pnl,
            "total_pnl":    float(r["pnl"])           if r.get("pnl")           is not None else None,
            "payload_json": json.dumps(r, default=str),
        })
    return rows


def _trades_rows(account: str, target_date: date, raw: list[dict]) -> list[dict]:
    rows = []
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NSE")
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      kite_seg_from_exchange(exchange),
            "kind":         "trades",
            "symbol":       symbol,
            "exchange":     exchange,
            "qty":          int(r.get("filled_quantity") or r.get("quantity") or 0),
            "avg_cost":     float(r["average_price"]) if r.get("average_price") is not None else None,
            "ltp":          None,
            "day_pnl":      None,
            "total_pnl":    None,
            "payload_json": json.dumps(r, default=str),
        })
    return rows


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------

_UPSERT_SQL = text("""
    INSERT INTO daily_book
        (date, account, segment, kind, symbol, exchange,
         qty, avg_cost, ltp, day_pnl, total_pnl, payload_json, captured_at)
    VALUES
        (:date, :account, :segment, :kind, :symbol, :exchange,
         :qty, :avg_cost, :ltp, :day_pnl, :total_pnl, :payload_json, :captured_at)
    ON CONFLICT (date, account, kind, symbol) DO UPDATE SET
        segment      = EXCLUDED.segment,
        exchange     = EXCLUDED.exchange,
        qty          = EXCLUDED.qty,
        avg_cost     = EXCLUDED.avg_cost,
        ltp          = EXCLUDED.ltp,
        day_pnl      = EXCLUDED.day_pnl,
        total_pnl    = EXCLUDED.total_pnl,
        payload_json = EXCLUDED.payload_json,
        captured_at  = EXCLUDED.captured_at
""")


async def _upsert_rows(rows: list[dict]) -> int:
    """Upsert a batch of row dicts. Returns count of rows processed."""
    if not rows:
        return 0
    now_utc = datetime.now(timezone.utc)
    for r in rows:
        r["captured_at"] = now_utc
    async with async_session() as session:
        await session.execute(_UPSERT_SQL, rows)
        await session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _get_connections():
    """Thin wrapper so tests can patch this module-level name."""
    from backend.shared.helpers.connections import Connections
    return Connections()


async def snapshot_daily_book(target_date: Optional[date] = None) -> dict:
    """
    Capture every loaded account's holdings + positions + trades for
    target_date (defaults to today IST). Upserts into daily_book.

    Returns:
        {
            "accounts":       <list of account codes processed>,
            "holdings_rows":  <int>,
            "positions_rows": <int>,
            "trades_rows":    <int>,
            "errors":         <list of error strings>,
        }

    NOTE: trades are only available for today's date. Snapshots for a past
    date will produce holdings + positions rows but zero trades rows.
    """

    if target_date is None:
        target_date = timestamp_indian().date()  # type: ignore[attr-defined]

    connections = _get_connections()
    accounts = list(connections.conn.keys())
    if not accounts:
        logger.warning("Snapshot: no loaded broker accounts — nothing to capture")
        return {"accounts": [], "holdings_rows": 0, "positions_rows": 0,
                "trades_rows": 0, "errors": ["No loaded broker accounts"]}

    loop = asyncio.get_running_loop()
    totals = {"holdings_rows": 0, "positions_rows": 0, "trades_rows": 0}
    errors: list[str] = []
    processed: list[str] = []

    for account, kite_conn in connections.conn.items():
        try:
            kite = kite_conn.get_kite_conn()
            raw = await loop.run_in_executor(
                _local_executor, _fetch_account_data, kite, account, target_date
            )

            h_rows = _holdings_rows(account,  target_date, raw["holdings"])
            p_rows = _positions_rows(account, target_date, raw["positions"])
            t_rows = _trades_rows(account,    target_date, raw["trades"])

            totals["holdings_rows"]  += await _upsert_rows(h_rows)
            totals["positions_rows"] += await _upsert_rows(p_rows)
            totals["trades_rows"]    += await _upsert_rows(t_rows)
            processed.append(account)

            logger.info(
                f"Snapshot [{account}] date={target_date} "
                f"holdings={len(h_rows)} positions={len(p_rows)} trades={len(t_rows)}"
            )
        except Exception as e:
            msg = f"Snapshot [{account}] failed: {e}"
            logger.error(msg)
            errors.append(msg)

    return {
        "accounts":      processed,
        "holdings_rows": totals["holdings_rows"],
        "positions_rows": totals["positions_rows"],
        "trades_rows":   totals["trades_rows"],
        "errors":        errors,
    }
