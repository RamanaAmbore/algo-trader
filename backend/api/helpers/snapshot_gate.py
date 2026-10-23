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
    """Record a successful live response for anti-flicker substitution.

    Invariant (A2, 2026-09): this is only ever called from the `try`
    branch of `closed_hours_or_broker` below, i.e. only when `broker_fn()`
    returned WITHOUT raising. Routes that can produce an empty-but-masked-
    failure payload (e.g. positions.py's `_fetch()`, which now raises via
    `_is_positions_outage()` whenever a fetch failure is disguised as an
    empty per-account result) must raise BEFORE returning in that case —
    the raise is the gate that keeps a degraded response out of this
    cache. A response that reaches here with empty `rows` is therefore
    always a confirmed genuine empty state (e.g. operator closed every
    position), never a masked outage — do NOT add a blanket "skip empty"
    filter here, that would also skip caching the legitimate empty-book
    case and cause a subsequent transient broker failure within
    `_STALE_LIVE_TTL_S` to incorrectly resurrect PRE-close stale-live data
    instead of the correct (empty) state.
    """
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
#
# Timing is now delegated to exchange_clock which reads from the DB-backed
# exchange_schedule table.  The old hardcoded _EXCHANGE_TO_GATE dict and
# YAML-segment reader have been removed; exchange_clock.is_exchange_closed()
# resolves each exchange to its gate via the ``exchanges`` column in the DB.


def is_segment_pending_open(exchange: str, now=None) -> bool:
    """Return True when *exchange*'s configured `market_segments` session
    has not genuinely opened yet, even though the broader broker-
    connectivity window (`is_exchange_closed_now` above, 08:00-23:31 IST)
    already reports it open.

    Item 1 (2026-10-22, live incident): the system's broad window is wider
    than each exchange SEGMENT's own real trading-session hours
    (`market_segments.equity` 09:15, `market_segments.commodity` 09:00 —
    `backend/config/backend_config.yaml`). Once the broker reconnects at
    08:00, live quote/LTP calls start flowing immediately — MCX options in
    particular already carry a reference price tied to international gold
    movement well before the local order book opens, so `last_price` can
    diverge from `prev_close` purely from this gap, producing a
    phantom Day P&L move. `positions.py` / `holdings.py` pin last_price to
    prev_close for any row where this returns True.

    Matches `quote.py:_qt_is_seg_open`'s exchange-membership rule (against
    each segment's own `exchanges` list / `holiday_exchange`), NOT
    `agent_engine.py`'s separate `_EXCHANGE_TO_SEGMENT` table — CLAUDE.md
    documents that table can drift from a given route file's own match
    rule (it is missing NCO, for example).

    Delegates the actual open/closed determination to
    `date_time_utils._segment_is_open` (holiday + special-session aware,
    not a hand-rolled hour check).

    Fail-open: returns False (don't pin — keep the live broker value) when
    *exchange* matches no configured segment, or on any config/holiday-
    fetch error. A row from an unrecognised exchange is never silently
    pinned forever.
    """
    try:
        from backend.shared.helpers.utils import config as _cfg
        from backend.shared.helpers.date_time_utils import _segment_is_open, timestamp_indian
        if now is None:
            now = timestamp_indian()
        exch_upper = (exchange or "").upper()
        segments = _cfg.get("market_segments", {}) or {}
        for seg_cfg in segments.values():
            seg_exch = (seg_cfg.get("holiday_exchange") or "NSE").upper()
            seg_exchanges = {e.upper() for e in (seg_cfg.get("exchanges") or [seg_exch])}
            if exch_upper in seg_exchanges or exch_upper == seg_exch:
                return not _segment_is_open(seg_cfg, now)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"is_segment_pending_open({exchange!r}) failed: {exc}")
        return False


async def prewarm_pending_open_segments(exchanges: "set[str] | None" = None) -> None:
    """Await the special-sessions cache warm for every `market_segments`
    entry (or only those whose exchange membership matches *exchanges*,
    when given) before a SYNC `is_segment_pending_open()` call from a
    coroutine already on the main loop (2026-10-22 fix — same bug class
    as the 2026-10-20 "4-site residual sweep" documented in CLAUDE.md:
    `fetch_special_sessions()` silently fails open to `[]` on a cold
    cache hit from the main loop, so a real Muhurat session or an
    MCX-evening-closed special session would be ignored on the first
    pending-open check of the day).

    Call sites: `positions.py:_patch_raw_positions`/`_fetch` and
    `holdings.py:_fetch`/`get_holdings` — each is itself a coroutine
    already running on the main loop (not inside `asyncio.to_thread`),
    so the self-deadlock guard `run_on_main_loop()` would otherwise hit
    never applies here; a plain `await` is safe and correct.

    Mirrors `quote.py:_qt_prewarm_special_sessions`'s own exchange-
    membership match rule rather than routing through
    `agent_engine.py`'s separate `_EXCHANGE_TO_SEGMENT` table (which can
    drift from a given route file's own match rule).
    """
    from backend.shared.helpers.utils import config as _cfg
    from backend.brokers.broker_apis import fetch_special_sessions_async

    segments = _cfg.get("market_segments", {}) or {}
    upper_set = {e.upper() for e in exchanges} if exchanges else None
    for seg_cfg in segments.values():
        seg_exch = (seg_cfg.get("holiday_exchange") or "NSE").upper()
        seg_exchanges = {e.upper() for e in (seg_cfg.get("exchanges") or [seg_exch])}
        if upper_set is not None and not (upper_set & seg_exchanges) and seg_exch not in upper_set:
            continue
        try:
            await fetch_special_sessions_async(seg_exch)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"prewarm_pending_open_segments: failed for {seg_exch!r}: {e}")


def is_exchange_closed_now(exchange: str) -> bool:
    """Return True when `exchange` is currently closed (row-level gate).

    Delegates to exchange_clock which resolves timing from the DB-backed
    exchange_schedule table.  Used by positions.py / holdings.py to decide
    whether an individual row's LTP should be served from the DB snapshot
    or from the live broker fetch.

    Fail-open: returns False (treat as open) when exchange_clock cache is
    empty or the exchange is unknown, so the caller keeps the broker value.
    """
    try:
        from backend.api.helpers import exchange_clock
        return exchange_clock.is_exchange_closed(exchange)
    except Exception:
        return False


async def latest_snapshot_ltp_map(kind: str) -> dict[tuple[str, str], tuple[float, float | None]]:
    """Return a `(account, symbol) → (ltp, day_pnl)` map from the most-recent
    daily_book batch per account for the given kind ('positions' | 'holdings').

    Uses the same `WITH latest_batch AS (...)` CTE as the per-route snapshot
    readers (positions.py `_positions_snapshot`, holdings.py `_holdings_snapshot`)
    so the two paths cannot drift on which batch is authoritative. This is
    what the per-exchange row overlay reads when a row's exchange has
    just closed and its LTP should be frozen at the close_settled value.

    `day_pnl` — the broker-computed EOD day P&L at settlement, used by
    closed-exchange overlay to avoid the weekend 0-delta problem (snap_price =
    close_px = same Friday settlement snapshot → (snap_price - close_px) × qty = 0).
    When `day_pnl` is non-zero it is authoritative; the price-recompute path
    is used only as a fallback when `day_pnl` is None or genuinely zero.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql_text

    kind_lower = str(kind or "").lower()
    if kind_lower not in ("positions", "holdings"):
        return {}
    out: dict[tuple[str, str], tuple[float, float | None]] = {}
    try:
        async with async_session() as session:
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = :kind AND ltp IS NOT NULL AND ltp > 0
                    GROUP BY account
                )
                SELECT db.account, db.symbol, db.ltp, db.day_pnl
                FROM daily_book db
                JOIN latest_batch lb
                  ON db.account = lb.account AND db.captured_at = lb.max_at
                WHERE db.kind = :kind
                  AND db.ltp IS NOT NULL AND db.ltp > 0
            """), {"kind": kind_lower})
            for account, symbol, ltp, day_pnl in result.all():
                out[(str(account), str(symbol))] = (
                    float(ltp),
                    float(day_pnl) if day_pnl is not None else None,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"latest_snapshot_ltp_map({kind}) failed: {exc}")
    return out


def _any_segment_open(exchanges: "list[str] | None" = None) -> bool:
    """Sync wrapper used by asyncio.to_thread.

    Delegates to exchange_clock.is_any_segment_open() which reads from the
    DB-backed exchange_schedule cache.

    Parameters
    ----------
    exchanges:
        Optional list of exchange names (e.g. ``["NSE"]``).  When provided,
        only segments whose ``exchanges`` column contains one of the names
        are consulted.  ``None`` (default) checks ALL configured segments.
    """
    try:
        from backend.api.helpers import exchange_clock
        return exchange_clock.is_any_segment_open(exchanges)
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
    segment_exchanges: "list[str] | None" = None,
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
    segment_exchanges:
        Optional list of exchange names to restrict the market-open check
        (e.g. ``["NSE"]``).  When provided, only segments whose
        ``holiday_exchange`` matches are consulted, so NSE-specific routes
        fire their snapshot at NSE close (15:30 IST) instead of waiting
        for MCX to close (23:30 IST).  ``None`` (default) uses all
        segments — the original behaviour.

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
    market_open: bool = await asyncio.to_thread(_any_segment_open, segment_exchanges)

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
