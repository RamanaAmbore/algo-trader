"""Market update endpoint — AI-generated report; DB cache; no YAML fallback.

Also exposes /api/market/status — holiday-aware open/closed state per
exchange. Frontend `marketHours.js` polls this so the popup that fires
on RefreshButton click ("Both NSE and MCX are currently closed") works
on Indian-market holidays where weekday+time alone would say "open".
"""

import asyncio
from datetime import time as _dt_time

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException

from backend.api.auth_guard import admin_guard
from backend.api.cache import get_or_fetch
from backend.api.schemas import MarketResponse
from backend.shared.helpers import genai_api
from backend.shared.helpers.date_time_utils import (
    is_market_open,
    timestamp_display,
    timestamp_indian,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as app_config, get_cycle_date, is_enabled, is_prod_branch


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
    """Call Gemini for a fresh market update. None if Gemini returned empty/failed.

    `get_cycle_date(hours=0, mins=0)` — a midnight IST cutoff, not the
    function's 8am default. The proactive daily refresh now runs from a
    05:30 IST wake-up (2026-09 scheduling redesign); with the old 8am
    default a report generated at 05:30 would mislabel itself as
    YESTERDAY's cycle (`now < today_cutoff` at 05:30 < 08:00), even
    though it represents the session about to open."""
    content = genai_api.get_market_update(strict=True)
    if content is None:
        return None
    return MarketResponse(
        content=content,
        cycle_date=str(get_cycle_date(hours=0, mins=0)),
        refreshed_at=timestamp_display(),
    )


async def _db_or_gemini() -> MarketResponse:
    """Serve the DB row regardless of its age (see `_load_market_from_db`
    docstring); fall through to a live, blocking Gemini call ONLY when
    literally no row exists at all (first-ever boot, empty DB)."""
    from backend.api.background import _load_market_from_db, _save_market_to_db

    cached = await _load_market_from_db()
    if cached:
        return cached

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, fetch_fresh)
    if result is None:
        return MarketResponse(
            content=_UNAVAILABLE,
            cycle_date=str(get_cycle_date(hours=0, mins=0)),
            refreshed_at=timestamp_display(),
        )
    await _save_market_to_db(result)
    return result


async def _dev_final_fallback() -> MarketResponse:
    """Dev's last resort when the loopback proxy to prod has no
    last-known-good payload yet (e.g. a fresh dev boot racing a prod
    outage) — serves dev's own historical `market_report` DB row if one
    exists (whatever content dev itself generated before the 2026-09
    shared-generation migration, however stale), and NEVER calls Gemini
    (dev must never generate). Falls through to the same static
    `_UNAVAILABLE` string `_db_or_gemini` uses for its own cold-boot case
    only when even that DB row doesn't exist.

    Always marks `stale=True, source="snapshot-fallback"` — this branch is
    only reached when the live proxy to prod has already failed AND no
    frozen proxy copy exists either, so whatever is served here is by
    definition not live."""
    from backend.api.background import _load_market_from_db

    cached = await _load_market_from_db()
    if cached:
        return msgspec.structs.replace(cached, stale=True, source="snapshot-fallback")
    return MarketResponse(
        content=_UNAVAILABLE,
        cycle_date=str(get_cycle_date(hours=0, mins=0)),
        refreshed_at=timestamp_display(),
        stale=True,
        source="snapshot-fallback",
    )


async def _dev_market_content() -> MarketResponse:
    """Dev-only: proxy-read prod's already-generated market report over
    loopback. See `backend.api.helpers.dev_content_proxy` for the
    short-TTL + freeze-to-last-good design."""
    from backend.api.helpers.dev_content_proxy import (
        ProdProxyUnavailable,
        fetch_from_prod,
    )

    try:
        return await fetch_from_prod(
            "/api/market", MarketResponse,
            is_valid=lambda m: bool(m.content) and m.content != _UNAVAILABLE,
        )
    except ProdProxyUnavailable:
        return await _dev_final_fallback()


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


async def _run_market_dry_run() -> MarketResponse:
    """B7 preview logic, factored out of the route handler so it's
    directly unit-testable without a Litestar Controller instance.

    Runs the CURRENT (possibly not-yet-merged) Gemini prompt through
    `fetch_fresh()` exactly ONCE and returns the generated text directly
    — does NOT write to the `market_report` DB table, does NOT call
    `cache.put`/`invalidate` for the "market" key, and does NOT go
    through `_perform_market_refresh_once` (the only function that
    persists). This is how the operator previews a reworked prompt (e.g.
    B7) before requesting `/dprod`: with shared generation, prod-as-
    sole-generator otherwise means dev could never produce a
    fresh-prompt sample before merge, and dev-as-generator would leak
    unreviewed output onto prod's live public page.

    Requires the `genai` capability enabled for THIS environment
    (`notifications.genai_enabled` DB override, or `cap_in_dev.genai` in
    backend_config.yaml — dev defaults this to False to avoid burning
    Gemini quota on an idle dev box). Raises a clear 400 — not an opaque
    502 — when that precondition isn't met, since a dev operator
    previewing B7 needs to know to flip the setting, not just that
    "Gemini returned nothing"."""
    if not is_enabled('genai'):
        raise HTTPException(
            status_code=400,
            detail=(
                "GenAI capability is disabled for this environment — enable "
                "notifications.genai_enabled (or cap_in_dev.genai) before "
                "running a dry-run preview."
            ),
        )
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, fetch_fresh)
    if result is None:
        raise HTTPException(status_code=502, detail="Gemini returned no content for dry-run")
    return result


class MarketController(Controller):
    path = "/api/market"

    @get("/")
    async def get_market(self) -> MarketResponse:
        try:
            if is_prod_branch():
                return await get_or_fetch("market", _db_or_gemini, ttl_seconds=_TTL)
            # Dev: proxy-read prod's already-generated content over
            # loopback instead of ever calling Gemini locally. Short TTL
            # (not prod's 24h _TTL) — see dev_content_proxy.TTL_SECONDS.
            from backend.api.helpers.dev_content_proxy import TTL_SECONDS as _DEV_TTL
            return await get_or_fetch("market_dev_proxy", _dev_market_content, ttl_seconds=_DEV_TTL)
        except Exception as e:
            logger.error(f"Market API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @post("/dry-run", guards=[admin_guard])
    async def dry_run_market(self) -> MarketResponse:
        """Admin-only B7 preview — see `_run_market_dry_run` for the full
        design/rationale."""
        return await _run_market_dry_run()

    @get("/status")
    async def get_market_status(self) -> MarketStatusResponse:
        """Holiday-aware open/closed state. 60s in-process cache —
        the holiday calendar is already cached by fetch_holidays,
        but the session-window check itself is sub-ms so caching
        anything more isn't useful. Unauthenticated; no sensitive
        data and every page may consult it."""
        return await get_or_fetch("market_status", _compute_market_status, ttl_seconds=60)
