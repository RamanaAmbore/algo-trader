"""
DB worker — drains db_queue and batches rows into PostgreSQL.

Dispatches on payload["kind"]:
  "ohlcv_daily"          — batched INSERT ... ON CONFLICT DO NOTHING
  "instruments_snapshot" — last-write-wins upsert per (exchange, date)
  "holidays_snapshot"    — last-write-wins upsert per (exchange, year)
  "intraday_bars"        — batched INSERT ... ON CONFLICT DO NOTHING per bar

Batches by size (500 items) or timeout (500 ms), whichever fires first.
Duplicate primary-key entries within a batch are coalesced last-write-wins
before the INSERT so the DB sees at most one row per PK.

A failed batch is dropped; the next read re-fetches from the broker.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_BATCH_ROWS = 500
_FLUSH_INTERVAL = 0.5   # seconds

_KNOWN_KINDS = ("ohlcv_daily", "instruments_snapshot", "holidays_snapshot", "intraday_bars")


async def run() -> None:
    """Supervisor — wraps the actual loop so an unhandled exception
    doesn't kill the worker silently. CancelledError is propagated so
    lifespan stop() can shut us down cleanly; everything else is logged
    and the loop restarts after a 1s back-off."""
    while True:
        try:
            await _run_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("db_worker: loop crashed; restarting in 1s")
            await asyncio.sleep(1.0)


async def _run_loop() -> None:
    from backend.api.persistence.write_queue import db_queue, _record_db_flush

    while True:
        batch: list[dict] = []
        deadline = asyncio.get_running_loop().time() + _FLUSH_INTERVAL

        while len(batch) < _BATCH_ROWS:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(db_queue.get(), timeout=remaining)
                batch.append(item)
                db_queue.task_done()
            except asyncio.TimeoutError:
                break

        if not batch:
            continue

        by_kind = _coalesce(batch)

        try:
            await _upsert(by_kind)
            total = sum(len(v) for v in by_kind.values())
            _record_db_flush(total)
        except Exception as exc:
            logger.warning(
                f"db_worker: batch upsert failed (batch dropped): {exc}"
            )


# ── Coalesce ──────────────────────────────────────────────────────────────────

def _coalesce(batch: list[dict]) -> dict[str, list[dict[str, Any]]]:
    """Split batch by kind and apply per-kind deduplication (last-write-wins)."""
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in _KNOWN_KINDS}
    for payload in batch:
        kind = payload.get("kind")
        if kind in by_kind:
            by_kind[kind].append(payload)
    return {
        "ohlcv_daily":          _coalesce_ohlcv(by_kind["ohlcv_daily"]),
        "instruments_snapshot": _coalesce_instruments(by_kind["instruments_snapshot"]),
        "holidays_snapshot":    _coalesce_holidays(by_kind["holidays_snapshot"]),
        "intraday_bars":        _coalesce_intraday(by_kind["intraday_bars"]),
    }


def _coalesce_ohlcv(payloads: list[dict]) -> list[dict[str, Any]]:
    """Merge into unique (symbol, exchange, date) rows, last-write-wins.

    `bar['date']` arrives as a "YYYY-MM-DD" string (set by
    ohlcv_store._materialise_bars). asyncpg now binds the date column
    strictly — it wants a `datetime.date`, not a string. Convert here
    so every row hits _upsert_ohlcv with the right type. Strings that
    fail to parse are dropped (better than a whole batch failing for
    one malformed row)."""
    from datetime import date as _date
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for payload in payloads:
        sym  = str(payload.get("symbol", ""))
        exch = str(payload.get("exchange", ""))
        if not sym or not exch:
            continue
        for bar in payload.get("bars", []):
            d_raw = bar.get("date", "")
            if not d_raw:
                continue
            if isinstance(d_raw, _date):
                d_obj = d_raw
                d_key = d_obj.isoformat()
            else:
                d_str = str(d_raw)[:10]
                try:
                    d_obj = _date.fromisoformat(d_str)
                except ValueError:
                    continue  # unparseable — drop row, keep batch
                d_key = d_str
            seen[(sym, exch, d_key)] = {
                "symbol":   sym,
                "exchange": exch,
                "date":     d_obj,
                "open":     bar.get("open",   0.0),
                "high":     bar.get("high",   0.0),
                "low":      bar.get("low",    0.0),
                "close":    bar.get("close",  0.0),
                "volume":   int(bar.get("volume", 0)),
            }
    return list(seen.values())


def _coalesce_instruments(payloads: list[dict]) -> list[dict[str, Any]]:
    """One row per (exchange, date), last-write-wins."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in payloads:
        exch = str(payload.get("exchange", ""))
        date = str(payload.get("date", ""))
        if not exch or not date:
            continue
        seen[(exch, date)] = {
            "exchange":  exch,
            "date":      date,
            "payload":   payload.get("payload", []),
            "row_count": int(payload.get("row_count", 0)),
        }
    return list(seen.values())


def _coalesce_holidays(payloads: list[dict]) -> list[dict[str, Any]]:
    """One row per (exchange, year), last-write-wins."""
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for payload in payloads:
        exch = str(payload.get("exchange", ""))
        year = payload.get("year")
        if not exch or year is None:
            continue
        seen[(exch, int(year))] = {
            "exchange": exch,
            "year":     int(year),
            "dates":    payload.get("dates", []),
        }
    return list(seen.values())


def _coalesce_intraday(payloads: list[dict]) -> list[dict[str, Any]]:
    """Merge into unique (symbol, exchange, date, interval, bar_ts) rows, last-write-wins."""
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for payload in payloads:
        sym      = str(payload.get("symbol",   ""))
        exch     = str(payload.get("exchange", ""))
        date_str = str(payload.get("date",     ""))
        interval = str(payload.get("interval", "30minute"))
        if not sym or not exch or not date_str:
            continue
        for bar in payload.get("bars", []):
            bar_ts = str(bar.get("bar_ts", ""))
            if not bar_ts:
                continue
            pk = (sym, exch, date_str, interval, bar_ts)
            seen[pk] = {
                "symbol":   sym,
                "exchange": exch,
                "date":     date_str,
                "interval": interval,
                "bar_ts":   bar_ts,
                "open":     bar.get("open",   0.0),
                "high":     bar.get("high",   0.0),
                "low":      bar.get("low",    0.0),
                "close":    bar.get("close",  0.0),
                "volume":   int(bar.get("volume", 0)),
            }
    return list(seen.values())


# ── Upsert ────────────────────────────────────────────────────────────────────

async def _upsert(by_kind: dict[str, list[dict[str, Any]]]) -> None:
    if by_kind["ohlcv_daily"]:
        await _upsert_ohlcv(by_kind["ohlcv_daily"])
    if by_kind["instruments_snapshot"]:
        await _upsert_instruments(by_kind["instruments_snapshot"])
    if by_kind["holidays_snapshot"]:
        await _upsert_holidays(by_kind["holidays_snapshot"])
    if by_kind.get("intraday_bars"):
        await _upsert_intraday(by_kind["intraday_bars"])


async def _upsert_ohlcv(rows: list[dict[str, Any]]) -> None:
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        INSERT INTO ohlcv_daily
            (symbol, exchange, date, open, high, low, close, volume)
        VALUES
            (:symbol, :exchange, :date, :open, :high, :low, :close, :volume)
        ON CONFLICT (symbol, exchange, date) DO NOTHING
    """)
    async with async_session() as session:
        await session.execute(stmt, rows)
        await session.commit()


async def _upsert_instruments(rows: list[dict[str, Any]]) -> None:
    """DO UPDATE so intra-day additions (new weekly options) are captured.

    payload is passed as a JSON string so SQLAlchemy/asyncpg binds it
    correctly as JSONB without needing psycopg2 Json wrappers.
    """
    import json
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        INSERT INTO instruments_snapshot
            (exchange, date, payload, row_count, captured_at)
        VALUES
            (:exchange, :date, :payload::jsonb, :row_count, now())
        ON CONFLICT (exchange, date)
        DO UPDATE SET
            payload     = EXCLUDED.payload,
            row_count   = EXCLUDED.row_count,
            captured_at = now()
    """)
    bound_rows = [
        {
            "exchange":  r["exchange"],
            "date":      r["date"],
            "payload":   json.dumps(r["payload"]),
            "row_count": r["row_count"],
        }
        for r in rows
    ]
    async with async_session() as session:
        await session.execute(stmt, bound_rows)
        await session.commit()


async def _upsert_intraday(rows: list[dict[str, Any]]) -> None:
    """DO NOTHING on conflict — intraday bars are immutable once timestamped."""
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        INSERT INTO intraday_bars
            (symbol, exchange, date, interval, bar_ts,
             open, high, low, close, volume)
        VALUES
            (:symbol, :exchange, :date, :interval, :bar_ts::timestamptz,
             :open, :high, :low, :close, :volume)
        ON CONFLICT (symbol, exchange, date, interval, bar_ts) DO NOTHING
    """)
    async with async_session() as session:
        await session.execute(stmt, rows)
        await session.commit()


async def _upsert_holidays(rows: list[dict[str, Any]]) -> None:
    """DO UPDATE — holiday lists may be amended mid-year by NSE announcements."""
    import json
    from sqlalchemy import text
    from backend.api.database import async_session

    stmt = text("""
        INSERT INTO holidays_snapshot
            (exchange, year, dates_json, captured_at)
        VALUES
            (:exchange, :year, :dates_json::jsonb, now())
        ON CONFLICT (exchange, year)
        DO UPDATE SET
            dates_json  = EXCLUDED.dates_json,
            captured_at = now()
    """)
    bound_rows = [
        {
            "exchange":   r["exchange"],
            "year":       r["year"],
            "dates_json": json.dumps(r["dates"]),
        }
        for r in rows
    ]
    async with async_session() as session:
        await session.execute(stmt, bound_rows)
        await session.commit()
