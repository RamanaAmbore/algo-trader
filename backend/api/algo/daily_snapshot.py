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
from datetime import date, datetime, timedelta, time as _dt_time, timezone
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


def _extract_snapshot_extras(r: dict, ltp_val: float | None,
                             settled: bool) -> dict:
    """Extract OHLC + volume + OI + day-change fields from a broker row
    into a stable, JSONB-serialisable dict. Attached to `payload_json` as
    a nested `snapshot_extras` block. Consumers (positions.py / holdings.py
    close-override, movers page, sparkline cache readers) read from this
    block when `daily_book.payload_json` is available.

    Fields:
      • open, high, low        — day OHLC (`ohlc` sub-dict on Kite payload)
      • close_settled          — Kite adjusted close (weighted avg last 30 min)
      • prev_close             — prior session close (used by frontend
                                 delta-to-prev display)
      • volume                 — day volume (int)
      • oi                     — open interest (F&O; None on equity)
      • day_change_val         — Kite `day_change` (rupees delta)
      • day_change_pct         — Kite `day_change_percentage`
      • ltp                    — last traded price (mirrored here for
                                 downstream readers that only load the
                                 payload without walking to `daily_book.ltp`)
      • settled                — True when produced by close_settled path;
                                 False for close (or unsettled) capture.

    None-safe — every field is optional. A row builder that doesn't have
    a value just leaves it None. Downstream readers must tolerate absence.
    """
    ohlc = r.get("ohlc") or {}

    def _f(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    return {
        "open":            _f(ohlc.get("open")),
        "high":            _f(ohlc.get("high")),
        "low":             _f(ohlc.get("low")),
        "close_settled":   _f(ohlc.get("close")) if settled else None,
        "prev_close":      _f(r.get("close_price")),
        "volume":          (int(r.get("volume")) if r.get("volume") is not None else None),
        "oi":              (int(r.get("oi"))     if r.get("oi")     is not None else None),
        "day_change_val":  _f(r.get("day_change")),
        "day_change_pct":  _f(r.get("day_change_percentage")),
        "ltp":             _f(ltp_val),
        "settled":         bool(settled),
    }


def _row_payload_with_extras(r: dict, ltp_val: float | None,
                             settled: bool) -> str:
    """Build the `payload_json` string with an embedded `snapshot_extras`
    block. Returns a JSON-encoded str. Ensures broker's raw row is still
    the top-level object (backwards compatible with any existing reader
    that json-loads payload_json and expects Kite-shape keys)."""
    body = dict(r)
    try:
        body["snapshot_extras"] = _extract_snapshot_extras(r, ltp_val, settled)
    except Exception:
        pass  # never let payload enrichment fail the snapshot
    return json.dumps(body, default=str)

# Reuse background.py's executor when called from there; create a local one
# for the admin endpoint path.
_local_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ramboq-snap")


# ---------------------------------------------------------------------------
# Safe fetch helper
# ---------------------------------------------------------------------------

from typing import Any as _Any, Callable as _Callable


def _safe_fetch(label: str, fn: _Callable[..., _Any], *args: _Any, default: _Any = None) -> _Any:
    """Call *fn* with *args*, log a warning and return *default* on any exception.

    Used for per-kind broker fetches in ``_fetch_account_data`` where a failure
    in one kind must not abort the others. The ``label`` is embedded in the
    warning message so the operator can grep by kind (e.g. "holdings", "trades").
    """
    try:
        return fn(*args)
    except Exception as e:
        logger.warning("Snapshot %s fetch failed: %s", label, e)
        return default if default is not None else []


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

def _backfill_market_data_dicts(rows: list[dict], *, qty_col: str = "opening_quantity") -> int:
    """Groww / Dhan often ship holdings + positions with `last_price=0`
    and `close_price=0` when their own market-data cache is cold. The
    live routes (`/api/holdings`, `/api/positions`) run
    `broker_apis.backfill_market_data(df)` after `pd.concat` so Kite
    quote() patches the missing fields before the P&L math runs.

    The snapshot writer used to skip that step, so Groww holdings +
    positions rows landed at the `_is_zero_payload_row` guard (avg_cost
    > 0 AND ltp=0 AND day_pnl=0 AND total_pnl=0 — a token-failure
    fingerprint) and were dropped from `daily_book`. Public
    `/performance` reads from the snapshot during closed hours and
    therefore lost the entire Groww breakdown (GR87DF row absent from
    Holdings grid + NAV grid).

    This helper mirrors `backfill_market_data` for a list-of-dicts
    payload. Builds a temporary pandas DataFrame, delegates to the
    canonical patcher (same PriceBroker.quote fan-out, same last-good
    fallback), then writes `last_price` + `close_price` back onto each
    dict by positional index. In-place mutation matches the live route
    convention.

    Returns the number of rows patched (informational; the caller
    doesn't need to react).
    """
    if not rows:
        return 0
    try:
        import pandas as _pd
        from backend.brokers.broker_apis import backfill_market_data as _bf
    except Exception:
        return 0
    df = _pd.DataFrame(rows)
    if df.empty:
        return 0
    # Ensure the columns backfill inspects exist — Groww holdings ships
    # both `last_price` + `close_price`; Groww positions the same. If
    # a broker skipped a column entirely, fill with zeros so the
    # missing-value gate ( <= 0 ) still fires.
    for _col in ("last_price", "close_price", "tradingsymbol", "exchange"):
        if _col not in df.columns:
            df[_col] = 0 if _col in ("last_price", "close_price") else ""
    # backfill expects an `opening_quantity` column for the day_change
    # recompute; positions ship `quantity` instead. Provide it as an
    # alias so the recompute succeeds — no impact on our dicts (we only
    # copy `last_price` / `close_price` back).
    if qty_col == "quantity" and "opening_quantity" not in df.columns:
        df["opening_quantity"] = df["quantity"] if "quantity" in df.columns else 0

    try:
        _bf(df)
    except Exception as e:
        logger.debug(f"snapshot backfill_market_data failed: {e}")
        return 0

    patched = 0
    # Write the patched fields back onto the original dicts. iloc keeps
    # us in sync with pandas' row order (which mirrors input list order
    # from DataFrame constructor).
    for i, r in enumerate(rows):
        try:
            _new_ltp = float(df.iloc[i]["last_price"] or 0)
            _new_cls = float(df.iloc[i]["close_price"] or 0)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        _old_ltp = float(r.get("last_price") or 0)
        _old_cls = float(r.get("close_price") or 0)
        if _new_ltp > 0 and _new_ltp != _old_ltp:
            r["last_price"] = _new_ltp
            patched += 1
        if _new_cls > 0 and _new_cls != _old_cls:
            r["close_price"] = _new_cls
        # Recompute day_change / pnl on Groww-shape rows where the
        # normaliser derived them from (ltp - close) but both were
        # zero at that moment. After backfill lands both numbers,
        # rewrite so the writer's `_is_zero_payload_row` doesn't
        # filter the row on a stale zero.
        try:
            _avg = float(r.get("average_price") or 0)
            # For holdings the qty column is `opening_quantity`; for
            # positions it's `quantity`. Prefer the caller-specified
            # column, fall back to whichever is present.
            _qty = int(r.get(qty_col) or r.get("quantity")
                        or r.get("opening_quantity") or 0)
        except (ValueError, TypeError):
            _avg = 0.0
            _qty = 0
        _cur_ltp = float(r.get("last_price") or 0)
        _cur_cls = float(r.get("close_price") or 0)
        if _cur_ltp > 0 and _avg > 0 and _qty and not r.get("pnl"):
            r["pnl"] = (_cur_ltp - _avg) * _qty
        if _cur_ltp > 0 and _cur_cls > 0 and not r.get("day_change"):
            r["day_change"] = _cur_ltp - _cur_cls
    return patched


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

    out["holdings"] = _safe_fetch(f"[{account}] holdings", broker.holdings) or []

    try:
        raw_pos = broker.positions() or {}
        out["positions"] = raw_pos.get("net", [])
    except Exception as e:
        logger.warning(f"Snapshot [{account}] positions fetch failed: {e}")

    # Market-data backfill — critical for Groww + Dhan where holdings /
    # positions rows ship with `last_price=0` + `close_price=0` when
    # the broker's own market-data cache is cold. Without this, the
    # `_is_zero_payload_row` guard downstream drops every Groww row
    # (avg_cost > 0 AND ltp=0 AND pnl=0 = token-failure fingerprint),
    # which manifests operator-visibly as "Groww account breakdown
    # missing from public /performance page". Mirrors the live routes
    # (`/api/holdings`, `/api/positions`) which run the same backfill
    # right after `pd.concat`.
    try:
        n_h = _backfill_market_data_dicts(out["holdings"], qty_col="opening_quantity")
        n_p = _backfill_market_data_dicts(out["positions"], qty_col="quantity")
        if n_h or n_p:
            logger.info(
                f"Snapshot [{account}] backfilled market data — "
                f"holdings={n_h} positions={n_p} rows patched"
            )
    except Exception as e:
        logger.warning(f"Snapshot [{account}] backfill failed (non-fatal): {e}")

    if is_today:
        out["trades"] = _safe_fetch(f"[{account}] trades", broker.trades) or []
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

def _is_zero_payload_row(row: dict, ltp: Optional[float], day_pnl: Optional[float], total_pnl: Optional[float]) -> bool:
    """Return True when a broker row looks like a bad/zeroed payload.

    The heuristic: the symbol has a real avg_cost (it exists in the portfolio)
    but the broker returned ltp=0, day_pnl=0, and total_pnl=0 all at once.
    This pattern only occurs on auth failures (invalid token) or upstream outages
    — never on a legitimately flat position. We skip such rows to avoid
    overwriting a previously-good snapshot with a zeroed-out one.
    """
    avg_cost = row.get("average_price")
    avg_cost_f = float(avg_cost) if avg_cost is not None else 0.0
    if avg_cost_f <= 0:
        return False  # no cost basis — could genuinely be zero
    ltp_f      = ltp      if ltp      is not None else 0.0
    day_pnl_f  = day_pnl  if day_pnl  is not None else 0.0
    total_pnl_f = total_pnl if total_pnl is not None else 0.0
    return ltp_f == 0.0 and day_pnl_f == 0.0 and total_pnl_f == 0.0


def _holdings_rows(
    account: str, target_date: date, raw: list[dict], now_ist: datetime,
    *, settled: bool = False,
) -> list[dict]:
    rows = []
    skipped = 0
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NSE")
        mid_session = _is_exchange_open_at(exchange, now_ist)
        last_price = r.get("last_price")
        day_change = r.get("day_change")
        total_pnl_raw = r.get("pnl")

        ltp_val   = None if mid_session else (float(last_price) if last_price is not None else None)
        day_pnl_v = None if mid_session else (float(day_change) if day_change is not None else None)
        total_pnl_v = float(total_pnl_raw) if total_pnl_raw is not None else None

        # Bad-payload guard: broker returned all zeros for a real holding.
        # This is the fingerprint of an invalid/expired token (e.g. ZG0790
        # auth failure). Skip the row — the existing snapshot (if any) is
        # preserved untouched, which is far better than overwriting it with zeros.
        if not mid_session and _is_zero_payload_row(r, ltp_val, day_pnl_v, total_pnl_v):
            skipped += 1
            continue

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
            "ltp":          ltp_val,
            "day_pnl":      day_pnl_v,
            "total_pnl":    total_pnl_v,
            "payload_json": _row_payload_with_extras(r, ltp_val, settled),
        })
    if skipped:
        logger.warning(
            f"Snapshot [{account}] holdings: skipped {skipped}/{skipped + len(rows)} rows "
            f"with ltp=0/day_pnl=0/total_pnl=0 (likely invalid token — prior snapshot preserved)"
        )
    return rows


def _positions_rows(
    account: str, target_date: date, raw: list[dict], now_ist: datetime,
    *, settled: bool = False,
) -> list[dict]:
    from backend.api.algo.pnl_math import decomposed_intraday_pnl, naive_day_pnl

    # Fields the decomposed formula needs — all returned by Kite /positions;
    # present in Dhan + Groww adapters too (see broker_apis._enrich_positions).
    _INTRADAY_FIELDS = {
        "overnight_quantity", "day_buy_quantity", "day_sell_quantity",
        "day_buy_value", "day_sell_value",
    }

    rows = []
    skipped = 0
    for r in raw:
        symbol = r.get("tradingsymbol", "")
        if not symbol:
            continue
        exchange = r.get("exchange", "NFO")
        last_price  = r.get("last_price")
        close_price = r.get("close_price")
        qty         = r.get("quantity") or 0
        mid_session = _is_exchange_open_at(exchange, now_ist)
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
            ltp_val = float(last_price) if last_price is not None else None
            day_pnl = None
            if ltp_val is not None and close_price is not None:
                # Use the decomposed intraday formula when all intraday
                # split fields are present in the broker row (Kite returns
                # them on /positions; Dhan + Groww adapters synthesise them).
                # This formula captures the realised leg on partially-closed
                # positions (e.g. overnight 10, sold 4 today, current 6):
                #
                #   day_pnl = overnight_qty × (LTP − close)   # carried carry
                #           + day_buy_qty   × LTP − day_buy_value   # opened
                #           + day_sell_value − day_sell_qty × LTP   # realised
                #
                # Naive fallback (LTP − close) × qty is used for brokers that
                # don't populate the split fields — the result collapses
                # correctly when oq == qty and no intraday trades occurred.
                if _INTRADAY_FIELDS.issubset(r.keys()):
                    try:
                        day_pnl = float(decomposed_intraday_pnl(
                            oq=float(r.get("overnight_quantity") or 0),
                            ltp=ltp_val,
                            cls=float(close_price),
                            bq=float(r.get("day_buy_quantity")  or 0),
                            bv=float(r.get("day_buy_value")     or 0),
                            sv=float(r.get("day_sell_value")    or 0),
                            sq=float(r.get("day_sell_quantity") or 0),
                        ))
                    except Exception:
                        day_pnl = float(naive_day_pnl(ltp_val, float(close_price), float(qty)))
                else:
                    day_pnl = float(naive_day_pnl(ltp_val, float(close_price), float(qty)))

            # Bad-payload guard: broker returned all zeros for a real position.
            # Same token-failure fingerprint as holdings — ltp=0, day_pnl=0,
            # total_pnl=0 when avg_cost > 0. Skip to preserve the prior snapshot.
            total_pnl_raw = r.get("pnl")
            total_pnl_v   = float(total_pnl_raw) if total_pnl_raw is not None else None
            if _is_zero_payload_row(r, ltp_val, day_pnl, total_pnl_v):
                skipped += 1
                continue

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
            "payload_json": _row_payload_with_extras(r, ltp_val, settled),
        })
    if skipped:
        logger.warning(
            f"Snapshot [{account}] positions: skipped {skipped}/{skipped + len(rows)} rows "
            f"with ltp=0/day_pnl=0/total_pnl=0 (likely invalid token — prior snapshot preserved)"
        )
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


async def snapshot_daily_book(target_date: Optional[date] = None,
                              *, settled: bool = False) -> dict:
    """
    Capture every loaded account's holdings + positions + trades for
    target_date (defaults to today IST). Upserts into daily_book.

    ``settled=True`` flags the row payload's ``snapshot_extras.settled``
    field, which downstream readers (positions.py / holdings.py) use to
    prefer this row's `ltp` as the authoritative close_price. Set by
    ``market_lifecycle_handlers._handle_close_settled`` when the
    ``<exch>:close_settled`` event fires ~15 min after close and the
    broker has published its adjusted (weighted-avg-last-30-min) close.

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

            h_rows = _holdings_rows(account,  target_date, raw["holdings"],  now_ist, settled=settled)
            p_rows = _positions_rows(account, target_date, raw["positions"], now_ist, settled=settled)
            t_rows = _trades_rows(account,    target_date, raw["trades"])
            f_rows = _funds_rows(account,     target_date, raw["funds"])

            # Account-level bad-payload guard: if the broker returned non-empty
            # holdings/positions but every row was filtered out (all zeros),
            # don't upsert the empty set — the prior snapshot is automatically
            # preserved because _upsert_rows([]) is a no-op. Emit a clear warning
            # so operators know the snapshot was skipped rather than confused by
            # silent zero totals.
            raw_h_count = len(raw["holdings"])
            raw_p_count = len(raw["positions"])
            all_filtered = (
                raw_h_count > 0 and len(h_rows) == 0 and
                raw_p_count > 0 and len(p_rows) == 0
            )
            if all_filtered:
                logger.warning(
                    f"Snapshot [{account}] date={target_date} — ALL "
                    f"{raw_h_count} holdings + {raw_p_count} positions rows "
                    f"filtered (bad payload / invalid token). "
                    f"Prior snapshot preserved. No upsert performed."
                )
                processed.append(account)
                continue

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


# ---------------------------------------------------------------------------
# Sparkline snapshot — per-symbol closing-bar series for closed-hours reads
# ---------------------------------------------------------------------------

async def snapshot_sparkline(*, settled: bool = False) -> dict:
    """Persist the last-N-day close-bar series for the sparkline universe
    into ``daily_book`` with ``kind='sparkline'``, one row per (account,
    symbol) using ``account='__firm__'`` as the market-wide sentinel.

    Payload is a JSON blob ``{"points": [{"t": "YYYY-MM-DD", "ltp": <float>},
    ...], "settled": <bool>, "captured_at": <iso>}``. Ordered oldest → newest.

    Trigger points:
      • ``<exch>:close`` → first-cut sparkline captured (settled=False).
      • ``<exch>:close_settled`` → final closing bar appended (settled=True).

    Frontend cell renderer reads this row when ``is_animating === false``
    (post-close, no live ticks) and draws the sparkline from `points`
    without touching the live SSE stream.

    Universe: read from ``watchlists`` + ``holdings`` + ``positions``
    tables via ``_sparkline_universe_symbols`` — matches the operator
    book (watched + owned) rather than the market-wide mover list.
    Cap 500 symbols so a single UPSERT stays under a few MB.
    """
    now_ist = timestamp_indian()
    target_date = now_ist.date()  # type: ignore[attr-defined]

    try:
        universe = await _sparkline_universe_symbols(cap=500)
    except Exception as e:
        logger.warning(f"snapshot_sparkline: universe fetch failed: {e}")
        return {"symbols": 0, "errors": [str(e)]}

    if not universe:
        return {"symbols": 0, "errors": []}

    # ── Resolve virtual MCX/CDS bare roots to front-month contracts ──────────
    # ohlcv_store keys data to the actual contract (e.g. CRUDEOIL26JULFUT),
    # not the virtual root (CRUDEOIL). Without this step symbols like
    # CRUDEOIL from old position snapshots return bars=None → skipped silently.
    try:
        from backend.api.algo.symbol_resolver import resolve_virtual_roots
        universe = await resolve_virtual_roots(universe)
    except Exception as e:
        logger.warning(f"snapshot_sparkline: virtual-root resolution failed: {e}")

    # Read close-bar series from ohlcv_store for each symbol (5-day tail).
    # DB reads are cheap and fire concurrently. Broker fallback is gated
    # by a semaphore (cap=3) so we don't hammer the broker on a cold cache.
    from backend.api.persistence import ohlcv_store as _oh
    _broker_sem = asyncio.Semaphore(3)
    from_d = target_date - timedelta(days=10)

    async def _fetch_bars(sym: str, exch: str) -> tuple[str, str, list]:
        try:
            bars = await _oh.get_or_fetch_daily(
                sym, exch,
                from_d=from_d,
                to_d=target_date,
                db_only=True,
            )
            if not bars:
                # DB miss — fetch from broker (persists result back to DB
                # so the next run hits the DB path).
                async with _broker_sem:
                    bars = await _oh.get_or_fetch_daily(
                        sym, exch,
                        from_d=from_d,
                        to_d=target_date,
                        db_only=False,
                    )
        except Exception:
            bars = None
        return sym, exch, bars or []

    results = await asyncio.gather(*[_fetch_bars(sym, exch) for (sym, exch) in universe])

    rows: list[dict] = []
    for sym, exch, bars in results:
        if not bars:
            continue
        points = []
        for b in bars:
            try:
                d = b.get("date") if isinstance(b, dict) else None
                c = b.get("close") if isinstance(b, dict) else None
                if d is None or c is None:
                    continue
                points.append({"t": str(d), "ltp": float(c)})
            except Exception:
                continue
        if not points:
            continue
        rows.append({
            "date":         target_date,
            "account":      "__firm__",
            "segment":      "equity" if exch in ("NSE", "BSE") else "derivatives",
            "kind":         "sparkline",
            "symbol":       sym,
            "exchange":     exch,
            "qty":          0,
            "avg_cost":     None,
            "ltp":          points[-1]["ltp"] if points else None,
            "day_pnl":      None,
            "total_pnl":    None,
            "payload_json": json.dumps({
                "points":      points,
                "settled":     bool(settled),
                "captured_at": now_ist.isoformat(),
            }, default=str),
        })

    if not rows:
        return {"symbols": 0, "errors": []}

    written = await _upsert_rows(rows)
    return {"symbols": written, "errors": []}


async def _sparkline_universe_symbols(cap: int = 500) -> list[tuple[str, str]]:
    """Return de-duplicated list of (tradingsymbol, exchange) for the
    sparkline snapshot. Reads from watchlists + open holdings + open
    positions so the persisted sparkline covers the operator book.

    Cheap query — one round-trip per source table with SELECT DISTINCT.
    """
    from sqlalchemy import select as sql_select, distinct
    seen: set[tuple[str, str]] = set()

    try:
        from backend.api.models import WatchlistItem
        async with async_session() as s:
            rows = (await s.execute(
                sql_select(
                    WatchlistItem.tradingsymbol,
                    WatchlistItem.exchange,
                ).where(WatchlistItem.tradingsymbol.isnot(None))
                .distinct()
            )).all()
            for (sym, exch) in rows:
                if sym:
                    seen.add((str(sym).upper(), (str(exch).upper() if exch else "NSE")))
    except Exception:
        pass

    # Holdings + positions — join through daily_book most-recent-row lookup
    # so we don't require live broker.
    try:
        async with async_session() as s:
            rows = (await s.execute(text("""
                SELECT DISTINCT symbol, exchange
                FROM daily_book
                WHERE kind IN ('positions', 'holdings')
                  AND date = (SELECT MAX(date) FROM daily_book WHERE kind IN ('positions','holdings'))
            """))).all()
            for (sym, exch) in rows:
                if sym and exch:
                    seen.add((str(sym).upper(), str(exch).upper()))
    except Exception:
        pass

    out = sorted(seen)[:cap]
    return out
