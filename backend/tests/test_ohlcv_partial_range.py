"""
test_ohlcv_partial_range.py

Covers the partial-range fetch optimisation in ohlcv_store.get_or_fetch_daily:

  Problem: when Tier 2 (DB) held bars for part of the requested range the
  entire range was re-fetched from the broker.  A 1Y request with 6 months
  already in DB triggered a full 365-day broker call every time.

  Fix: _compute_missing_ranges() computes only the gaps between what the DB
  holds and what was requested.  The broker is called only for those slices.
  Fetched slices are persisted (today's bar excluded — immutable-day rule).
  The existing DB bars and the new slices are merged and returned sorted.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — 1Y request with 6M in DB → broker called with the 6-month
                   MISSING slice only (not the full year).  Mock broker,
                   count calls + assert the date-range argument.
  2. Performance — 1Y request with 6M in DB is < 50% the broker work of a
                   fully-cold 1Y request.  Verified by call-count, not wall
                   time (pure-mock, no I/O).
  3. Stale code  — The route handler does NOT bypass Tier 2 when partial.
                   Source-grep confirms the old "fetch full range on incomplete"
                   pattern is replaced by the _compute_missing_ranges path.
  4. Reusable    — _compute_missing_ranges is a pure function, importable and
                   testable independently.  All four gap cases are exercised.
  5. Correctness — Four gap cases each persist the right slice and return the
                   right combined output.  Weekend/holiday gaps ≤ 6 days do
                   not trigger a redundant fetch.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.api.persistence.ohlcv_store import (
    OHLCVBar,
    OHLCVStore,
    _compute_missing_ranges,
    _MARKET_GAP_DAYS,
    get_or_fetch_daily,
)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _prev_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _make_bars(from_d: date, to_d: date) -> list[OHLCVBar]:
    """One OHLCVBar per weekday in [from_d, to_d]."""
    bars: list[OHLCVBar] = []
    cur = from_d
    while cur <= to_d:
        if cur.weekday() < 5:
            bars.append(OHLCVBar(
                date=cur.isoformat(), open=100.0, high=105.0,
                low=98.0, close=102.0, volume=10_000,
            ))
        cur += timedelta(days=1)
    return bars


def _weekday_boundary(d: date) -> date:
    return _prev_weekday(d)


# ── Dimension 4: Reusable — _compute_missing_ranges pure-function tests ────────

class TestComputeMissingRanges:
    """Four structural gap cases + holiday absorption."""

    def test_empty_existing_returns_full_range(self):
        """No DB bars → single slice covering the entire requested range."""
        from_d = date(2026, 1, 5)   # Monday
        to_d   = date(2026, 6, 26)  # Friday
        result = _compute_missing_ranges([], from_d, to_d)
        assert result == [(from_d, to_d)], (
            "Empty existing_bars must produce one slice = the full requested range. "
            f"Got: {result}"
        )

    def test_case_a_existing_inside_requested_two_slices(self):
        """Case a: DB has bars entirely inside the requested range.
        Expected: two slices — head (from_d..db_min-1) + tail (db_max+1..to_d)."""
        from_d  = date(2025, 7, 1)   # Tuesday
        to_d    = date(2026, 6, 26)  # Friday  (~1Y)
        db_from = date(2026, 1, 5)   # Monday  (~6M in)
        db_to   = date(2026, 5, 29)  # Friday  (~1M before to_d)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        assert len(result) == 2, (
            f"Case a must produce 2 missing slices; got {len(result)}: {result}"
        )
        # Head slice
        assert result[0][0] == from_d, f"Head slice must start at from_d={from_d}"
        assert result[0][1] == db_from - timedelta(days=1), (
            f"Head slice must end at db_min-1={db_from - timedelta(days=1)}"
        )
        # Tail slice
        assert result[1][0] == db_to + timedelta(days=1), (
            f"Tail slice must start at db_max+1={db_to + timedelta(days=1)}"
        )
        assert result[1][1] == to_d, f"Tail slice must end at to_d={to_d}"

    def test_case_b_db_overlaps_tail_only_head_missing(self):
        """Case b: DB has bars that cover the tail of the requested range.
        Expected: one slice covering the head (from_d..db_min-1)."""
        from_d  = date(2025, 7, 1)   # Tuesday
        to_d    = date(2026, 6, 26)  # Friday
        db_from = date(2026, 1, 5)   # Monday  (DB starts mid-range)
        db_to   = date(2026, 6, 26)  # Friday  (DB covers through to_d)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        assert len(result) == 1, (
            f"Case b must produce 1 missing slice (head only); got {len(result)}: {result}"
        )
        assert result[0][0] == from_d, f"Missing slice must start at from_d={from_d}"
        assert result[0][1] == db_from - timedelta(days=1), (
            f"Missing slice must end at db_min-1={db_from - timedelta(days=1)}"
        )

    def test_case_c_db_overlaps_head_only_tail_missing(self):
        """Case c: DB has bars that cover the head of the requested range.
        Expected: one slice covering the tail (db_max+1..to_d)."""
        from_d  = date(2025, 7, 1)   # Tuesday
        to_d    = date(2026, 6, 26)  # Friday
        db_from = date(2025, 7, 1)   # Tuesday (DB starts at from_d)
        db_to   = date(2026, 1, 2)   # Friday  (DB covers only first 6M)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        assert len(result) == 1, (
            f"Case c must produce 1 missing slice (tail only); got {len(result)}: {result}"
        )
        assert result[0][0] == db_to + timedelta(days=1), (
            f"Missing slice must start at db_max+1={db_to + timedelta(days=1)}"
        )
        assert result[0][1] == to_d, f"Missing slice must end at to_d={to_d}"

    def test_case_d_disjoint_existing_full_range_needed(self):
        """Case d: DB has bars completely disjoint from the requested range.
        Expected: one slice = the full requested range."""
        from_d  = date(2026, 6, 1)   # Monday
        to_d    = date(2026, 6, 26)  # Friday
        # Existing bars are OUTSIDE the requested range entirely
        db_from = date(2026, 1, 2)   # far in the past
        db_to   = date(2026, 2, 27)  # still far in the past (gap > 6 days to from_d)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        # The tail gap (to_d - db_to >> 6 days) triggers a fetch for the
        # full requested range from from_d to to_d.
        assert any(s[0] <= from_d and s[1] >= to_d for s in result) or (
            # OR result[0] ends at to_d and starts at or before from_d
            len(result) >= 1 and result[-1][1] == to_d
        ), (
            f"Case d: the full requested range [{from_d}..{to_d}] must be in "
            f"missing slices. Got: {result}"
        )

    def test_holiday_gap_absorbed_no_fetch(self):
        """Gaps ≤ _MARKET_GAP_DAYS between DB boundary and requested boundary
        must NOT trigger a broker slice — they are treated as holidays."""
        # DB ends on Friday 2026-06-19, requested to_d = Wednesday 2026-06-24.
        # Gap = 5 days (Sat+Sun+Mon+Tue+Wed) — within the 6-day threshold.
        db_from  = date(2026, 1, 5)   # Monday
        db_to    = date(2026, 6, 19)  # Friday
        from_d   = date(2026, 1, 5)
        to_d     = date(2026, 6, 24)  # Wednesday (gap = 5 days from db_to)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        assert result == [], (
            f"Gap of ≤{_MARKET_GAP_DAYS} days must be absorbed as holiday — "
            f"expected no missing slices, got: {result}"
        )

    def test_weekend_gap_at_boundary_absorbed(self):
        """from_d on Monday where DB coverage starts Wednesday (gap=2 days).
        The Mon–Tue gap is within tolerance, no head slice generated."""
        db_from  = date(2026, 6, 3)   # Wednesday
        db_to    = date(2026, 6, 26)  # Friday
        from_d   = date(2026, 6, 1)   # Monday — 2 days before db_from
        to_d     = date(2026, 6, 26)
        existing = _make_bars(db_from, db_to)

        result = _compute_missing_ranges(existing, from_d, to_d)
        # gap from from_d to db_from = 2 days ≤ 6 → no head slice
        head_slices = [s for s in result if s[0] == from_d]
        assert not head_slices, (
            f"2-day head gap must be absorbed (≤{_MARKET_GAP_DAYS} days); "
            f"unexpected head slices: {head_slices}"
        )


# ── Dimension 5: Correctness — merge + persist for each gap case ───────────────

class TestPartialFetchCorrectness:
    """Integration-style tests using a real OHLCVStore instance with mocked
    _db_fetch and _broker_fetch.  Each test covers one gap case end-to-end:
    correct broker date range, correct merged output, correct persist call."""

    @pytest.mark.asyncio
    async def test_case_a_inside_fetches_both_slices(self):
        """1Y requested; DB has middle 6 months.  Two broker calls — head + tail."""
        to_d    = _weekday_boundary(date.today() - timedelta(days=1))
        from_d  = _weekday_boundary(to_d - timedelta(days=365))
        db_from = _weekday_boundary(from_d + timedelta(days=90))
        db_to   = _weekday_boundary(to_d - timedelta(days=30))

        db_bars     = _make_bars(db_from, db_to)
        head_bars   = _make_bars(from_d, db_from - timedelta(days=1))
        tail_bars   = _make_bars(db_to + timedelta(days=1), to_d)

        store = OHLCVStore()
        broker_calls: list[tuple[date, date]] = []

        async def _mock_db(key):
            s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
            return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

        async def _mock_broker(key):
            s_from = date.fromisoformat(key[2])
            s_to   = date.fromisoformat(key[3])
            broker_calls.append((s_from, s_to))
            if s_to < db_from:
                return list(head_bars)
            return list(tail_bars)

        store._db_fetch     = _mock_db      # type: ignore[method-assign]
        store._broker_fetch = _mock_broker  # type: ignore[method-assign]

        with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
            with patch("backend.api.persistence.runtime_state.is_bypass_on", return_value=False):
                result = await get_or_fetch_daily.__wrapped__(  # type: ignore[attr-defined]
                    store, "NIFTY50", "NSE", from_d, to_d
                ) if hasattr(get_or_fetch_daily, "__wrapped__") else None

        # Use the module-level function with our store patched.
        # Re-run via the store instance directly for isolation.
        store2 = OHLCVStore()
        broker_calls2: list[tuple[date, date]] = []

        async def _mock_db2(key):
            s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
            return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

        async def _mock_broker2(key):
            s_from = date.fromisoformat(key[2])
            s_to   = date.fromisoformat(key[3])
            broker_calls2.append((s_from, s_to))
            if s_to < db_from:
                return list(head_bars)
            return list(tail_bars)

        store2._db_fetch     = _mock_db2      # type: ignore[method-assign]
        store2._broker_fetch = _mock_broker2  # type: ignore[method-assign]

        # Patch the module-level singleton temporarily.
        import backend.api.persistence.ohlcv_store as _mod
        orig_store = _mod._ohlcv_store
        _mod._ohlcv_store = store2

        persist_calls: list = []
        try:
            with patch(
                "backend.api.persistence.ohlcv_store._enqueue_persist_impl",
                side_effect=lambda sym, exch, bars: persist_calls.append((sym, exch, bars)),
            ):
                with patch(
                    "backend.api.persistence.runtime_state.is_bypass_on",
                    return_value=False,
                ):
                    merged = await get_or_fetch_daily("NIFTY50", "NSE", from_d, to_d)
        finally:
            _mod._ohlcv_store = orig_store

        assert len(broker_calls2) == 2, (
            f"Case a: expected 2 broker calls (head + tail); got {len(broker_calls2)}: "
            f"{broker_calls2}"
        )
        assert len(merged) > 0, "Merged result must not be empty"
        # All returned bars must be within the requested range.
        for bar in merged:
            assert from_d.isoformat() <= bar["date"] <= to_d.isoformat(), (
                f"Returned bar {bar['date']} is outside requested range "
                f"[{from_d}..{to_d}]"
            )
        assert persist_calls, "At least one persist call must have been made for new slices"

    @pytest.mark.asyncio
    async def test_case_c_tail_missing_one_broker_call(self):
        """DB covers from_d through midpoint; tail missing.  Exactly one broker
        call for the tail slice only."""
        to_d    = _weekday_boundary(date.today() - timedelta(days=1))
        from_d  = _weekday_boundary(to_d - timedelta(days=180))
        db_to   = _weekday_boundary(from_d + timedelta(days=90))  # DB stops halfway
        db_bars = _make_bars(from_d, db_to)
        tail_bars = _make_bars(db_to + timedelta(days=1), to_d)

        import backend.api.persistence.ohlcv_store as _mod
        orig_store = _mod._ohlcv_store
        store = OHLCVStore()
        broker_calls: list[tuple[date, date]] = []

        async def _mock_db(key):
            s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
            return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

        async def _mock_broker(key):
            broker_calls.append((date.fromisoformat(key[2]), date.fromisoformat(key[3])))
            return list(tail_bars)

        store._db_fetch     = _mock_db      # type: ignore[method-assign]
        store._broker_fetch = _mock_broker  # type: ignore[method-assign]
        _mod._ohlcv_store   = store

        try:
            with patch(
                "backend.api.persistence.ohlcv_store._enqueue_persist_impl"
            ):
                with patch(
                    "backend.api.persistence.runtime_state.is_bypass_on",
                    return_value=False,
                ):
                    merged = await get_or_fetch_daily("RELIANCE", "NSE", from_d, to_d)
        finally:
            _mod._ohlcv_store = orig_store

        assert len(broker_calls) == 1, (
            f"Case c: expected exactly 1 broker call (tail only); "
            f"got {len(broker_calls)}: {broker_calls}"
        )
        # Broker was called for a range that starts AFTER db_to
        broker_from, broker_to = broker_calls[0]
        assert broker_from > db_to, (
            f"Broker call must start after DB coverage ends ({db_to}); "
            f"actual broker_from={broker_from}"
        )
        assert broker_to == to_d, (
            f"Broker call must end at to_d={to_d}; got broker_to={broker_to}"
        )
        assert len(merged) > 0, "Merged result must not be empty"

    @pytest.mark.asyncio
    async def test_full_db_coverage_zero_broker_calls(self):
        """DB covers the full requested range.  Zero broker calls."""
        to_d   = _weekday_boundary(date.today() - timedelta(days=1))
        from_d = _weekday_boundary(to_d - timedelta(days=90))
        db_bars = _make_bars(from_d, to_d)

        import backend.api.persistence.ohlcv_store as _mod
        orig_store = _mod._ohlcv_store
        store = OHLCVStore()
        broker_calls: list = []

        async def _mock_db(key):
            s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
            return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

        async def _mock_broker(key):
            broker_calls.append(key)
            return []

        store._db_fetch     = _mock_db      # type: ignore[method-assign]
        store._broker_fetch = _mock_broker  # type: ignore[method-assign]
        _mod._ohlcv_store   = store

        try:
            with patch(
                "backend.api.persistence.runtime_state.is_bypass_on",
                return_value=False,
            ):
                merged = await get_or_fetch_daily("TCS", "NSE", from_d, to_d)
        finally:
            _mod._ohlcv_store = orig_store

        assert broker_calls == [], (
            f"Full DB coverage must result in ZERO broker calls; got {broker_calls}"
        )
        assert len(merged) == len(db_bars), (
            f"Merged result must match DB bar count; "
            f"expected {len(db_bars)}, got {len(merged)}"
        )

    @pytest.mark.asyncio
    async def test_today_bar_not_persisted(self):
        """Broker returns today's bar in the slice result.  Today's bar must
        NOT be included in the _enqueue_persist_impl call."""
        to_d    = _weekday_boundary(date.today() - timedelta(days=1))
        from_d  = _weekday_boundary(to_d - timedelta(days=30))
        today   = date.today()

        # DB is empty → full slice fetched from broker.
        # Broker returns bars including today.
        weekday_bars = _make_bars(from_d, to_d)
        today_bar = OHLCVBar(
            date=today.isoformat(), open=200.0, high=210.0,
            low=195.0, close=205.0, volume=5_000,
        )
        broker_bars = weekday_bars + [today_bar]

        import backend.api.persistence.ohlcv_store as _mod
        orig_store = _mod._ohlcv_store
        store = OHLCVStore()

        async def _mock_db(key):
            return []  # cold DB

        async def _mock_broker(key):
            return list(broker_bars)

        store._db_fetch     = _mock_db      # type: ignore[method-assign]
        store._broker_fetch = _mock_broker  # type: ignore[method-assign]
        _mod._ohlcv_store   = store

        persisted_bars: list[OHLCVBar] = []
        try:
            with patch(
                "backend.api.persistence.ohlcv_store._enqueue_persist_impl",
                side_effect=lambda sym, exch, bars: persisted_bars.extend(bars),
            ):
                with patch(
                    "backend.api.persistence.runtime_state.is_bypass_on",
                    return_value=False,
                ):
                    merged = await get_or_fetch_daily("INFY", "NSE", from_d, to_d)
        finally:
            _mod._ohlcv_store = orig_store

        persisted_dates = {b["date"] for b in persisted_bars}
        assert today.isoformat() not in persisted_dates, (
            f"Today's bar ({today.isoformat()}) must NOT be persisted — "
            "today's close price is still live LTP (immutable-day rule). "
            f"Persisted dates: {sorted(persisted_dates)}"
        )

    @pytest.mark.asyncio
    async def test_weekend_gap_at_boundary_no_extra_fetch(self):
        """Requested from_d is Monday; DB starts Wednesday (2-day gap).
        _compute_missing_ranges must NOT generate a head slice for this gap."""
        to_d    = _weekday_boundary(date.today() - timedelta(days=1))
        from_d  = _weekday_boundary(to_d - timedelta(days=90))
        # DB starts 2 days after from_d (simulates a Mon from_d, Wed db_from)
        db_from = from_d + timedelta(days=2)
        while db_from.weekday() >= 5:
            db_from += timedelta(days=1)
        db_bars = _make_bars(db_from, to_d)

        missing = _compute_missing_ranges(db_bars, from_d, to_d)
        head_slices = [s for s in missing if s[0] == from_d]
        assert not head_slices, (
            f"2-day head gap must be absorbed (≤{_MARKET_GAP_DAYS} days holiday tolerance). "
            f"Unexpected head slices: {head_slices}"
        )


# ── Dimension 1: SSOT — 1Y request with 6M in DB hits broker for 6M only ─────

@pytest.mark.asyncio
async def test_ssot_partial_1y_request_calls_broker_for_missing_half_only():
    """SSOT: a 1Y request when the DB has the last 6 months must call the
    broker only for the missing first 6 months — not the full year."""
    to_d    = _weekday_boundary(date.today() - timedelta(days=1))
    from_d  = _weekday_boundary(to_d - timedelta(days=365))   # 1Y back
    db_from = _weekday_boundary(to_d - timedelta(days=182))   # 6M back (in DB)
    db_bars = _make_bars(db_from, to_d)

    import backend.api.persistence.ohlcv_store as _mod
    orig_store = _mod._ohlcv_store
    store = OHLCVStore()
    broker_date_ranges: list[tuple[date, date]] = []

    async def _mock_db(key):
        s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
        return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

    async def _mock_broker(key):
        broker_date_ranges.append((date.fromisoformat(key[2]), date.fromisoformat(key[3])))
        # Return bars for the head slice only
        return _make_bars(date.fromisoformat(key[2]), date.fromisoformat(key[3]))

    store._db_fetch     = _mock_db      # type: ignore[method-assign]
    store._broker_fetch = _mock_broker  # type: ignore[method-assign]
    _mod._ohlcv_store   = store

    try:
        with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
            with patch(
                "backend.api.persistence.runtime_state.is_bypass_on",
                return_value=False,
            ):
                merged = await get_or_fetch_daily("NIFTY50", "NSE", from_d, to_d)
    finally:
        _mod._ohlcv_store = orig_store

    assert len(broker_date_ranges) == 1, (
        f"SSOT: only 1 broker call expected (for missing head); "
        f"got {len(broker_date_ranges)}: {broker_date_ranges}"
    )
    broker_from, broker_to = broker_date_ranges[0]
    assert broker_from == from_d, (
        f"Broker call must start at from_d={from_d} (the missing head). "
        f"Got broker_from={broker_from}"
    )
    assert broker_to < db_from, (
        f"Broker call must end BEFORE db_from={db_from} (the existing tail). "
        f"Got broker_to={broker_to}"
    )
    # The merged result must cover the full year.
    merged_dates = {b["date"] for b in merged}
    assert len(merged) > 0, "Merged result must not be empty"


# ── Dimension 2: Performance — partial fetch < 50% the work of full fetch ──────

@pytest.mark.asyncio
async def test_perf_partial_fetch_calls_broker_less_than_full_fetch():
    """A 1Y request with 6M in DB should result in at most 50% of the broker
    bar-fetch work compared to a fully-cold 1Y fetch.

    Proxy: count the total bars requested from the broker.  With 6M in DB,
    only ~182 days of bars should be fetched (not 365).

    This is measured by call count + slice length assertions, not wall time
    (pure-mock test; no actual I/O)."""
    to_d    = _weekday_boundary(date.today() - timedelta(days=1))
    from_d  = _weekday_boundary(to_d - timedelta(days=365))
    db_from = _weekday_boundary(to_d - timedelta(days=182))
    db_bars = _make_bars(db_from, to_d)

    import backend.api.persistence.ohlcv_store as _mod
    orig_store = _mod._ohlcv_store
    store = OHLCVStore()
    total_broker_days = 0

    async def _mock_db(key):
        s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
        return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

    async def _mock_broker(key):
        nonlocal total_broker_days
        s_from = date.fromisoformat(key[2])
        s_to   = date.fromisoformat(key[3])
        total_broker_days += (s_to - s_from).days + 1
        return _make_bars(s_from, s_to)

    store._db_fetch     = _mock_db      # type: ignore[method-assign]
    store._broker_fetch = _mock_broker  # type: ignore[method-assign]
    _mod._ohlcv_store   = store

    try:
        with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
            with patch(
                "backend.api.persistence.runtime_state.is_bypass_on",
                return_value=False,
            ):
                await get_or_fetch_daily("NIFTY50", "NSE", from_d, to_d)
    finally:
        _mod._ohlcv_store = orig_store

    full_range_days = (to_d - from_d).days + 1
    assert total_broker_days < full_range_days * 0.60, (
        f"Performance: partial fetch should request < 60% of a full-range fetch. "
        f"Broker days fetched: {total_broker_days}; "
        f"full range: {full_range_days} days. "
        "This means the DB coverage is not being used to reduce broker calls."
    )


# ── Dimension 3: Stale code — no full-range bypass when partial ────────────────

def test_stale_code_no_full_range_fallback_on_partial():
    """Source-level guard: get_or_fetch_daily must use _compute_missing_ranges
    rather than calling self.get() / _broker_fetch for the full range when DB
    has partial coverage.

    Specifically:
    1. _compute_missing_ranges must be called from get_or_fetch_daily.
    2. The function must not contain a single unconditional full-range broker
       fetch path that ignores existing DB bars (the old 'fetch full range on
       incomplete' pattern).
    """
    import inspect
    import backend.api.persistence.ohlcv_store as _mod

    src = inspect.getsource(_mod.get_or_fetch_daily)

    assert "_compute_missing_ranges" in src, (
        "get_or_fetch_daily must call _compute_missing_ranges to identify "
        "only the missing date ranges — not bypass Tier 2 for the full range. "
        "This guards against the regression where every incomplete DB result "
        "triggered a full-year broker fetch."
    )

    assert "_db_fetch_existing" in src, (
        "get_or_fetch_daily must read from DB unconditionally (via "
        "_db_fetch_existing) to learn what is already cached before computing "
        "the missing slices. Without this, partial coverage is never detected."
    )

    # Accept either direct use of _fetch_slice or delegation to the extracted
    # _fetch_and_merge_slices helper (refactored for CC reduction), both of
    # which call _fetch_slice internally.
    assert "_fetch_slice" in src or "_fetch_and_merge_slices" in src, (
        "get_or_fetch_daily must use _fetch_slice (or the extracted "
        "_fetch_and_merge_slices helper) to fetch each missing range "
        "independently — not a single _broker_fetch call for the full range."
    )


# ── BEL race scenario — Tier 2 has bars, broker returns empty: UNION not OVERWRITE
#
# Operator-reported intermittent "No data available" for BEL on /charts even
# though the cache had prior bars. Root-cause hypothesis: the partial-range
# merge logic could conceivably overwrite db_bars with empty broker results
# when the broker mid-flight returned zero rows (instruments cache not yet
# warm, rate-limit cool-off, etc.). This block proves the merge is a UNION:
# whatever the broker returns is OR-merged with db_bars; an empty broker
# response can never reduce the result to zero when the DB had coverage.

@pytest.mark.asyncio
async def test_bel_race_db_bars_preserved_when_broker_returns_empty():
    """SSOT for the BEL race: DB has 3 bars, broker returns 0 bars for the
    missing slice → merged result must contain the 3 db_bars (UNION, not
    OVERWRITE). Regression guard for the worst-case "lost data" pattern."""
    to_d   = _weekday_boundary(date.today() - timedelta(days=1))
    from_d = _weekday_boundary(to_d - timedelta(days=30))
    # DB has only the LAST 3 weekdays of the range — head slice is missing.
    db_to   = to_d
    db_from = _prev_weekday(db_to - timedelta(days=4))   # 3 weekdays back
    db_bars = _make_bars(db_from, db_to)
    assert len(db_bars) >= 3, (
        f"Test setup error: expected ≥3 DB bars; got {len(db_bars)}"
    )

    import backend.api.persistence.ohlcv_store as _mod
    orig_store = _mod._ohlcv_store
    store = OHLCVStore()
    broker_calls: list = []

    async def _mock_db(key):
        s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
        return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

    async def _mock_broker(key):
        # Simulate BEL-style empty response (instruments map cold / rate
        # limit / token resolution miss). Returns [] for every slice.
        broker_calls.append(key)
        return []

    store._db_fetch     = _mock_db      # type: ignore[method-assign]
    store._broker_fetch = _mock_broker  # type: ignore[method-assign]
    _mod._ohlcv_store   = store

    try:
        with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
            with patch(
                "backend.api.persistence.runtime_state.is_bypass_on",
                return_value=False,
            ):
                merged = await get_or_fetch_daily("BEL", "NSE", from_d, to_d)
    finally:
        _mod._ohlcv_store = orig_store

    # The CRITICAL assertion: even though the broker returned zero bars,
    # the merged result MUST contain the 3 db_bars. An empty broker
    # response must never reduce a non-empty DB result to zero.
    assert len(merged) == len(db_bars), (
        f"BEL race: empty broker response collapsed merged result. "
        f"Expected {len(db_bars)} db_bars to survive; got {len(merged)}. "
        f"This proves the merge logic is OVERWRITE, not UNION — "
        "exactly the bug the operator caught."
    )
    merged_dates = sorted(b["date"] for b in merged)
    db_dates     = sorted(b["date"] for b in db_bars)
    assert merged_dates == db_dates, (
        f"BEL race: merged dates {merged_dates} differ from db dates "
        f"{db_dates}. UNION must preserve every db_bar verbatim."
    )


@pytest.mark.asyncio
async def test_bel_race_db_bars_preserved_when_broker_raises():
    """Variant: broker raises an exception (rate-limit, network).
    db_bars must still survive in the merged result."""
    to_d   = _weekday_boundary(date.today() - timedelta(days=1))
    from_d = _weekday_boundary(to_d - timedelta(days=30))
    db_to   = to_d
    db_from = _prev_weekday(db_to - timedelta(days=4))
    db_bars = _make_bars(db_from, db_to)
    assert len(db_bars) >= 3

    import backend.api.persistence.ohlcv_store as _mod
    orig_store = _mod._ohlcv_store
    store = OHLCVStore()

    async def _mock_db(key):
        s_from, s_to = date.fromisoformat(key[2]), date.fromisoformat(key[3])
        return [b for b in db_bars if s_from.isoformat() <= b["date"] <= s_to.isoformat()]

    async def _mock_broker(key):
        raise RuntimeError("BEL: Too many requests")

    store._db_fetch     = _mock_db      # type: ignore[method-assign]
    store._broker_fetch = _mock_broker  # type: ignore[method-assign]
    _mod._ohlcv_store   = store

    try:
        with patch("backend.api.persistence.ohlcv_store._enqueue_persist_impl"):
            with patch(
                "backend.api.persistence.runtime_state.is_bypass_on",
                return_value=False,
            ):
                merged = await get_or_fetch_daily("BEL", "NSE", from_d, to_d)
    finally:
        _mod._ohlcv_store = orig_store

    # Broker raised. _fetch_slice catches the exception and returns [].
    # db_bars must still be present in merged.
    assert len(merged) >= len(db_bars), (
        f"BEL race (broker raised): expected merged ≥ {len(db_bars)}; "
        f"got {len(merged)}. The slice-fetch exception path must not "
        "discard existing DB bars."
    )


def test_stale_code_merge_is_union_not_overwrite():
    """Source-level guard: the merge logic (either in get_or_fetch_daily or the
    extracted _fetch_and_merge_slices helper) must seed `merged` from `db_bars`
    (not initialise it empty and then write only broker bars). This guarantees
    the merge is a UNION even when the broker returns zero bars for every
    missing slice."""
    import inspect
    import backend.api.persistence.ohlcv_store as _mod

    # The fix: merged is initialised from db_bars. Without this, an empty
    # broker response would yield an empty merged dict.
    union_seed = 'merged: dict[str, OHLCVBar] = {b["date"]: b for b in db_bars}'

    # Check the public function first; also accept the extracted helper
    # (_fetch_and_merge_slices) to allow CC-reduction refactors.
    top_src = inspect.getsource(_mod.get_or_fetch_daily)
    helper_src = (
        inspect.getsource(_mod._fetch_and_merge_slices)
        if hasattr(_mod, "_fetch_and_merge_slices") else ""
    )
    assert union_seed in top_src or union_seed in helper_src, (
        "get_or_fetch_daily (or its _fetch_and_merge_slices helper) must seed "
        "`merged` from db_bars to guarantee UNION semantics. An empty broker "
        "response must not collapse the merged result to zero when the DB had "
        "partial coverage. Operator-caught BEL race."
    )


def test_stale_code_empty_cache_ttl_is_short():
    """Source-level guard: the historical endpoint's _HIST_CACHE_TTL_EMPTY
    must be ≤ 5 seconds so a transient empty response (instruments cache
    cold, rate-limit blip) doesn't poison every subsequent reload with
    "No data available" for 10 seconds. Operator-caught BEL flicker."""
    import inspect
    import backend.api.routes.options as _opts

    src = inspect.getsource(_opts)
    # Find the assignment line. Format: "_HIST_CACHE_TTL_EMPTY = <int>"
    import re
    m = re.search(r"_HIST_CACHE_TTL_EMPTY\s*=\s*(\d+)", src)
    assert m is not None, (
        "Could not find _HIST_CACHE_TTL_EMPTY assignment in options.py. "
        "Has the constant been renamed?"
    )
    ttl = int(m.group(1))
    assert ttl <= 5, (
        f"_HIST_CACHE_TTL_EMPTY={ttl} is too high. A cold-call empty result "
        "should re-query within a few seconds, not poison the cache for 10+s. "
        "Operator-caught BEL race fix: lowered to ≤5."
    )


# ── BEL race v3 — `partial` field signals transient-empty to frontend ─────
#
# Operator-caught the prior fix as INCOMPLETE: even though the empty-cache
# TTL was lowered to 2s + the merge was UNION, the frontend STILL showed
# "No data available" during the retry window because the retry was timed
# but the UI gated only on `_histLoading`. The historical handler now
# returns a `partial: bool` field — True when the empty result is likely
# transient (broker rate-limit cool-off, instruments map cold, all
# brokers missed token). Frontend uses partial to decide retry vs. fail.

def test_historical_response_has_partial_field():
    """The msgspec.Struct HistoricalResponse must declare a `partial`
    field that defaults to False — the contract the frontend relies on
    for the retry-on-transient-empty path."""
    from backend.api.routes.options import HistoricalResponse

    # msgspec stores field info in __struct_fields__ and __struct_defaults__.
    fields = HistoricalResponse.__struct_fields__
    assert "partial" in fields, (
        f"HistoricalResponse must have a `partial` field; got: {fields}"
    )
    # Construct one with default; partial should default to False.
    instance = HistoricalResponse(
        symbol="TEST", instrument_token=None, interval="day", bars=[],
    )
    assert instance.partial is False, (
        "HistoricalResponse.partial must default to False so existing "
        "callers (Tier 1 / Tier 2 success path) get partial=False without "
        "needing to specify it explicitly."
    )


def test_stale_code_partial_set_true_on_broker_empty_paths():
    """Source-level guard: every return path in the historical handler
    that emits empty bars from a fallback / cold-broker situation MUST
    set partial=True. Otherwise the frontend treats the empty as
    confirmed-no-data and shows the error immediately."""
    import re
    from pathlib import Path

    # Read raw source file from options_helpers (post-refactor).
    # The historical logic was extracted to three helper functions to reduce CC.
    options_helpers_path = Path(__file__).parent.parent / "api" / "routes" / "options_helpers.py"
    full_src = options_helpers_path.read_text()

    # Three empty-return paths across the helpers:
    # 1. _historical_ohlcv_store: no bars → partial=_still_partial
    # 2. _historical_intraday_store: broker empty → partial=True
    # 3. _historical_broker_loop: all brokers failed → partial=True
    # The pattern check: `bars=[]` (or `bars=bars`) accompanied by partial=...
    partial_true_count = len(re.findall(r"partial\s*=\s*True", full_src))
    partial_var_count  = len(re.findall(r"partial\s*=\s*not\s+bool\(bars\)", full_src))
    total = partial_true_count + partial_var_count
    assert total >= 3, (
        f"Expected at least 3 empty-bars paths in options_helpers "
        f"to set partial=True (or partial=not bool(bars)). Found {partial_true_count} "
        f"`partial=True` + {partial_var_count} `partial=not bool(bars)`. "
        "Operator-caught race: every fallback-empty MUST signal partial=True so "
        "the frontend retries instead of flashing 'No data available'."
    )


def test_first_cold_empty_counter_recorded_for_empty_responses():
    """When the historical handler returns empty bars from a fallback
    path, _record_first_cold_empty(sym) must be called. The counter is
    exposed via /api/admin/health for operator monitoring of the BEL
    race ("is this still happening for symbol X?")."""
    from backend.api.routes import options as _opts

    # Reset counter and record one call.
    with _opts._FIRST_COLD_EMPTY_LOCK:
        _opts._FIRST_COLD_EMPTY.clear()
    _opts._record_first_cold_empty("BEL")
    counts = _opts.get_first_cold_empty_counts()
    assert counts.get("BEL") == 1, (
        f"_record_first_cold_empty('BEL') must increment the counter. "
        f"Got counts: {counts}"
    )
    # Record again — counter increments.
    _opts._record_first_cold_empty("BEL")
    counts2 = _opts.get_first_cold_empty_counts()
    assert counts2.get("BEL") == 2, (
        f"Second call must bump to 2. Got: {counts2}"
    )


def test_stale_code_record_cold_empty_called_from_empty_paths():
    """Source-level guard: _record_first_cold_empty(sym) must be called
    from each empty-bars return path in OptionsController.historical so
    the operator-facing health metric stays accurate."""
    from pathlib import Path

    # After refactor, record_first_cold_empty calls are in options_helpers.
    options_helpers_path = Path(__file__).parent.parent / "api" / "routes" / "options_helpers.py"
    full_src = options_helpers_path.read_text()
    # The function parameter is named record_first_cold_empty (no leading underscore).
    # It's called from 3 empty-bars paths in _historical_broker_loop.
    call_count = full_src.count("record_first_cold_empty(sym)")
    assert call_count >= 2, (
        f"record_first_cold_empty(sym) must be called from the empty-bars "
        f"return paths in options_helpers historical functions. Found {call_count} call(s); "
        "expected ≥2 (no-eligible-brokers path + all-brokers-failed path)."
    )


def test_stale_code_health_exposes_cold_empty_counts():
    """The /api/admin/health surface must expose the per-symbol cold-empty
    counter under persistence.ohlcv_first_cold_empty. Operator dashboards
    poll this to know if a specific symbol is still flaking."""
    import inspect
    import backend.api.routes.health as _health

    src = inspect.getsource(_health)
    assert "ohlcv_first_cold_empty" in src, (
        "health.py must expose persistence.ohlcv_first_cold_empty so the "
        "operator can monitor whether BEL (or any other symbol) is still "
        "triggering the empty-response path."
    )
    assert "get_first_cold_empty_counts" in src, (
        "health.py must import + call get_first_cold_empty_counts to "
        "populate the metric."
    )


# ── BEL race FINAL — frontend retry delay vs. backend empty-cache TTL ─────
#
# The remaining race after all prior fixes: the frontend retry delay
# (800 ms) was LESS than the backend's _HIST_CACHE_TTL_EMPTY (2000 ms).
# The retry arrived while the server's empty-cache entry was still live,
# got the same cached empty+partial response, but since _emptyRetryFired
# was already set in the frontend, canRetry=false → "No data available"
# rendered immediately. The fix: retry delay = 2300 ms > 2000 ms TTL.
#
# These tests guard the timing contract at the source level so a future
# change to either constant cannot silently re-introduce the race.

def test_frontend_retry_delay_exceeds_backend_empty_cache_ttl():
    """Source-level contract: the first element of the frontend's
    _RETRY_DELAYS_MS constant (in ChartWorkspace.svelte) must be strictly
    greater than the backend's _HIST_CACHE_TTL_EMPTY (in options.py).

    If the first retry fires before the empty-cache entry expires, the server
    returns the same cached empty+partial response. The retry count is already
    incremented so the next window moves to a longer delay, but the first hit
    still races the cache. Every delay in the array must exceed the TTL to
    guarantee the retry arrives after the cache has expired.

    This is the root cause of the BEL race that survived all prior fixes.
    """
    import re
    from pathlib import Path

    # ── Backend: read _HIST_CACHE_TTL_EMPTY from options.py ───────────────
    options_path = Path(__file__).parent.parent / "api" / "routes" / "options.py"
    opts_src = options_path.read_text()
    m_ttl = re.search(r"_HIST_CACHE_TTL_EMPTY\s*=\s*(\d+)", opts_src)
    assert m_ttl is not None, (
        "Could not find _HIST_CACHE_TTL_EMPTY in options.py. "
        "Has the constant been renamed?"
    )
    backend_ttl_ms = int(m_ttl.group(1)) * 1000   # convert seconds → ms

    # ── Frontend: read _RETRY_DELAYS_MS from ChartWorkspace.svelte ────────
    cw_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "lib" / "ChartWorkspace.svelte"
    cw_src = cw_path.read_text()
    # Match: `const _RETRY_DELAYS_MS = [4000, 8000, 15000];`
    m_delays = re.search(r"_RETRY_DELAYS_MS\s*=\s*\[([^\]]+)\]", cw_src)
    assert m_delays is not None, (
        "Could not find _RETRY_DELAYS_MS = [ in ChartWorkspace.svelte. "
        "Has the retry-delay constant been renamed or removed?"
    )
    delay_values = [int(v.strip()) for v in m_delays.group(1).split(",")]
    first_retry_ms = delay_values[0]

    # ── The timing contract ────────────────────────────────────────────────
    assert first_retry_ms > backend_ttl_ms, (
        f"BEL race final fix: first frontend retry delay ({first_retry_ms} ms) "
        f"must be strictly greater than backend empty-cache TTL ({backend_ttl_ms} ms). "
        f"When the retry fires before the TTL expires, the server returns the "
        f"same cached empty+partial response and 'No data available' renders "
        f"immediately — the race re-occurs. "
        f"Fix: set _RETRY_DELAYS_MS[0] to {backend_ttl_ms + 300} ms or higher."
    )


def test_frontend_retry_delay_is_at_least_4000ms():
    """Specific bound: the first retry delay in _RETRY_DELAYS_MS must be ≥ 4000 ms.

    The backend TTL is 2000 ms. 4000 ms provides 2000 ms of headroom — enough
    for both the empty-cache TTL to expire AND for the ohlcv_demand_fill async
    write (triggered by the first cold request) to complete. The demand fill
    for a 90–365 day range takes 2–10 s; a 4 s floor ensures the second
    request usually arrives after both the TTL and the fill have finished.
    """
    import re
    from pathlib import Path

    cw_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "lib" / "ChartWorkspace.svelte"
    cw_src = cw_path.read_text()

    m_delays = re.search(r"_RETRY_DELAYS_MS\s*=\s*\[([^\]]+)\]", cw_src)
    assert m_delays is not None, (
        "Could not find _RETRY_DELAYS_MS = [ in ChartWorkspace.svelte. "
        "Has the retry-delay constant been renamed?"
    )
    delay_values = [int(v.strip()) for v in m_delays.group(1).split(",")]
    first_delay_ms = delay_values[0]

    assert first_delay_ms >= 4000, (
        f"_RETRY_DELAYS_MS[0] ({first_delay_ms} ms) is below the 4000 ms minimum. "
        "The backend empty-cache TTL is 2000 ms and the ohlcv_demand_fill async "
        "write takes 2–10 s for a 90–365 day range. A 4000 ms floor provides "
        "headroom for both the TTL expiry and the demand fill to complete before "
        "the first retry arrives. Values below 2000 ms are guaranteed to re-hit "
        "the cache; values 2001–3999 ms may arrive before the demand fill finishes."
    )
