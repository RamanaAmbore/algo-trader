"""
Tests for the 2026-11-23 fix (operator: "there are multiple bugs in
snapshot refresh, when snapshot is not taken at the end of mcx because
of server issue or connection with specific account").

Root cause, confirmed by reading backend/api/algo/daily_snapshot.py and
backend/api/background.py: `snapshot_daily_book()` already isolates a
per-account fetch failure (a connection/server blip at MCX close) inside
its own try/except and returns normally with that account's failure
recorded in `result["errors"]` — but the three call sites that invoke it
(`_snapshot_probe_nse_mcx`, `_sg_recover_mcx_snapshot`, via
`_snapshot_fire`) used to mark the daily "already fired" dedup sentinel
UNCONDITIONALLY, regardless of whether `errors` was empty. That account's
EOD `daily_book` row for the day was then never retried by anything: not
the next 30s tick (its own dedup check saw "already fired"), not the
SessionGuard restart catch-up path (same sentinel). The failure was also
only ever logged at INFO, never reaching the alert pipeline.

Fix, in `_snapshot_fire()`:
  - A clean run (no per-account errors) → returns True → sentinel marked,
    same as before.
  - A raised exception (e.g. DB unreachable) → returns True → sentinel
    still marked (preserves the original "don't retry-storm a systemic
    failure" intent).
  - A run with per-account errors but no exception → returns False →
    sentinel NOT marked, so the next 30s tick inside the same ±1 min
    snapshot_time window retries it — AND now logs at ERROR (was INFO),
    so ERROR_AGENT's existing blanket catch-all picks it up with zero new
    alert wiring.

Five quality dimensions:
  1. SSOT   — tests the real `_snapshot_fire`/`_snapshot_probe_nse_mcx`/
              `_sg_recover_mcx_snapshot` functions, not reimplementations.
  2. Perf   — no new polling mechanism; reuses the EXISTING ±1 min window
              and 30s cadence, confirmed via the dedup-sentinel behavior.
  3. Stale  — reproduces the exact pre-fix symptom (sentinel marked despite
              errors) and confirms it no longer happens.
  4. Reuse  — the fix is one shared decision point (`_snapshot_fire`'s
              return value), consumed identically by all three call sites.
  5. UX     — confirms the errors-present branch logs at ERROR (reaches
              the operator via ERROR_AGENT's catch-all) instead of INFO.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.background as bg


# ── _snapshot_fire itself: the three outcomes ──────────────────────────

class TestSnapshotFireReturnValue:
    @pytest.mark.asyncio
    async def test_clean_run_returns_true_and_logs_info(self):
        clean_result = {
            "accounts": ["ZG0790"], "holdings_rows": 1, "positions_rows": 2,
            "trades_rows": 0, "funds_rows": 1, "errors": [],
        }
        with patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book",
            new=AsyncMock(return_value=clean_result),
        ), patch("backend.api.background.logger") as mock_logger:
            result = await bg._snapshot_fire("mcx")

        assert result is True
        mock_logger.info.assert_called()
        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_account_error_returns_false_and_logs_error(self):
        """The exact reported scenario: one account's fetch failed
        (connection/server issue), snapshot_daily_book returns normally
        with that failure recorded, no exception raised."""
        degraded_result = {
            "accounts": ["ZG0790"], "holdings_rows": 1, "positions_rows": 2,
            "trades_rows": 0, "funds_rows": 1,
            "errors": ["Snapshot [ZJ6294] failed: Connection timed out"],
        }
        with patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book",
            new=AsyncMock(return_value=degraded_result),
        ), patch("backend.api.background.logger") as mock_logger:
            result = await bg._snapshot_fire("mcx")

        assert result is False, (
            "a per-account failure must NOT be treated as a clean fire — "
            "this is what allows the next 30s tick to retry it"
        )
        mock_logger.error.assert_called()
        error_msg = mock_logger.error.call_args[0][0]
        assert "ZJ6294" in error_msg
        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_raised_exception_returns_true_preserving_anti_storm(self):
        """A systemic failure (snapshot_daily_book itself raises) still
        marks the sentinel — unlike the per-account case, retrying this
        every 30s is unlikely to help and was the original "no retry
        storm" design intent."""
        with patch(
            "backend.api.algo.daily_snapshot.snapshot_daily_book",
            new=AsyncMock(side_effect=RuntimeError("DB unreachable")),
        ), patch("backend.api.background.logger") as mock_logger:
            result = await bg._snapshot_fire("mcx")

        assert result is True
        mock_logger.error.assert_called()


# ── _snapshot_probe_nse_mcx: sentinel only set when the fire was clean ──

def _make_session(gate: str, session_name: str):
    from types import SimpleNamespace
    return SimpleNamespace(gate=gate, session_name=session_name)


class TestProbeSentinelOnlyMarkedOnCleanFire:
    def setup_method(self):
        bg._snapshot_fired_today = {"NON-MCX": None, "MCX": None}

    @pytest.mark.asyncio
    async def test_sentinel_not_marked_when_close_snapshot_had_errors(self):
        sessions = [_make_session("MCX", "regular")]
        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot",
                  new=AsyncMock(return_value=False)),
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions
            await bg._snapshot_probe_nse_mcx()

        assert bg._snapshot_fired_today.get("MCX") is None, (
            "a degraded fire must leave the sentinel unset so the next "
            "30s tick retries this gate — this IS the reported bug"
        )

    @pytest.mark.asyncio
    async def test_sentinel_marked_when_close_snapshot_clean(self):
        sessions = [_make_session("MCX", "regular")]
        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot",
                  new=AsyncMock(return_value=True)),
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions
            await bg._snapshot_probe_nse_mcx()

        assert bg._snapshot_fired_today.get("MCX") == date.today()

    @pytest.mark.asyncio
    async def test_next_tick_retries_a_gate_that_previously_errored(self):
        """End-to-end within the probe: first tick errors (sentinel stays
        unset), second tick (same ±1 min window) succeeds and marks it —
        proving the retry path actually works, not just that the sentinel
        was left unset once."""
        sessions = [_make_session("MCX", "regular")]
        call_results = [False, True]

        async def _fake_trigger(gate):
            return call_results.pop(0)

        with (
            patch("backend.api.background.exchange_clock") as mock_ec,
            patch("backend.api.background.trigger_close_snapshot",
                  side_effect=_fake_trigger),
        ):
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions
            await bg._snapshot_probe_nse_mcx()
            assert bg._snapshot_fired_today.get("MCX") is None
            await bg._snapshot_probe_nse_mcx()
            assert bg._snapshot_fired_today.get("MCX") == date.today()

    @pytest.mark.asyncio
    async def test_settlement_skip_branch_still_marks_sentinel(self):
        """The settlement-skip branch never calls _snapshot_fire at all
        (it's a deliberate no-op), so it must still mark the sentinel —
        it has no "error" state to retry against."""
        sessions = [_make_session("NSE", "settlement")]
        with patch("backend.api.background.exchange_clock") as mock_ec:
            mock_ec.sessions_with_snapshot_time_now.return_value = sessions
            await bg._snapshot_probe_nse_mcx()

        assert bg._snapshot_fired_today.get("NSE") == date.today()


# ── _sg_recover_mcx_snapshot: same policy on the restart catch-up path ──

class TestSessionGuardRecoverySentinelPolicy:
    def setup_method(self):
        bg._snapshot_fired_today.pop("MCX", None)

    def teardown_method(self):
        bg._snapshot_fired_today.pop("MCX", None)

    @pytest.mark.asyncio
    async def test_sentinel_not_marked_when_fire_returns_false(self):
        from types import SimpleNamespace
        from datetime import datetime, time as dtime
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        today = date(2026, 9, 1)
        mcx_row = SimpleNamespace(close_time=dtime(23, 30), snapshot_time=None)
        now = datetime(2026, 9, 1, 23, 50, tzinfo=ist)

        with patch("backend.api.background._snapshot_fire",
                   new=AsyncMock(return_value=False)):
            await bg._sg_recover_mcx_snapshot([mcx_row], now, today)

        assert bg._snapshot_fired_today.get("MCX") is None

    @pytest.mark.asyncio
    async def test_sentinel_marked_when_fire_returns_true(self):
        from types import SimpleNamespace
        from datetime import datetime, time as dtime
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        today = date(2026, 9, 1)
        mcx_row = SimpleNamespace(close_time=dtime(23, 30), snapshot_time=None)
        now = datetime(2026, 9, 1, 23, 50, tzinfo=ist)

        with patch("backend.api.background._snapshot_fire",
                   new=AsyncMock(return_value=True)):
            await bg._sg_recover_mcx_snapshot([mcx_row], now, today)

        assert bg._snapshot_fired_today.get("MCX") == today
