"""
Test suite for daily_snapshot SSOT refactor — _is_exchange_open_at removal.

Verifies that _is_exchange_open_at wrapper has been removed and _exchange_clock
is accessible at module level.
"""

import pytest


def test_is_exchange_open_at_removed():
    """Verify that _is_exchange_open_at function no longer exists
    in backend.api.algo.daily_snapshot module."""
    from backend.api.algo import daily_snapshot

    # The wrapper should be removed as part of the refactor
    assert not hasattr(daily_snapshot, '_is_exchange_open_at'), (
        "_is_exchange_open_at wrapper should be removed from daily_snapshot. "
        "Use _exchange_clock.is_exchange_open() directly instead."
    )


def test_exchange_clock_accessible():
    """Verify that _exchange_clock is imported and accessible at module level."""
    from backend.api.algo import daily_snapshot

    # _exchange_clock should be available for direct use
    assert hasattr(daily_snapshot, '_exchange_clock'), (
        "_exchange_clock should be imported at module level in daily_snapshot"
    )


def test_exchange_clock_has_is_exchange_open_method():
    """Verify that _exchange_clock has the is_exchange_open method."""
    from backend.api.algo import daily_snapshot

    exchange_clock_obj = daily_snapshot._exchange_clock

    # Should have the is_exchange_open method
    assert hasattr(exchange_clock_obj, 'is_exchange_open'), (
        "_exchange_clock should have is_exchange_open method"
    )
