"""Feeds new log_events rows to the agent cycle.

The high-water mark is kept in Redis, so a restart resumes where it stopped. The
default fetch looks back at most _LOOKBACK_S, so a long outage cannot replay old
alerts. If Redis is unavailable, the first call starts at the newest row.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

_FETCH_LIMIT = 2000
_LOOKBACK_S = 600
_HW_KEY = "ramboq:logfeed:hw"
_high_water: int | None = None


def _redis_store():
    from backend.shared.helpers.alert_utils import _get_redis
    return _get_redis()


def _load_stored_high_water() -> int | None:
    client = _redis_store()
    if client is None:
        return None
    try:
        raw = client.get(_HW_KEY)
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _store_high_water(value: int) -> None:
    client = _redis_store()
    if client is None:
        return
    try:
        client.set(_HW_KEY, int(value))
    except Exception:
        pass


async def _default_fetch(after_id: int) -> list[dict]:
    from backend.api.database import async_session
    from backend.api.models import LogEvent
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_LOOKBACK_S)
    async with async_session() as s:
        rows = (await s.execute(
            select(LogEvent).where(LogEvent.id > after_id, LogEvent.ts >= cutoff)
            .order_by(LogEvent.id).limit(_FETCH_LIMIT)
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
            stored = _load_stored_high_water()
            _high_water = stored if stored is not None else await newest()
            if stored is None:
                return []
        rows = await fetch(_high_water)
    except Exception as e:
        logger.warning(f"log_feed: fetch failed, retrying next cycle: {e}")
        return []
    if rows:
        _high_water = rows[-1]["id"]
        _store_high_water(_high_water)
    return rows


def reset_for_tests() -> None:
    global _high_water
    _high_water = None
