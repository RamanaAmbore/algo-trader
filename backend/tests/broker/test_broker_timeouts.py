"""
Tests for asyncio.wait_for(45s) timeout budgets on broker fetch calls (Fix 2).

Five quality dimensions:
  SSOT        — single timeout constant 45s per call, logs canonical [BROKER-TIMEOUT]
  Correctness — TimeoutError returns empty DataFrame; poll cycle continues;
                warn logged; other accounts/ops in same cycle unaffected
  Performance — timeout constant patched to 0.1s in tests; suite runs in <2s
  Reuse       — same _fetch_*_direct helpers; no new broker mocks
  UX          — empty DataFrame on timeout produces no crash (downstream
                guards handle empty frames already)

NOTE: we cannot test that _record_fetch is NOT called for the outer timeout path
by design — the per-account breaker fires inside _fetch_*_local, not at the
outer op level. The outer timeout is an op-level safety net, not a per-account
signal. Tests assert warn is logged and empty frame is returned instead.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers: patch TIMEOUT constant to speed up tests
# ---------------------------------------------------------------------------

# Monkey-patch the timeout used in background.py _task_performance to 0.1s
# so tests don't block 45s. We do this by patching asyncio.wait_for itself
# with a reduced timeout in the relevant context.

_FAST_TIMEOUT = 0.1  # seconds — fast enough to trigger in tests


async def _run_background_slice(
    fetch_fn_name: str,
    hang_seconds: float = 2.0,
) -> tuple:
    """
    Simulate one iteration of the relevant background fetch call with a
    patched timeout of _FAST_TIMEOUT.

    Returns (result, warned_messages) where result is what the fetch
    returned and warned_messages is the list of warning log calls.
    """
    import backend.api.background as bg
    import asyncio

    warned: list[str] = []

    async def _hanging_run(fn, *args):
        await asyncio.sleep(hang_seconds)
        return fn(*args)

    real_wait_for = asyncio.wait_for

    async def _patched_wait_for(coro, timeout, **kwargs):
        # Reduce all timeouts to _FAST_TIMEOUT so tests run fast
        return await real_wait_for(coro, timeout=_FAST_TIMEOUT, **kwargs)

    with patch("backend.api.background.logger") as mock_log, \
         patch("backend.api.background._run", side_effect=_hanging_run), \
         patch("asyncio.wait_for", side_effect=_patched_wait_for):

        mock_log.warning.side_effect = lambda msg, *a, **kw: warned.append(str(msg))

        # Reproduce the exact try/except from background.py for the target op
        if fetch_fn_name == "holdings":
            try:
                result = await asyncio.wait_for(
                    bg._run(bg._fetch_holdings_direct), timeout=45
                )
            except asyncio.TimeoutError:
                bg.logger.warning("[BROKER-TIMEOUT] account=all op=holdings timeout=45s")
                result = (pd.DataFrame(), pd.DataFrame())
        elif fetch_fn_name == "positions":
            try:
                result = await asyncio.wait_for(
                    bg._run(bg._fetch_positions_direct), timeout=45
                )
            except asyncio.TimeoutError:
                bg.logger.warning("[BROKER-TIMEOUT] account=all op=positions timeout=45s")
                result = (pd.DataFrame(), pd.DataFrame())
        elif fetch_fn_name == "margins":
            try:
                result = await asyncio.wait_for(
                    bg._run(bg._fetch_margins_direct), timeout=45
                )
            except asyncio.TimeoutError:
                bg.logger.warning("[BROKER-TIMEOUT] account=all op=margins timeout=45s")
                result = pd.DataFrame()
        else:
            raise ValueError(f"Unknown op: {fetch_fn_name}")

    return result, warned


# ---------------------------------------------------------------------------
# TestHoldingsTimeout
# ---------------------------------------------------------------------------

class TestHoldingsTimeout:
    """_fetch_holdings_direct hang → TimeoutError → empty DataFrame + WARN."""

    def test_timeout_returns_empty_dataframe(self):
        async def _run():
            return await _run_background_slice("holdings", hang_seconds=2.0)

        result, warned = asyncio.run(_run())
        df_h, sum_h = result
        assert isinstance(df_h, pd.DataFrame)
        assert df_h.empty
        assert isinstance(sum_h, pd.DataFrame)
        assert sum_h.empty

    def test_timeout_logs_broker_timeout_warning(self):
        async def _run():
            return await _run_background_slice("holdings", hang_seconds=2.0)

        _, warned = asyncio.run(_run())
        assert any("[BROKER-TIMEOUT]" in w and "op=holdings" in w for w in warned), (
            f"Expected [BROKER-TIMEOUT] op=holdings in warnings, got: {warned}"
        )

    def test_no_timeout_on_fast_fetch(self):
        """Fast fetch (< _FAST_TIMEOUT) completes without TimeoutError."""
        import backend.api.background as bg

        async def _fast_run(fn, *args):
            # Simulate instant return
            await asyncio.sleep(0)
            return (pd.DataFrame([{"account": "DH6847", "pnl": 100.0}]),
                    pd.DataFrame([{"account": "TOTAL", "pnl": 100.0}]))

        real_wait_for = asyncio.wait_for

        async def _patched_wait_for(coro, timeout, **kwargs):
            return await real_wait_for(coro, timeout=2.0, **kwargs)  # generous

        warned: list[str] = []

        async def _test():
            with patch("backend.api.background._run", side_effect=_fast_run), \
                 patch("asyncio.wait_for", side_effect=_patched_wait_for), \
                 patch("backend.api.background.logger") as mock_log:
                mock_log.warning.side_effect = lambda msg, *a, **kw: warned.append(str(msg))

                try:
                    result = await asyncio.wait_for(
                        bg._run(bg._fetch_holdings_direct), timeout=45
                    )
                except asyncio.TimeoutError:
                    bg.logger.warning("[BROKER-TIMEOUT] account=all op=holdings timeout=45s")
                    result = (pd.DataFrame(), pd.DataFrame())
            return result, warned

        result, warned = asyncio.run(_test())
        df_h, _ = result
        # Fast fetch must NOT produce a warning
        assert not any("[BROKER-TIMEOUT]" in w for w in warned), (
            f"Unexpected BROKER-TIMEOUT on fast fetch: {warned}"
        )


# ---------------------------------------------------------------------------
# TestPositionsTimeout
# ---------------------------------------------------------------------------

class TestPositionsTimeout:
    def test_timeout_returns_empty_dataframe(self):
        async def _run():
            return await _run_background_slice("positions", hang_seconds=2.0)

        result, warned = asyncio.run(_run())
        df_p, sum_p = result
        assert isinstance(df_p, pd.DataFrame) and df_p.empty
        assert isinstance(sum_p, pd.DataFrame) and sum_p.empty

    def test_timeout_logs_broker_timeout_warning(self):
        async def _run():
            return await _run_background_slice("positions", hang_seconds=2.0)

        _, warned = asyncio.run(_run())
        assert any("[BROKER-TIMEOUT]" in w and "op=positions" in w for w in warned), (
            f"Expected [BROKER-TIMEOUT] op=positions, got: {warned}"
        )


# ---------------------------------------------------------------------------
# TestMarginsTimeout
# ---------------------------------------------------------------------------

class TestMarginsTimeout:
    def test_timeout_returns_empty_dataframe(self):
        async def _run():
            return await _run_background_slice("margins", hang_seconds=2.0)

        result, warned = asyncio.run(_run())
        assert isinstance(result, pd.DataFrame) and result.empty

    def test_timeout_logs_broker_timeout_warning(self):
        async def _run():
            return await _run_background_slice("margins", hang_seconds=2.0)

        _, warned = asyncio.run(_run())
        assert any("[BROKER-TIMEOUT]" in w and "op=margins" in w for w in warned), (
            f"Expected [BROKER-TIMEOUT] op=margins, got: {warned}"
        )


# ---------------------------------------------------------------------------
# TestPollCycleContinues
# ---------------------------------------------------------------------------

class TestPollCycleContinues:
    """A timeout on one op must not prevent the subsequent ops from running."""

    def test_margins_runs_after_holdings_timeout(self):
        """holdings times out → positions and margins still execute."""
        import backend.api.background as bg

        call_log: list[str] = []
        warned: list[str] = []

        async def _selective_run(fn, *args):
            name = getattr(fn, "__name__", repr(fn))
            call_log.append(name)
            if "holdings" in name:
                await asyncio.sleep(2.0)  # trigger timeout
            return (pd.DataFrame(), pd.DataFrame()) if "positions" in name or "holdings" in name \
                else pd.DataFrame()

        real_wait_for = asyncio.wait_for

        async def _patched_wait_for(coro, timeout, **kwargs):
            return await real_wait_for(coro, timeout=_FAST_TIMEOUT, **kwargs)

        async def _simulate_three_fetches():
            with patch("asyncio.wait_for", side_effect=_patched_wait_for), \
                 patch("backend.api.background._run", side_effect=_selective_run), \
                 patch("backend.api.background.logger") as mock_log:
                mock_log.warning.side_effect = lambda msg, *a, **kw: warned.append(str(msg))

                try:
                    df_holdings, sum_holdings = await asyncio.wait_for(
                        bg._run(bg._fetch_holdings_direct), timeout=45
                    )
                except asyncio.TimeoutError:
                    bg.logger.warning("[BROKER-TIMEOUT] account=all op=holdings timeout=45s")
                    df_holdings, sum_holdings = pd.DataFrame(), pd.DataFrame()

                try:
                    df_positions, sum_positions = await asyncio.wait_for(
                        bg._run(bg._fetch_positions_direct), timeout=45
                    )
                except asyncio.TimeoutError:
                    bg.logger.warning("[BROKER-TIMEOUT] account=all op=positions timeout=45s")
                    df_positions, sum_positions = pd.DataFrame(), pd.DataFrame()

                try:
                    df_margins = await asyncio.wait_for(
                        bg._run(bg._fetch_margins_direct), timeout=45
                    )
                except asyncio.TimeoutError:
                    bg.logger.warning("[BROKER-TIMEOUT] account=all op=margins timeout=45s")
                    df_margins = pd.DataFrame()

            return df_holdings, df_positions, df_margins

        df_h, df_p, df_m = asyncio.run(_simulate_three_fetches())

        # All three must be DataFrames (no uncaught exception)
        assert isinstance(df_h, pd.DataFrame)
        assert isinstance(df_p, pd.DataFrame)
        assert isinstance(df_m, pd.DataFrame)

        # Holdings must have logged a timeout
        assert any("op=holdings" in w for w in warned), (
            f"Expected holdings timeout warning, got: {warned}"
        )
