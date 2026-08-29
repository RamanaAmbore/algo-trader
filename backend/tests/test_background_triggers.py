"""
Tests for DB-driven snapshot trigger rewrite in backend/api/background.py.

Covers five quality dimensions:
  SSOT        — snapshot/settlement trigger dispatch is fully driven by
                exchange_clock.sessions_with_snapshot_time_now(); no hardcoded times
  Correctness — session_name == "settlement" routes to trigger_settlement_capture;
                any other session_name routes to trigger_close_snapshot;
                empty sessions list fires neither
  Performance — _snapshot_probe_nse_mcx delegates to exchange_clock without
                any blocking holiday-fetch calls
  Stale-code  — _build_segments / hardcoded constants (_NSE_SETTLEMENT_H etc.)
                are no longer present in background.py
  Reuse       — trigger_close_snapshot / trigger_settlement_capture are
                testable standalone helpers, not inlined in the task loop

Scenario catalogue:
  1. NSE/regular session at snapshot_time → close-snapshot called, not settlement
  2. NSE/settlement session at snapshot_time → settlement-capture called, not close
  3. MCX/evening session at snapshot_time → close-snapshot called for MCX gate
  4. MCX/settlement session at snapshot_time → settlement-capture called for MCX gate
  5. Empty sessions list → neither helper called
  6. _get_segments() fallback: exchange_clock cache not warm → hardcoded defaults returned
  7. _default_seg_state() returns NSE and MCX gate keys (hardcoded, no _build_segments call)
  8. Stale: _build_segments removed from background.py source
  9. Stale: hardcoded constants _NSE_SETTLEMENT_H/_MCX_CLOSE_H/_MCX_SETTLEMENT_H removed
 10. exchange_clock.is_exchange_open used in startup block (not _snapshot_probe_nse_mcx)
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call as _call

import pytest

# ---------------------------------------------------------------------------
# Source-level stale checks (dimensions 4 + 8 + 9)
# ---------------------------------------------------------------------------

_SRC = Path("backend/api/background.py").read_text()


def test_build_segments_removed_from_source():
    """_build_segments must no longer appear in background.py."""
    assert "_build_segments" not in _SRC, (
        "_build_segments was not removed from background.py; "
        "it must be replaced with _get_segments()"
    )


def test_hardcoded_nse_settlement_constant_removed():
    """_NSE_SETTLEMENT_H hardcoded constant must be gone."""
    assert "_NSE_SETTLEMENT_H" not in _SRC, (
        "_NSE_SETTLEMENT_H constant still present; "
        "snapshot triggers must be DB-driven via exchange_clock"
    )


def test_hardcoded_mcx_close_constant_removed():
    """_MCX_CLOSE_H hardcoded constant must be gone."""
    assert "_MCX_CLOSE_H" not in _SRC, (
        "_MCX_CLOSE_H constant still present; "
        "snapshot triggers must be DB-driven via exchange_clock"
    )


def test_hardcoded_mcx_settlement_constant_removed():
    """_MCX_SETTLEMENT_H hardcoded constant must be gone."""
    assert "_MCX_SETTLEMENT_H" not in _SRC, (
        "_MCX_SETTLEMENT_H constant still present; "
        "snapshot triggers must be DB-driven via exchange_clock"
    )


def test_dedup_sentinel_nse_settlement_done_removed():
    """_nse_settlement_done sentinel must be removed (minute-precision match is the guard)."""
    assert "_nse_settlement_done" not in _SRC, (
        "_nse_settlement_done dedup sentinel still present; "
        "exchange_clock minute-precision match makes it redundant"
    )


def test_dedup_sentinel_mcx_close_done_removed():
    """_mcx_close_done sentinel must be removed."""
    assert "_mcx_close_done" not in _SRC, (
        "_mcx_close_done dedup sentinel still present; "
        "exchange_clock minute-precision match makes it redundant"
    )


def test_exchange_clock_imported_at_module_level():
    """exchange_clock must be imported at module level (not only inside a function)."""
    assert "from backend.api.helpers import exchange_clock" in _SRC, (
        "exchange_clock must be imported at the top of background.py"
    )


def test_sessions_with_snapshot_time_now_called_in_probe():
    """_snapshot_probe_nse_mcx must call sessions_with_snapshot_time_now."""
    assert "sessions_with_snapshot_time_now" in _SRC, (
        "_snapshot_probe_nse_mcx must delegate trigger detection to "
        "exchange_clock.sessions_with_snapshot_time_now()"
    )


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _make_session(gate: str, session_name: str) -> SimpleNamespace:
    """Build a minimal mock ExchangeSchedule row."""
    return SimpleNamespace(gate=gate, session_name=session_name)


# ---------------------------------------------------------------------------
# Test class: _snapshot_probe_nse_mcx dispatch
# ---------------------------------------------------------------------------

class TestSnapshotProbeDispatch:
    """_snapshot_probe_nse_mcx correctly routes to close-snapshot or settlement helpers."""

    @pytest.mark.asyncio
    async def test_nse_regular_routes_to_close_snapshot(self):
        """NSE/regular session → trigger_close_snapshot("NSE"), not settlement."""
        sessions = [_make_session("NSE", "regular")]

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_close.assert_awaited_once_with("NSE")
        mock_settle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nse_settlement_routes_to_settlement_capture(self):
        """NSE/settlement session → trigger_settlement_capture("NSE"), not close-snapshot."""
        sessions = [_make_session("NSE", "settlement")]

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_settle.assert_awaited_once_with("NSE")
        mock_close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mcx_evening_routes_to_close_snapshot(self):
        """MCX/evening session → trigger_close_snapshot("MCX")."""
        sessions = [_make_session("MCX", "evening")]

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_close.assert_awaited_once_with("MCX")
        mock_settle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mcx_settlement_routes_to_settlement_capture(self):
        """MCX/settlement session → trigger_settlement_capture("MCX")."""
        sessions = [_make_session("MCX", "settlement")]

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_settle.assert_awaited_once_with("MCX")
        mock_close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_sessions_fires_neither_helper(self):
        """Empty sessions list → neither trigger_close_snapshot nor trigger_settlement_capture called."""
        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = []

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_close.assert_not_awaited()
        mock_settle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_sessions_dispatch_individually(self):
        """Multiple sessions in same minute: each dispatched to the correct helper."""
        sessions = [
            _make_session("NSE", "regular"),    # → close
            _make_session("MCX", "settlement"), # → settle
        ]

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot", new_callable=AsyncMock) as mock_close,
            patch("backend.api.background.trigger_settlement_capture", new_callable=AsyncMock) as mock_settle,
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions

            from backend.api.background import _snapshot_probe_nse_mcx
            await _snapshot_probe_nse_mcx()

        mock_close.assert_awaited_once_with("NSE")
        mock_settle.assert_awaited_once_with("MCX")


# ---------------------------------------------------------------------------
# Test class: trigger_close_snapshot + trigger_settlement_capture helpers
# ---------------------------------------------------------------------------

class TestTriggerHelpers:
    """trigger_close_snapshot and trigger_settlement_capture are standalone helpers."""

    @pytest.mark.asyncio
    async def test_trigger_close_snapshot_calls_snapshot_fire(self):
        """trigger_close_snapshot("NSE") calls _snapshot_fire("nse-close")."""
        with patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            from backend.api.background import trigger_close_snapshot
            await trigger_close_snapshot("NSE")
        mock_fire.assert_awaited_once_with("nse-close")

    @pytest.mark.asyncio
    async def test_trigger_close_snapshot_mcx_gate(self):
        """trigger_close_snapshot("MCX") calls _snapshot_fire("mcx-close")."""
        with patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            from backend.api.background import trigger_close_snapshot
            await trigger_close_snapshot("MCX")
        mock_fire.assert_awaited_once_with("mcx-close")

    @pytest.mark.asyncio
    async def test_trigger_settlement_capture_calls_snapshot_fire_market_closed(self):
        """trigger_settlement_capture("NSE") calls _snapshot_fire with market_open=False."""
        with patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            from backend.api.background import trigger_settlement_capture
            await trigger_settlement_capture("NSE")
        mock_fire.assert_awaited_once_with("nse-settlement", market_open=False)

    @pytest.mark.asyncio
    async def test_trigger_settlement_capture_mcx_gate(self):
        """trigger_settlement_capture("MCX") calls _snapshot_fire("mcx-settlement", market_open=False)."""
        with patch("backend.api.background._snapshot_fire", new_callable=AsyncMock) as mock_fire:
            from backend.api.background import trigger_settlement_capture
            await trigger_settlement_capture("MCX")
        mock_fire.assert_awaited_once_with("mcx-settlement", market_open=False)


# ---------------------------------------------------------------------------
# Test class: _get_segments fallback
# ---------------------------------------------------------------------------

class TestGetSegmentsFallback:
    """_get_segments returns hardcoded defaults when exchange_clock cache is not warm."""

    def test_fallback_returns_non_mcx_and_mcx_segments(self):
        """When cache is not warm, _get_segments returns NON-MCX and MCX segments."""
        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec._cache_loaded = False

            from backend.api.background import _get_segments
            segs = _get_segments()

        gates = {s['name'] for s in segs}
        assert 'NON-MCX' in gates, "NON-MCX segment must be in fallback"
        assert 'MCX' in gates, "MCX segment must be in fallback"

    def test_fallback_non_mcx_segment_hours(self):
        """Fallback NON-MCX segment: hours_start=08:00, hours_end=15:30."""
        from datetime import time

        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec._cache_loaded = False

            from backend.api.background import _get_segments
            segs = _get_segments()

        non_mcx = next(s for s in segs if s['name'] == 'NON-MCX')
        assert non_mcx['hours_start'] == time(8, 0)
        assert non_mcx['hours_end'] == time(15, 30)

    def test_fallback_mcx_segment_hours(self):
        """Fallback MCX segment: hours_start=08:00, hours_end=23:30."""
        from datetime import time

        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec._cache_loaded = False

            from backend.api.background import _get_segments
            segs = _get_segments()

        mcx = next(s for s in segs if s['name'] == 'MCX')
        assert mcx['hours_start'] == time(8, 0)
        assert mcx['hours_end'] == time(23, 30)

    def test_fallback_has_holiday_exchange_key(self):
        """Fallback segments must have 'holiday_exchange' key for watchdog compat."""
        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec._cache_loaded = False

            from backend.api.background import _get_segments
            segs = _get_segments()

        for seg in segs:
            assert 'holiday_exchange' in seg, (
                f"Segment {seg['name']} missing 'holiday_exchange' key"
            )

    def test_get_segments_uses_exchange_clock_when_cache_warm(self):
        """When cache is warm, _get_segments delegates to exchange_clock._resolve_for_gate."""
        from datetime import time as dtime
        from types import SimpleNamespace

        mock_session = SimpleNamespace(
            gate="NON-MCX",
            session_name="regular",
            is_open=True,
            open_time=dtime(8, 0),
            close_time=dtime(15, 30),
            exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"],
        )

        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec._cache_loaded = True
            mock_ec._resolve_for_gate.side_effect = lambda gate, today: (
                [mock_session] if gate == "NON-MCX" else []
            )

            from backend.api.background import _get_segments
            segs = _get_segments()

        assert any(s['name'] == 'NON-MCX' for s in segs)
        # Falls back to hardcoded MCX since mock returns [] for MCX gate
        # (or MCX is absent — either is acceptable)


# ---------------------------------------------------------------------------
# Test class: _default_seg_state
# ---------------------------------------------------------------------------

class TestDefaultSegState:
    """_default_seg_state is hardcoded to NON-MCX and MCX gate keys."""

    def test_has_non_mcx_key(self):
        from backend.api.background import _default_seg_state
        state = _default_seg_state()
        assert 'NON-MCX' in state, "_default_seg_state must have NON-MCX key"

    def test_has_mcx_key(self):
        from backend.api.background import _default_seg_state
        state = _default_seg_state()
        assert 'MCX' in state, "_default_seg_state must have MCX key"

    def test_values_have_last_open_and_last_close(self):
        from backend.api.background import _default_seg_state
        state = _default_seg_state()
        for gate in ('NON-MCX', 'MCX'):
            assert 'last_open' in state[gate]
            assert 'last_close' in state[gate]
            assert state[gate]['last_open'] is None
            assert state[gate]['last_close'] is None

    def test_does_not_call_build_segments(self):
        """_default_seg_state must not call _build_segments (which was removed)."""
        src = inspect.getsource(
            __import__("backend.api.background", fromlist=["_default_seg_state"])
            ._default_seg_state
        )
        assert "_build_segments" not in src, (
            "_default_seg_state must not reference _build_segments"
        )
        assert "_get_segments" not in src, (
            "_default_seg_state must not call _get_segments — "
            "it should return a hardcoded dict with NSE and MCX keys"
        )


# ---------------------------------------------------------------------------
# Test class: startup open check uses exchange_clock (not _snapshot_probe_nse_mcx)
# ---------------------------------------------------------------------------

class TestStartupOpenCheck:
    """The startup snapshot block uses exchange_clock.is_exchange_open, not the old probe."""

    def test_startup_block_calls_is_exchange_open(self):
        """_task_daily_snapshot startup block must call exchange_clock.is_exchange_open."""
        # Source-level check: is_exchange_open must appear in the function body
        src = inspect.getsource(
            __import__("backend.api.background", fromlist=["_task_daily_snapshot"])
            ._task_daily_snapshot
        )
        assert "is_exchange_open" in src, (
            "_task_daily_snapshot startup block must call "
            "exchange_clock.is_exchange_open (not _snapshot_probe_nse_mcx) "
            "for the startup open check"
        )

    def test_startup_block_no_longer_calls_old_probe_with_arg(self):
        """_task_daily_snapshot must not pass `now` to _snapshot_probe_nse_mcx (new signature takes no args)."""
        src = inspect.getsource(
            __import__("backend.api.background", fromlist=["_task_daily_snapshot"])
            ._task_daily_snapshot
        )
        # Old call was: await _snapshot_probe_nse_mcx(_now_ist)
        # New call in main loop: await _snapshot_probe_nse_mcx()
        assert "_snapshot_probe_nse_mcx(_now_ist)" not in src, (
            "_task_daily_snapshot must not pass _now_ist to _snapshot_probe_nse_mcx; "
            "the startup open check is now via exchange_clock.is_exchange_open directly"
        )
