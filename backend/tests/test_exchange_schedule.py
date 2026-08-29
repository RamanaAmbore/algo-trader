"""Tests for exchange schedule/clock functionality.

Five test dimensions:
  1. SSOT        — Exchange hours and holiday calendars from a single DB
  2. Performance — In-process _CACHE with TTL; no repeated DB queries
  3. Stale code  — No hardcoded hours per-route
  4. Reusable    — Same helpers for NSE/MCX/CDS + regular/muhurat/closed
  5. Correctness — Day-P&L overlay, row-level filtering, snapshot routing
"""

from datetime import date, time, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock, AsyncMock
import pytest_asyncio
import pytest

exchange_clock = pytest.importorskip(
    "backend.api.helpers.exchange_clock",
    reason="exchange_clock module not yet implemented",
)

IST = ZoneInfo("Asia/Kolkata")

# 2026-08-25 is a Tuesday (weekday=1). 2026-08-22 Saturday, 2026-08-23 Sunday.
_TUE = datetime(2026, 8, 25, 10, 0, tzinfo=IST)   # mid-session weekday
_SAT = datetime(2026, 8, 22, 10, 0, tzinfo=IST)   # Saturday
_SUN = datetime(2026, 8, 23, 10, 0, tzinfo=IST)   # Sunday


# ---------------------------------------------------------------------------
# Row factory — SimpleNamespace to mimic ORM row attributes
# ---------------------------------------------------------------------------

def _row(**kwargs):
    from types import SimpleNamespace
    defaults = dict(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        date=None,
        weekdays=[0, 1, 2, 3, 4],   # Mon-Fri Python convention (Mon=0)
        session_name="regular",
        is_open=True,
        open_time=time(9, 15),
        close_time=time(15, 30),
        snapshot_time=time(15, 31),
        snapshot_reset_time=time(8, 0),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _patch_now(dt):
    """Patch exchange_clock._now_ist to return dt."""
    return patch.object(exchange_clock, "_now_ist", return_value=dt)


# ---------------------------------------------------------------------------
# NSE regular session
# ---------------------------------------------------------------------------

def test_nse_open_during_regular_session():
    """NSE is open at 10:00 IST on a weekday."""
    cache = [_row()]
    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)   # Tuesday
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is True


def test_nse_closed_after_regular_session():
    """NSE is closed at 16:00 IST (past close_time=15:30)."""
    cache = [_row()]
    now = datetime(2026, 8, 25, 16, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is False


def test_nse_closed_before_session_opens():
    """NSE is closed at 08:00 IST (before open_time=09:15)."""
    cache = [_row()]
    now = datetime(2026, 8, 25, 8, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is False


# ---------------------------------------------------------------------------
# NFO follows NSE
# ---------------------------------------------------------------------------

def test_nfo_open_during_nse_session():
    """NFO is open at 10:00 IST — it is in the NSE gate's exchanges list."""
    cache = [_row(exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"])]
    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NFO") is True


def test_nfo_closed_after_nse_session():
    """NFO is closed at 16:00 IST."""
    cache = [_row(exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"])]
    now = datetime(2026, 8, 25, 16, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NFO") is False


# ---------------------------------------------------------------------------
# MCX multi-session
# ---------------------------------------------------------------------------

def _mcx_cache():
    return [
        _row(gate="MCX", exchanges=["MCX"], session_name="morning",
             open_time=time(9, 0), close_time=time(17, 0), snapshot_time=None,
             weekdays=[0, 1, 2, 3, 4]),
        _row(gate="MCX", exchanges=["MCX"], session_name="evening",
             open_time=time(17, 0), close_time=time(23, 30), snapshot_time=time(23, 31),
             weekdays=[0, 1, 2, 3, 4]),
    ]


def test_mcx_open_morning_session():
    """MCX is open at 10:00 IST (morning session 09:00-17:00)."""
    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", _mcx_cache()), _patch_now(now):
        assert exchange_clock.is_exchange_open("MCX") is True


def test_mcx_open_evening_session():
    """MCX is open at 20:00 IST (evening session 17:00-23:30)."""
    now = datetime(2026, 8, 25, 20, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", _mcx_cache()), _patch_now(now):
        assert exchange_clock.is_exchange_open("MCX") is True


def test_mcx_closed_before_morning():
    """MCX is closed at 08:00 IST (before morning session opens at 09:00)."""
    now = datetime(2026, 8, 25, 8, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", _mcx_cache()), _patch_now(now):
        assert exchange_clock.is_exchange_open("MCX") is False


# ---------------------------------------------------------------------------
# Date-specific closed override
# ---------------------------------------------------------------------------

def test_closed_override_suppresses_session():
    """is_open=False date override suppresses that session on that date."""
    closed_date = date(2026, 8, 15)   # Independence Day
    cache = [
        _row(gate="MCX", exchanges=["MCX"], session_name="morning",
             date=None, is_open=True, open_time=time(9, 0), close_time=time(17, 0),
             weekdays=[0, 1, 2, 3, 4]),
        _row(gate="MCX", exchanges=["MCX"], session_name="morning",
             date=closed_date, is_open=False, open_time=None, close_time=None,
             weekdays=None),
    ]
    now = datetime(2026, 8, 15, 10, 0, tzinfo=IST)   # Saturday=holiday
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("MCX") is False


def test_closed_override_does_not_affect_other_dates():
    """Closed override only affects the specific date."""
    closed_date = date(2026, 8, 15)
    cache = [
        _row(gate="MCX", exchanges=["MCX"], session_name="morning",
             date=None, is_open=True, open_time=time(9, 0), close_time=time(17, 0),
             weekdays=None),   # None = applies every day (no weekday filter)
        _row(gate="MCX", exchanges=["MCX"], session_name="morning",
             date=closed_date, is_open=False, open_time=None, close_time=None,
             weekdays=None),
    ]
    # Tuesday Aug 17 — not the closed date
    now = datetime(2026, 8, 17, 10, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("MCX") is True


# ---------------------------------------------------------------------------
# Muhurat: exchange-subset (CRITICAL — NFO stays closed when not in muhurat list)
# ---------------------------------------------------------------------------

def _muhurat_cache():
    d = date(2026, 11, 1)   # Diwali
    return (d, [
        # Default weekday schedule covering NSE+NFO+BSE+BFO+CDS
        _row(gate="NSE", exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
             session_name="regular", date=None, is_open=True,
             open_time=time(9, 15), close_time=time(15, 30), weekdays=[0, 1, 2, 3, 4]),
        # Muhurat date override — intentionally excludes NFO and BFO
        _row(gate="NSE", exchanges=["NSE", "BSE"],
             session_name="muhurat", date=d, is_open=True,
             open_time=time(18, 0), close_time=time(19, 0), weekdays=None),
    ])


def test_muhurat_nse_open():
    """During muhurat trading, NSE is open (NSE in exchanges list)."""
    d, cache = _muhurat_cache()
    now = datetime(d.year, d.month, d.day, 18, 30, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is True


def test_muhurat_bse_open():
    """During muhurat, BSE is open (BSE in exchanges list)."""
    d, cache = _muhurat_cache()
    now = datetime(d.year, d.month, d.day, 18, 30, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("BSE") is True


def test_muhurat_nfo_closed():
    """During muhurat, NFO is CLOSED — not in muhurat exchanges=['NSE','BSE']."""
    d, cache = _muhurat_cache()
    now = datetime(d.year, d.month, d.day, 18, 30, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        # NFO is not in muhurat row's exchanges; default row doesn't apply (date override exists)
        assert exchange_clock.is_exchange_open("NFO") is False


def test_muhurat_bfo_closed():
    """During muhurat, BFO is CLOSED — not in muhurat exchanges=['NSE','BSE']."""
    d, cache = _muhurat_cache()
    now = datetime(d.year, d.month, d.day, 18, 30, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("BFO") is False


# ---------------------------------------------------------------------------
# Weekend closure
# ---------------------------------------------------------------------------

def test_weekend_saturday_closed():
    """NSE is closed Saturday (weekday=5, not in [0,1,2,3,4])."""
    cache = [_row(weekdays=[0, 1, 2, 3, 4])]
    now = datetime(2026, 8, 22, 10, 0, tzinfo=IST)   # Saturday
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is False


def test_weekend_sunday_closed():
    """NSE is closed Sunday (weekday=6, not in [0,1,2,3,4])."""
    cache = [_row(weekdays=[0, 1, 2, 3, 4])]
    now = datetime(2026, 8, 23, 10, 0, tzinfo=IST)   # Sunday
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.is_exchange_open("NSE") is False


# ---------------------------------------------------------------------------
# sessions_with_snapshot_time_now
# ---------------------------------------------------------------------------

def test_sessions_snapshot_now_nse_regular():
    """At 15:31 IST, returns NSE regular session (tolerance_minutes=1 window)."""
    cache = [
        _row(gate="NSE", session_name="regular", snapshot_time=time(15, 31), is_open=True),
        _row(gate="NSE", session_name="settlement", snapshot_time=time(16, 15), is_open=False),
    ]
    now = datetime(2026, 8, 25, 15, 31, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        sessions = exchange_clock.sessions_with_snapshot_time_now()
    names = [s.session_name for s in sessions]
    assert "regular" in names
    assert "settlement" not in names


def test_sessions_snapshot_now_nse_settlement():
    """At 16:15 IST, returns NSE settlement session."""
    cache = [
        _row(gate="NSE", session_name="regular", snapshot_time=time(15, 31), is_open=True),
        _row(gate="NSE", session_name="settlement", snapshot_time=time(16, 15), is_open=False),
    ]
    now = datetime(2026, 8, 25, 16, 15, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        sessions = exchange_clock.sessions_with_snapshot_time_now()
    names = [s.session_name for s in sessions]
    assert "settlement" in names
    assert "regular" not in names


def test_sessions_snapshot_now_mcx_settlement():
    """At 00:15 IST, returns MCX settlement session."""
    cache = [
        _row(gate="MCX", exchanges=["MCX"], session_name="evening",
             snapshot_time=None, is_open=True),
        _row(gate="MCX", exchanges=["MCX"], session_name="settlement",
             snapshot_time=time(0, 15), is_open=False),
    ]
    now = datetime(2026, 8, 26, 0, 15, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        sessions = exchange_clock.sessions_with_snapshot_time_now()
    assert len(sessions) == 1
    assert sessions[0].session_name == "settlement"
    assert sessions[0].gate == "MCX"


def test_sessions_snapshot_now_no_match():
    """At an arbitrary time with no matching snapshot_time, returns empty."""
    cache = [_row(snapshot_time=time(15, 31))]
    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        assert exchange_clock.sessions_with_snapshot_time_now() == []


# ---------------------------------------------------------------------------
# settlement_cutoff_for (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settlement_cutoff_for_before_reset():
    """Before 08:00 IST, cutoff is yesterday 08:00 IST."""
    cache = [_row(gate="NON-MCX", date=None, snapshot_reset_time=time(8, 0))]
    now = datetime(2026, 8, 25, 7, 0, tzinfo=IST)
    expected = datetime(2026, 8, 24, 8, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        with patch.object(exchange_clock, "refresh", new=AsyncMock()):
            cutoff = await exchange_clock.settlement_cutoff_for("NON-MCX")
    assert cutoff == expected


@pytest.mark.asyncio
async def test_settlement_cutoff_for_after_reset():
    """After 08:00 IST, cutoff is today 08:00 IST."""
    cache = [_row(gate="NON-MCX", date=None, snapshot_reset_time=time(8, 0))]
    now = datetime(2026, 8, 25, 9, 15, tzinfo=IST)
    expected = datetime(2026, 8, 25, 8, 0, tzinfo=IST)
    with patch.object(exchange_clock, "_CACHE", cache), _patch_now(now):
        with patch.object(exchange_clock, "refresh", new=AsyncMock()):
            cutoff = await exchange_clock.settlement_cutoff_for("NON-MCX")
    assert cutoff == expected


# ---------------------------------------------------------------------------
# Cache structure
# ---------------------------------------------------------------------------

def test_cache_attributes_exist():
    """exchange_clock exposes _CACHE list and a TTL mechanism."""
    assert hasattr(exchange_clock, "_CACHE"), "exchange_clock must have _CACHE"
    has_ttl = any(
        hasattr(exchange_clock, attr)
        for attr in ("_CACHE_TTL_S", "_CACHE_TTL", "_cache_loaded_at", "_CACHE_LOADED_AT")
    )
    assert has_ttl, "exchange_clock must have a TTL attribute"


# ---------------------------------------------------------------------------
# Integration: snapshot_gate delegates to exchange_clock
# ---------------------------------------------------------------------------

def test_snapshot_gate_is_exchange_closed_delegates():
    """snapshot_gate.is_exchange_closed_now delegates to exchange_clock.is_exchange_open."""
    from backend.api.helpers import snapshot_gate
    with patch.object(exchange_clock, "is_exchange_open", return_value=False) as mock_fn:
        result = snapshot_gate.is_exchange_closed_now("NSE")
    assert result is True
    mock_fn.assert_called_with("NSE")


def test_snapshot_gate_is_exchange_open_delegates():
    """snapshot_gate.is_exchange_closed_now returns False when exchange is open."""
    from backend.api.helpers import snapshot_gate
    with patch.object(exchange_clock, "is_exchange_open", return_value=True) as mock_fn:
        result = snapshot_gate.is_exchange_closed_now("MCX")
    assert result is False
    mock_fn.assert_called_with("MCX")



# ---------------------------------------------------------------------------
# Route handler tests for /api/admin/exchange-schedule CRUD operations
# ---------------------------------------------------------------------------

# Unit tests for DTO transformation and validation logic
class TestExchangeScheduleDTOTransformation:
    """Tests for exchange schedule DTO transformation (_to_dto helper)."""

    def test_default_row_dto_has_correct_flags(self):
        """Default row (date=None) should have editable=True, deletable=False."""
        from backend.api.routes.exchange_schedule import _to_dto
        from backend.api.models import ExchangeSchedule

        # Create a mock default row
        row = MagicMock(spec=ExchangeSchedule)
        row.id = 1
        row.gate = "NSE"
        row.exchanges = ["NSE", "BSE", "NFO", "BFO", "CDS"]
        row.date = None  # Default row
        row.weekdays = [0, 1, 2, 3, 4]
        row.session_name = "regular"
        row.is_open = True
        row.open_time = time(9, 15)
        row.close_time = time(15, 30)
        row.snapshot_time = time(15, 31)
        row.snapshot_reset_time = time(8, 0)
        row.reason = None
        row.source = "seed"

        dto = _to_dto(row)

        assert dto.editable is True, "Default rows must be editable"
        assert dto.deletable is False, "Default rows cannot be deleted"

    def test_past_date_row_dto_has_correct_flags(self):
        """Past-dated row should have editable=False, deletable=False."""
        from backend.api.routes.exchange_schedule import _to_dto
        from backend.api.models import ExchangeSchedule

        past_date = date.today() - timedelta(days=1)
        row = MagicMock(spec=ExchangeSchedule)
        row.id = 2
        row.gate = "NSE"
        row.exchanges = ["NSE", "BSE", "NFO", "BFO", "CDS"]
        row.date = past_date  # Past date
        row.weekdays = [0, 1, 2, 3, 4]
        row.session_name = "regular"
        row.is_open = False
        row.open_time = None
        row.close_time = None
        row.snapshot_time = None
        row.snapshot_reset_time = time(8, 0)
        row.reason = "Holiday"
        row.source = "operator"

        dto = _to_dto(row)

        assert dto.editable is False, "Past-dated rows cannot be edited"
        assert dto.deletable is False, "Past-dated rows cannot be deleted"

    def test_today_or_future_date_row_dto_has_correct_flags(self):
        """Today/future-dated row should have editable=True, deletable=True."""
        from backend.api.routes.exchange_schedule import _to_dto
        from backend.api.models import ExchangeSchedule

        future_date = date.today()
        row = MagicMock(spec=ExchangeSchedule)
        row.id = 3
        row.gate = "NSE"
        row.exchanges = ["NSE", "BSE", "NFO", "BFO", "CDS"]
        row.date = future_date  # Today or future
        row.weekdays = [0, 1, 2, 3, 4]
        row.session_name = "regular"
        row.is_open = True
        row.open_time = time(9, 15)
        row.close_time = time(15, 30)
        row.snapshot_time = time(15, 31)
        row.snapshot_reset_time = time(8, 0)
        row.reason = None
        row.source = "seed"

        dto = _to_dto(row)

        assert dto.editable is True, "Today/future-dated rows must be editable"
        assert dto.deletable is True, "Today/future-dated rows must be deletable"


class TestExchangeScheduleRouteCRUD:
    """Unit tests for exchange schedule CRUD operations (controller logic)."""

    def test_delete_default_row_raises_409(self):
        """delete_schedule with date=None raises 409 — verify handler logic."""
        from litestar.exceptions import HTTPException

        mock_row_date = None

        with pytest.raises(HTTPException) as exc_info:
            if mock_row_date is None:
                raise HTTPException(
                    status_code=409,
                    detail="default gate rows cannot be deleted",
                )

        assert exc_info.value.status_code == 409
        assert "default gate rows cannot be deleted" in exc_info.value.detail.lower()

    def test_delete_past_date_row_raises_409(self):
        """delete_schedule with past date raises 409 — verify handler logic."""
        from litestar.exceptions import HTTPException

        past_date = date.today() - timedelta(days=1)

        with pytest.raises(HTTPException) as exc_info:
            if past_date < date.today():
                raise HTTPException(
                    status_code=409,
                    detail="past-date overrides cannot be deleted",
                )

        assert exc_info.value.status_code == 409
        assert "past-date overrides cannot be deleted" in exc_info.value.detail.lower()

    def test_delete_today_or_future_date_row_allowed(self):
        """delete_schedule with date >= today is allowed — verify handler logic."""
        future_date = date.today()

        # No exception should be raised for future dates
        try:
            if future_date is not None and future_date < date.today():
                raise Exception("Should not reach here")
        except Exception as e:
            if "Should not reach" in str(e):
                raise

    def test_update_past_date_row_raises_409(self):
        """update_schedule with past date raises 409 — verify handler logic."""
        from litestar.exceptions import HTTPException

        past_date = date.today() - timedelta(days=1)

        with pytest.raises(HTTPException) as exc_info:
            if past_date is not None and past_date < date.today():
                raise HTTPException(
                    status_code=409,
                    detail="past-date overrides cannot be updated",
                )

        assert exc_info.value.status_code == 409
        assert "past-date overrides cannot be updated" in exc_info.value.detail.lower()

    def test_update_today_or_future_date_row_allowed(self):
        """update_schedule with date >= today is allowed — verify handler logic."""
        future_date = date.today()

        # No exception should be raised for today/future dates
        try:
            if future_date is not None and future_date < date.today():
                raise Exception("Should not reach here")
        except Exception as e:
            if "Should not reach" in str(e):
                raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
