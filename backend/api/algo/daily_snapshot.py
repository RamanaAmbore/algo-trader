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
from datetime import date, datetime, time as _dt_time, timezone
from typing import Optional

from sqlalchemy import text

from backend.api.database import async_session
from backend.shared.helpers.date_time_utils import timestamp_indian
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Exchange session windows (IST) — used to decide whether a snapshot
# captures EOD or mid-session data. MCX runs 09:00–23:30; NSE/BSE and
# derivatives run 09:15–15:30. Weekends and holidays are not checked
# here — the upstream task gates by `is_market_open` already; this
# helper only answers "if it were a trading day, is this exchange in
# session at now_ist?". Used by row builders to skip ltp/day_pnl
# capture for mid-session rows.
_NSE_OPEN_T  = _dt_time(9, 15)
_NSE_CLOSE_T = _dt_time(15, 30)
_MCX_OPEN_T  = _dt_time(9, 0)
_MCX_CLOSE_T = _dt_time(23, 30)


def _is_exchange_open_at(exchange: str, now_ist: datetime) -> bool:
    """True when `exchange` is in active session at `now_ist` (time-of-day
    only). MCX session 09:00–23:30 IST; equity exchanges 09:15–15:30 IST.
    Mid-session captures pollute the close-override path in positions.py
    (which treats the most recent pre-today daily_book row as yesterday's
    EOD) so callers emit `ltp=None`, `day_pnl=None` for in-session rows
    and rely on the 23:35 IST follow-up snapshot to fill MCX correctly."""
    exch = (exchange or "").upper()
    t = now_ist.time()
    if exch == "MCX":
        return _MCX_OPEN_T <= t <= _MCX_CLOSE_T
    return _NSE_OPEN_T <= t <= _NSE_CLOSE_T

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

def _fetch_account_data(broker, account: str, target_date: date) -> dict:
    """
    Fetch holdings, positions, and (if target_date == today) trades for one
    account. Returns a dict with keys 'holdings', 'positions', 'trades', each
    being a list of raw row dicts.

    Takes a `Broker` adapter from the registry — broker-agnostic path,
    so a Groww/Dhan account snapshots the same way.

    Errors are caught per-kind; a failure in one kind does NOT abort the
    others.
    """
    from backend.shared.helpers.date_time_utils import timestamp_indian
    today_ist = timestamp_indian().date()
    is_today = (target_date == today_ist)

    out: dict[str, list[dict]] = {
        "holdings": [], "positions": [], "trades": [], "funds": [],
    }

    try:
        out["holdings"] = broker.holdings() or []
    except Exception as e:
        logger.warning(f"Snapshot [{account}] holdings fetch failed: {e}")

    try:
        raw_pos = broker.positions() or {}
        out["positions"] = raw_pos.get("net", [])
    except Exception as e:
        logger.warning(f"Snapshot [{account}] positions fetch failed: {e}")

    if is_today:
        try:
            out["trades"] = broker.trades() or []
        except Exception as e:
            logger.warning(f"Snapshot [{account}] trades fetch failed: {e}")
        # Funds snapshot — capture today's per-segment margin
        # balances so the History → Funds tab can show a per-
        # account ledger over time. Stored as one row per segment
        # (equity / commodity); past dates are skipped because the
        # broker's margins() endpoint only returns CURRENT state.
        try:
            m = broker.margins() or {}
            if isinstance(m, dict):
                # Kite-shape: { equity: {available: {...}, utilised: {...}, net: X},
                #               commodity: {...} }. Dhan / Groww broker
                # adapters synthesise the same envelope.
                for seg_key in ("equity", "commodity"):
                    seg = m.get(seg_key)
                    if isinstance(seg, dict):
                        out["funds"].append({
                            "segment_label": seg_key,
                            "available":     (seg.get("available") or {}),
                            "utilised":      (seg.get("utilised")  or {}),
                            "net":           seg.get("net", 0),
                        })
        except Exception as e:
            logger.warning(f"Snapshot [{account}] margins fetch failed: {e}")
    else:
        logger.debug(f"Snapshot [{account}] skipping trades for past date {target_date}")

    return out


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _holdings_rows(
    account: str, target_date: date, raw: list[dict], now_ist: datetime,
) -> list[dict]:
    rows = []
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NSE")
        mid_session = _is_exchange_open_at(exchange, now_ist)
        last_price = r.get("last_price")
        day_change = r.get("day_change")
        # Holdings are eq-only (no MCX), so this branch matters only on
        # mid-NSE-session snapshots — e.g. an operator triggering a manual
        # snapshot via /admin during market hours. Mid-session ltp/day_pnl
        # would feed downstream P&L summation as a partial-day value
        # masquerading as EOD, so emit None and let the next EOD pass
        # (15:35 IST default) fill them.
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      kite_seg_from_exchange(exchange),
            "kind":         "holdings",
            "symbol":       symbol,
            "exchange":     exchange,
            "qty":          int(r.get("opening_quantity") or r.get("quantity") or 0),
            "avg_cost":     float(r["average_price"]) if r.get("average_price") is not None else None,
            "ltp":          None if mid_session else (float(last_price) if last_price is not None else None),
            "day_pnl":      None if mid_session else (float(day_change) if day_change is not None else None),
            "total_pnl":    float(r["pnl"]) if r.get("pnl") is not None else None,
            "payload_json": json.dumps(r, default=str),
        })
    return rows


def _positions_rows(
    account: str, target_date: date, raw: list[dict], now_ist: datetime,
) -> list[dict]:
    rows = []
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NFO")
        last_price  = r.get("last_price")
        close_price = r.get("close_price")
        qty         = r.get("quantity") or 0
        mid_session = _is_exchange_open_at(exchange, now_ist)
        # (last - close) × qty is the move from yesterday's EOD to now.
        # Captured AT EOD (after the exchange closes) this is the correct
        # day_pnl. Captured MID-SESSION it's a partial-day value — and
        # positions.py's close-override consumes daily_book.ltp as
        # "yesterday's close" the next session, which would silently
        # displace the real prior-session EOD by hours. Skip both fields
        # when the row's exchange is mid-session; rely on the 23:35 IST
        # follow-up pass (added in BE) to capture MCX EOD post-close.
        if mid_session:
            day_pnl = None
            ltp_val = None
        else:
            day_pnl = None
            if last_price is not None and close_price is not None:
                day_pnl = float((last_price - close_price) * qty)
            ltp_val = float(last_price) if last_price is not None else None
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      kite_seg_from_exchange(exchange),
            "kind":         "positions",
            "symbol":       symbol,
            "exchange":     exchange,
            "qty":          int(qty),
            "avg_cost":     float(r["average_price"]) if r.get("average_price") is not None else None,
            "ltp":          ltp_val,
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


def _funds_rows(account: str, target_date: date, raw: list[dict]) -> list[dict]:
    """Per-segment funds snapshot rows. One row per (account, segment).
    Goes into daily_book with kind='funds', symbol='__seg__' (sentinel
    since the table's unique constraint requires a symbol), exchange =
    segment_label uppercased.

    The amount columns map to:
        qty       — utilised.debits (total cash spent today; integer ₹)
        avg_cost  — available.cash  (free cash, withdrawable)
        ltp       — available.opening_balance  (start-of-day cash)
        day_pnl   — utilised.realised_m2m  (today's realised P&L)
        total_pnl — net  (segment net worth)
    """
    rows = []
    for r in raw:
        seg_label = (r.get("segment_label") or "equity").lower()
        avail = r.get("available") or {}
        util  = r.get("utilised")  or {}
        rows.append({
            "date":         target_date,
            "account":      account,
            "segment":      seg_label,
            "kind":         "funds",
            "symbol":       "__seg__",
            "exchange":     seg_label.upper(),
            "qty":          int(float(util.get("debits") or 0)),
            "avg_cost":     (float(avail.get("cash"))
                             if avail.get("cash") is not None else None),
            "ltp":          (float(avail.get("opening_balance"))
                             if avail.get("opening_balance") is not None else None),
            "day_pnl":      (float(util.get("realised_m2m"))
                             if util.get("realised_m2m") is not None else None),
            "total_pnl":    (float(r["net"])
                             if r.get("net") is not None else None),
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
    from backend.brokers.connections import Connections
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

    now_ist = timestamp_indian()
    if target_date is None:
        target_date = now_ist.date()  # type: ignore[attr-defined]

    connections = _get_connections()
    accounts = list(connections.conn.keys())
    if not accounts:
        # Cutover branch — Connections is empty when conn_service owns
        # sessions; pull the canonical account list from there.
        from backend.brokers.client import is_cutover_on
        if is_cutover_on():
            from backend.brokers.client.remote_broker import list_remote_accounts
            accounts = [r["account"] for r in list_remote_accounts() if r.get("account")]
    if not accounts:
        logger.warning("Snapshot: no loaded broker accounts — nothing to capture")
        return {"accounts": [], "holdings_rows": 0, "positions_rows": 0,
                "trades_rows": 0, "errors": ["No loaded broker accounts"]}

    loop = asyncio.get_running_loop()
    totals = {"holdings_rows": 0, "positions_rows": 0, "trades_rows": 0,
              "funds_rows": 0}
    errors: list[str] = []
    processed: list[str] = []

    from backend.brokers.registry import all_brokers
    for broker in all_brokers():
        account = broker.account
        try:
            raw = await loop.run_in_executor(
                _local_executor, _fetch_account_data, broker, account, target_date
            )

            h_rows = _holdings_rows(account,  target_date, raw["holdings"],  now_ist)
            p_rows = _positions_rows(account, target_date, raw["positions"], now_ist)
            t_rows = _trades_rows(account,    target_date, raw["trades"])
            f_rows = _funds_rows(account,     target_date, raw["funds"])

            totals["holdings_rows"]  += await _upsert_rows(h_rows)
            totals["positions_rows"] += await _upsert_rows(p_rows)
            totals["trades_rows"]    += await _upsert_rows(t_rows)
            totals["funds_rows"]     += await _upsert_rows(f_rows)
            processed.append(account)

            logger.info(
                f"Snapshot [{account}] date={target_date} "
                f"holdings={len(h_rows)} positions={len(p_rows)} "
                f"trades={len(t_rows)} funds={len(f_rows)}"
            )
        except Exception as e:
            msg = f"Snapshot [{account}] failed: {e}"
            logger.error(msg)
            errors.append(msg)

    return {
        "accounts":       processed,
        "holdings_rows":  totals["holdings_rows"],
        "positions_rows": totals["positions_rows"],
        "trades_rows":    totals["trades_rows"],
        "funds_rows":     totals["funds_rows"],
        "errors":         errors,
    }
