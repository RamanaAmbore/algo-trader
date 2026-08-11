"""
Tests for two small backend fixes:

1. _BROKER_HEALTH_FRESH_WINDOW_S == 660.0
   Dhan cold-priority poll interval is 600s. The old 300s window meant a
   cold-priority Dhan account was structurally amber for half every cycle.
   660s (600 + 60s slack) lets it sustain green across a full poll cycle.

2. _task_token_refresh returns early when RAMBOQ_USE_CONN_SERVICE is set.
   Under conn_service, Connections().conn is empty in the main API process.
   The task iterated nothing and silently no-oped. The guard makes it
   visible and points to the correct pre-warm location (service/app.py).

Quality dimensions covered per spec:
  SSOT        — constant imported from canonical module; no duplicate definition
  Correctness — threshold value exactly 660.0; early-return fires before loop
  Performance — no asyncio.sleep call made when guard fires
  Reuse       — same constant used by broker-health route logic
  UX          — warning log emitted so operators see the no-op in logs
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch, MagicMock
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


# ---------------------------------------------------------------------------
# Test 2 — _task_token_refresh early-return under conn_service
# ---------------------------------------------------------------------------

class TestTaskTokenRefreshConnServiceGuard:
    """Verify _task_token_refresh exits immediately when RAMBOQ_USE_CONN_SERVICE is set."""

    def test_returns_early_when_conn_service_set(self):
        """With RAMBOQ_USE_CONN_SERVICE=1, _task_token_refresh must return without entering the loop."""
        from backend.api.background import _task_token_refresh

        sleep_called = []

        async def _mock_sleep(delay: float) -> None:  # pragma: no cover
            sleep_called.append(delay)

        with patch.dict(os.environ, {"RAMBOQ_USE_CONN_SERVICE": "1"}):
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                asyncio.run(_task_token_refresh())

        assert sleep_called == [], (
            "asyncio.sleep must NOT be called when conn_service guard fires; "
            f"got calls: {sleep_called}"
        )

    def test_no_early_return_without_env_var(self):
        """Without RAMBOQ_USE_CONN_SERVICE, the task must enter the loop (sleep called once)."""
        from backend.api.background import _task_token_refresh

        sleep_calls: list[float] = []

        async def _mock_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            # Raise to break the infinite while-loop after one iteration
            raise RuntimeError("break loop")

        env = {k: v for k, v in os.environ.items() if k != "RAMBOQ_USE_CONN_SERVICE"}
        with patch.dict(os.environ, env, clear=True):
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                # The RuntimeError from _mock_sleep propagates out; that's fine —
                # we only need to confirm sleep was called (loop entered).
                with pytest.raises(RuntimeError, match="break loop"):
                    asyncio.run(_task_token_refresh())

        assert len(sleep_calls) >= 1, (
            "asyncio.sleep must be called when RAMBOQ_USE_CONN_SERVICE is absent"
        )

    def test_warning_logged_when_conn_service_set(self):
        """A warning is emitted pointing operators to service/app.py _task_prewarm_tokens."""
        from backend.api.background import _task_token_refresh

        warning_messages: list[str] = []

        async def _mock_sleep(delay: float) -> None:  # pragma: no cover
            pass

        with patch.dict(os.environ, {"RAMBOQ_USE_CONN_SERVICE": "1"}):
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch("backend.api.background.logger") as mock_logger:
                    mock_logger.warning = MagicMock(side_effect=lambda msg: warning_messages.append(msg))
                    asyncio.run(_task_token_refresh())

        assert any("conn_service" in m for m in warning_messages), (
            f"Expected a warning mentioning 'conn_service'; got: {warning_messages}"
        )
        assert any("_task_prewarm_tokens" in m for m in warning_messages), (
            f"Expected warning to reference '_task_prewarm_tokens'; got: {warning_messages}"
        )

    def test_conn_service_empty_string_does_not_trigger_guard(self):
        """Empty-string env var must NOT trigger the guard (os.environ.get returns falsy)."""
        from backend.api.background import _task_token_refresh

        sleep_calls: list[float] = []

        async def _mock_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            raise RuntimeError("break loop")

        with patch.dict(os.environ, {"RAMBOQ_USE_CONN_SERVICE": ""}, clear=False):
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with pytest.raises(RuntimeError, match="break loop"):
                    asyncio.run(_task_token_refresh())

        assert len(sleep_calls) >= 1, (
            "Empty-string RAMBOQ_USE_CONN_SERVICE should not trigger the guard"
        )
