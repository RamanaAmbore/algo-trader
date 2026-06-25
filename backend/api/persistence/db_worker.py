"""
DB worker — drains db_queue and batches OHLCV bars into PostgreSQL via
INSERT ... ON CONFLICT DO NOTHING.

Batches by size (500 rows) or timeout (500 ms), whichever fires first.
Duplicate (symbol, exchange, date) entries within a batch are coalesced
last-write-wins before the INSERT so the batch carries at most one row
per primary key.

A failed batch is dropped; the next read re-fetches from the broker.
"""

from __future__ import annotations

import asyncio

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_BATCH_ROWS = 500
_FLUSH_INTERVAL = 0.5   # seconds


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
        deadline = asyncio.get_event_loop().time() + _FLUSH_INTERVAL

        while len(batch) < _BATCH_ROWS:
            remaining = deadline - asyncio.get_event_loop().time()
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

        rows = _coalesce(batch)
        if not rows:
            continue

        try:
            await _upsert(rows)
            _record_db_flush(len(rows))
        except Exception as exc:
            logger.warning(
                f"db_worker: batch upsert failed ({len(rows)} rows dropped): {exc}"
            )


def _coalesce(batch: list[dict]) -> list[dict]:
    """Merge batch into unique (symbol, exchange, date) rows, last-write-wins."""
    seen: dict[tuple[str, str, str], dict] = {}
    for payload in batch:
        if payload.get("kind") != "ohlcv_daily":
            continue
        sym  = str(payload.get("symbol", ""))
        exch = str(payload.get("exchange", ""))
        if not sym or not exch:
            continue
        for bar in payload.get("bars", []):
            d = str(bar.get("date", ""))
            if not d:
                continue
            seen[(sym, exch, d)] = {
                "symbol":   sym,
                "exchange": exch,
                "date":     d,
                "open":     bar.get("open",   0.0),
                "high":     bar.get("high",   0.0),
                "low":      bar.get("low",    0.0),
                "close":    bar.get("close",  0.0),
                "volume":   int(bar.get("volume", 0)),
            }
    return list(seen.values())


async def _upsert(rows: list[dict]) -> None:
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
