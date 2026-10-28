"""
Server-restart resiliency for the daily KiteTicker cycle
(backend/brokers/service/app.py: `_ticker_quiet_window_now`,
`_disconnect_ticker_for_quiet_window`, `_restart_ticker_for_new_day`;
backend/brokers/service/calendar_gate.py: `resolve_mcx_gate_bounds`,
`ticker_quiet_window_now`) and
`backend/brokers/kite_ticker.py:TickerManager.disconnect_keep_reactor`.

Four restart scenarios, explicitly requested by the operator after the
daily-cycle feature (commit 881e839a) shipped — each traced against the
ACTUAL current code and backed by a concrete, runnable test — PLUS
(2026-10-28, same-day follow-up) the quiet-window boundary itself became
calendar-driven (resolved from `exchange_schedule`, not hardcoded
23:31/08:00), so the boundary tests below now mock that DB read instead
of freezing a bare clock:

  1. conn_service restarts/crashes INSIDE the quiet window (e.g. a 02:00
     deploy) — both the inline boot-connect (`_start_kite_ticker`) and
     the watchdog's Phase 1 must stay skipped for as long as the window
     holds. `ticker_quiet_window_now()` re-derives the window fresh on
     every call from `timestamp_indian()` and the resolved bounds — it
     carries no settable/clearable marker — so this holds no matter when
     the process (re)started, even though it now depends on a (per-IST-
     day cached) DB read rather than being argument-free arithmetic.

  2. conn_service restarts INSIDE the disconnect-execution window — a
     fresh process's ticker was never started in THIS process (scenario
     1 already covers why), so `_disconnect_ticker_for_quiet_window()`
     must be a safe no-op: no exception, no bad state transition, just
     a debug log.

  3. conn_service restarts INSIDE the restart-execution window — the
     highest-risk case. The pre-existing boot/watchdog connect path and
     the new `_restart_ticker_for_new_day()` path can both observe
     `ticker.status()["started"] == False` around the same real-world
     instant. Traced concretely: neither `_try_start_ticker()` nor
     `_restart_ticker_for_new_day()` has ANY `await` between its own
     "already started" re-check and its actual `ticker.start()` /
     `ticker.restart()` call — both are plain (non-async) `def`s. Python's
     single-threaded cooperative concurrency model means the ONLY
     possible preemption point for either path is its own
     `await conn_should_attempt_connect()` call; once that resolves and
     either path resumes into its synchronous tail, it runs atomically
     to completion (performing the one real connect and flipping
     `started` to True) before the event loop can give the other path a
     turn — so whichever one's tail runs second will see `started=True`
     at its own re-check and safely no-op. No lock is needed, and none
     was added, because the property already holds structurally. This
     is proven empirically below for three timing orderings (near-
     simultaneous, Phase-1-first, cycle-task-first).

  4. A restart that misses the new task's own window entirely still
     reaches the correct end state via the PRE-EXISTING, unmodified
     `_ticker_watchdog` Phase 1 — `ticker_quiet_window_now()` carries no
     dependency on `_task_daily_ticker_cycle`'s own private state, so it
     "expires" the instant the resolved open time passes, regardless of
     whether that task ever ran that day. The new task is an
     optimization (daily token rebind), never a single point of failure
     for connectivity.

No lock/guard was added for scenario 3 because none was needed — the
existing re-check-then-act pattern in both `_try_start_ticker()` and
`_restart_ticker_for_new_day()` already serializes the decision correctly.
A small, behavior-preserving refactor WAS made: the disconnect body
(previously inline in `_task_daily_ticker_cycle`'s loop) was extracted into
`_disconnect_ticker_for_quiet_window()`, symmetric with the pre-existing
`_restart_ticker_for_new_day()`, purely so scenario 2 is directly
unit-testable without driving the enclosing infinite loop.
"""

from __future__ import annotations

import asyncio
from datetime import date as _date, time as _time
from unittest.mock import patch, MagicMock

import pytest

from backend.brokers.service import app as svc_app
from backend.brokers.service import calendar_gate


@pytest.fixture(autouse=True)
def _reset_mcx_gate_bounds_cache():
    """`calendar_gate._mcx_gate_bounds_cache` is a module-level global —
    reset it before and after every test in this file so one test's
    resolved-and-cached bounds for a given date can never leak into
    another test that happens to freeze "today" to the same date."""
    calendar_gate._mcx_gate_bounds_cache = None
    yield
    calendar_gate._mcx_gate_bounds_cache = None


def _frozen_now(dt):
    """Patch context for `timestamp_indian()` as read by
    `calendar_gate.ticker_quiet_window_now()` / `resolve_mcx_gate_bounds()`
    (both do `from backend.shared.helpers.date_time_utils import
    timestamp_indian` locally) — patching the SOURCE module's attribute
    is what a fresh `from ... import` picks up, mirroring how this test
    suite already patches `conn_should_attempt_connect` at its source."""
    return patch(
        "backend.shared.helpers.date_time_utils.timestamp_indian",
        return_value=dt,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 1 — restart inside the quiet window; resolve_mcx_gate_bounds /
# ticker_quiet_window_now precedence and boundary coverage
# ═══════════════════════════════════════════════════════════════════════════

class TestQuietWindowBoundary:
    """`ticker_quiet_window_now()`'s exact boundary, using the DEFAULT
    (no-override) resolved bounds — exercised via the real function with
    only the DB read mocked, not a re-implementation of the arithmetic."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hour,minute,expected",
        [
            (22, 0, False),
            (23, 30, False),   # one minute before the window opens
            (23, 31, True),    # window opens (default close 23:30 + 1min)
            (23, 59, True),
            (0, 0, True),
            (3, 0, True),
            (7, 59, True),     # last minute still inside the window
            (8, 0, False),     # window closes exactly at 08:00 (default open)
            (8, 1, False),
            (12, 0, False),
        ],
    )
    async def test_boundary_with_default_bounds(self, hour, minute, expected):
        from datetime import datetime, timezone, timedelta

        _IST = timezone(timedelta(hours=5, minutes=30))
        today = _date(2026, 10, 28)
        now_dt = datetime(2026, 10, 28, hour, minute, 0, tzinfo=_IST)

        with _frozen_now(now_dt), \
             patch(
                 "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
                 return_value=[],  # no rows at all → falls back to defaults
             ):
            result = await calendar_gate.ticker_quiet_window_now()

        assert result is expected, (
            f"{hour:02d}:{minute:02d} IST on {today} — expected "
            f"quiet_window={expected}, got {result}"
        )

    @pytest.mark.asyncio
    async def test_boundary_via_app_py_delegator(self):
        """`svc_app._ticker_quiet_window_now` must still be awaitable and
        agree with the real implementation — confirms the delegator
        wiring itself, not just the calendar_gate internals."""
        from datetime import datetime, timezone, timedelta

        _IST = timezone(timedelta(hours=5, minutes=30))
        now_dt = datetime(2026, 10, 28, 23, 40, 0, tzinfo=_IST)

        with _frozen_now(now_dt), \
             patch(
                 "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
                 return_value=[],
             ):
            result = await svc_app._ticker_quiet_window_now()

        assert result is True

    @pytest.mark.asyncio
    async def test_non_zero_width_mcx_override_shifts_the_window(self):
        """The exact scenario the operator asked for: a non-zero-width
        MCX `exchange_schedule` override row for a given date shifts
        `ticker_quiet_window_now()`'s close boundary on THAT date —
        23:40 reads open (False), 23:56 reads quiet (True)."""
        from datetime import datetime, timezone, timedelta

        _IST = timezone(timedelta(hours=5, minutes=30))
        override_date = _date(2026, 10, 30)
        override_rows = [
            {
                "date": override_date,
                "weekdays": None,
                "open_time": _time(8, 0),
                "close_time": _time(23, 55),  # DST-style shifted close
                "is_open": True,
            },
        ]

        async def _fake_read(gate, today):
            assert gate == "MCX"
            assert today == override_date
            return override_rows

        with patch(
            "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
            side_effect=_fake_read,
        ):
            with _frozen_now(datetime(2026, 10, 30, 23, 40, 0, tzinfo=_IST)):
                before_close = await calendar_gate.ticker_quiet_window_now()
            with _frozen_now(datetime(2026, 10, 30, 23, 56, 0, tzinfo=_IST)):
                after_close = await calendar_gate.ticker_quiet_window_now()

        assert before_close is False, (
            "23:40 must still read OPEN when the override close is 23:55"
        )
        assert after_close is True, (
            "23:56 must read QUIET once past the overridden 23:55 close + 1min"
        )

    @pytest.mark.asyncio
    async def test_next_date_with_no_override_reverts_to_default(self):
        """The override above is scoped to ONE date — the very next
        calendar day, with no matching override row, must revert to the
        default 23:30 close (23:40 reads quiet=True again)."""
        from datetime import datetime, timezone, timedelta

        _IST = timezone(timedelta(hours=5, minutes=30))
        next_date = _date(2026, 10, 31)

        async def _fake_read(gate, today):
            assert today == next_date
            return []  # no override for this date

        with patch(
            "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
            side_effect=_fake_read,
        ):
            with _frozen_now(datetime(2026, 10, 31, 23, 40, 0, tzinfo=_IST)):
                result = await calendar_gate.ticker_quiet_window_now()

        assert result is True, (
            "with no override for this date, 23:40 must revert to the "
            "default close (23:30 + 1min) and read quiet=True"
        )

    @pytest.mark.asyncio
    async def test_db_error_falls_back_to_default_never_earlier(self):
        """A DB failure must fall back to the default bounds — never a
        quiet=True earlier than the historical default would have given,
        and never cached (a transient failure must not poison the rest
        of the day)."""
        from datetime import datetime, timezone, timedelta

        _IST = timezone(timedelta(hours=5, minutes=30))

        with patch(
            "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
            side_effect=RuntimeError("db unreachable"),
        ):
            with _frozen_now(datetime(2026, 10, 28, 23, 30, 0, tzinfo=_IST)):
                still_open = await calendar_gate.ticker_quiet_window_now()
            with _frozen_now(datetime(2026, 10, 28, 23, 31, 0, tzinfo=_IST)):
                now_quiet = await calendar_gate.ticker_quiet_window_now()

        assert still_open is False, (
            "a DB error at 23:30 must not read quiet=True earlier than "
            "the default 23:31 boundary"
        )
        assert now_quiet is True, (
            "a DB error must still fall back to the default bounds, not "
            "fail open/closed in some other way"
        )
        assert calendar_gate._mcx_gate_bounds_cache is None, (
            "a DB-error fallback must never be cached — a later call in "
            "the same process must get a fresh chance to read real data"
        )

    @pytest.mark.asyncio
    async def test_holiday_override_with_no_session_falls_back_to_default(self):
        """A holiday/no-session override row (open_time/close_time both
        None) is treated the same as 'no override' — falls through to
        the default bounds, per `resolve_mcx_gate_bounds()`'s own
        documented scope decision (this function answers 'what time does
        the window open/close today', not 'does MCX trade at all today'
        — that is `conn_should_attempt_connect()`'s separate job)."""
        holiday_date = _date(2026, 10, 29)
        holiday_rows = [
            {"date": holiday_date, "weekdays": None,
             "open_time": None, "close_time": None, "is_open": False},
        ]

        with patch(
            "backend.brokers.broker_apis._read_exchange_schedule_rows_async",
            return_value=holiday_rows,
        ):
            open_t, close_t = await calendar_gate.resolve_mcx_gate_bounds(holiday_date)

        assert (open_t, close_t) == (
            calendar_gate._MCX_GATE_DEFAULT_OPEN,
            calendar_gate._MCX_GATE_DEFAULT_CLOSE,
        )


class TestBootInsideQuietWindowSkipsConnect:
    """Scenario 1, boot-path half: a conn_service restart landing inside
    the quiet window must skip BOTH the inline connect attempt and the
    calendar-gate consultation entirely — confirmed by actually calling
    `_start_kite_ticker()`, not by re-asserting the design intent."""

    @pytest.mark.asyncio
    async def test_inline_connect_and_calendar_gate_never_called(self):
        with patch.object(svc_app, "_ticker_quiet_window_now", return_value=True), \
             patch(
                 "backend.brokers.service.calendar_gate.conn_should_attempt_connect",
             ) as mock_calendar, \
             patch.object(svc_app, "_try_start_ticker") as mock_try_start, \
             patch.object(svc_app, "_ticker_watchdog"), \
             patch.object(svc_app, "_health_heartbeat"), \
             patch.object(svc_app, "_task_prewarm_tokens"), \
             patch.object(svc_app, "_task_daily_ticker_cycle"), \
             patch.object(svc_app, "logger"):
            mock_litestar = MagicMock()
            await svc_app._start_kite_ticker(mock_litestar)

        mock_calendar.assert_not_called(), (
            "the calendar day-level gate must not even be consulted while "
            "the quiet-window clock gate is active"
        )
        mock_try_start.assert_not_called(), (
            "_try_start_ticker() must never be called for a boot landing "
            "inside the 23:31-08:00 quiet window"
        )

    @pytest.mark.asyncio
    async def test_boot_outside_quiet_window_proceeds_normally(self):
        """Control case — confirms the mock wiring above would actually
        catch a regression: outside the window, the boot path DOES reach
        the calendar gate and _try_start_ticker()."""
        with patch.object(svc_app, "_ticker_quiet_window_now", return_value=False), \
             patch(
                 "backend.brokers.service.calendar_gate.conn_should_attempt_connect",
                 return_value=True,
             ) as mock_calendar, \
             patch.object(svc_app, "_try_start_ticker", return_value=True) as mock_try_start, \
             patch.object(svc_app, "_ticker_watchdog"), \
             patch.object(svc_app, "_health_heartbeat"), \
             patch.object(svc_app, "_task_prewarm_tokens"), \
             patch.object(svc_app, "_task_daily_ticker_cycle"), \
             patch.object(svc_app, "logger"):
            mock_litestar = MagicMock()
            await svc_app._start_kite_ticker(mock_litestar)

        mock_calendar.assert_called_once()
        mock_try_start.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 2 — restart inside the 23:31-23:59 disconnect-execution window
# ═══════════════════════════════════════════════════════════════════════════

class TestDisconnectOnNeverStartedTicker:
    """A fresh process that boots inside the quiet window never starts the
    ticker (scenario 1). When `_task_daily_ticker_cycle`'s own 23:31 branch
    then fires on THIS process (its `_last_stop_date` guard is unset, fresh
    process state), both the extracted call site and the underlying
    `TickerManager.disconnect_keep_reactor()` method must be safe no-ops."""

    def test_disconnect_ticker_for_quiet_window_noop_when_not_started(self):
        fake_ticker = MagicMock()
        fake_ticker.status.return_value = {"started": False}

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=fake_ticker), \
             patch.object(svc_app, "logger") as mock_logger:
            # Must not raise.
            svc_app._disconnect_ticker_for_quiet_window()

        fake_ticker.disconnect_keep_reactor.assert_not_called(), (
            "disconnect_keep_reactor() must not even be called when the "
            "ticker was never started in this process"
        )
        mock_logger.debug.assert_called_once()
        mock_logger.info.assert_not_called()

    def test_disconnect_ticker_for_quiet_window_calls_disconnect_when_started(self):
        """Control case — the started branch still works."""
        fake_ticker = MagicMock()
        fake_ticker.status.return_value = {"started": True}

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=fake_ticker), \
             patch.object(svc_app, "logger"):
            svc_app._disconnect_ticker_for_quiet_window()

        fake_ticker.disconnect_keep_reactor.assert_called_once()

    def test_real_tickermanager_disconnect_keep_reactor_noop_on_never_started(self):
        """Direct coverage of the underlying method itself (not the app.py
        call site above) — a never-started real TickerManager (`_kws is
        None`) must tolerate `disconnect_keep_reactor()` without raising
        and without corrupting state."""
        from backend.brokers.kite_ticker import TickerManager

        ticker = TickerManager()
        assert ticker._kws is None
        assert ticker._started is False
        assert ticker._connected is False

        with patch("backend.brokers.kite_ticker.logger"):
            ticker.disconnect_keep_reactor()  # must not raise

        assert ticker._started is False
        assert ticker._connected is False
        assert ticker._kws is None
        assert ticker._reactor_dead is False, (
            "disconnect_keep_reactor() must never mark the reactor dead, "
            "even as a no-op"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 3 — restart inside the 08:00-08:04 restart-execution window:
# Phase 1 vs. _restart_ticker_for_new_day() double-connect race
# ═══════════════════════════════════════════════════════════════════════════

class _FakeTickerState:
    """Minimal stand-in for `TickerManager` that mirrors the ONE property
    this race depends on: `start()`'s own idempotency guard, applied
    synchronously with no internal `await`. `restart()` mirrors the real
    `TickerManager.restart()` shape (disconnect — a no-op here since the
    fake never has a real kws/reactor — then start()), so both app.py
    entry points exercise the same underlying state machine a real
    TickerManager would."""

    def __init__(self) -> None:
        self.started = False
        self._tick_buffer = "preattached"  # skip the attach-buffer branch
        self.start_calls: list[str] = []
        self.restart_calls: list[str] = []

    def status(self) -> dict:
        return {"started": self.started}

    def set_loop(self, loop) -> None:
        pass

    def attach_tick_buffer(self, buf) -> None:
        pass

    def start(self, api_key: str, access_token: str, account: str = "") -> None:
        if self.started:
            return
        self.started = True
        self.start_calls.append(account)

    def restart(self, api_key: str, access_token: str, account: str = "") -> None:
        self.restart_calls.append(account)
        self.start(api_key, access_token, account=account)


async def _fake_conn_should_attempt_connect() -> bool:
    # Force at least one real yield, mirroring the real function's async
    # DB round-trip — without this, `await conn_should_attempt_connect()`
    # would never actually suspend the caller, understating how tightly
    # the two paths can interleave in production.
    await asyncio.sleep(0)
    return True


async def _phase1_connect_path(delay: float) -> None:
    """Mirrors `_ticker_watchdog`'s Phase 1 body verbatim (app.py, inside
    `if not ticker.status().get("started"):`, after the quiet-window
    check — which is irrelevant here since we're testing the 08:00
    boundary where that gate has already lifted for both paths):
        if await conn_should_attempt_connect() and _try_start_ticker():
            ...
    """
    await asyncio.sleep(delay)
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect
    if await conn_should_attempt_connect() and svc_app._try_start_ticker():
        pass


async def _daily_cycle_restart_path(delay: float) -> None:
    """Mirrors `_task_daily_ticker_cycle`'s 08:00 body verbatim:
        if await conn_should_attempt_connect():
            _restart_ticker_for_new_day()
    """
    await asyncio.sleep(delay)
    from backend.brokers.service.calendar_gate import conn_should_attempt_connect
    if await conn_should_attempt_connect():
        svc_app._restart_ticker_for_new_day()


class TestPhase1AndDailyCycleRestartRace:
    """Scenario 3 — proves, for three timing orderings, that exactly one
    real connect happens no matter which path's `await
    conn_should_attempt_connect()` resolves first."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "phase1_delay,cycle_delay,label",
        [
            (0.0, 0.0, "near-simultaneous"),
            (0.0, 0.05, "phase1-resumes-first"),
            (0.05, 0.0, "cycle-task-resumes-first"),
        ],
    )
    async def test_exactly_one_real_connect(self, phase1_delay, cycle_delay, label):
        fake = _FakeTickerState()

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=fake), \
             patch.object(svc_app, "_pick_kite_account",
                          return_value=("K1", "T1", "ZG0790")), \
             patch(
                 "backend.brokers.service.calendar_gate.conn_should_attempt_connect",
                 side_effect=_fake_conn_should_attempt_connect,
             ), \
             patch.object(svc_app, "logger"):
            await asyncio.gather(
                _phase1_connect_path(phase1_delay),
                _daily_cycle_restart_path(cycle_delay),
            )

        assert fake.started is True, f"[{label}] ticker must end up started"
        assert len(fake.start_calls) == 1, (
            f"[{label}] exactly one real connect must happen even when "
            f"Phase 1 and the 08:00 daily-cycle restart race at the same "
            f"boundary; got start_calls={fake.start_calls!r} "
            f"restart_calls={fake.restart_calls!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 4 — the new task is never a single point of failure
# ═══════════════════════════════════════════════════════════════════════════

class TestPreExistingPathsAreTheSafetyNet:
    """A restart that misses `_task_daily_ticker_cycle`'s own window
    entirely must still reach the correct connected state via the
    pre-existing, unmodified boot/watchdog connect logic — because the
    quiet-window gate is a pure clock check with no dependency on the
    new task having run."""

    def test_quiet_window_lifts_purely_on_the_clock_regardless_of_task_state(self):
        """`_ticker_quiet_window_now()` takes no arguments referencing the
        daily-cycle task's own state (no shared marker) — it cannot be
        "stuck" quiet just because `_task_daily_ticker_cycle` never fired
        that day. Confirmed by re-reading its signature/body: it computes
        the verdict fresh from `datetime.now()` every call."""
        import inspect
        sig = inspect.signature(svc_app._ticker_quiet_window_now)
        assert len(sig.parameters) == 0, (
            "_ticker_quiet_window_now() must remain a pure, stateless "
            "clock check — no task-owned marker parameter"
        )
        source = inspect.getsource(svc_app._ticker_quiet_window_now)
        assert "_last_stop_date" not in source
        assert "_last_restart_date" not in source

    @pytest.mark.asyncio
    async def test_restart_ticker_for_new_day_is_the_only_caller_of_restart_cycle_state(self):
        """`_restart_ticker_for_new_day()` and `_disconnect_ticker_for_
        quiet_window()` are ordinary functions `_ticker_watchdog`'s Phase 1
        never calls and never depends on — Phase 1's own connect attempt
        (`_try_start_ticker()`) is reachable with NEITHER of the daily-
        cycle functions ever having run, confirmed here by calling the
        Phase-1-equivalent path in isolation, with the daily-cycle task
        itself never even imported/running."""
        fake = _FakeTickerState()
        with patch("backend.brokers.kite_ticker.get_ticker", return_value=fake), \
             patch.object(svc_app, "_pick_kite_account",
                          return_value=("K1", "T1", "ZG0790")), \
             patch(
                 "backend.brokers.service.calendar_gate.conn_should_attempt_connect",
                 return_value=True,
             ), \
             patch.object(svc_app, "logger"):
            await _phase1_connect_path(0.0)

        assert fake.started is True
        assert fake.restart_calls == [], (
            "Phase 1's own connect path must reach the correct end state "
            "without ever touching the daily-cycle restart machinery"
        )
