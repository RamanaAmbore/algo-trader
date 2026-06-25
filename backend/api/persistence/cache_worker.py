"""
Cache worker — drains disk_queue and atomically writes OHLCV bars to
.log/ohlcv_cache.json.

Batches by size (200) or timeout (5 s), whichever fires first.
Duplicate (symbol, exchange, date) entries within a batch are coalesced
last-write-wins before the file is touched.

Schema on disk (v2):
{
  "version": 2,
  "saved_at": "<iso>",
  "ohlcv_daily": {
    "<symbol>|<exchange>": {"YYYY-MM-DD": [open, high, low, close, volume]}
  },
  "instruments_snapshot": {
    "<exchange>": {"date": "YYYY-MM-DD", "payload": [...]}
  },
  "holidays_snapshot": {
    "<exchange>": {"<year>": ["YYYY-MM-DD", ...]}
  }
}

v1 files (missing instruments_snapshot / holidays_snapshot keys) are read
back-compatibly — the absent keys are treated as empty dicts.

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
        deadline = asyncio.get_event_loop().time() + _FLUSH_INTERVAL

        while len(batch) < _BATCH_SIZE:
            remaining = deadline - asyncio.get_event_loop().time()
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


def _flush_batch(batch: list[dict]) -> None:
    ohlcv_inc     = _coalesce_ohlcv(batch)
    instru_inc    = _coalesce_instruments(batch)
    holidays_inc  = _coalesce_holidays(batch)

    if not ohlcv_inc and not instru_inc and not holidays_inc:
        return

    # Load existing file (support both v1 and v2 on disk).
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

    payload: dict[str, Any] = {
        "version":              2,
        "saved_at":             datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ohlcv_daily":          ohlcv,
        "instruments_snapshot": instruments,
        "holidays_snapshot":    holidays,
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
