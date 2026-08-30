"""
Tests for exchange_clock date-override-first logic.

The exchange_clock module maintains an in-process cache of ExchangeSchedule rows.
Each row can be either:
  - A default row (date=None): applies on matching weekdays
  - An override row (date=specific_date): applies only on that date, superseding default

This test suite verifies that override rows take precedence over defaults
and that all public functions respect the override-first rule.
"""

import pytest
import types
from datetime import datetime, time, date, timedelta, timezone
from unittest.mock import patch, AsyncMock
from zoneinfo import ZoneInfo


_IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def sample_default_row():
    """Build a default (date=None) NON-MCX row."""
    return types.SimpleNamespace(
        gate="NON-MCX",
        date=None,  # Default row
        weekdays=[0, 1, 2, 3, 4],  # Mon-Fri
        open_time=time(8, 0),
        close_time=time(15, 30),
        snapshot_time=time(15, 45),
        snapshot_reset_time=time(8, 0),
        exchanges=["NSE", "BSE", "NFO"],
        is_open=True,
        session_name="regular",
        source="system",
    )


@pytest.fixture
def sample_mcx_default_row():
    """Build a default MCX row."""
    return types.SimpleNamespace(
        gate="MCX",
        date=None,
        weekdays=[0, 1, 2, 3, 4],
        open_time=time(8, 0),
        close_time=time(23, 30),
        snapshot_time=time(23, 45),
        snapshot_reset_time=time(8, 0),
        exchanges=["MCX"],
        is_open=True,
        session_name="regular",
        source="system",
    )


def _make_override_row(gate, date_obj, open_time=None, close_time=None,
                       snapshot_time=None, snapshot_reset_time=None, exchanges=None):
    """Build an override (date != None) row."""
    return types.SimpleNamespace(
        gate=gate,
        date=date_obj,  # Override row — specific date
        weekdays=None,  # Overrides ignore weekdays
        open_time=open_time,  # None means closed
        close_time=close_time,
        snapshot_time=snapshot_time,
        snapshot_reset_time=snapshot_reset_time,
        exchanges=exchanges or ["NSE", "BSE", "NFO"],
        is_open=(open_time is not None),
        session_name="override",
        source="user",
    )


class TestHolidayOverrideBlocksDefault:
    """Holiday (open_time=None) override blocks default market hours."""

    def test_holiday_override_closes_market(self, sample_default_row):
        """Holiday override (date=today, open_time=None) should close market."""
        import backend.api.helpers.exchange_clock as ec

        # Use a fixed date that matches the mock time
        test_date = date(2025, 1, 15)  # Wednesday
        default = sample_default_row
        override = _make_override_row("NON-MCX", test_date, open_time=None)

        ec._CACHE = [default, override]

        # Mock time to 11:00 on the same Wednesday
        mock_time = datetime(2025, 1, 15, 11, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            result = ec.is_exchange_open("NSE")
            assert result is False, "Holiday override should close market"

    def test_holiday_override_no_sessions(self, sample_default_row):
        """get_today_gate_sessions should return [] for holiday."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        default = sample_default_row
        override = _make_override_row("NON-MCX", test_date, open_time=None)

        ec._CACHE = [default, override]

        mock_time = datetime(2025, 1, 15, 11, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            sessions = ec.get_today_gate_sessions("NON-MCX")
            assert sessions == [], "Holiday should have no sessions"

    def test_holiday_override_no_snapshot(self, sample_default_row):
        """sessions_with_snapshot_time_now should return [] on holiday."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        default = sample_default_row
        override = _make_override_row("NON-MCX", test_date, open_time=None)

        ec._CACHE = [default, override]

        # Mock to 15:45 (normal snapshot time)
        mock_time = datetime(2025, 1, 15, 15, 45, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            rows = ec.sessions_with_snapshot_time_now()
            assert rows == [], "No snapshot on holiday"


class TestSpecialSessionCustomTimes:
    """Special session override can change hours and snapshot time."""

    def test_special_session_custom_open_close(self, sample_default_row):
        """Special session override (18:00-21:00) should open at custom time."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        default = sample_default_row
        override = _make_override_row(
            "NON-MCX", test_date,
            open_time=time(18, 0), close_time=time(21, 0),
            snapshot_time=None
        )

        ec._CACHE = [default, override]

        # Test inside special window (19:00)
        mock_time = datetime(2025, 1, 15, 19, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            result = ec.is_exchange_open("NSE")
            assert result is True, "Should be open during special session 18:00-21:00"

        # Test outside special window (14:00 — inside default but outside override)
        mock_time = datetime(2025, 1, 15, 14, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            result = ec.is_exchange_open("NSE")
            assert result is False, "Should be closed outside special session"


class TestWeekendDefaultRowBlocked:
    """Default row with weekdays=[0-4] should not apply on Saturday/Sunday."""

    def test_saturday_blocks_default(self, sample_default_row):
        """Saturday (weekday=5) should not match default weekdays=[0-4]."""
        import backend.api.helpers.exchange_clock as ec

        default = sample_default_row  # weekdays=[0,1,2,3,4]
        ec._CACHE = [default]

        # Mock to Saturday 11:00
        mock_time = datetime(2025, 1, 18, 11, 0, 0, tzinfo=_IST)  # Saturday
        with patch.object(ec, '_now_ist', return_value=mock_time):
            result = ec.is_exchange_open("NSE")
            assert result is False, "Default row should not apply on Saturday"

    def test_sunday_blocks_default(self, sample_default_row):
        """Sunday (weekday=6) should not match default weekdays=[0-4]."""
        import backend.api.helpers.exchange_clock as ec

        default = sample_default_row
        ec._CACHE = [default]

        # Mock to Sunday 11:00
        mock_time = datetime(2025, 1, 19, 11, 0, 0, tzinfo=_IST)  # Sunday
        with patch.object(ec, '_now_ist', return_value=mock_time):
            result = ec.is_exchange_open("NSE")
            assert result is False, "Default row should not apply on Sunday"


class TestNoSnapshotOnHoliday:
    """Holiday (snapshot_time=None) should have no snapshot session."""

    def test_holiday_no_snapshot_time(self, sample_default_row):
        """Holiday override with snapshot_time=None should not snapshot."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        default = sample_default_row
        override = _make_override_row(
            "NON-MCX", test_date,
            open_time=None, close_time=None, snapshot_time=None
        )

        ec._CACHE = [default, override]

        # Mock to 15:45 (would be snapshot time on normal day)
        mock_time = datetime(2025, 1, 15, 15, 45, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            rows = ec.sessions_with_snapshot_time_now()
            assert rows == [], "Holiday should have no snapshot"


class TestMCXHolidayDoesNotAffectNSE:
    """MCX holiday override should not affect NON-MCX (NSE) status."""

    def test_mcx_holiday_nse_open(self, sample_default_row, sample_mcx_default_row):
        """MCX holiday should not close NSE."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        mcx_override = _make_override_row("MCX", test_date, open_time=None)

        ec._CACHE = [sample_default_row, sample_mcx_default_row, mcx_override]

        # Mock to weekday 11:00
        mock_time = datetime(2025, 1, 15, 11, 0, 0, tzinfo=_IST)  # Wed
        with patch.object(ec, '_now_ist', return_value=mock_time):
            # NSE should still be open
            assert ec.is_exchange_open("NSE") is True
            # MCX should be closed
            assert ec.is_exchange_open("MCX") is False


class TestSpecialSessionSnapshot:
    """Special session override can define a custom snapshot time."""

    def test_special_session_snapshot_time(self, sample_default_row):
        """Special session at 21:15 should snapshot at that time."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        override = _make_override_row(
            "NON-MCX", test_date,
            open_time=time(18, 0), close_time=time(21, 30),
            snapshot_time=time(21, 15)
        )

        ec._CACHE = [sample_default_row, override]

        # Mock to 21:15 (snapshot time)
        mock_time = datetime(2025, 1, 15, 21, 15, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            rows = ec.sessions_with_snapshot_time_now()
            assert len(rows) > 0, "Should snapshot at special session time"
            assert rows[0].snapshot_time == time(21, 15)


class TestSettlementCutoff:
    """settlement_cutoff_for should return last 08:00 IST boundary."""

    @pytest.mark.asyncio
    async def test_settlement_cutoff_after_reset(self, sample_default_row):
        """After 08:00, cutoff should be today 08:00."""
        import backend.api.helpers.exchange_clock as ec

        ec._CACHE = [sample_default_row]

        # Mock to 10:00 (after reset)
        mock_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            with patch.object(ec, 'refresh', new_callable=AsyncMock):
                result = await ec.settlement_cutoff_for("NON-MCX")

                # Should be today 08:00
                assert result.hour == 8
                assert result.minute == 0
                assert result.date() == mock_time.date()

    @pytest.mark.asyncio
    async def test_settlement_cutoff_before_reset(self, sample_default_row):
        """Before 08:00, cutoff should be yesterday 08:00."""
        import backend.api.helpers.exchange_clock as ec

        ec._CACHE = [sample_default_row]

        # Mock to 07:00 (before reset)
        mock_time = datetime(2025, 1, 15, 7, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            with patch.object(ec, 'refresh', new_callable=AsyncMock):
                result = await ec.settlement_cutoff_for("NON-MCX")

                # Should be yesterday 08:00
                expected = mock_time - timedelta(days=1)
                assert result.hour == 8
                assert result.minute == 0
                assert result.date() == expected.date()


class TestIsAnySegmentOpen:
    """is_any_segment_open should check all or filtered segments."""

    def test_any_segment_open_all_closed(self, sample_default_row, sample_mcx_default_row):
        """After-hours should show all closed."""
        import backend.api.helpers.exchange_clock as ec

        ec._CACHE = [sample_default_row, sample_mcx_default_row]

        # Mock to 16:00 (after NSE close 15:30, before MCX open 08:00 next day)
        mock_time = datetime(2025, 1, 15, 16, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            # NSE should be closed
            assert ec.is_exchange_open("NSE") is False
            # MCX should still be open (closes 23:30)
            assert ec.is_exchange_open("MCX") is True
            # Any segment open should be True (MCX is open)
            assert ec.is_any_segment_open() is True

    def test_any_segment_open_filtered(self, sample_default_row):
        """Filtered check for NSE-only."""
        import backend.api.helpers.exchange_clock as ec

        ec._CACHE = [sample_default_row]

        # Mock to 08:30 (market open)
        mock_time = datetime(2025, 1, 15, 8, 30, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            # Check NSE only
            assert ec.is_any_segment_open(["NSE"]) is True
            # Check MCX only (not in default cache for this test)
            assert ec.is_any_segment_open(["MCX"]) is False


class TestOverridePrecedenceExplicit:
    """Override rows must take precedence over default rows explicitly."""

    def test_override_closes_open_default(self, sample_default_row):
        """Override closing overrides open default."""
        import backend.api.helpers.exchange_clock as ec

        test_date = date(2025, 1, 15)  # Wednesday
        # Default says open 08:00-15:30
        # Override says closed (open_time=None)
        override = _make_override_row("NON-MCX", test_date, open_time=None)

        ec._CACHE = [sample_default_row, override]

        # Even though default applies to weekdays, override for today should win
        mock_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=_IST)
        with patch.object(ec, '_now_ist', return_value=mock_time):
            # Override should be checked first — date match wins
            assert ec.is_exchange_open("NSE") is False
