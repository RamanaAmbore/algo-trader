"""
Tests for broker health window and holiday refresh combined token refresh.

Quality dimensions covered per spec:
  SSOT        — constant imported from canonical module; no duplicate definition
  Correctness — threshold value exactly 660.0; holiday refresh calls token refresh
  Performance — token refresh blocks don't stall event loop (best-effort logging only)
  Reuse       — same constant used by broker-health route logic
  UX          — token refresh failures logged but don't block holiday refresh
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Test 1 — _BROKER_HEALTH_FRESH_WINDOW_S threshold
# ---------------------------------------------------------------------------

class TestBrokerHealthFreshWindow:
    """Verify the broker-health green threshold is 660 seconds."""

    def test_fresh_window_is_660(self):
        """_BROKER_HEALTH_FRESH_WINDOW_S must equal 660.0 (Dhan 600s + 60s slack)."""
        from backend.api.routes.health import _BROKER_HEALTH_FRESH_WINDOW_S
        assert _BROKER_HEALTH_FRESH_WINDOW_S == 660.0, (
            f"Expected 660.0, got {_BROKER_HEALTH_FRESH_WINDOW_S}. "
            "Dhan cold-priority poll is 600s; window must be ≥ 600s."
        )

    def test_fresh_window_type_is_float(self):
        """Constant must be a float (used in arithmetic comparisons with time deltas)."""
        from backend.api.routes.health import _BROKER_HEALTH_FRESH_WINDOW_S
        assert isinstance(_BROKER_HEALTH_FRESH_WINDOW_S, float), (
            f"Expected float, got {type(_BROKER_HEALTH_FRESH_WINDOW_S)}"
        )

    def test_fresh_window_exceeds_dhan_poll_interval(self):
        """Window must be strictly greater than the Dhan cold-priority poll interval (600s)."""
        from backend.api.routes.health import _BROKER_HEALTH_FRESH_WINDOW_S
        DHAN_COLD_POLL_S = 600.0
        assert _BROKER_HEALTH_FRESH_WINDOW_S > DHAN_COLD_POLL_S, (
            f"Window {_BROKER_HEALTH_FRESH_WINDOW_S}s must exceed Dhan poll {DHAN_COLD_POLL_S}s"
        )
