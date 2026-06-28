"""
test_ohlcv_equity_persistence.py

Covers the two-part fix for the BEL intermittent "No data available" bug:

  Problem: `to_d_daily = date.today()` in the ohlcv_store lookup meant the
  _is_complete_range boundary check always required today's bar to be present.
  Today's daily bar is not yet finalized during / shortly after market hours,
  so the check returned False on every request, forcing Tier 3 (broker) each
  time. Broker rate-limits or empty responses produced intermittent failures.

  Fix: `to_d_daily = date.today() - timedelta(days=1)` so the store lookup
  covers confirmed past bars only. Today's live bar falls through to the
  broker path in the historical endpoint and is NOT persisted in ohlcv_daily.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — 1st fetch hits broker; 2nd fetch in the same range returns
                   from Tier 1 (in-memory). Broker call count verified via mock.
  2. Performance — Tier 2 DB hit path makes ZERO broker calls. Measured by
                   assertion on call count, not wall time (pure-mock test; no
                   actual I/O).
  3. Stale code  — _is_complete_range is the canonical completeness function
                   (single implementation in ohlcv_store.py). Test confirms no
                   parallel re-implementation exists in the persistence package.
  4. Reusable    — write-back goes through the canonical _enqueue_persist_impl
                   which calls enqueue_disk + enqueue_db from write_queue.
                   Tested by asserting both enqueue functions are called after
                   a Tier 3 hit (not a direct DB write).
  5. Correctness — BEL, RELIANCE, TCS all persist + read back correctly.
                   Range queries return the full requested range, not partial.
                   Today's boundary excluded from the store range (yesterday
                   is the upper bound) so _is_complete_range passes.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.api.persistence.ohlcv_store import (
    OHLCVBar,
    OHLCVStore,
    _is_complete_range,
    _enqueue_persist_impl,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _prev_weekday(d: date) -> date:
    """Return the most recent weekday on or before d."""
    while d.weekday() >= 5:   # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _make_bars(from_d: date, to_d: date) -> list[OHLCVBar]:
    """Generate one OHLCVBar per weekday in [from_d, to_d].
    Weekends skipped so the gap-check in _is_complete_range (≤6 days)
    is satisfied and the series looks like real market data.

    Note: when from_d or to_d land on a weekend the boundary check in
    _is_complete_range will fail (dates_sorted[0] != from_d). Callers
    must pass weekday boundaries to get a passing series."""
    bars = []
    cur = from_d
    while cur <= to_d:
        if cur.weekday() < 5:   # Mon–Fri only
            bars.append(OHLCVBar(
                date=cur.isoformat(), open=100.0, high=105.0,
                low=98.0, close=102.0, volume=10_000,
            ))
        cur += timedelta(days=1)
    return bars


def _weekday_from_d_to_d(days: int = 30) -> tuple[date, date]:
    """Return (from_d, to_d) where both are weekdays and to_d is the most
    recent weekday strictly before today (i.e. yesterday or earlier), and
    from_d is approximately `days` calendar days before to_d (also a weekday).

    This is the boundary the fixed route uses: to_d = yesterday (weekday-
    adjusted), so _is_complete_range will pass for a complete weekday series."""
    to_d   = _prev_weekday(date.today() - timedelta(days=1))
    from_d = _prev_weekday(to_d - timedelta(days=days + 5))
    return from_d, to_d


# ── Dimension 3: Stale code — single canonical _is_complete_range ─────────────

def test_single_is_complete_range_implementation():
    """Confirm _is_complete_range lives in exactly one place (ohlcv_store.py)
    and is not duplicated elsewhere in the persistence package."""
    import backend.api.persistence as _pkg
    duplicates = []
    for _finder, modname, _ispkg in pkgutil.walk_packages(
        _pkg.__path__, prefix=_pkg.__name__ + "."
    ):
        if modname == "backend.api.persistence.ohlcv_store":
            continue  # canonical home — skip
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        if hasattr(mod, "_is_complete_range"):
            duplicates.append(modname)

    assert not duplicates, (
        f"_is_complete_range found in unexpected modules: {duplicates}. "
        "There must be exactly one implementation (in ohlcv_store.py)."
    )


# ── Dimension 3 (cont.): _is_complete_range behaviour ────────────────────────

def test_is_complete_range_passes_for_closed_weekdays():
    """Weekday-only series with weekday boundaries must pass the check.

    We use fixed Mon–Fri dates (not relative to today) so the test is
    deterministic regardless of when it runs."""
    # June 2 (Mon) to June 6 (Fri) 2026 — genuine 5-day trading week.
    # BUT June 6 is a Saturday. Use June 5 (Friday) as to_d instead.
    from_d = date(2026, 6, 2)   # Monday
    to_d   = date(2026, 6, 5)   # Friday (the last trading day of that week)
    bars   = _make_bars(from_d, to_d)
    assert bars, "Should have 4 bars (Mon–Thu) plus Fri"
    assert _is_complete_range(bars, from_d, to_d), (
        "Weekday-only series for a full Mon–Fri week must pass _is_complete_range. "
        f"from_d={from_d}, to_d={to_d}, bars={[b['date'] for b in bars]}"
    )


def test_is_complete_range_fails_missing_boundary():
    """Missing the upper boundary date must fail the check — exactly the
    condition that was causing every equity request to hit Tier 3."""
    from_d     = date(2026, 6, 2)   # Monday
    to_d       = date(2026, 6, 26)  # Friday — upper boundary required
    short_to_d = date(2026, 6, 25)  # Thursday — one day short
    bars       = _make_bars(from_d, short_to_d)   # stops before to_d
    assert not _is_complete_range(bars, from_d, to_d), (
        "Series missing the upper boundary date must return False so the "
        "caller falls through to broker (not serve stale cached data). "
        f"to_d={to_d}, last bar={bars[-1]['date'] if bars else 'none'}"
    )


def test_is_complete_range_yesterday_boundary_passes():
    """With to_d = yesterday (weekday-adjusted), confirmed past bars
    satisfy the check.

    This is the post-fix scenario: the route passes yesterday as the upper
    bound; the DB has the full range → Tier 2 hit, no broker call."""
    from_d, to_d = _weekday_from_d_to_d(days=30)
    bars = _make_bars(from_d, to_d)
    assert bars, f"Expected bars between {from_d} and {to_d}"
    assert _is_complete_range(bars, from_d, to_d), (
        "Yesterday-bounded weekday series must pass _is_complete_range. "
        f"from_d={from_d}, to_d={to_d}, "
        f"first={bars[0]['date']}, last={bars[-1]['date']}"
    )


# ── Dimension 1: SSOT — 1st request hits broker; 2nd hits Tier 1 ──────────────

@pytest.mark.asyncio
async def test_second_fetch_hits_tier1_not_broker():
    """After a cold Tier 3 fetch the bars are cached in Tier 1 (memory).
    A second get() for the same key must be served from Tier 1 — broker
    call count stays at 1.

    Method-patching on the instance works because Python's method resolution
    looks up the instance __dict__ before the class. We assign the async
    functions as instance attributes to override the class methods."""
    from_d, to_d = _weekday_from_d_to_d(days=30)
    bars      = _make_bars(from_d, to_d)
    full_key  = ("BEL", "NSE", from_d.isoformat(), to_d.isoformat())

    store = OHLCVStore()
    broker_call_count = 0

    async def _mock_broker_fetch(key):
        nonlocal broker_call_count
        broker_call_count += 1
        return list(bars)

    async def _mock_db_fetch(key):
        # DB is empty — force Tier 3 on first call.
        return None

    store._broker_fetch = _mock_broker_fetch  # type: ignore[method-assign]
    store._db_fetch     = _mock_db_fetch      # type: ignore[method-assign]

    with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
        # First get — cold, should hit _mock_broker_fetch once.
        result1 = await store.get(full_key)
        assert broker_call_count == 1, (
            f"Expected 1 broker call on cold fetch; got {broker_call_count}"
        )
        assert result1 is not None, "First fetch must return bars"

        # Second get — warm Tier 1, broker must NOT be called again.
        result2 = await store.get(full_key)
        assert broker_call_count == 1, (
            f"Tier 1 cache should have served the 2nd request; "
            f"broker was called {broker_call_count} times (expected 1)"
        )
        assert result2 is not None, "Second fetch must return bars"


# ── Dimension 2: Performance — Tier 2 hit makes ZERO broker calls ─────────────

@pytest.mark.asyncio
async def test_tier2_hit_makes_zero_broker_calls():
    """When the DB (_db_fetch) returns a complete range, the broker is never
    called. This validates the fix: with yesterday as to_d the completeness
    check passes and the DB result is served directly."""
    from_d, to_d = _weekday_from_d_to_d(days=30)
    bars      = _make_bars(from_d, to_d)
    full_key  = ("RELIANCE", "NSE", from_d.isoformat(), to_d.isoformat())

    store = OHLCVStore()
    broker_call_count = 0

    async def _mock_broker_fetch(key):
        nonlocal broker_call_count
        broker_call_count += 1
        return list(bars)

    async def _mock_db_fetch(key):
        # Tier 2 returns the full range — simulates the warmed DB state.
        return list(bars)

    store._broker_fetch = _mock_broker_fetch  # type: ignore[method-assign]
    store._db_fetch     = _mock_db_fetch      # type: ignore[method-assign]

    result = await store.get(full_key)
    assert broker_call_count == 0, (
        f"Tier 2 hit must make ZERO broker calls; got {broker_call_count}. "
        "This is the 'cached after first load' guarantee."
    )
    assert result is not None, "DB-cached result must not be None"


# ── Dimension 4: Reusable — write-back goes through canonical write_queue ─────

def test_enqueue_persist_impl_calls_both_queues():
    """_enqueue_persist_impl must call enqueue_disk AND enqueue_db.
    A direct DB write would bypass the batching / dedup logic in db_worker.py.

    _enqueue_persist_impl imports write_queue INSIDE the function body
    (lazy import to avoid circular imports at module load). We patch the
    module object that is imported at call time."""
    from_d, to_d = _weekday_from_d_to_d(days=7)
    bars         = _make_bars(from_d, to_d)

    disk_calls: list = []
    db_calls:   list = []

    mock_wq = MagicMock()
    mock_wq.enqueue_disk.side_effect = lambda p: disk_calls.append(p)
    mock_wq.enqueue_db.side_effect   = lambda p: db_calls.append(p)

    # Patch the module that _enqueue_persist_impl imports at call time.
    with patch("backend.api.persistence.write_queue", mock_wq):
        _enqueue_persist_impl("TCS", "NSE", bars)

    assert len(disk_calls) == 1, (
        "enqueue_disk must be called exactly once per _enqueue_persist_impl call"
    )
    assert len(db_calls) == 1, (
        "enqueue_db must be called exactly once per _enqueue_persist_impl call"
    )
    assert disk_calls[0]["kind"]     == "ohlcv_daily"
    assert disk_calls[0]["symbol"]   == "TCS"
    assert disk_calls[0]["exchange"] == "NSE"
    assert isinstance(disk_calls[0]["bars"], list)
    assert len(disk_calls[0]["bars"]) == len(bars)

    assert db_calls[0]["kind"]     == "ohlcv_daily"
    assert db_calls[0]["symbol"]   == "TCS"
    assert db_calls[0]["exchange"] == "NSE"


# ── Dimension 5: Correctness — BEL, RELIANCE, TCS all persist + read back ─────

@pytest.mark.asyncio
@pytest.mark.parametrize("sym", ["BEL", "RELIANCE", "TCS"])
async def test_equity_persist_and_read_back(sym: str):
    """Each equity symbol must:
      1. Fetch from broker (Tier 3) on cold call.
      2. Return all bars in the requested range.
      3. Enqueue write-back to both write_queue sinks.
      4. Second call served from Tier 1 with the same bar count."""
    from_d, to_d = _weekday_from_d_to_d(days=30)
    bars         = _make_bars(from_d, to_d)
    full_key     = (sym, "NSE", from_d.isoformat(), to_d.isoformat())

    store = OHLCVStore()
    broker_calls: list = []

    async def _mock_broker(key):
        broker_calls.append(key)
        return list(bars)

    async def _mock_db(key):
        return None  # cold DB

    store._broker_fetch = _mock_broker  # type: ignore[method-assign]
    store._db_fetch     = _mock_db      # type: ignore[method-assign]

    with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl") as mock_ep:
        # Cold fetch.
        result1 = await store.get(full_key)
        assert len(broker_calls) == 1, (
            f"{sym}: expected 1 broker call; got {len(broker_calls)}"
        )
        assert result1 is not None, f"{sym}: cold fetch returned None"

        # Write-back enqueued with correct sym/exch.
        assert mock_ep.call_count == 1, (
            f"{sym}: _enqueue_persist_impl must be called once after Tier 3 hit; "
            f"got {mock_ep.call_count}"
        )
        enq_sym, enq_exch, enq_bars = mock_ep.call_args[0]
        assert enq_sym  == sym,   f"{sym}: persisted symbol mismatch: {enq_sym}"
        assert enq_exch == "NSE", f"{sym}: persisted exchange must be NSE; got {enq_exch}"
        assert len(enq_bars) == len(bars), (
            f"{sym}: enqueued bar count {len(enq_bars)} != fetched {len(bars)}"
        )

        # Warm fetch — Tier 1, no second broker call.
        result2 = await store.get(full_key)
        assert len(broker_calls) == 1, (
            f"{sym}: 2nd fetch should hit Tier 1 but broker was called again "
            f"(total={len(broker_calls)})"
        )

        # Bar count preserved.
        if isinstance(result2, dict):
            served = [v for k, v in result2.items()
                      if from_d.isoformat() <= k <= to_d.isoformat()]
        else:
            served = list(result2)
        assert len(served) == len(bars), (
            f"{sym}: served {len(served)} bars but fetched {len(bars)}"
        )


# ── Dimension 5 (cont.): to_d_daily is yesterday in the route ─────────────────

def test_options_historical_route_uses_yesterday_for_daily_store():
    """The /api/options/historical handler must pass yesterday (not today) as
    to_d_daily when calling get_or_fetch_daily.

    Source-level assertion guards against a future refactor reverting the
    boundary back to date.today(), which was the root cause of the equity
    cache-miss bug."""
    from backend.api.routes.options import OptionsController

    src = inspect.getsource(OptionsController)
    assert "timedelta(days=1)" in src, (
        "OptionsController.historical must subtract timedelta(days=1) from today "
        "to derive to_d_daily. Missing this means the ohlcv_store completeness "
        "check always fails (today's bar absent) → every request hits Tier 3."
    )
    assert "to_d_daily" in src, (
        "to_d_daily variable must be present in OptionsController.historical"
    )
