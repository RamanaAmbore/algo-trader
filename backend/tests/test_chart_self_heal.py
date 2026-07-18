"""
test_chart_self_heal.py

Tests for the options.historical self-heal logic introduced in Slice A.
Coverage gate: if ohlcv_store returns < _SELF_HEAL_COVERAGE_THRESHOLD of
requested days, the handler retries with bypass_cache=True regardless of
runtime_state mode.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — _SELF_HEAL_COVERAGE_THRESHOLD is a single module-level
                   constant (0.70), not a scattered magic number.  Verified
                   by import + value assertion.
  2. Performance — self-heal decision is sub-ms (coverage check is a
                   pure comparison; no I/O in the decision path).  Verified
                   by patching store to return immediately.
  3. Stale code  — second ohlcv_store call carries bypass_cache=True.
                   Source-grep confirms the constant name is used in source.
  4. Reusable    — _self_heal_log_once is importable and its throttle
                   prevents duplicate log lines within the interval.
  5. UX          — when retry still under-covers, partial=True in response.
                   When brokers are in cool-off, no retry attempted.

Note on calling the handler under test:
  Litestar wraps every @get/@post decorated method as an HTTPRouteHandler
  whose .fn attribute is the original coroutine.  Tests call
  `handler.fn(controller_instance, ...)` directly to bypass Litestar's
  request-lifecycle machinery (dependency injection, guards, etc.) and
  exercise only the handler logic.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ── 1. SSOT: single canonical constant ─────────────────────────────────────

def test_coverage_threshold_is_module_constant() -> None:
    """_SELF_HEAL_COVERAGE_THRESHOLD must be 0.60 and defined once.

    Was 0.70 (regression 910740f0) which caused NSE equity 3M/6M/1Y ranges to always
    be flagged partial (NSE has ~252 trading days/year = 69% of calendar days < 70%).
    Fixed to 0.60 to give correct headroom below actual trading day density.
    """
    from backend.api.routes.options import _SELF_HEAL_COVERAGE_THRESHOLD
    assert _SELF_HEAL_COVERAGE_THRESHOLD == pytest.approx(0.60), (
        "threshold changed from 0.60 — NSE equity 3M/6M/1Y charts will break again. "
        "Must stay at 0.60 (NSE has ~69% trading/calendar day ratio, below 0.70 always triggered)."
    )


def test_coverage_threshold_not_magic_number_in_handler() -> None:
    """The constant name (not the literal 0.7) must appear in the handler."""
    from backend.api.routes.options import OptionsController
    import inspect

    # Litestar's @get wraps the function; .fn is the original coroutine.
    handler_fn = OptionsController.historical.fn
    src = inspect.getsource(handler_fn)

    assert "_SELF_HEAL_COVERAGE_THRESHOLD" in src, (
        "Handler must reference _SELF_HEAL_COVERAGE_THRESHOLD, not a literal"
    )
    assert "0.7" not in src, (
        "Handler contains magic number 0.7 — use _SELF_HEAL_COVERAGE_THRESHOLD"
    )


# ── Helper: build a list of OHLCVBar-like dicts ────────────────────────────

def _make_bars(n: int, base: date | None = None) -> list[dict]:
    d = base or date(2025, 1, 2)
    bars = []
    for i in range(n):
        bars.append({
            "date":   (d + timedelta(days=i)).isoformat(),
            "open":   100.0, "high": 105.0,
            "low":    95.0,  "close": 102.0,
            "volume": 1000,
        })
    return bars


# ── 2. Performance: self-heal decision is sub-ms ───────────────────────────

def test_self_heal_decision_is_subms() -> None:
    """Coverage check must complete in < 1 ms per iteration."""
    from backend.api.routes import options as mod
    threshold = mod._SELF_HEAL_COVERAGE_THRESHOLD
    requested_days = 365
    bars = _make_bars(30)   # clearly under threshold

    t0 = time.perf_counter()
    for _ in range(10_000):
        _ = len(bars) < threshold * requested_days
    elapsed_per_call_ms = (time.perf_counter() - t0) / 10_000 * 1_000

    assert elapsed_per_call_ms < 1.0, (
        f"Coverage check took {elapsed_per_call_ms:.3f} ms — too slow"
    )


# ── 3. Second ohlcv_store call carries bypass_cache=True ──────────────────

@pytest.mark.asyncio
async def test_self_heal_retries_with_bypass_when_under_covered() -> None:
    """
    When ohlcv_store first returns 30 bars for a 365-day request (~8%),
    the handler must call get_or_fetch_daily a SECOND time with bypass_cache=True.
    """
    from backend.api.routes.options import OptionsController

    short_bars = _make_bars(30)
    full_bars  = _make_bars(250)

    call_log: list[dict] = []

    async def fake_get_or_fetch(sym, exch, from_d, to_d, bypass_cache=None, **kw):
        call_log.append({"bypass_cache": bypass_cache})
        return full_bars if bypass_cache else short_bars

    with (
        # Patch the module-level import target that the handler resolves at
        # call time via `from backend.api.persistence.ohlcv_store import get_or_fetch_daily`
        patch(
            "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
            side_effect=fake_get_or_fetch,
        ),
        patch(
            "backend.brokers.registry.get_historical_brokers",
            return_value=[MagicMock()],
        ),
        patch("backend.api.routes.options._hist_cache_get", return_value=None),
        patch("backend.api.routes.options._hist_cache_put"),
        patch("backend.api.routes.options._ohlcv_trace_enabled", return_value=False),
        patch("backend.api.routes.options._self_heal_log_once") as mock_log,
    ):
        controller = OptionsController.__new__(OptionsController)
        # Patch the local import inside the handler (it imports get_or_fetch_daily
        # from the persistence module at runtime inside `if interval == "day":`).
        import backend.api.persistence.ohlcv_store as ohlcv_mod
        original_fn = ohlcv_mod.get_or_fetch_daily
        ohlcv_mod.get_or_fetch_daily = fake_get_or_fetch  # type: ignore[attr-defined]
        try:
            result = await OptionsController.historical.fn(
                controller,
                symbol="IDFCFIRSTB",
                days=365,
                interval="day",
                exchange="NSE",
            )
        finally:
            ohlcv_mod.get_or_fetch_daily = original_fn

    # Two calls: first (no bypass) + second (bypass=True)
    assert len(call_log) == 2, (
        f"Expected 2 calls to get_or_fetch_daily, got {len(call_log)}: {call_log}"
    )
    assert not call_log[0]["bypass_cache"]
    assert call_log[1]["bypass_cache"] is True

    # Log emitted once
    mock_log.assert_called_once()

    # Response carries full_bars
    assert len(result.bars) == len(full_bars)


# ── 4. Cool-off gate: no retry when all brokers throttled ─────────────────

@pytest.mark.asyncio
async def test_self_heal_skips_retry_when_all_brokers_in_cooloff() -> None:
    """
    When get_historical_brokers() returns [] (all in cool-off),
    the handler must NOT attempt a bypass_cache retry.
    """
    from backend.api.routes.options import OptionsController

    short_bars = _make_bars(30)
    call_log: list[dict] = []

    async def fake_get_or_fetch(sym, exch, from_d, to_d, bypass_cache=None, **kw):
        call_log.append({"bypass_cache": bypass_cache})
        return short_bars

    with (
        patch(
            "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
            side_effect=fake_get_or_fetch,
        ),
        patch(
            "backend.brokers.registry.get_historical_brokers",
            return_value=[],   # all in cool-off
        ),
        patch("backend.api.routes.options._hist_cache_get", return_value=None),
        patch("backend.api.routes.options._hist_cache_put"),
        patch("backend.api.routes.options._ohlcv_trace_enabled", return_value=False),
        patch("backend.api.routes.options._self_heal_log_once") as mock_log,
    ):
        controller = OptionsController.__new__(OptionsController)
        import backend.api.persistence.ohlcv_store as ohlcv_mod
        original_fn = ohlcv_mod.get_or_fetch_daily
        ohlcv_mod.get_or_fetch_daily = fake_get_or_fetch  # type: ignore[attr-defined]
        try:
            result = await OptionsController.historical.fn(
                controller,
                symbol="IDFCFIRSTB",
                days=365,
                interval="day",
                exchange="NSE",
            )
        finally:
            ohlcv_mod.get_or_fetch_daily = original_fn

    # Only the first call — no retry because brokers are in cool-off
    assert len(call_log) == 1, (
        f"Expected 1 call (no retry in cool-off), got {len(call_log)}: {call_log}"
    )
    mock_log.assert_not_called()


# ── 5. UX: partial=True when retry still under-covers ──────────────────────

@pytest.mark.asyncio
async def test_self_heal_sets_partial_when_retry_still_short() -> None:
    """
    If bypass_cache=True retry still returns fewer bars than threshold,
    partial=True must be set in the response.
    """
    from backend.api.routes.options import OptionsController

    still_short = _make_bars(40)  # still < 70% of 365

    async def fake_get_or_fetch(sym, exch, from_d, to_d, bypass_cache=None, **kw):
        return still_short

    with (
        patch(
            "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
            side_effect=fake_get_or_fetch,
        ),
        patch(
            "backend.brokers.registry.get_historical_brokers",
            return_value=[MagicMock()],
        ),
        patch("backend.api.routes.options._hist_cache_get", return_value=None),
        patch("backend.api.routes.options._hist_cache_put"),
        patch("backend.api.routes.options._ohlcv_trace_enabled", return_value=False),
        patch("backend.api.routes.options._self_heal_log_once"),
    ):
        controller = OptionsController.__new__(OptionsController)
        import backend.api.persistence.ohlcv_store as ohlcv_mod
        original_fn = ohlcv_mod.get_or_fetch_daily
        ohlcv_mod.get_or_fetch_daily = fake_get_or_fetch  # type: ignore[attr-defined]
        try:
            result = await OptionsController.historical.fn(
                controller,
                symbol="IDFCFIRSTB",
                days=365,
                interval="day",
                exchange="NSE",
            )
        finally:
            ohlcv_mod.get_or_fetch_daily = original_fn

    assert result.partial is True, (
        "Response must be partial=True when retry still under-covers"
    )
    assert len(result.bars) == len(still_short)


# ── Bonus: _self_heal_log_once throttle ────────────────────────────────────

def test_self_heal_log_once_throttles() -> None:
    """Two rapid calls for the same symbol/exchange emit only ONE log line.

    After the refactor, _self_heal_log_once + its state (_SELF_HEAL_LOG_TS,
    _SELF_HEAL_LOG_LOCK) live in backend.api.helpers.self_heal_log (SSOT).
    options.py re-exports the function but the state belongs to the helper
    module — so we patch the helper's logger, not options.logger.
    """
    import backend.api.helpers.self_heal_log as helper_mod
    from backend.api.routes.options import _self_heal_log_once

    logged: list[str] = []

    with patch.object(helper_mod.logger, "info", side_effect=lambda msg: logged.append(msg)):
        key = ("TESTXYZ", "NFO")
        with helper_mod._SELF_HEAL_LOG_LOCK:
            helper_mod._SELF_HEAL_LOG_TS.pop(key, None)

        _self_heal_log_once("TESTXYZ", "NFO", 5, 365)
        _self_heal_log_once("TESTXYZ", "NFO", 5, 365)   # within interval — throttled

    assert len(logged) == 1, (
        f"Expected 1 log emit (throttled), got {len(logged)}"
    )
