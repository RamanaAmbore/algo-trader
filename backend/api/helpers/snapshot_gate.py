"""Canonical gate for every operator-visible data route during closed hours.

Usage
-----
Every route handler that reads broker data MUST call this at the top:

    data, source = await closed_hours_or_broker(
        exchange='NSE',
        snapshot_fn=_my_snapshot,
        broker_fn=_my_live_fetch,
    )

`source` is one of:
  'live'              — broker_fn() succeeded during market hours
  'snapshot'          — market closed; snapshot_fn() returned data
  'snapshot-fallback' — market open but broker_fn() raised; snapshot_fn() used

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
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


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
        ``fallback_to_snapshot_on_broker_error`` is True.
    broker_fn:
        Async callable that fetches live data from the broker.  Called
        only when the market is open.
    fallback_to_snapshot_on_broker_error:
        When True (default), a broker_fn exception during market hours
        triggers a snapshot_fn() call.  The returned source is
        ``'snapshot-fallback'`` so the caller can log/surface the
        degraded-mode indicator.

    Returns
    -------
    tuple[T, str]
        ``(data, source)`` where source is ``'live'``, ``'snapshot'``, or
        ``'snapshot-fallback'``.

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
        return data, "live"
    except Exception as broker_exc:
        if not fallback_to_snapshot_on_broker_error:
            raise
        logger.warning(
            f"closed_hours_or_broker [{exchange}]: broker_fn failed "
            f"({broker_exc!r}) — falling back to snapshot"
        )
        data = await snapshot_fn()
        return data, "snapshot-fallback"
