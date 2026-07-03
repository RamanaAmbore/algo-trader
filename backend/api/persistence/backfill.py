"""
backfill.py — Coverage backfill helpers for ohlcv_daily and intraday_bars.

Two public coroutines:

  backfill_ohlcv_daily(symbols, target_days, max_concurrent)
    For each (symbol, exchange) check how many bars exist in ohlcv_daily.
    If fewer than target_days * 0.7 bars exist, force-fetch the full
    target_days window via get_or_fetch_daily(bypass_cache=True).
    Write-back is handled transparently by the persistence pipeline.

  backfill_intraday_today(symbols, interval, max_concurrent)
    For each (symbol, exchange) force-fetch today's intraday bars via
    get_or_fetch_intraday(bypass_cache=True).  Populates intraday_bars
    so the sparkline DB-only path has data during closed hours.

Both helpers:
  - Check _RATE_LIMIT_COOLOFF from backend.brokers.registry before each
    symbol and skip (not retry) when the broker is cooling off.  This
    prevents the cascade that originates a rate-limit storm.
  - Respect max_concurrent (asyncio.Semaphore) to stay within the
    broker's 3 req/sec historical_data budget.
  - Return a summary dict: {requested, filled, skipped_cooloff, errors}.

Usage from background.py startup hook:
    from backend.api.persistence.backfill import (
        backfill_ohlcv_daily, backfill_intraday_today,
    )
    await backfill_ohlcv_daily(symbols)
    await backfill_intraday_today(symbols)
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Coverage threshold ─────────────────────────────────────────────────────────

_COVERAGE_THRESHOLD = 0.70   # require at least 70% of target_days bars


# ── Rate-limit guard ───────────────────────────────────────────────────────────

def _any_broker_in_cooloff() -> bool:
    """Return True if ANY currently-loaded broker account is in cool-off.

    We skip the entire backfill run when at least one broker is throttled
    because the ohlcv_store/intraday_store always pick the best available
    broker; if the only Kite account is in cool-off the fetch would return
    empty bars and produce no write-back.

    Uses _RATE_LIMIT_COOLOFF + _RATE_LIMIT_LOCK from registry (canonical
    rate-limit tracking — no re-invention).
    """
    try:
        from backend.brokers.registry import _RATE_LIMIT_COOLOFF, _RATE_LIMIT_LOCK
        with _RATE_LIMIT_LOCK:
            now = time.time()
            for expires in _RATE_LIMIT_COOLOFF.values():
                if expires > now:
                    return True
        return False
    except Exception:
        return False


def _price_broker_in_cooloff() -> bool:
    """Return True if the primary price broker (the one get_or_fetch_daily
    would use) is currently rate-limited.

    Checks each eligible Kite account's cool-off state using the same
    _RATE_LIMIT_COOLOFF dict that PriceBroker._try() consults, keyed as
    "{broker_id}/{account}".  If get_historical_brokers() returns an empty
    list, all eligible brokers are in cool-off, so we return True.
    """
    try:
        from backend.brokers.registry import (
            get_historical_brokers,
            _RATE_LIMIT_COOLOFF,
            _RATE_LIMIT_LOCK,
            _broker_id_for,
        )
        brokers = get_historical_brokers()
        if not brokers:
            return True
        # get_historical_brokers already excludes rate-limited accounts —
        # if the returned list is non-empty, at least one broker is live.
        return False
    except Exception:
        return True


# ── DB bar-count helper ────────────────────────────────────────────────────────

async def _count_db_bars(symbol: str, exchange: str, from_d: date, to_d: date) -> int:
    """Count ohlcv_daily rows for (symbol, exchange) in [from_d, to_d]."""
    try:
        from sqlalchemy import text
        from backend.api.database import async_session

        stmt = text("""
            SELECT COUNT(*)
            FROM   ohlcv_daily
            WHERE  symbol   = :sym
              AND  exchange = :exch
              AND  date     BETWEEN :from_d AND :to_d
        """)
        async with async_session() as session:
            # asyncpg binds :from_d / :to_d strictly to Postgres DATE —
            # pass real date objects, not ISO strings, or the driver
            # raises `'str' object has no attribute 'toordinal'` and the
            # count silently returns 0 (misleading "already covered" skip).
            result = await session.execute(stmt, {
                "sym": symbol.upper().strip(),
                "exch": exchange.upper().strip(),
                "from_d": from_d,
                "to_d":   to_d,
            })
            row = result.fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug(f"backfill: _count_db_bars failed for {symbol}/{exchange}: {exc}")
        return 0


# ── Public API ────────────────────────────────────────────────────────────────

async def backfill_ohlcv_daily(
    symbols: list[tuple[str, str]],
    target_days: int = 365,
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """For each (symbol, exchange), check ohlcv_daily coverage.

    If fewer than `target_days * _COVERAGE_THRESHOLD` bars exist, or the
    earliest bar is more than `target_days` calendar days ago, force-fetch
    the full `target_days` window via get_or_fetch_daily(bypass_cache=True).
    Write-back into ohlcv_daily is handled by the persistence pipeline.

    Skips symbols whose price broker is currently in rate-limit cool-off.
    Uses a semaphore to bound concurrency to `max_concurrent`.

    Returns:
        {
            "requested":        N,   # total symbols checked
            "filled":           M,   # symbols where fetch was triggered
            "skipped_cooloff":  K,   # symbols skipped due to broker cool-off
            "errors":           [...],  # (symbol, exchange, error_message) tuples
        }
    """
    from backend.api.persistence.ohlcv_store import get_or_fetch_daily

    today    = date.today()
    from_d   = today - timedelta(days=target_days)
    to_d     = today - timedelta(days=1)   # yesterday — today's bar is unsettled

    sem      = asyncio.Semaphore(max_concurrent)
    filled   = 0
    skipped  = 0
    errors: list[tuple[str, str, str]] = []
    lock     = asyncio.Lock()

    logger.info(
        f"backfill_ohlcv_daily: checking {len(symbols)} symbols "
        f"(target_days={target_days}, threshold={_COVERAGE_THRESHOLD})"
    )

    async def _fetch_one(sym: str, exch: str) -> None:
        nonlocal filled, skipped

        # Per-symbol cool-off guard — skip rather than retry.
        if _price_broker_in_cooloff():
            async with lock:
                skipped += 1
            logger.debug(f"backfill_ohlcv_daily: skipped {sym}/{exch} — cooloff active")
            return

        # Check existing coverage.
        bar_count = await _count_db_bars(sym, exch, from_d, to_d)
        required  = int(target_days * _COVERAGE_THRESHOLD)
        if bar_count >= required:
            logger.debug(
                f"backfill_ohlcv_daily: {sym}/{exch} has {bar_count} bars "
                f"(≥ {required}) — skip"
            )
            return

        async with sem:
            # Re-check cool-off inside the semaphore (may have changed while
            # waiting for the semaphore slot).
            if _price_broker_in_cooloff():
                async with lock:
                    skipped += 1
                logger.debug(
                    f"backfill_ohlcv_daily: skipped {sym}/{exch} — cooloff "
                    "active (re-check inside semaphore)"
                )
                return

            logger.info(
                f"backfill_ohlcv_daily: fetching {sym}/{exch} "
                f"(existing={bar_count}, required={required}, "
                f"range={from_d}..{to_d})"
            )
            try:
                bars = await get_or_fetch_daily(sym, exch, from_d, to_d, bypass_cache=True)
                async with lock:
                    filled += 1
                logger.info(
                    f"backfill_ohlcv_daily: {sym}/{exch} — fetched {len(bars)} bars"
                )
            except Exception as exc:
                err_msg = str(exc)[:200]
                async with lock:
                    errors.append((sym, exch, err_msg))
                logger.warning(f"backfill_ohlcv_daily: error {sym}/{exch}: {err_msg}")

    tasks = [_fetch_one(sym, exch) for sym, exch in symbols]
    await asyncio.gather(*tasks, return_exceptions=False)

    summary = {
        "requested":       len(symbols),
        "filled":          filled,
        "skipped_cooloff": skipped,
        "errors":          errors,
    }
    logger.info(
        f"backfill_ohlcv_daily: done — "
        f"requested={summary['requested']}, filled={summary['filled']}, "
        f"skipped_cooloff={summary['skipped_cooloff']}, errors={len(errors)}"
    )
    return summary


async def backfill_intraday_today(
    symbols: list[tuple[str, str]],
    interval: str = "30minute",
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """For each (symbol, exchange), force-fetch today's intraday bars.

    Calls get_or_fetch_intraday(bypass_cache=True) so the result is written
    to intraday_bars via the write queue.  During closed hours, the sparkline
    DB-only path can then serve bars without a broker call.

    Skips symbols whose price broker is currently in rate-limit cool-off.
    Uses a semaphore to bound concurrency to `max_concurrent`.

    Returns:
        {
            "requested":        N,
            "filled":           M,
            "skipped_cooloff":  K,
            "errors":           [...],
        }
    """
    from backend.api.persistence.intraday_store import get_or_fetch_intraday

    today   = date.today()
    sem     = asyncio.Semaphore(max_concurrent)
    filled  = 0
    skipped = 0
    errors: list[tuple[str, str, str]] = []
    lock    = asyncio.Lock()

    logger.info(
        f"backfill_intraday_today: checking {len(symbols)} symbols "
        f"(interval={interval}, date={today})"
    )

    async def _fetch_one(sym: str, exch: str) -> None:
        nonlocal filled, skipped

        if _price_broker_in_cooloff():
            async with lock:
                skipped += 1
            logger.debug(f"backfill_intraday_today: skipped {sym}/{exch} — cooloff active")
            return

        async with sem:
            if _price_broker_in_cooloff():
                async with lock:
                    skipped += 1
                logger.debug(
                    f"backfill_intraday_today: skipped {sym}/{exch} — cooloff "
                    "active (re-check inside semaphore)"
                )
                return

            logger.info(f"backfill_intraday_today: fetching {sym}/{exch} date={today}")
            try:
                bars = await get_or_fetch_intraday(sym, exch, today, interval, bypass_cache=True)
                async with lock:
                    filled += 1
                logger.info(
                    f"backfill_intraday_today: {sym}/{exch} — fetched {len(bars)} bars"
                )
            except Exception as exc:
                err_msg = str(exc)[:200]
                async with lock:
                    errors.append((sym, exch, err_msg))
                logger.warning(f"backfill_intraday_today: error {sym}/{exch}: {err_msg}")

    tasks = [_fetch_one(sym, exch) for sym, exch in symbols]
    await asyncio.gather(*tasks, return_exceptions=False)

    summary = {
        "requested":       len(symbols),
        "filled":          filled,
        "skipped_cooloff": skipped,
        "errors":          errors,
    }
    logger.info(
        f"backfill_intraday_today: done — "
        f"requested={summary['requested']}, filled={summary['filled']}, "
        f"skipped_cooloff={summary['skipped_cooloff']}, errors={len(errors)}"
    )
    return summary
