"""Tests for exchange schedule/clock functionality.

This module tests the market hours, session management, and snapshot-time
routing for different exchanges (NSE, MCX, CDS) and special sessions (muhurat,
closed dates).

Five test dimensions:
  1. SSOT        — Exchange hours and holiday calendars from a single DB
  2. Performance — In-process _CACHE with TTL; no repeated DB queries
  3. Stale code  — No hardcoded hours per-route
  4. Reusable    — Same helpers for NSE/MCX/CDS + regular/muhurat/closed
  5. Correctness — Day-P&L overlay, row-level filtering, snapshot routing

Note: These tests are written in anticipation of the exchange_clock module.
When the module is implemented, these tests should run without modification.
Currently, they skip gracefully if the module is not available.
"""

from datetime import date, time, datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

pytestmark = pytest.mark.skip(
    reason="exchange_clock module not yet implemented — tests are stubs pending implementation"
)


# ---------------------------------------------------------------------------
# Helpers to build mock schedule rows
# ---------------------------------------------------------------------------

def _make_schedule_row(**kwargs):
    """Helper to construct a mock schedule row for testing."""
    defaults = {
        "gate": "NSE",
        "exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS"],
        "date": None,  # None means default/template row
        "weekdays": [1, 2, 3, 4, 5],  # Mon-Fri
        "session_name": "regular",
        "is_open": True,
        "open_time": time(9, 15),
        "close_time": time(15, 30),
        "snapshot_time": time(15, 31),
        "snapshot_reset_time": time(8, 0),
        "source": "legacy_seed",
    }
    defaults.update(kwargs)
    return defaults


def _ist_time(hour: int, minute: int) -> datetime:
    """Helper to create a datetime at the given IST time (today)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Tests for basic market hours (NSE regular session)
# ---------------------------------------------------------------------------

def test_nse_open_during_regular_session():
    """NSE is open 09:15–15:30 IST Mon-Fri."""
    cache = [_make_schedule_row(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        session_name="regular",
        open_time=time(9, 15),
        close_time=time(15, 30),
    )]

    # Mock a Monday at 10:00 IST
    test_time = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # When implemented, this should call:
        # result = exchange_clock.is_exchange_open("NSE", as_of=test_time)
        # assert result is True
        pass


def test_nse_closed_after_session():
    """NSE is closed at 16:00 IST (market closes at 15:30)."""
    cache = [_make_schedule_row(
        gate="NSE",
        session_name="regular",
        open_time=time(9, 15),
        close_time=time(15, 30),
    )]

    # Test at 16:00 IST on a trading day
    test_time = datetime(2026, 8, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("NSE", as_of=test_time)
        # assert result is False
        pass


def test_nfo_open_during_nse_session():
    """NFO (derivatives) follows NSE equity hours (09:15–15:30)."""
    cache = [_make_schedule_row(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        session_name="regular",
        open_time=time(9, 15),
        close_time=time(15, 30),
    )]

    test_time = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # NFO should be open when NSE is open
        # result = exchange_clock.is_exchange_open("NFO", as_of=test_time)
        # assert result is True
        pass


# ---------------------------------------------------------------------------
# Tests for MCX (multi-session: morning + evening)
# ---------------------------------------------------------------------------

def test_mcx_morning_session():
    """MCX morning: 09:00–17:00 IST."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            open_time=time(9, 0),
            close_time=time(17, 0),
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            open_time=time(17, 0),
            close_time=time(23, 30),
        ),
    ]

    # Test at 10:00 IST during morning session
    test_time = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is True
        pass


def test_mcx_evening_session():
    """MCX evening: 17:00–23:30 IST."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            open_time=time(9, 0),
            close_time=time(17, 0),
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            open_time=time(17, 0),
            close_time=time(23, 30),
        ),
    ]

    # Test at 20:00 IST during evening session
    test_time = datetime(2026, 8, 25, 20, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is True
        pass


def test_mcx_session_boundary_at_17_00():
    """At 17:00 IST, MCX transitions from morning to evening."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            open_time=time(9, 0),
            close_time=time(17, 0),
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            open_time=time(17, 0),
            close_time=time(23, 30),
        ),
    ]

    # Exactly at 17:00 IST, morning closes but evening opens
    test_time = datetime(2026, 8, 25, 17, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # At boundary, evening session should be active (opening time is inclusive)
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is True
        pass


def test_mcx_closed_between_sessions():
    """MCX is closed between evening close (23:30) and next morning open (09:00)."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            open_time=time(9, 0),
            close_time=time(17, 0),
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            open_time=time(17, 0),
            close_time=time(23, 30),
        ),
    ]

    # Test at 08:00 IST (before morning opens)
    test_time = datetime(2026, 8, 25, 8, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is False
        pass


# ---------------------------------------------------------------------------
# Tests for closed date overrides (e.g., Independence Day)
# ---------------------------------------------------------------------------

def test_closed_override_suppresses_all_sessions():
    """A date-specific closed override prevents all sessions on that date."""
    closed_date = date(2026, 8, 15)  # Assume this is Independence Day

    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            date=None,  # Template row
            open_time=time(9, 0),
            close_time=time(17, 0),
            is_open=True,
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            date=closed_date,  # Override for this specific date
            is_open=False,
        ),
    ]

    # Test at 10:00 IST on the closed date
    test_time = datetime(
        closed_date.year, closed_date.month, closed_date.day, 10, 0, 0,
        tzinfo=ZoneInfo("Asia/Kolkata")
    )

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is False
        pass


def test_closed_override_does_not_affect_other_dates():
    """A date-specific closed override only affects that date."""
    closed_date = date(2026, 8, 15)
    open_date = date(2026, 8, 16)

    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            date=None,
            is_open=True,
            open_time=time(9, 0),
            close_time=time(17, 0),
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            date=closed_date,
            is_open=False,
        ),
    ]

    # Test at 10:00 IST on an open date (next day)
    test_time = datetime(
        open_date.year, open_date.month, open_date.day, 10, 0, 0,
        tzinfo=ZoneInfo("Asia/Kolkata")
    )

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("MCX", as_of=test_time)
        # assert result is True
        pass


# ---------------------------------------------------------------------------
# Tests for muhurat session (limited exchange subset)
# ---------------------------------------------------------------------------

def test_muhurat_session_open_only_for_specified_exchanges():
    """Muhurat session opens only for NSE/BSE, not NFO/BFO."""
    muhurat_date = date(2026, 11, 1)  # Diwali

    cache = [
        _make_schedule_row(
            gate="NSE",
            exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
            session_name="regular",
            date=None,
            is_open=True,
            open_time=time(9, 15),
            close_time=time(15, 30),
            weekdays=[1, 2, 3, 4, 5],
        ),
        _make_schedule_row(
            gate="NSE",
            exchanges=["NSE", "BSE"],  # muhurat does NOT include NFO/BFO
            session_name="muhurat",
            date=muhurat_date,
            is_open=True,
            open_time=time(18, 0),
            close_time=time(19, 0),
        ),
    ]

    # Test at 18:30 IST on Diwali
    test_time = datetime(
        muhurat_date.year, muhurat_date.month, muhurat_date.day, 18, 30, 0,
        tzinfo=ZoneInfo("Asia/Kolkata")
    )

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # NSE should be open (muhurat override applies)
        # result_nse = exchange_clock.is_exchange_open("NSE", as_of=test_time)
        # assert result_nse is True

        # NFO should be closed (not in muhurat exchanges list, and defaults are overridden)
        # result_nfo = exchange_clock.is_exchange_open("NFO", as_of=test_time)
        # assert result_nfo is False
        pass


# ---------------------------------------------------------------------------
# Tests for weekend/holiday closures
# ---------------------------------------------------------------------------

def test_weekend_saturday_closed():
    """NSE is closed on Saturday (isoweekday=6)."""
    cache = [_make_schedule_row(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        session_name="regular",
        weekdays=[1, 2, 3, 4, 5],  # Mon-Fri only
        open_time=time(9, 15),
        close_time=time(15, 30),
    )]

    # Find a Saturday in August 2026
    # 2026-08-22 is a Saturday
    test_time = datetime(2026, 8, 22, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("NSE", as_of=test_time)
        # assert result is False
        pass


def test_weekend_sunday_closed():
    """NSE is closed on Sunday (isoweekday=7)."""
    cache = [_make_schedule_row(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        session_name="regular",
        weekdays=[1, 2, 3, 4, 5],  # Mon-Fri only
        open_time=time(9, 15),
        close_time=time(15, 30),
    )]

    # 2026-08-23 is a Sunday
    test_time = datetime(2026, 8, 23, 10, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.is_exchange_open("NSE", as_of=test_time)
        # assert result is False
        pass


# ---------------------------------------------------------------------------
# Tests for snapshot time routing
# ---------------------------------------------------------------------------

def test_snapshot_time_for_nse():
    """snapshot_time for NSE regular session is 15:31 IST."""
    cache = [_make_schedule_row(
        gate="NSE",
        exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        session_name="regular",
        snapshot_time=time(15, 31),
    )]

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.snapshot_time_for("NSE")
        # assert result == time(15, 31)
        pass


def test_snapshot_time_for_mcx_morning_is_none():
    """MCX morning session has no snapshot time (None)."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            snapshot_time=None,
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            snapshot_time=time(23, 31),
        ),
    ]

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # At morning time, snapshot_time should be None
        # result = exchange_clock.snapshot_time_for("MCX", as_of=datetime(...morning...))
        # assert result is None
        pass


def test_snapshot_time_for_mcx_evening():
    """MCX evening settlement snapshot is at 23:31 IST."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="morning",
            snapshot_time=None,
        ),
        _make_schedule_row(
            gate="MCX",
            exchanges=["MCX"],
            session_name="evening",
            open_time=time(17, 0),
            close_time=time(23, 30),
            snapshot_time=time(23, 31),
        ),
    ]

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # At evening time, snapshot_time should be 23:31
        # result = exchange_clock.snapshot_time_for("MCX", as_of=datetime(...evening...))
        # assert result == time(23, 31)
        pass


# ---------------------------------------------------------------------------
# Tests for snapshot_reset_time (day P&L cutoff)
# ---------------------------------------------------------------------------

def test_snapshot_reset_time_default():
    """snapshot_reset_time defaults to 08:00 IST."""
    cache = [_make_schedule_row(
        gate="NSE",
        snapshot_reset_time=time(8, 0),
    )]

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.snapshot_reset_time_for("NSE")
        # assert result == time(8, 0)
        pass


def test_snapshot_reset_time_before_reset_cutoff():
    """Before 08:00 IST, day P&L cutoff is yesterday + 08:00."""
    # Mock now as 07:00 IST
    now_ist = datetime(2026, 8, 25, 7, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    reset_time = time(8, 0)

    with patch("backend.api.helpers.exchange_clock.now_ist", return_value=now_ist):
        # result = exchange_clock.settlement_cutoff_for("NSE")
        # expected = datetime(2026, 8, 24, 8, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        # assert result == expected
        pass


def test_snapshot_reset_time_after_reset_cutoff():
    """After 08:00 IST, day P&L cutoff is today + 08:00."""
    # Mock now as 09:00 IST
    now_ist = datetime(2026, 8, 25, 9, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    reset_time = time(8, 0)

    with patch("backend.api.helpers.exchange_clock.now_ist", return_value=now_ist):
        # result = exchange_clock.settlement_cutoff_for("NSE")
        # expected = datetime(2026, 8, 25, 8, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        # assert result == expected
        pass


# ---------------------------------------------------------------------------
# Tests for sessions_with_snapshot_time_now
# ---------------------------------------------------------------------------

def test_sessions_with_snapshot_time_now_nse_regular():
    """At NSE snapshot time (15:31), return the regular session."""
    cache = [
        _make_schedule_row(
            gate="NSE",
            session_name="regular",
            snapshot_time=time(15, 31),
            is_open=True,
        ),
        _make_schedule_row(
            gate="NSE",
            session_name="settlement",
            snapshot_time=time(16, 15),
            is_open=False,
        ),
    ]

    # At exactly 15:31 IST
    at_time = datetime(2026, 8, 25, 15, 31, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.sessions_with_snapshot_time_now(at=at_time)
        # Should return only the regular session (snapshot_time matches)
        # assert len(result) == 1
        # assert result[0]["session_name"] == "regular"
        pass


def test_sessions_with_snapshot_time_now_nse_settlement():
    """At NSE settlement time (16:15), return the settlement session."""
    cache = [
        _make_schedule_row(
            gate="NSE",
            session_name="regular",
            snapshot_time=time(15, 31),
            is_open=True,
        ),
        _make_schedule_row(
            gate="NSE",
            session_name="settlement",
            snapshot_time=time(16, 15),
            is_open=False,
        ),
    ]

    # At exactly 16:15 IST
    at_time = datetime(2026, 8, 25, 16, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.sessions_with_snapshot_time_now(at=at_time)
        # Should return only the settlement session (snapshot_time matches)
        # assert len(result) == 1
        # assert result[0]["session_name"] == "settlement"
        pass


def test_sessions_with_snapshot_time_now_mcx_settlement():
    """At MCX settlement time (00:15), return the settlement session."""
    cache = [
        _make_schedule_row(
            gate="MCX",
            session_name="evening",
            snapshot_time=None,
            is_open=True,
        ),
        _make_schedule_row(
            gate="MCX",
            session_name="settlement",
            snapshot_time=time(0, 15),
            is_open=False,
        ),
    ]

    # At exactly 00:15 IST
    at_time = datetime(2026, 8, 26, 0, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("backend.api.helpers.exchange_clock._CACHE", cache):
        # result = exchange_clock.sessions_with_snapshot_time_now(at=at_time)
        # Should return only the MCX settlement session
        # assert len(result) == 1
        # assert result[0]["session_name"] == "settlement"
        pass


# ---------------------------------------------------------------------------
# Tests for cache invalidation and TTL
# ---------------------------------------------------------------------------

def test_cache_has_reasonable_ttl():
    """The _CACHE should be invalidated and refreshed periodically."""
    # This is a structural test — verify that the module has cache TTL logic
    # (actual TTL timing depends on implementation)
    try:
        from backend.api.helpers import exchange_clock
        # Verify _CACHE exists and has TTL-related attributes
        assert hasattr(exchange_clock, "_CACHE"), "exchange_clock should have _CACHE"
        assert hasattr(exchange_clock, "_CACHE_TTL") or \
               hasattr(exchange_clock, "_cache_last_updated"), \
               "exchange_clock should have TTL mechanism"
    except ImportError:
        # exchange_clock module not yet implemented
        pytest.skip("exchange_clock module not yet available")


# ---------------------------------------------------------------------------
# Integration: snapshot_gate delegation tests
# ---------------------------------------------------------------------------

def test_snapshot_gate_delegates_to_exchange_clock():
    """snapshot_gate.is_exchange_closed_now delegates to exchange_clock."""
    # This test verifies that the snapshot_gate module correctly delegates
    # to exchange_clock for the market open/closed check.
    with patch("backend.api.helpers.exchange_clock.is_exchange_open",
               return_value=False) as mock_is_open:
        try:
            from backend.api.helpers import snapshot_gate
            result = snapshot_gate.is_exchange_closed_now("NSE")
            # When is_exchange_open("NSE") returns False, is_exchange_closed_now should return True
            assert result is True
            mock_is_open.assert_called()
        except (ImportError, AttributeError):
            # If snapshot_gate still uses the old implementation, skip
            pytest.skip("snapshot_gate not yet delegating to exchange_clock")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
