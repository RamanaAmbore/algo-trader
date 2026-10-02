"""
Cadence-unification test for the open-order watchdog (backend/api/background.py).

`_task_open_order_watchdog` is a safety-net reconcile sweep, not the primary
fill-detection path (that's event-driven via postback + `_subscribe_filled_pairs`),
so its default poll interval dropped from 300s (5 min) to 60s —
`orders.open_order_watchdog_seconds`'s own seeded default moved with it
(see backend/shared/helpers/settings.py).

Uses a mocked `asyncio.sleep` — no real timing waits.
"""
import asyncio
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_watchdog_default_interval_is_60s():
    """
    With no explicit `orders.open_order_watchdog_seconds` override, the
    watchdog's sleep interval resolves to the new 60s default (was 300s).
    """
    import backend.api.background as bg

    captured_intervals: list = []

    async def _fake_sleep(secs):
        captured_intervals.append(secs)
        raise asyncio.CancelledError()  # stop the loop after first sleep

    def _fake_get_int(key: str, default: int = 0) -> int:
        # Simulate the DB/YAML read chain falling through to the call's
        # own in-code default — i.e. nothing overrides this key anywhere.
        return default

    with (
        patch.object(bg, "get_int", side_effect=_fake_get_int),
        patch.object(bg.asyncio, "sleep", new=_fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await bg._task_open_order_watchdog()

    assert captured_intervals == [60], (
        f"expected watchdog default interval to be 60s (was 300s), "
        f"got {captured_intervals!r}"
    )


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
