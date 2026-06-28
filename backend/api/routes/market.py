"""Market update endpoint — AI-generated report; DB cache; no YAML fallback.

Also exposes /api/market/status — holiday-aware open/closed state per
exchange. Frontend `marketHours.js` polls this so the popup that fires
on RefreshButton click ("Both NSE and MCX are currently closed") works
on Indian-market holidays where weekday+time alone would say "open".
"""

import asyncio
from datetime import time as _dt_time

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException

from backend.api.cache import get_or_fetch
from backend.api.schemas import MarketResponse
from backend.shared.helpers import genai_api
from backend.shared.helpers.date_time_utils import (
    is_market_open,
    timestamp_display,
    timestamp_indian,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as app_config, get_cycle_date


class MarketStatusResponse(msgspec.Struct):
    """Holiday-aware market-session state for the two Indian segments.

    Fields:
      nse_open    — True iff NSE/BSE equity + derivatives are in session.
      mcx_open    — True iff MCX commodity is in session.
      any_open    — convenience: nse_open OR mcx_open.
      is_holiday  — True iff today is an NSE-recognised holiday (covers
                    Republic Day, Diwali, etc. where weekday+time alone
                    would falsely report "open").
      checked_at  — IST timestamp the status was computed; lets the
                    frontend invalidate its cache at session-boundary
                    transitions.
    """
    nse_open: bool
    mcx_open: bool
    any_open: bool
    is_holiday: bool
    checked_at: str

logger = get_logger(__name__)

# Flow: in-process cache → DB row (<24h old) → Gemini. Never YAML.
_TTL = 86400  # 24 hours


_UNAVAILABLE = "Market report is temporarily unavailable. Please try again shortly."


def fetch_fresh() -> MarketResponse | None:
    """Call Gemini for a fresh market update. None if Gemini returned empty/failed."""
    content = genai_api.get_market_update(strict=True)
    if content is None:
        return None
    return MarketResponse(
        content=content,
        cycle_date=str(get_cycle_date()),
        refreshed_at=timestamp_display(),
    )


async def _db_or_gemini() -> MarketResponse:
    """Try DB row (<24h old). Else call Gemini inline and persist on success."""
    from backend.api.background import _load_market_from_db, _save_market_to_db

    cached = await _load_market_from_db()
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, fetch_fresh)
    if result is None:
        return MarketResponse(
            content=_UNAVAILABLE,
            cycle_date=str(get_cycle_date()),
            refreshed_at=timestamp_display(),
        )
    await _save_market_to_db(result)
    return result


def _parse_hhmm(s: str, fallback: tuple[int, int]) -> _dt_time:
    try:
        h, m = s.split(":")
        return _dt_time(int(h), int(m))
    except Exception:
        return _dt_time(*fallback)


async def _compute_market_status() -> MarketStatusResponse:
    """Probe the configured market_segments for current open state. Holiday-
    aware via the shared fetch_holidays cache (per (exchange, today's date),
    so a single fetch per day across the whole process). Weekends + holidays
    return False without hitting the broker."""
    from backend.brokers.broker_apis import fetch_holidays

    now = timestamp_indian()
    segments = app_config.get("market_segments", {}) or {}

    # equity (NSE/BSE/derivatives) — 09:15-15:30 default
    eq = segments.get("equity", {}) or {}
    nse_open_t  = _parse_hhmm(eq.get("hours_start", "09:15"), (9, 15))
    nse_close_t = _parse_hhmm(eq.get("hours_end",   "15:30"), (15, 30))
    # commodity (MCX) — 09:00-23:30 default
    co = segments.get("commodity", {}) or {}
    mcx_open_t  = _parse_hhmm(co.get("hours_start", "09:00"), (9, 0))
    mcx_close_t = _parse_hhmm(co.get("hours_end",   "23:30"), (23, 30))

    try:
        nse_holidays = await asyncio.to_thread(fetch_holidays, eq.get("holiday_exchange", "NSE"))
    except Exception:
        nse_holidays = set()
    try:
        mcx_holidays = await asyncio.to_thread(fetch_holidays, co.get("holiday_exchange", "MCX"))
    except Exception:
        mcx_holidays = set()

    nse_open = is_market_open(now, nse_holidays, nse_open_t, nse_close_t,
                              exchange=eq.get("holiday_exchange", "NSE"))
    mcx_open = is_market_open(now, mcx_holidays, mcx_open_t, mcx_close_t,
                              exchange=co.get("holiday_exchange", "MCX"))
    is_holiday = (now.date() in nse_holidays) or (now.date() in mcx_holidays)

    return MarketStatusResponse(
        nse_open=bool(nse_open),
        mcx_open=bool(mcx_open),
        any_open=bool(nse_open or mcx_open),
        is_holiday=bool(is_holiday),
        checked_at=timestamp_display(),
    )


class MarketController(Controller):
    path = "/api/market"

    @get("/")
    async def get_market(self) -> MarketResponse:
        try:
            return await get_or_fetch("market", _db_or_gemini, ttl_seconds=_TTL)
        except Exception as e:
            logger.error(f"Market API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @get("/status")
    async def get_market_status(self) -> MarketStatusResponse:
        """Holiday-aware open/closed state. 60s in-process cache —
        the holiday calendar is already cached by fetch_holidays,
        but the session-window check itself is sub-ms so caching
        anything more isn't useful. Unauthenticated; no sensitive
        data and every page may consult it."""
        return await get_or_fetch("market_status", _compute_market_status, ttl_seconds=60)
