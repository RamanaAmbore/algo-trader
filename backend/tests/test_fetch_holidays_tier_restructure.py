"""
Tier-restructured `fetch_holidays` in `backend/brokers/broker_apis.py`.

Read priority tested:
  Tier 1 — `holidays_store._MEM_CACHE`         (unchanged; regression guard)
  Tier 2 — module-level `_HOLIDAY_CACHE`       (unchanged; regression guard)
  Tier 3 — NEW: `market_holidays` DB table via `_read_market_holidays_sync`
  Tier 4 — NSE public API via `_fetch_holidays_from_nse`   (last resort)

Five quality dimensions:
  • SSOT       — the NSE URL is invoked from ONE place
                 (`_fetch_holidays_from_nse`).
  • Correctness— fall-through is strict; earlier tiers short-circuit later.
  • Performance— Tier-3 DB hit avoids the NSE HTTP round-trip.
  • Reuse      — same helpers used by cron + Tier-4 fallback path.
  • UX         — empty Tier-3 falls through (no false-empty caching).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


def _clear_module_caches():
    """Fresh state between tests — clear both cache tiers."""
    from backend.brokers import broker_apis as ba
    ba._HOLIDAY_CACHE.clear()
    try:
        from backend.api.persistence.holidays_store import _MEM_CACHE
        _MEM_CACHE.clear()
    except Exception:
        pass


def test_tier1_short_circuits_when_holidays_store_warm():
    """When `holidays_store._MEM_CACHE` has an entry for (exchange, year),
    `fetch_holidays` returns it WITHOUT touching Tiers 2/3/4."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays

    from backend.api.persistence.holidays_store import _MEM_CACHE, _ist_year
    yr = _ist_year()
    _MEM_CACHE[("NSE", yr)] = {date(2026, 1, 26)}

    with patch("backend.brokers.broker_apis._read_market_holidays_sync") as m_db, \
         patch("backend.brokers.broker_apis._fetch_holidays_from_nse") as m_nse:
        got = fetch_holidays("NSE")

    assert got == {date(2026, 1, 26)}
    m_db.assert_not_called()
    m_nse.assert_not_called()


def test_tier3_db_read_serves_when_tier1_and_2_cold():
    """Cold in-memory caches + populated DB → Tier-3 wins, mirrors into
    Tiers 1+2, and the NSE fallback is NOT invoked."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays

    db_set = {date(2026, 1, 26), date(2026, 3, 8)}
    with patch("backend.brokers.broker_apis._read_market_holidays_sync",
               return_value=db_set), \
         patch("backend.brokers.broker_apis._fetch_holidays_from_nse") as m_nse:
        got = fetch_holidays("NSE")

    assert got == db_set
    m_nse.assert_not_called()

    # Verify Tier 2 mirror populated for a subsequent same-day call.
    from backend.brokers import broker_apis as ba
    assert "NSE" in ba._HOLIDAY_CACHE
    assert ba._HOLIDAY_CACHE["NSE"][1] == db_set


def test_tier4_nse_fallback_when_db_empty():
    """Empty DB → falls through to Tier 4 (NSE) and caches the result."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays

    with patch("backend.brokers.broker_apis._read_market_holidays_sync",
               return_value=set()), \
         patch("backend.brokers.broker_apis._fetch_holidays_from_nse",
               return_value={date(2026, 1, 26)}) as m_nse, \
         patch("backend.brokers.broker_apis._upsert_market_holidays_async") as m_up:
        got = fetch_holidays("NSE")

    assert got == {date(2026, 1, 26)}
    m_nse.assert_called_once_with("NSE")
    # Fire-and-forget upsert to seed Tier 3 for next boot.
    m_up.assert_called_once()


def test_tier4_empty_response_still_cached_to_avoid_hammering():
    """When NSE returns empty (API down), the result is cached as empty
    for the rest of the day so we don't hammer NSE every 5 min."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays

    with patch("backend.brokers.broker_apis._read_market_holidays_sync",
               return_value=set()), \
         patch("backend.brokers.broker_apis._fetch_holidays_from_nse",
               return_value=set()) as m_nse:
        got1 = fetch_holidays("NSE")
        got2 = fetch_holidays("NSE")  # Tier-2 cache should serve this.

    assert got1 == set()
    assert got2 == set()
    # NSE called only once — Tier 2 (which cached the empty result) serves
    # the second call.
    assert m_nse.call_count == 1


def test_tier3_falls_through_on_db_exception():
    """DB unavailable (init not done, connection failure) → falls through
    to Tier 4 rather than raising to the caller."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays

    def _boom(_exch):
        raise RuntimeError("db down")

    with patch("backend.brokers.broker_apis._read_market_holidays_sync",
               side_effect=_boom), \
         patch("backend.brokers.broker_apis._fetch_holidays_from_nse",
               return_value={date(2026, 3, 8)}) as m_nse:
        got = fetch_holidays("NSE")

    assert got == {date(2026, 3, 8)}
    m_nse.assert_called_once()


def test_ssot_nse_url_is_in_one_place():
    """SSOT — NSE URL appears as a real HTTP call in exactly one place
    (the fetch primitive), so a URL migration is a one-line change.

    Docstrings that mention the URL informationally are allowed; the
    check is scoped to the actual ``https://…`` scheme so only real call
    sites match.
    """
    import inspect
    from backend.brokers import broker_apis as ba
    src = inspect.getsource(ba)
    # Only count actual HTTP invocations (scheme + host + path).
    n = src.count("https://www.nseindia.com/api/holiday-master")
    assert n == 1, (
        f"Expected NSE holiday URL invocation in exactly ONE place (the "
        f"fetch primitive), found {n}. Move duplicates into "
        f"`_fetch_holidays_from_nse`."
    )
