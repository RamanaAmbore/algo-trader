"""
Cache worker — drains disk_queue and atomically writes OHLCV bars to
.log/ohlcv_cache.json.

Batches by size (200) or timeout (5 s), whichever fires first.
Duplicate (symbol, exchange, date) entries within a batch are coalesced
last-write-wins before the file is touched.

Schema on disk (v3):
{
  "version": 3,
  "saved_at": "<iso>",
  "ohlcv_daily": {
    "<symbol>|<exchange>": {"YYYY-MM-DD": [open, high, low, close, volume]}
  },
  "instruments_snapshot": {
    "<exchange>": {"date": "YYYY-MM-DD", "payload": [...]}
  },
  "holidays_snapshot": {
    "<exchange>": {"<year>": ["YYYY-MM-DD", ...]}
  },
  "intraday_bars": {
    "<symbol>|<exchange>|<date>|<interval>": {
      "<bar_ts_iso>": [open, high, low, close, volume]
    }
  }
}

v1 / v2 files (missing intraday_bars key) are read back-compatibly — the
absent key is treated as an empty dict.

Note: instruments_snapshot payloads can be large (~500 kB × 6 exchanges).
The disk cache is primarily useful for fast restart; the DB tier covers
the durable persistence path. Instruments/holidays are therefore included
for completeness but the DB is the authoritative warm source on restart.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_CACHE_PATH = Path(__file__).resolve().parents[4] / ".log" / "ohlcv_cache.json"
_BATCH_SIZE = 200
_FLUSH_INTERVAL = 5.0   # seconds


async def run() -> None:
    """Supervisor — see db_worker.run for rationale. Self-heals on any
    unhandled exception; only CancelledError shuts the worker down."""
    while True:
        try:
            await _run_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cache_worker: loop crashed; restarting in 1s")
            await asyncio.sleep(1.0)


async def _run_loop() -> None:
    from backend.api.persistence.write_queue import disk_queue, _record_disk_flush

    while True:
        batch: list[dict] = []
        deadline = asyncio.get_running_loop().time() + _FLUSH_INTERVAL

        while len(batch) < _BATCH_SIZE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(disk_queue.get(), timeout=remaining)
                batch.append(item)
                disk_queue.task_done()
            except asyncio.TimeoutError:
                break

        if not batch:
            continue

        try:
            _flush_batch(batch)
            _record_disk_flush(len(batch))
        except Exception as exc:
            logger.warning(f"cache_worker: flush failed: {exc}")


# ── Coalesce helpers ──────────────────────────────────────────────────────────

def _coalesce_ohlcv(batch: list[dict]) -> dict[str, dict[str, list]]:
    """Merge ohlcv_daily payloads into {sym|exch: {date: [o,h,l,c,v]}} last-write-wins."""
    merged: dict[str, dict[str, list]] = {}
    for payload in batch:
        if payload.get("kind") != "ohlcv_daily":
            continue
        sym  = str(payload.get("symbol", ""))
        exch = str(payload.get("exchange", ""))
        if not sym or not exch:
            continue
        key = f"{sym}|{exch}"
        if key not in merged:
            merged[key] = {}
        for bar in payload.get("bars", []):
            d = bar.get("date", "")
            if not d:
                continue
            merged[key][d] = [
                bar.get("open",   0.0),
                bar.get("high",   0.0),
                bar.get("low",    0.0),
                bar.get("close",  0.0),
                bar.get("volume", 0),
            ]
    return merged


def _coalesce_instruments(batch: list[dict]) -> dict[str, dict[str, Any]]:
    """Merge instruments_snapshot payloads into {exchange: {date, payload}} last-write-wins."""
    merged: dict[str, dict[str, Any]] = {}
    for payload in batch:
        if payload.get("kind") != "instruments_snapshot":
            continue
        exch = str(payload.get("exchange", ""))
        date = str(payload.get("date", ""))
        if not exch or not date:
            continue
        merged[exch] = {
            "date":    date,
            "payload": payload.get("payload", []),
        }
    return merged


def _coalesce_holidays(batch: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Merge holidays_snapshot payloads into {exchange: {year_str: [dates]}} last-write-wins."""
    merged: dict[str, dict[str, list[str]]] = {}
    for payload in batch:
        if payload.get("kind") != "holidays_snapshot":
            continue
        exch = str(payload.get("exchange", ""))
        year = payload.get("year")
        if not exch or year is None:
            continue
        if exch not in merged:
            merged[exch] = {}
        merged[exch][str(year)] = list(payload.get("dates", []))
    return merged


def _coalesce_intraday(batch: list[dict]) -> dict[str, dict[str, list]]:
    """Merge intraday_bars payloads into
    {sym|exch|date|interval: {bar_ts_iso: [o,h,l,c,v]}} last-write-wins."""
    merged: dict[str, dict[str, list]] = {}
    for payload in batch:
        if payload.get("kind") != "intraday_bars":
            continue
        sym      = str(payload.get("symbol",   ""))
        exch     = str(payload.get("exchange", ""))
        date_str = str(payload.get("date",     ""))
        interval = str(payload.get("interval", "30minute"))
        if not sym or not exch or not date_str:
            continue
        bucket_key = f"{sym}|{exch}|{date_str}|{interval}"
        if bucket_key not in merged:
            merged[bucket_key] = {}
        for bar in payload.get("bars", []):
            bar_ts = str(bar.get("bar_ts", ""))
            if not bar_ts:
                continue
            merged[bucket_key][bar_ts] = [
                bar.get("open",   0.0),
                bar.get("high",   0.0),
                bar.get("low",    0.0),
                bar.get("close",  0.0),
                int(bar.get("volume", 0)),
            ]
    return merged


def _flush_batch(batch: list[dict]) -> None:
    ohlcv_inc    = _coalesce_ohlcv(batch)
    instru_inc   = _coalesce_instruments(batch)
    holidays_inc = _coalesce_holidays(batch)
    intraday_inc = _coalesce_intraday(batch)

    if not ohlcv_inc and not instru_inc and not holidays_inc and not intraday_inc:
        return

    # Load existing file (support v1, v2, and v3 on disk).
    existing: dict = {}
    if _CACHE_PATH.exists():
        try:
            with open(_CACHE_PATH) as f:
                existing = json.load(f)
        except Exception as exc:
            logger.warning(f"cache_worker: could not read existing cache: {exc}")
            existing = {}

    # ── OHLCV ──
    ohlcv = existing.get("ohlcv_daily") or {}
    for key, dates in ohlcv_inc.items():
        if key not in ohlcv:
            ohlcv[key] = {}
        ohlcv[key].update(dates)

    # ── Instruments (last-write-wins per exchange) ──
    instruments = existing.get("instruments_snapshot") or {}
    instruments.update(instru_inc)

    # ── Holidays (merge per exchange/year, last-write-wins per year) ──
    holidays = existing.get("holidays_snapshot") or {}
    for exch, year_map in holidays_inc.items():
        if exch not in holidays:
            holidays[exch] = {}
        holidays[exch].update(year_map)

    # ── Intraday bars (merge per bucket/bar_ts, last-write-wins) ──
    intraday = existing.get("intraday_bars") or {}
    for bucket_key, bar_map in intraday_inc.items():
        if bucket_key not in intraday:
            intraday[bucket_key] = {}
        intraday[bucket_key].update(bar_map)

    payload: dict[str, Any] = {
        "version":              3,
        "saved_at":             datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ohlcv_daily":          ohlcv,
        "instruments_snapshot": instruments,
        "holidays_snapshot":    holidays,
        "intraday_bars":        intraday,
    }

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".ohlcv.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _CACHE_PATH)
    except Exception as exc:
        logger.warning(f"cache_worker: atomic write failed: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
