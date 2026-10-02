"""
Cadence-unification tests for the open-order watchdog (backend/api/background.py).

`_task_open_order_watchdog` is a safety-net reconcile sweep, not the primary
fill-detection path (that's event-driven via postback + `_subscribe_filled_pairs`),
so its default poll interval now derives from the canonical SLOW cadence
setting (`polling.slow_ms`, settings.py — 60000 ms by default, was a
hardcoded 300s literal). `orders.open_order_watchdog_seconds` still
overrides per-deploy when explicitly set.

Uses a mocked `asyncio.sleep` and a mocked `get_int` — no real timing waits,
no DB/YAML read chain involved.
"""
import asyncio
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_watchdog_default_interval_derives_from_polling_slow_ms():
    """
    With no explicit `orders.open_order_watchdog_seconds` override, the
    watchdog's sleep interval resolves from `polling.slow_ms` (60000 ms ->
    60s), not a hardcoded 300s literal.
    """
    import backend.api.background as bg

    captured_intervals: list = []

    async def _fake_sleep(secs):
        captured_intervals.append(secs)
        raise asyncio.CancelledError()  # stop the loop after first sleep

    def _fake_get_int(key: str, default: int = 0) -> int:
        if key == "polling.slow_ms":
            return 60000
        if key == "orders.open_order_watchdog_seconds":
            # Simulate the DB/YAML read chain falling through to this
            # call's own in-code default (the polling.slow_ms-derived value).
            return default
        return default

    with (
        patch.object(bg, "get_int", side_effect=_fake_get_int),
        patch.object(bg.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await bg._task_open_order_watchdog()

    assert captured_intervals == [60], (
        f"expected watchdog default interval to resolve to 60s via "
        f"polling.slow_ms, got {captured_intervals!r}"
    )


@pytest.mark.asyncio
async def test_watchdog_tracks_a_different_polling_slow_ms_value():
    """
    If an operator tunes polling.slow_ms to a different value (e.g. 90s),
    the watchdog's default follows it — confirming the dependency is a
    real read, not a second hardcoded literal.
    """
    import backend.api.background as bg

    captured_intervals: list = []

    async def _fake_sleep(secs):
        captured_intervals.append(secs)
        raise asyncio.CancelledError()

    def _fake_get_int(key: str, default: int = 0) -> int:
        if key == "polling.slow_ms":
            return 90000
        if key == "orders.open_order_watchdog_seconds":
            return default
        return default

    with (
        patch.object(bg, "get_int", side_effect=_fake_get_int),
        patch.object(bg.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await bg._task_open_order_watchdog()

    assert captured_intervals == [90]


@pytest.mark.asyncio
async def test_watchdog_explicit_setting_still_overrides():
    """
    An operator-configured `orders.open_order_watchdog_seconds` value still
    wins over the new 60s default — cadence unification only changes the
    DEFAULT, not the override mechanism.
    """
    import backend.api.background as bg

    captured_intervals: list = []

    async def _fake_sleep(secs):
        captured_intervals.append(secs)
        raise asyncio.CancelledError()

    def _fake_get_int(key: str, default: int = 0) -> int:
        if key == "orders.open_order_watchdog_seconds":
            return 120  # explicit operator override
        return default

    with (
        patch.object(bg, "get_int", side_effect=_fake_get_int),
        patch.object(bg.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await bg._task_open_order_watchdog()

    assert captured_intervals == [120]


@pytest.mark.asyncio
async def test_watchdog_floor_still_enforced():
    """
    The 60s floor (`max(60, ...)`) is preserved even if a misconfigured
    setting tries to go lower.
    """
    import backend.api.background as bg

    captured_intervals: list = []

    async def _fake_sleep(secs):
        captured_intervals.append(secs)
        raise asyncio.CancelledError()

    def _fake_get_int(key: str, default: int = 0) -> int:
        if key == "orders.open_order_watchdog_seconds":
            return 5  # below floor
        return default

    with (
        patch.object(bg, "get_int", side_effect=_fake_get_int),
        patch.object(bg.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await bg._task_open_order_watchdog()

    assert captured_intervals == [60]
