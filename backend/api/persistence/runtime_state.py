"""
Process-level runtime knobs for the persistence pipeline.

Two responsibilities:

1. Global bypass switch — when ON, every store skips Tier 1 + Tier 2 and
   hits the broker directly. The fresh broker payload still writes back
   through the queue, so the next non-bypass read sees the corrected
   data. Defect-recovery tool for "code shipped bad data into the
   cache/DB and I need it cleaned without manual SQL". Operator:
   "switch to use api with no db, will help refresh cache and db if
   they are not accurate because code defects".

2. Per-store / per-key invalidation — wipe in-memory + delete DB rows
   for a specific data class so the next read re-fetches from broker.

Both are runtime-only (no DB persistence of the flag itself). A process
restart resets bypass to OFF, which is the safe default.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ── Bypass flag ──────────────────────────────────────────────────────────────

_bypass_db: bool = False


def is_bypass_on() -> bool:
    return _bypass_db


def set_bypass(value: bool) -> None:
    global _bypass_db
    if _bypass_db == bool(value):
        return
    _bypass_db = bool(value)
    logger.warning(
        f"persistence: bypass_db = {_bypass_db} — all stores will now "
        f"{'skip cache + DB and hit broker directly' if _bypass_db else 'use the normal cache → DB → broker hierarchy'}"
    )


# ── Invalidation helpers ─────────────────────────────────────────────────────

async def invalidate_ohlcv(symbol: str | None = None, exchange: str | None = None) -> int:
    """Wipe in-memory + delete DB rows for ohlcv_daily.

    - symbol + exchange specified: targeted invalidation for one key
    - symbol only: drop every exchange for that symbol
    - neither: full wipe of the in-memory cache + truncate-equivalent on DB

    Returns the number of DB rows deleted (in-memory drops not counted).
    """
    from backend.api.persistence import ohlcv_store
    from backend.api.database import async_session
    from sqlalchemy import text

    sym  = symbol.upper().strip() if symbol else None
    exch = exchange.upper().strip() if exchange else None

    # Tier 1 wipe
    keys_to_drop = []
    for key in list(ohlcv_store._MEM_CACHE.keys()):
        k_sym, k_exch = key
        if sym and k_sym != sym:
            continue
        if exch and k_exch != exch:
            continue
        keys_to_drop.append(key)
    for k in keys_to_drop:
        ohlcv_store._MEM_CACHE.pop(k, None)

    # Tier 2 delete
    where = []
    params: dict[str, Any] = {}
    if sym:
        where.append("symbol = :sym")
        params["sym"] = sym
    if exch:
        where.append("exchange = :exch")
        params["exch"] = exch
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = text(f"DELETE FROM ohlcv_daily{where_sql}")
    try:
        async with async_session() as session:
            result = await session.execute(sql, params)
            await session.commit()
            n = result.rowcount or 0
    except Exception as exc:
        logger.warning(f"invalidate_ohlcv: DB delete failed: {exc}")
        n = 0

    logger.info(
        f"invalidate_ohlcv: dropped {len(keys_to_drop)} mem keys, "
        f"deleted {n} DB rows (sym={sym}, exch={exch})"
    )
    return n


async def invalidate_instruments(exchange: str | None = None) -> int:
    """Wipe instruments_snapshot for the given exchange (or all)."""
    from backend.api.persistence import instruments_store
    from backend.api.database import async_session
    from sqlalchemy import text

    exch = exchange.upper().strip() if exchange else None
    keys_to_drop = []
    for key in list(instruments_store._MEM_CACHE.keys()):
        _, k_exch = key
        if exch and k_exch != exch:
            continue
        keys_to_drop.append(key)
    for k in keys_to_drop:
        instruments_store._MEM_CACHE.pop(k, None)

    sql = text(
        "DELETE FROM instruments_snapshot"
        + (" WHERE exchange = :exch" if exch else "")
    )
    params = {"exch": exch} if exch else {}
    try:
        async with async_session() as session:
            result = await session.execute(sql, params)
            await session.commit()
            n = result.rowcount or 0
    except Exception as exc:
        logger.warning(f"invalidate_instruments: DB delete failed: {exc}")
        n = 0

    logger.info(
        f"invalidate_instruments: dropped {len(keys_to_drop)} mem keys, "
        f"deleted {n} DB rows (exch={exch})"
    )
    return n


async def invalidate_holidays(exchange: str | None = None) -> int:
    """Wipe holidays_snapshot for the given exchange (or all)."""
    from backend.api.persistence import holidays_store
    from backend.api.database import async_session
    from sqlalchemy import text

    exch = exchange.upper().strip() if exchange else None
    keys_to_drop = []
    for key in list(holidays_store._MEM_CACHE.keys()):
        k_exch, _ = key
        if exch and k_exch != exch:
            continue
        keys_to_drop.append(key)
    for k in keys_to_drop:
        holidays_store._MEM_CACHE.pop(k, None)

    sql = text(
        "DELETE FROM holidays_snapshot"
        + (" WHERE exchange = :exch" if exch else "")
    )
    params = {"exch": exch} if exch else {}
    try:
        async with async_session() as session:
            result = await session.execute(sql, params)
            await session.commit()
            n = result.rowcount or 0
    except Exception as exc:
        logger.warning(f"invalidate_holidays: DB delete failed: {exc}")
        n = 0

    logger.info(
        f"invalidate_holidays: dropped {len(keys_to_drop)} mem keys, "
        f"deleted {n} DB rows (exch={exch})"
    )
    return n
