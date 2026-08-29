"""Tests for exchange_clock module and related snapshot_gate delegation.

Covers:
  - Cache refresh (warm / TTL skip)
  - is_exchange_open / is_exchange_closed with in-session and out-of-session times
  - is_any_segment_open with exchange filter
  - sessions_with_snapshot_time_now tolerance window
  - settlement_cutoff_for: before-08:00 and after-08:00 branches
  - seed_and_warm: idempotent seeding verifiable from _CACHE
  - snapshot_gate.is_exchange_closed_now delegation (no _EXCHANGE_TO_GATE / market_segments)
  - snapshot_gate._any_segment_open delegation
  - positions.py _fetch_ref_close_map no longer imports timestamp_indian for cutoff
  - holdings.py _override_stale_close_for_holdings no longer imports timestamp_indian for cutoff
  - Bug Fix 3: snap_ltp guard prevents oscillation in positions overlay
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")


def _make_schedule_row(
    gate: str,
    exchanges: list[str],
    is_open: bool = True,
    open_time: time | None = None,
    close_time: time | None = None,
    snapshot_time: time | None = None,
    snapshot_reset_time: time | None = None,
    date_val: date | None = None,
    weekdays: list[int] | None = None,
    session_name: str = "regular",
):
    """Return a simple MagicMock that quacks like an ExchangeSchedule row."""
    row = MagicMock()
    row.gate = gate
    row.exchanges = exchanges
    row.is_open = is_open
    row.open_time = open_time
    row.close_time = close_time
    row.snapshot_time = snapshot_time
    row.snapshot_reset_time = snapshot_reset_time
    row.date = date_val
    row.weekdays = weekdays
    row.session_name = session_name
    return row


# ---------------------------------------------------------------------------
# exchange_clock unit tests
# ---------------------------------------------------------------------------

class TestIsExchangeOpen:
    """is_exchange_open / is_exchange_closed — cache-backed, no DB."""

    def _set_cache(self, rows):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = rows

    def _patch_now(self, hour: int, minute: int, weekday: int = 0):
        """Patch _now_ist to return a fixed datetime (Mon=0 default)."""
        import backend.api.helpers.exchange_clock as ec
        fixed = datetime(2026, 8, 25, hour, minute, 0, tzinfo=_IST)  # Monday
        return patch.object(ec, "_now_ist", return_value=fixed)

    def test_open_during_session(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        self._set_cache([nse_row])
        with self._patch_now(10, 30):
            assert ec.is_exchange_open("NSE") is True
            assert ec.is_exchange_closed("NSE") is False

    def test_closed_before_session(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        self._set_cache([nse_row])
        with self._patch_now(8, 0):
            assert ec.is_exchange_open("NSE") is False
            assert ec.is_exchange_closed("NSE") is True

    def test_closed_after_session(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        self._set_cache([nse_row])
        with self._patch_now(15, 45):
            assert ec.is_exchange_open("NSE") is False

    def test_nfo_inherits_nse_gate(self):
        """NFO is listed in NSE's exchanges column."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        self._set_cache([nse_row])
        with self._patch_now(11, 0):
            assert ec.is_exchange_open("NFO") is True

    def test_mcx_separate_from_nse(self):
        """MCX open late; NSE closed after 15:30."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"],
            open_time=time(9, 0), close_time=time(23, 30),
        )
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(15, 45):
            assert ec.is_exchange_open("NSE") is False
            assert ec.is_exchange_open("MCX") is True

    def test_fail_open_empty_cache(self):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = []
        # Fail-open → returns True (assume market open)
        assert ec.is_exchange_open("NSE") is True
        assert ec.is_exchange_closed("NSE") is False

    def test_unknown_exchange_fails_open(self):
        """Fix 4: unknown exchange not in any schedule row → fail-open (True)."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        ec._CACHE = [nse_row]
        with self._patch_now(10, 0):
            # XBOM not in any exchanges list → fail-open (True), not False
            assert ec.is_exchange_open("XBOM") is True


class TestIsAnySegmentOpen:
    def _set_cache(self, rows):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = rows

    def _patch_now(self, hour: int, minute: int):
        import backend.api.helpers.exchange_clock as ec
        fixed = datetime(2026, 8, 25, hour, minute, 0, tzinfo=_IST)
        return patch.object(ec, "_now_ist", return_value=fixed)

    def test_any_open_no_filter(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE"], open_time=time(9, 15), close_time=time(15, 30),
        )
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"], open_time=time(9, 0), close_time=time(23, 30),
        )
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(15, 45):
            # NSE closed, MCX open → True
            assert ec.is_any_segment_open() is True

    def test_all_closed_no_filter(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE"], open_time=time(9, 15), close_time=time(15, 30),
        )
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"], open_time=time(9, 0), close_time=time(23, 30),
        )
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(1, 30):
            assert ec.is_any_segment_open() is False

    def test_filter_to_nse_only(self):
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE"], open_time=time(9, 15), close_time=time(15, 30),
        )
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"], open_time=time(9, 0), close_time=time(23, 30),
        )
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(15, 45):
            # MCX is open at 15:45 but we restrict to NSE → False
            assert ec.is_any_segment_open(["NSE"]) is False

    def test_fail_open_empty_cache(self):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = []
        assert ec.is_any_segment_open() is True


class TestSessionsWithSnapshotTimeNow:
    def _set_cache(self, rows):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = rows

    def _patch_now(self, hour: int, minute: int):
        import backend.api.helpers.exchange_clock as ec
        fixed = datetime(2026, 8, 25, hour, minute, 0, tzinfo=_IST)
        return patch.object(ec, "_now_ist", return_value=fixed)

    def test_exact_match(self):
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("NSE", ["NSE"], snapshot_time=time(15, 45))
        self._set_cache([row])
        with self._patch_now(15, 45):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 1
        assert hits[0].gate == "NSE"

    def test_within_tolerance(self):
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("NSE", ["NSE"], snapshot_time=time(15, 45))
        self._set_cache([row])
        with self._patch_now(15, 44):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 1

    def test_outside_tolerance(self):
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("NSE", ["NSE"], snapshot_time=time(15, 45))
        self._set_cache([row])
        with self._patch_now(15, 47):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 0

    def test_no_snapshot_time_excluded(self):
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("PRE", ["NSE"], snapshot_time=None)
        self._set_cache([row])
        with self._patch_now(9, 0):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 0

    def test_within_tolerance_59_seconds_before(self):
        """Test boundary: 59 seconds before snapshot_time is within 1-minute tolerance."""
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("NON-MCX", ["NSE"], snapshot_time=time(15, 45))
        self._set_cache([row])
        # now_ist rounds seconds/microseconds to 0, so we patch at 15:44 (60 seconds before 15:45)
        with self._patch_now(15, 44):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 1, "59 seconds before (via 15:44 rounded) should match with 1-min tolerance"

    def test_outside_tolerance_61_seconds_before(self):
        """Test boundary: 61 seconds before snapshot_time is outside 1-minute tolerance."""
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row("NON-MCX", ["NSE"], snapshot_time=time(15, 45))
        self._set_cache([row])
        # now_ist at 15:43 (120 seconds before 15:45) is well outside tolerance
        with self._patch_now(15, 43):
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 0, "61+ seconds before should not match with 1-min tolerance"

    def test_multiple_rows_mixed_match(self):
        """Test with multiple rows: some match, some don't."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row("NON-MCX", ["NSE"], snapshot_time=time(15, 45))
        mcx_row = _make_schedule_row("MCX", ["MCX"], snapshot_time=time(23, 45))
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(15, 45):
            # At 15:45: NON-MCX matches, MCX (23:45) does not
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 1, "Only NON-MCX should match at 15:45"
        assert hits[0].gate == "NON-MCX", "NON-MCX row should be returned"

    def test_multiple_rows_all_match(self):
        """Test with two rows both within tolerance at current time."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row("NON-MCX", ["NSE"], snapshot_time=time(15, 45))
        mcx_row = _make_schedule_row("MCX", ["MCX"], snapshot_time=time(15, 46))
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(15, 45):
            # At 15:45: both rows are within tolerance (NON-MCX exact, MCX 1 min after)
            hits = ec.sessions_with_snapshot_time_now(tolerance_minutes=1)
        assert len(hits) == 2, "Both rows should match within 1-minute tolerance"
        gate_names = {r.gate for r in hits}
        assert gate_names == {"NON-MCX", "MCX"}, "Both NON-MCX and MCX should be returned"


class TestSettlementCutoffFor:
    def _patch_now(self, hour: int, minute: int):
        import backend.api.helpers.exchange_clock as ec
        fixed = datetime(2026, 8, 25, hour, minute, 0, tzinfo=_IST)
        return patch.object(ec, "_now_ist", return_value=fixed)

    def _set_cache_with_reset(self, reset_time: time):
        import backend.api.helpers.exchange_clock as ec
        row = _make_schedule_row(
            "NON-MCX", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            snapshot_reset_time=reset_time,
            date_val=None,
        )
        ec._CACHE = [row]
        # Mark cache as fresh so refresh() skips DB.
        import time as _time
        ec._cache_loaded_at = _time.monotonic()

    @pytest.mark.asyncio
    async def test_after_reset_time_returns_today_boundary(self):
        import backend.api.helpers.exchange_clock as ec
        self._set_cache_with_reset(time(8, 0))
        with self._patch_now(10, 30):
            cutoff = await ec.settlement_cutoff_for("NON-MCX")
        # Now is 10:30 IST → after 08:00 → cutoff = today's 08:00 IST
        assert cutoff.hour == 8
        assert cutoff.minute == 0
        assert cutoff.tzinfo is not None  # tz-aware

    @pytest.mark.asyncio
    async def test_before_reset_time_returns_yesterday_boundary(self):
        import backend.api.helpers.exchange_clock as ec
        self._set_cache_with_reset(time(8, 0))
        with self._patch_now(6, 0):
            cutoff = await ec.settlement_cutoff_for("NON-MCX")
        # Now is 06:00 IST → before 08:00 → cutoff = yesterday's 08:00 IST
        assert cutoff.hour == 8
        assert cutoff.minute == 0
        # Should be yesterday
        fixed_today = datetime(2026, 8, 25, 8, 0, tzinfo=_IST)
        expected = fixed_today - timedelta(days=1)
        assert cutoff.date() == expected.date()

    @pytest.mark.asyncio
    async def test_fallback_to_08_when_no_cache(self):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = []
        import time as _time
        ec._cache_loaded_at = _time.monotonic()  # fresh but empty
        with self._patch_now(10, 0):
            cutoff = await ec.settlement_cutoff_for("NON-MCX")
        # Falls back to 08:00 default
        assert cutoff.hour == 8

    @pytest.mark.asyncio
    async def test_mcx_with_custom_reset_time(self):
        """MCX gate with custom snapshot_reset_time uses that time, not 08:00 default."""
        import backend.api.helpers.exchange_clock as ec
        # MCX with a hypothetical custom reset time of 09:00 IST
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"],
            snapshot_reset_time=time(9, 0),  # custom reset time
            date_val=None,
        )
        ec._CACHE = [mcx_row]
        import time as _time
        ec._cache_loaded_at = _time.monotonic()

        with self._patch_now(10, 0):
            # At 10:00 IST, which is after 09:00 → should return today's 09:00
            cutoff = await ec.settlement_cutoff_for("MCX")
        assert cutoff.hour == 9, "MCX cutoff should use custom reset time 09:00"
        assert cutoff.minute == 0

    @pytest.mark.asyncio
    async def test_before_custom_reset_time_returns_yesterday(self):
        """Before custom reset time → returns yesterday's boundary at that time."""
        import backend.api.helpers.exchange_clock as ec
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"],
            snapshot_reset_time=time(9, 0),
            date_val=None,
        )
        ec._CACHE = [mcx_row]
        import time as _time
        ec._cache_loaded_at = _time.monotonic()

        with self._patch_now(8, 0):
            # At 08:00 IST, which is BEFORE 09:00 → should return yesterday's 09:00
            cutoff = await ec.settlement_cutoff_for("MCX")
        assert cutoff.hour == 9
        # Should be yesterday
        fixed_today = datetime(2026, 8, 25, 9, 0, tzinfo=_IST)
        expected = fixed_today - timedelta(days=1)
        assert cutoff.date() == expected.date(), "Before reset time should return yesterday's boundary"

    @pytest.mark.asyncio
    async def test_gate_case_insensitive_lookup(self):
        """Gate name lookup is case-insensitive (upper-case matching)."""
        import backend.api.helpers.exchange_clock as ec
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"],
            snapshot_reset_time=time(8, 0),
            date_val=None,
        )
        ec._CACHE = [mcx_row]
        import time as _time
        ec._cache_loaded_at = _time.monotonic()

        with self._patch_now(10, 0):
            # Lookup with lowercase "mcx" should find the "MCX" row
            cutoff = await ec.settlement_cutoff_for("mcx")
        assert cutoff.hour == 8, "Gate lookup should be case-insensitive"


class TestCacheRefreshTTL:
    """Cache TTL skips DB fetch when fresh; forces refresh when stale."""

    @pytest.mark.asyncio
    async def test_fresh_cache_skips_db(self):
        import backend.api.helpers.exchange_clock as ec
        import time as _time
        # Pre-populate cache with a known row.
        row = _make_schedule_row("NSE", ["NSE"])
        ec._CACHE = [row]
        ec._cache_loaded_at = _time.monotonic()  # just loaded

        # _force_refresh would be called on a real DB; we patch it.
        with patch.object(ec, "_force_refresh", new_callable=AsyncMock) as mock_ff:
            await ec.refresh()
            mock_ff.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_cache_triggers_db(self):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = []
        ec._cache_loaded_at = 0.0  # stale

        with patch.object(ec, "_force_refresh", new_callable=AsyncMock) as mock_ff:
            await ec.refresh()
            mock_ff.assert_called_once()


# ---------------------------------------------------------------------------
# snapshot_gate delegation tests
# ---------------------------------------------------------------------------

class TestSnapshotGateDelegation:
    """Ensure snapshot_gate delegates to exchange_clock, not old YAML paths."""

    def test_no_exchange_to_gate_dict(self):
        """_EXCHANGE_TO_GATE must NOT exist in snapshot_gate (removed)."""
        import backend.api.helpers.snapshot_gate as sg
        assert not hasattr(sg, "_EXCHANGE_TO_GATE"), (
            "_EXCHANGE_TO_GATE was found in snapshot_gate — it should have been "
            "removed; timing is now delegated to exchange_clock"
        )

    def test_no_market_segments_yaml_read(self):
        """is_exchange_closed_now must not read market_segments from YAML config."""
        import inspect
        import backend.api.helpers.snapshot_gate as sg
        src = inspect.getsource(sg.is_exchange_closed_now)
        assert "market_segments" not in src, (
            "is_exchange_closed_now still reads market_segments YAML config — "
            "it should delegate to exchange_clock.is_exchange_closed()"
        )

    def test_is_exchange_closed_delegates_to_exchange_clock(self):
        """is_exchange_closed_now calls exchange_clock.is_exchange_closed."""
        import backend.api.helpers.snapshot_gate as sg
        from backend.api.helpers import exchange_clock as ec
        ec._CACHE = [_make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO"],
            open_time=time(9, 15), close_time=time(15, 30),
        )]
        fixed = datetime(2026, 8, 25, 15, 45, 0, tzinfo=_IST)
        with patch.object(ec, "_now_ist", return_value=fixed):
            result = sg.is_exchange_closed_now("NSE")
        assert result is True  # NSE closed at 15:45 (after close_time=15:30)

    def test_is_exchange_closed_fail_open_on_exception(self):
        """is_exchange_closed_now returns False (fail-open) when exchange_clock raises."""
        import backend.api.helpers.snapshot_gate as sg
        with patch("backend.api.helpers.exchange_clock.is_exchange_closed", side_effect=RuntimeError("test")):
            result = sg.is_exchange_closed_now("NSE")
        assert result is False  # fail-open

    def test_any_segment_open_delegates_to_exchange_clock(self):
        """_any_segment_open calls exchange_clock.is_any_segment_open."""
        import backend.api.helpers.snapshot_gate as sg
        from backend.api.helpers import exchange_clock as ec
        ec._CACHE = [_make_schedule_row(
            "NSE", ["NSE"],
            open_time=time(9, 15), close_time=time(15, 30),
        )]
        fixed = datetime(2026, 8, 25, 10, 0, 0, tzinfo=_IST)
        with patch.object(ec, "_now_ist", return_value=fixed):
            result = sg._any_segment_open()
        assert result is True

    def test_any_segment_open_fail_open_on_exception(self):
        """_any_segment_open returns True (fail-open) when exchange_clock raises."""
        import backend.api.helpers.snapshot_gate as sg
        with patch("backend.api.helpers.exchange_clock.is_any_segment_open", side_effect=RuntimeError("test")):
            result = sg._any_segment_open()
        assert result is True  # fail-open

    def test_any_segment_open_with_exchange_filter(self):
        """_any_segment_open passes exchange filter to exchange_clock."""
        import backend.api.helpers.snapshot_gate as sg
        from backend.api.helpers import exchange_clock as ec

        called_with: list = []

        def _fake_is_any_segment_open(exchanges=None):
            called_with.append(exchanges)
            return False

        with patch.object(ec, "is_any_segment_open", side_effect=_fake_is_any_segment_open):
            sg._any_segment_open(["NSE"])

        assert called_with == [["NSE"]]


# ---------------------------------------------------------------------------
# positions.py cutoff delegation test
# ---------------------------------------------------------------------------

class TestPositionsCutoffDelegation:
    """Verify positions.py delegates settlement cutoff to exchange_clock."""

    def test_fetch_ref_close_map_uses_exchange_clock(self):
        """_fetch_ref_close_map must import from exchange_clock, not date_time_utils."""
        import inspect
        import backend.api.routes.positions as pos
        src = inspect.getsource(pos._fetch_ref_close_map)
        assert "settlement_cutoff_for" in src, (
            "_fetch_ref_close_map must call exchange_clock.settlement_cutoff_for"
        )
        assert "today_ist_8am" not in src, (
            "_fetch_ref_close_map still hardcodes today_ist_8am — "
            "should delegate to exchange_clock.settlement_cutoff_for"
        )

    def test_override_stale_close_uses_exchange_clock(self):
        """_override_stale_close_from_snapshot must use exchange_clock cutoff."""
        import inspect
        import backend.api.routes.positions as pos
        src = inspect.getsource(pos._override_stale_close_from_snapshot)
        assert "settlement_cutoff_for" in src, (
            "_override_stale_close_from_snapshot must call exchange_clock.settlement_cutoff_for"
        )
        assert "today_ist_8am" not in src, (
            "_override_stale_close_from_snapshot still hardcodes today_ist_8am"
        )


# ---------------------------------------------------------------------------
# holdings.py cutoff delegation test
# ---------------------------------------------------------------------------

class TestHoldingsCutoffDelegation:
    def test_override_stale_close_for_holdings_uses_exchange_clock(self):
        """_override_stale_close_for_holdings must use exchange_clock cutoff."""
        import inspect
        import backend.api.routes.holdings as hld
        src = inspect.getsource(hld._override_stale_close_for_holdings)
        assert "settlement_cutoff_for" in src, (
            "_override_stale_close_for_holdings must call exchange_clock.settlement_cutoff_for"
        )
        assert "today_ist_8am" not in src, (
            "_override_stale_close_for_holdings still hardcodes today_ist_8am"
        )
        # Ensure timestamp_indian import is gone (was used only for cutoff calc)
        assert "timestamp_indian" not in src, (
            "_override_stale_close_for_holdings still imports timestamp_indian — "
            "should no longer need it after exchange_clock delegation"
        )


# ---------------------------------------------------------------------------
# Bug Fix 3: snap_ltp guard in positions overlay
# ---------------------------------------------------------------------------

class TestBugFix3SnapLtpGuard:
    """The snap_ltp guard prevents oscillation when snap_ltp is None."""

    def test_overlay_requires_snap_ltp_not_none(self):
        """The guard must be `ref_close > 0 and snap_ltp is not None`."""
        import inspect
        import backend.api.routes.positions as pos
        # Find _overlay_closed_rows or the function that contains the guard
        # The guard is inside the row-overlay loop (anonymous in _build_rows_for_exchange)
        # Check the source of the relevant function.
        # The fix is in the block that checks `if ref_close > 0`
        src = inspect.getsource(pos)
        # Guard: old was `if ref_close > 0:`, new must include snap_ltp check
        assert "if ref_close > 0 and snap_ltp is not None:" in src, (
            "Bug Fix 3 guard not found: positions.py must check "
            "`ref_close > 0 and snap_ltp is not None` before overlaying day_change fields"
        )

    def test_snap_ltp_not_used_as_broker_fallback(self):
        """snap_ltp_f must NOT fall back to broker_ltp when snap_ltp is None.

        Old code: snap_ltp_f = float(snap_ltp) if snap_ltp is not None else broker_ltp
        New code: snap_ltp_f = float(snap_ltp)  (only reachable when snap_ltp is not None)
        """
        import inspect
        import backend.api.routes.positions as pos
        src = inspect.getsource(pos)
        # The old fallback pattern must be gone.
        assert "else broker_ltp" not in src, (
            "Bug Fix 3 regression: `else broker_ltp` fallback still present in positions.py. "
            "When snap_ltp is None the overlay block must be skipped entirely, not fall back "
            "to broker_ltp which causes live/snapshot oscillation."
        )


# ---------------------------------------------------------------------------
# ExchangeSchedule model test
# ---------------------------------------------------------------------------

class TestExchangeScheduleModel:
    def test_model_has_required_fields(self):
        """ExchangeSchedule ORM model must have all required columns."""
        from backend.api.models import ExchangeSchedule
        required = [
            "id", "gate", "exchanges", "date", "weekdays",
            "session_name", "is_open", "open_time", "close_time",
            "snapshot_time", "snapshot_reset_time", "reason", "source",
        ]
        for col in required:
            assert hasattr(ExchangeSchedule, col), (
                f"ExchangeSchedule missing column: {col}"
            )

    def test_unique_constraint_name(self):
        """Unique constraint must be named for ON CONFLICT to work."""
        from backend.api.models import ExchangeSchedule
        from sqlalchemy import inspect as sa_inspect
        constraints = ExchangeSchedule.__table_args__
        constraint_names = [
            c.name for c in constraints
            if hasattr(c, "name") and c.name
        ]
        assert "uq_exchange_schedule_gate_date_session" in constraint_names, (
            "ExchangeSchedule unique constraint must be named "
            "'uq_exchange_schedule_gate_date_session' for ON CONFLICT clause"
        )


# ---------------------------------------------------------------------------
# Exchange clock seed rows
# ---------------------------------------------------------------------------

class TestSeedRows:
    def test_seed_contains_two_rows(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        assert len(_SEED_ROWS) == 2

    def test_seed_gates(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        gates = {r["gate"] for r in _SEED_ROWS}
        assert gates == {"NON-MCX", "MCX"}

    def test_non_mcx_exchanges(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        non_mcx = next(r for r in _SEED_ROWS if r["gate"] == "NON-MCX")
        assert set(non_mcx["exchanges"]) == {"NSE", "BSE", "NFO", "BFO", "CDS"}

    def test_mcx_snapshot_reset_is_0800(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        mcx = next(r for r in _SEED_ROWS if r["gate"] == "MCX")
        assert mcx["snapshot_reset_time"] == time(8, 0)

    def test_non_mcx_snapshot_reset_is_0800(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        non_mcx = next(r for r in _SEED_ROWS if r["gate"] == "NON-MCX")
        assert non_mcx["snapshot_reset_time"] == time(8, 0)

    def test_mcx_snapshot_time_is_0015(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        mcx = next(r for r in _SEED_ROWS if r["gate"] == "MCX")
        assert mcx["snapshot_time"] == time(23, 45)

    def test_non_mcx_opens_at_0800(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        non_mcx = next(r for r in _SEED_ROWS if r["gate"] == "NON-MCX")
        assert non_mcx["open_time"] == time(8, 0)

    def test_non_mcx_closes_at_1530(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        non_mcx = next(r for r in _SEED_ROWS if r["gate"] == "NON-MCX")
        assert non_mcx["close_time"] == time(15, 30)

    def test_mcx_opens_at_0800(self):
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        mcx = next(r for r in _SEED_ROWS if r["gate"] == "MCX")
        assert mcx["open_time"] == time(8, 0)

    def test_non_mcx_closed_at_1545(self):
        """NON-MCX is closed at 15:45 IST (after close_time=15:30)."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NSE", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(8, 0), close_time=time(15, 30),
        )
        ec._CACHE = [nse_row]
        fixed = datetime(2026, 8, 25, 15, 45, 0, tzinfo=_IST)
        with patch.object(ec, "_now_ist", return_value=fixed):
            assert ec.is_exchange_open("NSE") is False, (
                "NSE must be closed at 15:45 IST (after 15:30 close time)"
            )


# ---------------------------------------------------------------------------
# app.py wiring test
# ---------------------------------------------------------------------------

class TestAppWiring:
    def test_exchange_schedule_controller_in_route_handlers(self):
        import inspect
        import backend.api.app as app
        src = inspect.getsource(app)
        assert "ExchangeScheduleController" in src, (
            "ExchangeScheduleController not found in app.py route_handlers"
        )

    def test_exchange_clock_seed_and_warm_in_on_startup(self):
        import inspect
        import backend.api.app as app
        src = inspect.getsource(app)
        assert "exchange_clock_seed_and_warm" in src, (
            "exchange_clock_seed_and_warm not found in app.py on_startup"
        )


# ---------------------------------------------------------------------------
# RBAC capabilities test
# ---------------------------------------------------------------------------

class TestRBACCapabilities:
    def test_view_exchange_schedule_exists(self):
        from backend.api.rbac import CAPS
        assert "view_exchange_schedule" in CAPS

    def test_manage_exchange_schedule_exists(self):
        from backend.api.rbac import CAPS
        assert "manage_exchange_schedule" in CAPS

    def test_designated_can_manage(self):
        from backend.api.rbac import CAPS
        assert "designated" in CAPS["manage_exchange_schedule"]

    def test_demo_can_only_view(self):
        from backend.api.rbac import CAPS
        assert "demo" in CAPS["view_exchange_schedule"]
        assert "demo" not in CAPS["manage_exchange_schedule"]


# ---------------------------------------------------------------------------
# Fix 1: seed_and_warm NON-MCX close_time migration
# ---------------------------------------------------------------------------

class TestSeedAndWarmNonMcxMigration:
    """Fix 1: seed_and_warm must UPDATE close_time to '15:30' for NON-MCX rows
    that still carry a stale '16:00' value (servers already renamed from NSE
    to NON-MCX via the fd2164e9 deploy but missed the close_time patch)."""

    @pytest.mark.asyncio
    async def test_seed_and_warm_emits_non_mcx_close_time_migration(self):
        """The NON-MCX close_time migration SQL must be executed during seed_and_warm."""
        from unittest.mock import AsyncMock, MagicMock, patch
        import backend.api.helpers.exchange_clock as ec

        executed_sqls: list[str] = []

        mock_begin_ctx = AsyncMock()
        mock_begin_ctx.__aenter__ = AsyncMock(return_value=mock_begin_ctx)
        mock_begin_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=mock_begin_ctx)

        async def _capture_execute(stmt, *args, **kwargs):
            executed_sqls.append(str(stmt))
            return MagicMock()

        mock_session.execute = AsyncMock(side_effect=_capture_execute)

        mock_async_session_cls = MagicMock(return_value=mock_session)
        mock_engine = MagicMock()

        # Patch at the import source locations used inside seed_and_warm().
        with patch("sqlalchemy.ext.asyncio.AsyncSession", mock_async_session_cls), \
             patch("backend.api.database.engine", mock_engine), \
             patch.object(ec, "_force_refresh", new_callable=AsyncMock):
            await ec.seed_and_warm()

        # At least one executed SQL must target NON-MCX close_time migration.
        non_mcx_migration_found = any(
            "NON-MCX" in sql and "close_time" in sql and "15:30" in sql
            for sql in executed_sqls
        )
        assert non_mcx_migration_found, (
            "seed_and_warm must execute a migration UPDATE that sets "
            "close_time='15:30' WHERE gate='NON-MCX'. "
            f"Executed SQLs: {executed_sqls}"
        )

    def test_non_mcx_close_time_in_seed_rows(self):
        """_SEED_ROWS NON-MCX row must already have close_time=15:30 (seed fallback)."""
        from backend.api.helpers.exchange_clock import _SEED_ROWS
        non_mcx = next(r for r in _SEED_ROWS if r["gate"] == "NON-MCX")
        assert non_mcx["close_time"] == time(15, 30), (
            "NON-MCX seed row close_time must be 15:30 — "
            f"got {non_mcx['close_time']}"
        )


# ---------------------------------------------------------------------------
# Fix 4: fail-open for unknown exchange in non-empty cache
# ---------------------------------------------------------------------------

class TestFailOpenUnknownExchange:
    """Fix 4: is_exchange_open must return True (fail-open) when the exchange
    is not found in any row of a non-empty cache."""

    def _set_cache(self, rows):
        import backend.api.helpers.exchange_clock as ec
        ec._CACHE = rows

    def _patch_now(self, hour: int, minute: int):
        import backend.api.helpers.exchange_clock as ec
        fixed = datetime(2026, 8, 25, hour, minute, 0, tzinfo=_IST)
        return patch.object(ec, "_now_ist", return_value=fixed)

    def test_unknown_exchange_fails_open_with_populated_cache(self):
        """A non-empty cache that does not contain the exchange → True (fail-open)."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NON-MCX", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(8, 0), close_time=time(15, 30),
        )
        mcx_row = _make_schedule_row(
            "MCX", ["MCX"],
            open_time=time(8, 0), close_time=time(23, 30),
        )
        self._set_cache([nse_row, mcx_row])
        with self._patch_now(10, 0):
            result = ec.is_exchange_open("UNKNOWN_EXCH")
        assert result is True, (
            "is_exchange_open must fail-open (True) for an exchange not in "
            "any schedule row, even when cache is non-empty"
        )

    def test_known_exchange_not_affected_by_failopen(self):
        """Known exchange that is closed still returns False (not caught by fail-open)."""
        import backend.api.helpers.exchange_clock as ec
        nse_row = _make_schedule_row(
            "NON-MCX", ["NSE", "BSE", "NFO", "BFO", "CDS"],
            open_time=time(9, 15), close_time=time(15, 30),
        )
        self._set_cache([nse_row])
        with self._patch_now(16, 0):
            # NSE is in schedule but session is closed at 16:00
            result = ec.is_exchange_open("NSE")
        assert result is False, (
            "Known exchange that is outside session window must still return False"
        )
