"""Feeds new log_events rows to the agent cycle.

The first call in a process starts at the newest stored row, so a restart does not
replay history. Records written while the engine is down are not seen (known gap).
"""
import logging

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

_FETCH_LIMIT = 2000
_high_water: int | None = None


async def _default_fetch(after_id: int) -> list[dict]:
    from backend.api.database import async_session
    from backend.api.models import LogEvent
    async with async_session() as s:
        rows = (await s.execute(
            select(LogEvent).where(LogEvent.id > after_id).order_by(LogEvent.id).limit(_FETCH_LIMIT)
        )).scalars().all()
    return [
        {"id": r.id, "ts": r.ts, "level": r.level, "logger": r.logger,
         "message": r.message, "tags": list(r.tags or []), "extra": r.extra or {}}
        for r in rows
    ]


async def _default_newest_id() -> int:
    from backend.api.database import async_session
    from backend.api.models import LogEvent
    async with async_session() as s:
        return int((await s.execute(select(func.max(LogEvent.id)))).scalar() or 0)


async def records_since_last_cycle(fetch=_default_fetch, newest=_default_newest_id) -> list[dict]:
    global _high_water
    try:
        if _high_water is None:
            _high_water = await newest()
            return []
        rows = await fetch(_high_water)
    except Exception as e:
        logger.warning(f"log_feed: fetch failed, retrying next cycle: {e}")
        return []
    if rows:
        _high_water = rows[-1]["id"]
    return rows


def reset_for_tests() -> None:
    global _high_water
    _high_water = None
