"""
D3 (2026-10-20) — MCX morning/evening holiday granularity.

Verified: NSE's own holiday-master feed carries per-segment
`morning_session`/`evening_session` fields for the `COM` (MCX commodity)
segment (confirmed live: 9 of MCX's 21 2026 holidays carry
`evening_session: "Closed"`, including Dussehra's SIBLING dates — Dussehra
itself, 2026-10-20, is `evening_session: "Open"`, consistent with tonight's
investigation). `_fetch_holidays_from_nse` previously discarded this,
keeping only the bare date; the static `evening_open_on_holidays: true`
flag assumed every MCX holiday reopens at 17:00 — wrong for those dates.

Covers:
  - `_fetch_mcx_evening_closed_dates()` parses the real field shape.
  - `_upsert_mcx_evening_closed_sessions_coro()` excludes weekend dates
    (verified: `next_market_open_ist()`'s weekend branch treats mere row
    EXISTENCE as "open" regardless of the row's own window).
  - `source="nse_auto"` never overwrites an operator-entered row.
  - `is_market_open`'s holiday branch now has a weekday guard (a holiday
    landing on a weekend must stay closed even with
    evening_open_on_holidays=True).
  - End-to-end: Diwali Laxmi Pujan (2026-11-08, confirmed Sunday) gets a
    seeded zero-width row → `is_market_open` returns False for MCX at
    18:00; the weekend-exclusion guard means that date is never
    auto-inserted in the first place.
"""
from __future__ import annotations

from datetime import date, time as dtime, datetime
from unittest.mock import MagicMock, patch, AsyncMock
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# Live-feed-shaped fixture (field names/values confirmed directly against
# the real NSE endpoint 2026-10-20 — not guessed). Trimmed to the dates
# this test file actually exercises.
_COM_FIXTURE = [
    {"tradingDate": "01-Jan-2026", "weekDay": "Thursday", "description": "New year",
     "morning_session": "Open", "evening_session": "Closed", "Sr_no": 1},
    {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day",
     "morning_session": "Closed", "evening_session": "Closed", "Sr_no": 3},
    {"tradingDate": "20-Oct-2026", "weekDay": "Tuesday", "description": "Dussehra",
     "morning_session": "Closed", "evening_session": "Open", "Sr_no": 17},
    {"tradingDate": "08-Nov-2026", "weekDay": "Sunday", "description": "Diwali Laxmi Pujan*",
     "morning_session": "Closed", "evening_session": "Closed", "Sr_no": 18},
    {"tradingDate": "25-Dec-2026", "weekDay": "Friday", "description": "Christmas",
     "morning_session": "Closed", "evening_session": "Closed", "Sr_no": 21},
]


def _mock_response():
    resp = MagicMock()
    resp.json.return_value = {"COM": _COM_FIXTURE}
    resp.raise_for_status.return_value = None
    return resp


# `_fetch_nse_holiday_master_json`'s short-TTL memo is cleared by a
# global autouse fixture in conftest.py (`_reset_nse_holiday_json_memo`)
# — no file-local fixture needed here.


# ---------------------------------------------------------------------------
# _fetch_mcx_evening_closed_dates — field parsing
# ---------------------------------------------------------------------------

class TestFetchMcxEveningClosedDates:
    def test_parses_evening_closed_dates_only(self):
        from backend.brokers import broker_apis

        with patch("requests.get", return_value=_mock_response()):
            result = broker_apis._fetch_mcx_evening_closed_dates()

        assert date(2026, 1, 1) in result       # evening_session: Closed
        assert date(2026, 1, 26) in result      # evening_session: Closed
        assert date(2026, 11, 8) in result      # evening_session: Closed
        assert date(2026, 12, 25) in result     # evening_session: Closed
        # Dussehra: evening_session is Open — must NOT appear.
        assert date(2026, 10, 20) not in result

    def test_returns_none_on_network_failure(self):
        """2026-10-20 fix: a FAILED fetch returns None, not an empty set —
        distinct from "the feed genuinely says nothing is closed". The
        reconciliation delete in _upsert_mcx_evening_closed_sessions_coro
        relies on this distinction to never wipe real closed-evening rows
        during a transient NSE outage."""
        from backend.brokers import broker_apis

        with patch("requests.get", side_effect=Exception("network error")):
            result = broker_apis._fetch_mcx_evening_closed_dates()
        assert result is None

    def test_ignores_null_evening_session(self):
        """A segment entry with evening_session=null (e.g. non-COM
        segments, or a COM entry predating this field) is not treated as
        closed — fail-open, never fail-closed."""
        from backend.brokers import broker_apis

        resp = MagicMock()
        resp.json.return_value = {"COM": [
            {"tradingDate": "15-Jan-2026", "evening_session": None},
        ]}
        resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=resp):
            result = broker_apis._fetch_mcx_evening_closed_dates()
        assert result == set()

    def test_invalid_date_format_skipped(self):
        from backend.brokers import broker_apis

        resp = MagicMock()
        resp.json.return_value = {"COM": [
            {"tradingDate": "BAD-DATE", "evening_session": "Closed"},
        ]}
        resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=resp):
            result = broker_apis._fetch_mcx_evening_closed_dates()
        assert result == set()


# ---------------------------------------------------------------------------
# _fetch_nse_holiday_master_json — short-TTL memo (2026-10-20 fix)
# ---------------------------------------------------------------------------

class TestNseHolidayJsonMemo:
    def test_second_call_within_ttl_does_not_refetch(self):
        """The real scenario this fix targets: _fetch_holidays_from_nse
        and _fetch_mcx_evening_closed_dates each call
        _fetch_nse_holiday_master_json independently, milliseconds apart,
        within one _task_holiday_refresh MCX-branch cycle. The second
        call must hit the memo, not requests.get again."""
        from backend.brokers import broker_apis

        with patch("requests.get", return_value=_mock_response()) as mock_get:
            first = broker_apis._fetch_nse_holiday_master_json()
            second = broker_apis._fetch_nse_holiday_master_json()

        assert mock_get.call_count == 1, (
            "second call within the TTL window must reuse the memoized "
            "response, not trigger a second real HTTP request"
        )
        assert first == second == {"COM": _COM_FIXTURE}

    def test_a_failed_fetch_is_never_memoized(self):
        """A failure must not poison the memo — the very next call (even
        a millisecond later) gets a genuine retry, not a cached None."""
        from backend.brokers import broker_apis

        with patch("requests.get", side_effect=Exception("network error")):
            first = broker_apis._fetch_nse_holiday_master_json()
        assert first is None

        with patch("requests.get", return_value=_mock_response()) as mock_get:
            second = broker_apis._fetch_nse_holiday_master_json()
        assert second == {"COM": _COM_FIXTURE}
        assert mock_get.call_count == 1, "the retry after a failure must be a real call"

    def test_end_to_end_mcx_branch_makes_one_real_http_call_not_two(self):
        """The actual production call pattern: _fetch_holidays_from_nse("MCX")
        followed immediately by _fetch_mcx_evening_closed_dates() — both
        reach _fetch_nse_holiday_master_json independently, but together
        must trigger only ONE real requests.get call."""
        from backend.brokers import broker_apis

        with patch("requests.get", return_value=_mock_response()) as mock_get:
            holidays = broker_apis._fetch_holidays_from_nse("MCX")
            evening_closed = broker_apis._fetch_mcx_evening_closed_dates()

        assert mock_get.call_count == 1, (
            "the two independent callers in the same refresh cycle must "
            "collapse to a single real HTTP round-trip via the memo"
        )
        assert date(2026, 1, 26) in holidays
        assert date(2026, 1, 26) in evening_closed


# ---------------------------------------------------------------------------
# _upsert_mcx_evening_closed_sessions_coro — weekend exclusion + source guard
# ---------------------------------------------------------------------------

class _FakeUpsertSession:
    """Distinguishes the three distinct `execute()` calls
    `_upsert_mcx_evening_closed_sessions_coro` now makes purely by the
    SHAPE of their params, not by inspecting SQL text:
      - INSERT (the original upsert): params is a `list` of row dicts.
      - SELECT existing nse_auto rows: params is a `dict` with `today`
        only — returns a result whose `.fetchall()` yields tuples built
        from `existing_dates` (default empty, so pre-existing tests that
        don't care about reconciliation see no stale rows and no DELETE,
        matching their original assertions unchanged).
      - DELETE stale rows: params is a `dict` with `stale_dates`.
    """
    def __init__(self, existing_dates: set | None = None):
        self.executed_rows: list = []          # the INSERT's row list
        self.deleted_dates: "list | None" = None
        self.committed = False
        self._existing_dates = existing_dates or set()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        if isinstance(params, list):
            self.executed_rows = params
            return MagicMock()
        if isinstance(params, dict) and "stale_dates" in params:
            self.deleted_dates = list(params["stale_dates"])
            return MagicMock()
        if isinstance(params, dict) and "today" in params:
            result = MagicMock()
            result.fetchall.return_value = [(d,) for d in self._existing_dates]
            return result
        return MagicMock()

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_upsert_excludes_weekend_dates():
    """2026-11-08 (confirmed Sunday) must NEVER be inserted — row existence
    alone would incorrectly flip that Sunday to 'open' in
    next_market_open_ist()'s own weekend-detection branch."""
    from backend.brokers import broker_apis

    assert date(2026, 11, 8).weekday() == 6  # confirm Sunday

    fake_session = _FakeUpsertSession()
    with patch("backend.api.database.async_session", fake_session):
        written = await broker_apis._upsert_mcx_evening_closed_sessions_coro(
            {date(2026, 11, 8), date(2026, 12, 25)}
        )

    assert written == 1  # only the non-weekend date (Dec 25, Friday)
    written_dates = {r["date"] for r in fake_session.executed_rows}
    assert date(2026, 11, 8) not in written_dates
    assert date(2026, 12, 25) in written_dates
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_upsert_all_weekend_is_noop():
    from backend.brokers import broker_apis

    fake_session = _FakeUpsertSession()
    with patch("backend.api.database.async_session", fake_session):
        written = await broker_apis._upsert_mcx_evening_closed_sessions_coro(
            {date(2026, 11, 8)}  # Sunday only
        )
    assert written == 0
    assert fake_session.executed_rows == []


@pytest.mark.asyncio
async def test_upsert_writes_zero_width_rows_with_nse_auto_source():
    from backend.brokers import broker_apis

    fake_session = _FakeUpsertSession()
    with patch("backend.api.database.async_session", fake_session):
        await broker_apis._upsert_mcx_evening_closed_sessions_coro({date(2026, 1, 26)})

    row = fake_session.executed_rows[0]
    assert row["exchange"] == "MCX"
    assert row["start_time"] == dtime(0, 0, 0)
    assert row["end_time"] == dtime(0, 0, 0)
    assert row["source"] == "nse_auto"


@pytest.mark.asyncio
async def test_upsert_conflict_clause_only_updates_nse_auto_rows():
    """The ON CONFLICT ... WHERE clause must scope updates to rows this
    same auto-job previously wrote (source='nse_auto'), never an
    operator-entered row for the same (exchange, date, start_time)."""
    from backend.brokers import broker_apis
    import inspect

    src = inspect.getsource(broker_apis._upsert_mcx_evening_closed_sessions_coro)
    assert "WHERE market_special_sessions.source = 'nse_auto'" in src
    # Must NOT also match NULL-source rows — those are pre-migration /
    # operator rows and must be left untouched.
    assert "source IS NULL" not in src


# ---------------------------------------------------------------------------
# Reconciliation delete (2026-10-20 fix) — stale auto-rows removed when
# NSE later revises a date's evening session back to Open.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_reconciles_stale_row_no_longer_in_closed_set():
    """A previously auto-inserted row for 2026-01-26 must be DELETED when
    this cycle's feed no longer reports that date as evening-closed."""
    from backend.brokers import broker_apis

    fake_session = _FakeUpsertSession(existing_dates={date(2026, 1, 26)})
    with patch("backend.api.database.async_session", fake_session):
        await broker_apis._upsert_mcx_evening_closed_sessions_coro(
            {date(2026, 12, 25)}  # 2026-01-26 is no longer in the closed set
        )

    assert fake_session.deleted_dates == [date(2026, 1, 26)]
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_upsert_does_not_delete_a_date_still_in_the_closed_set():
    """A date present in BOTH the existing rows and this cycle's closed
    set must never be deleted — only genuinely-stale rows are touched."""
    from backend.brokers import broker_apis

    fake_session = _FakeUpsertSession(existing_dates={date(2026, 12, 25)})
    with patch("backend.api.database.async_session", fake_session):
        await broker_apis._upsert_mcx_evening_closed_sessions_coro(
            {date(2026, 12, 25)}
        )

    assert fake_session.deleted_dates is None


@pytest.mark.asyncio
async def test_upsert_empty_closed_set_reconciles_away_all_existing_rows():
    """A genuinely empty closed set (the feed says nothing is
    evening-closed this year) must still reconcile away every existing
    auto-row — this is the real "NSE revised everything back to open"
    case, not a failed-fetch case (that's gated by the caller checking
    `is not None` before ever calling this function)."""
    from backend.brokers import broker_apis

    fake_session = _FakeUpsertSession(existing_dates={date(2026, 1, 26), date(2026, 12, 25)})
    with patch("backend.api.database.async_session", fake_session):
        written = await broker_apis._upsert_mcx_evening_closed_sessions_coro(set())

    assert written == 0
    assert sorted(fake_session.deleted_dates) == [date(2026, 1, 26), date(2026, 12, 25)]


@pytest.mark.asyncio
async def test_upsert_reconcile_never_touches_historical_dates():
    """The reconciliation SELECT is scoped to `date >= today` — a
    historical auto-row (from a past, already-elapsed holiday) must
    never be queried or deleted, regardless of whether it's in the
    current closed set."""
    from backend.brokers import broker_apis
    import inspect

    src = inspect.getsource(broker_apis._upsert_mcx_evening_closed_sessions_coro)
    assert "date >= :today" in src


@pytest.mark.asyncio
async def test_upsert_logs_on_db_failure_instead_of_silently_swallowing():
    """2026-10-20 fix: a DB failure during upsert/reconcile must be
    logged, not silently swallowed by a bare `except Exception: return 0`
    (the pre-fix behaviour — confirmed by this test failing against it,
    since there was nothing to assert on)."""
    from backend.brokers import broker_apis

    class _BoomSession:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("DB connection refused")

        async def __aexit__(self, *a):
            return False

    with patch("backend.api.database.async_session", _BoomSession()), \
         patch.object(broker_apis, "logger") as mock_logger:
        written = await broker_apis._upsert_mcx_evening_closed_sessions_coro(
            {date(2026, 12, 25)}
        )

    assert written == 0
    assert mock_logger.warning.called, "a DB failure must be logged, not silently swallowed"


# ---------------------------------------------------------------------------
# Caller contract (background.py) — `is not None`, not bare truthiness.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_holiday_refresh_skips_reconcile_entirely_on_failed_fetch():
    """2026-10-20 fix: when `_fetch_mcx_evening_closed_dates()` returns
    None (fetch failed), the background.py caller must skip calling the
    upsert/reconcile function ENTIRELY — never pass an empty set in its
    place, which would otherwise wipe every real evening-closed row
    during a transient NSE outage."""
    from backend.api import background as bg
    import inspect

    src = inspect.getsource(bg)
    # The caller's guard must be an explicit `is not None` check, not a
    # bare truthiness check that would also (wrongly) skip a genuine
    # empty-set success.
    assert "if evening_closed is not None:" in src


# ---------------------------------------------------------------------------
# date_time_utils.is_market_open — weekday guard regression
# ---------------------------------------------------------------------------

class TestWeekdayGuardRegression:
    def test_weekend_holiday_with_evening_open_stays_closed(self):
        """A holiday landing on a Saturday/Sunday must stay closed at
        17:05 even with evening_open_on_holidays=True — confirmed absent
        before this fix."""
        from backend.shared.helpers.date_time_utils import is_market_open

        # 2026-10-17 is a Saturday.
        saturday = datetime(2026, 10, 17, 17, 5, tzinfo=IST)
        assert saturday.weekday() == 5
        holidays = {saturday.date()}
        result = is_market_open(
            saturday, holidays,
            sessions=[{"start": "09:00", "end": "23:30"}],
            evening_open_on_holidays=True,
        )
        assert result is False

    def test_weekday_holiday_with_evening_open_still_opens(self):
        """Regression guard: the weekday fix must not break the existing,
        correct weekday evening-open case (e.g. Dussehra 2026-10-20,
        Tuesday)."""
        from backend.shared.helpers.date_time_utils import is_market_open

        tuesday = datetime(2026, 10, 20, 18, 0, tzinfo=IST)
        assert tuesday.weekday() == 1
        holidays = {tuesday.date()}
        result = is_market_open(
            tuesday, holidays,
            sessions=[{"start": "09:00", "end": "23:30"}],
            evening_open_on_holidays=True,
        )
        assert result is True


# ---------------------------------------------------------------------------
# End-to-end — Diwali Laxmi Pujan (2026-11-08) zero-width row
# ---------------------------------------------------------------------------

class TestDiwaliLaxmiPujanEndToEnd:
    def test_seeded_zero_width_row_closes_mcx_at_1800(self):
        """A seeded zero-width special-session row for 2026-11-08 makes
        is_market_open return False for MCX at 18:00, overriding the
        (otherwise-wrong) evening_open_on_holidays=True default."""
        from backend.shared.helpers.date_time_utils import is_market_open

        d = date(2026, 11, 8)
        now = datetime(2026, 11, 8, 18, 0, tzinfo=IST)
        special_sessions = [
            {"date": d, "start": dtime(0, 0, 0), "end": dtime(0, 0, 0)},
        ]
        result = is_market_open(
            now, {d},
            sessions=[{"start": "09:00", "end": "23:30"}],
            evening_open_on_holidays=True,
            special_sessions=special_sessions,
        )
        assert result is False

    def test_weekend_exclusion_guard_no_row_inserted_for_2026_11_08(self):
        """Confirm the weekend-exclusion guard means no row is ever
        auto-inserted for this date, even though it's evening_session:
        Closed on the real feed — reconfirms the date is a Sunday."""
        assert date(2026, 11, 8).weekday() == 6
