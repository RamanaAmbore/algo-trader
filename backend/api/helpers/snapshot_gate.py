"""Canonical gate for every operator-visible data route during closed hours.

Usage
-----
Every route handler that reads broker data MUST call this at the top:

    data, source = await closed_hours_or_broker(
        exchange='NSE',
        snapshot_fn=_my_snapshot,
        broker_fn=_my_live_fetch,
        route_key='positions',
    )

`source` is one of:
  'live'              — broker_fn() succeeded during market hours
  'snapshot'          — market closed; snapshot_fn() returned data
  'snapshot-fallback' — market open but broker_fn() raised; snapshot_fn() used
  'stale-live'        — market open but broker_fn() raised; last-known live
                        payload (< _STALE_LIVE_TTL_S old) returned instead of
                        snapshot to prevent live/snapshot alternation flicker

This module eliminates the class of bug where individual routes reinvent
the "should I call broker or return snapshot?" decision inconsistently.
All per-route snapshot functions (``_positions_snapshot``,
``_holdings_snapshot``, etc.) stay as-is — they are the domain-specific
SSOT for what to return during closed hours.  This helper only decides
WHEN to call them vs. when to call the broker.

Thread / async safety
---------------------
``is_any_segment_open`` is synchronous (reads config + fetch_holidays
from the in-process cache); it is called via ``asyncio.to_thread`` so the
event loop is not blocked.  The caller-supplied ``snapshot_fn`` and
``broker_fn`` are both awaitable.

Anti-flicker cache (``route_key``)
-----------------------------------
When the broker fails during market hours, consecutive poll cycles would
alternate between live (broker up) and snapshot-fallback (broker down),
producing visible flicker every ~30 s.  The fix: each route nominates a
``route_key`` string; on every successful broker call the response is
stashed in ``_last_response_by_route`` with a timestamp.  On broker
failure, if a stale-live entry younger than ``_STALE_LIVE_TTL_S`` (120 s)
exists, return it with source ``'stale-live'`` instead of the DB snapshot.
The operator sees the last-good live payload for up to 2 min during a
transient outage, with no snapshot/live alternation.

When no ``route_key`` is supplied (backward-compatible), the anti-flicker
path is skipped and the existing ``'snapshot-fallback'`` behaviour applies.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any, Awaitable, Callable, TypeVar

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Anti-flicker last-known-live cache
# ---------------------------------------------------------------------------
# Shape: {route_key: (unix_ts, T)}
# Eviction: 2-minute TTL (much shorter than the daily_book snapshot TTL so
# we don't serve truly stale live data for extended outages — after 2 min
# the helper falls through to snapshot-fallback which is always correct).
_last_response_by_route: dict[str, tuple[float, Any]] = {}
_STALE_LIVE_TTL_S: float = 120.0  # 2 minutes


def _stash_live_response(route_key: str, data: Any) -> None:
    """Record a successful live response for anti-flicker substitution."""
    if route_key:
        _last_response_by_route[route_key] = (_time.time(), data)


def _get_stale_live(route_key: str) -> Any | None:
    """Return the cached live response if it's younger than _STALE_LIVE_TTL_S,
    else None."""
    if not route_key:
        return None
    entry = _last_response_by_route.get(route_key)
    if entry is None:
        return None
    ts, data = entry
    if _time.time() - ts > _STALE_LIVE_TTL_S:
        return None
    return data


# ---------------------------------------------------------------------------
# Per-exchange closed check (row-level snapshot serving)
# ---------------------------------------------------------------------------
# When one exchange has closed but another is still open (e.g. NSE closed at
# 15:30 IST while MCX runs till 23:30) we want to serve NSE rows from the
# daily_book snapshot while MCX rows stay live.  These helpers give routes a
# single sync predicate they can consult per-row after fetching the raw
# broker frame.

# Kite exchange → lifecycle gate label. NFO/BFO derivatives track the NSE
# equity session (same 09:15-15:30 IST window); MCX tracks its own longer
# window; CDS (currency derivatives) also inherits equity hours. Rows on
# unknown exchanges fall through to NSE (safest default — most equity-like).
_EXCHANGE_TO_GATE: dict[str, str] = {
    "NSE":  "NSE",
    "BSE":  "NSE",
    "NFO":  "NSE",
    "BFO":  "NSE",
    "CDS":  "NSE",   # 09:15-15:30 IST
    "MCX":  "MCX",
}


def is_exchange_closed_now(exchange: str) -> bool:
    """Return True when `exchange` is currently closed (row-level gate).

    Consults `is_market_open()` with the per-segment hours + holiday
    calendar. Used by positions.py / holdings.py to decide whether an
    individual row's LTP should be served from the DB snapshot or from
    the live broker fetch.

    Fail-open: if the check raises (config missing, holiday API down),
    return False so the caller keeps the broker value.
    """
    gate = _EXCHANGE_TO_GATE.get((exchange or "").upper(), "NSE")
    try:
        from backend.shared.helpers.date_time_utils import (
            is_market_open, timestamp_indian,
        )
        from backend.shared.helpers.utils import config as _cfg
        from backend.brokers.broker_apis import fetch_holidays, fetch_special_sessions
        from datetime import time as _dt_time

        segments = _cfg.get("market_segments", {}) or {}
        for seg_cfg in segments.values():
            exch = str(seg_cfg.get("holiday_exchange", "NSE")).upper()
            if exch != gate:
                continue
            h_s, m_s = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
            h_e, m_e = map(int, seg_cfg.get("hours_end",   "15:30").split(":"))
            seg_start = _dt_time(h_s, m_s)
            seg_end   = _dt_time(h_e, m_e)
            try:
                holidays = fetch_holidays(exch)
            except Exception:
                holidays = set()
            try:
                special = fetch_special_sessions(exch)
            except Exception:
                special = []
            now_ist = timestamp_indian()
            return not is_market_open(
                now_ist, holidays, seg_start, seg_end, exchange=exch,
                special_sessions=special,
            )
    except Exception:
        return False
    # No matching segment configured — treat as closed (conservative).
    return True


async def latest_snapshot_ltp_map(kind: str) -> dict[tuple[str, str], float]:
    """Return a `(account, symbol) → ltp` map from the most-recent daily_book
    batch per account for the given kind ('positions' | 'holdings').

    Uses the same `WITH latest_batch AS (...)` CTE as the per-route snapshot
    readers (positions.py `_positions_snapshot`, holdings.py `_holdings_snapshot`)
    so the two paths cannot drift on which batch is authoritative. This is
    what the per-exchange row overlay reads when a row's exchange has
    just closed and its LTP should be frozen at the close_settled value.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    kind_lower = str(kind or "").lower()
    if kind_lower not in ("positions", "holdings"):
        return {}
    out: dict[tuple[str, str], float] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = :kind AND ltp IS NOT NULL AND ltp > 0
                    GROUP BY account
                )
                SELECT db.account, db.symbol, db.ltp
                FROM daily_book db
                JOIN latest_batch lb
                  ON db.account = lb.account AND db.captured_at = lb.max_at
                WHERE db.kind = :kind
                  AND db.ltp IS NOT NULL AND db.ltp > 0
            """), {"kind": kind_lower})
            for account, symbol, ltp in result.all():
                out[(str(account), str(symbol))] = float(ltp)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"latest_snapshot_ltp_map({kind}) failed: {exc}")
    return out


def _any_segment_open() -> bool:
    """Sync wrapper used by asyncio.to_thread."""
    try:
        from backend.shared.helpers.date_time_utils import (
            is_any_segment_open,
            timestamp_indian,
        )
        return is_any_segment_open(timestamp_indian())
    except Exception:
        # fail-open: if we cannot determine market state, assume open so
        # the live broker path runs and the operator sees fresh data.
        return True


async def closed_hours_or_broker(
    exchange: str,
    snapshot_fn: Callable[[], Awaitable[T]],
    broker_fn: Callable[[], Awaitable[T]],
    *,
    fallback_to_snapshot_on_broker_error: bool = True,
    route_key: str = "",
) -> tuple[T, str]:
    """Return ``(data, source)`` for a data route.

    Parameters
    ----------
    exchange:
        Exchange code used only for logging (e.g. 'NSE', 'MCX').
        The actual market-open test is ``is_any_segment_open`` (covers
        all configured segments in one call) so a route covering multiple
        exchanges passes any representative value.
    snapshot_fn:
        Async callable that returns the most-recent DB snapshot.  Called
        when market is closed OR when broker_fn raises and
        ``fallback_to_snapshot_on_broker_error`` is True and no recent
        stale-live entry is available.
    broker_fn:
        Async callable that fetches live data from the broker.  Called
        only when the market is open.
    fallback_to_snapshot_on_broker_error:
        When True (default), a broker_fn exception during market hours
        triggers the anti-flicker path (stale-live if available, else
        snapshot_fn()).  The returned source is ``'stale-live'`` or
        ``'snapshot-fallback'`` so the caller can log/surface the
        degraded-mode indicator.
    route_key:
        Stable string identifying this route (e.g. 'positions',
        'holdings').  Enables the anti-flicker stale-live cache.  When
        empty (default) the cache is bypassed and the original
        snapshot-fallback behaviour applies.

    Returns
    -------
    tuple[T, str]
        ``(data, source)`` where source is one of:
        ``'live'``, ``'snapshot'``, ``'snapshot-fallback'``,
        ``'stale-live'``.

    Notes
    -----
    * ``broker_fn`` is NEVER called during closed hours — this is the
      primary invariant this helper enforces.
    * If both broker_fn AND snapshot_fn raise during market hours and
      fallback is enabled, the exception from snapshot_fn propagates so
      the route can return an appropriate HTTP error.
    * If snapshot_fn raises during market-closed hours, the exception
      propagates (no silent swallow of DB errors).
    """
    market_open: bool = await asyncio.to_thread(_any_segment_open)

    if not market_open:
        # Market is closed — NEVER call broker; return snapshot directly.
        logger.debug(
            f"closed_hours_or_broker [{exchange}]: market closed — snapshot path"
        )
        data = await snapshot_fn()
        return data, "snapshot"

    # Market is open — call broker_fn.
    try:
        data = await broker_fn()
        # Stash successful live payload for anti-flicker substitution.
        _stash_live_response(route_key, data)
        return data, "live"
    except Exception as broker_exc:
        if not fallback_to_snapshot_on_broker_error:
            raise
        logger.warning(
            f"closed_hours_or_broker [{exchange}]: broker_fn failed "
            f"({broker_exc!r}) — checking stale-live cache (route_key={route_key!r})"
        )
        # Anti-flicker: return last-known live payload if recent enough.
        stale_data = _get_stale_live(route_key)
        if stale_data is not None:
            logger.info(
                f"closed_hours_or_broker [{exchange}]: serving stale-live "
                f"payload for route_key={route_key!r} (avoids live/snapshot flicker)"
            )
            return stale_data, "stale-live"
        # No recent live payload — fall back to DB snapshot.
        logger.warning(
            f"closed_hours_or_broker [{exchange}]: no stale-live entry — "
            f"falling back to snapshot"
        )
        data = await snapshot_fn()
        return data, "snapshot-fallback"
