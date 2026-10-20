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


def test_tier2_cache_keys_on_ist_date_not_system_local_date():
    """2026-10-20 fix, same bug class as the special-sessions cache fix
    (commit 0e304f40): the prod/dev servers run on UTC system time, and
    IST is UTC+5:30. Between IST 00:00 and 05:30, the UTC calendar date
    hasn't rolled over yet — a Tier-2 cache keyed on `date.today()`
    (system-local) would mis-key during that ~5.5h window every night,
    serving yesterday's holiday set (or re-querying under the wrong
    key). Simulate exactly that window: system-local/UTC says
    2026-10-20 23:45 ("the 20th"), but IST is already 2026-10-21 05:15
    ("the 21st"). fetch_holidays must key Tier 2 on the IST date."""
    _clear_module_caches()
    from backend.brokers.broker_apis import fetch_holidays
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import patch as _patch

    fake_ist_now = _datetime(2026, 10, 21, 5, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

    with _patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=fake_ist_now), \
         _patch("backend.brokers.broker_apis._read_market_holidays_sync",
                return_value={date(2026, 1, 26)}), \
         _patch("backend.brokers.broker_apis._fetch_holidays_from_nse") as m_nse:
        fetch_holidays("NSE")

    from backend.brokers import broker_apis as ba
    cached_date = ba._HOLIDAY_CACHE.get("NSE", (None,))[0]
    assert cached_date == date(2026, 10, 21), (
        f"expected the IST date 2026-10-21, got {cached_date} (looks "
        "like the system-local/UTC date leaked back in)"
    )
    m_nse.assert_not_called()


@pytest.mark.asyncio
async def test_read_market_holidays_async_year_uses_ist_date():
    """Same IST-vs-UTC fix, for `_read_market_holidays_async`'s year
    filter — a bare `date.today().year` would read the PRIOR year
    during the IST 00:00-05:30 window on Jan 1 specifically (system-local
    UTC time still Dec 31), filtering out the very holidays this call
    needs."""
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import AsyncMock, MagicMock, patch as _patch
    from backend.brokers.broker_apis import _read_market_holidays_async

    fake_ist_now = _datetime(2027, 1, 1, 2, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    captured = {}

    class _FakeResult:
        def all(self):
            return []

    class _FakeSession:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, *a, **kw):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return _FakeResult()

    with _patch("backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=fake_ist_now), \
         _patch("backend.api.database.async_session", _FakeSession()):
        await _read_market_holidays_async("NSE")

    assert "2027" in captured["sql"], (
        f"expected the query to filter on IST year 2027, got: {captured['sql']}"
    )


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
