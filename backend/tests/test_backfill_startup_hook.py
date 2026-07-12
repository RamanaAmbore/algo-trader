"""
test_backfill_startup_hook.py

Covers _task_warm_backfill in backend/api/background.py.

Three dimensions (per the spec):

  1. Fires once per process start with a 60 s delay:
     - The 60 s asyncio.sleep is present in the source.
     - The singleton guard (_fired flag) prevents double-execution.

  2. Calls backfill_ohlcv_daily with the collected universe:
     - Mock universe builder to return 5 symbols.
     - Assert all 5 hit backfill_ohlcv_daily.

  3. Does NOT run twice across module reloads (singleton guard):
     - Patch _fired = True before running.
     - Assert backfill functions are never called.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Dimension 1: Source-level guard — 60 s settle sleep present ───────────────

def test_startup_hook_has_60s_settle_sleep():
    """_task_warm_backfill must sleep for 60 s before doing any work.
    This gives the conn_service time to mint broker tokens after process start.
    """
    from backend.api.background import _task_warm_backfill

    src = inspect.getsource(_task_warm_backfill)

    assert "asyncio.sleep(60)" in src, (
        "_task_warm_backfill must contain `await asyncio.sleep(60)` to let "
        "the conn_service mint fresh broker tokens before the first backfill "
        "fetch. Without this, the first historical_data call fails with "
        "InvalidToken before the token lifecycle has completed."
    )


def test_startup_hook_has_singleton_guard():
    """_task_warm_backfill must have a singleton guard (_fired flag) so it
    fires at most once per process lifetime regardless of module reloads.
    """
    from backend.api.background import _task_warm_backfill

    src = inspect.getsource(_task_warm_backfill)

    assert "_fired" in src, (
        "_task_warm_backfill must check and set a `_fired` attribute on itself "
        "to prevent running more than once per process. Without this guard, "
        "a module reload (e.g. during testing or hot-reload) would trigger a "
        "duplicate backfill cycle."
    )


def test_startup_hook_registered_in_on_startup():
    """_task_warm_backfill must be registered as an asyncio.Task in on_startup.
    Source-level guard: if on_startup doesn't include it, the hook is dead code.
    """
    from backend.api import background as _bg
    src = inspect.getsource(_bg.on_startup)

    assert "_task_warm_backfill" in src, (
        "on_startup must create an asyncio.Task for _task_warm_backfill. "
        "Without this the backfill never fires on process start and the "
        "IDFCFIRSTB / CRUDEOIL coverage problem recurs on every deploy."
    )


# ── Dimension 3: Singleton guard prevents double-run ──────────────────────────

@pytest.mark.asyncio
async def test_singleton_guard_prevents_double_run():
    """When _fired is already True, _task_warm_backfill must return immediately
    without calling backfill_ohlcv_daily or backfill_intraday_today.
    """
    from backend.api.background import _task_warm_backfill

    ohlcv_calls: list = []
    intraday_calls: list = []

    async def _mock_ohlcv(symbols, target_days=365, max_concurrent=3):
        ohlcv_calls.append(symbols)
        return {"requested": 0, "filled": 0, "skipped_cooloff": 0, "errors": []}

    async def _mock_intraday(symbols, interval="30minute", max_concurrent=3):
        intraday_calls.append(symbols)
        return {"requested": 0, "filled": 0, "skipped_cooloff": 0, "errors": []}

    # Set _fired = True BEFORE calling the hook.
    _task_warm_backfill._fired = True  # type: ignore[attr-defined]

    with patch(
        "backend.api.persistence.backfill.backfill_ohlcv_daily",
        new=_mock_ohlcv,
    ), patch(
        "backend.api.persistence.backfill.backfill_intraday_today",
        new=_mock_intraday,
    ):
        await _task_warm_backfill()

    assert ohlcv_calls == [], (
        "backfill_ohlcv_daily must NOT be called when _fired=True. "
        "The singleton guard must return immediately."
    )
    assert intraday_calls == [], (
        "backfill_intraday_today must NOT be called when _fired=True."
    )

    # Cleanup so other tests can run the hook fresh.
    _task_warm_backfill._fired = False  # type: ignore[attr-defined]


# ── Dimension 2: Calls backfill_ohlcv_daily with collected universe ────────────

@pytest.mark.asyncio
async def test_startup_hook_calls_backfill_with_universe():
    """_task_warm_backfill must call backfill_ohlcv_daily with whatever
    symbols it collects.  We patch mover_warm_pairs (the one universe
    source that always fires without a live DB or broker) to return 5
    known symbols, and patch the backfill functions to capture calls.
    The 60 s sleep is patched to a no-op.
    """
    from backend.api.background import _task_warm_backfill

    # Reset guard for this test.
    _task_warm_backfill._fired = False  # type: ignore[attr-defined]

    test_symbols = [
        ("NIFTY50",    "NSE"),
        ("RELIANCE",   "NSE"),
        ("IDFCFIRSTB", "NSE"),
        ("CRUDEOIL26JUL6500CE", "MCX"),
        ("GOLD26JUNFUT", "MCX"),
    ]

    received_ohlcv_calls: list[list] = []
    received_intraday_calls: list[list] = []

    async def _mock_ohlcv(symbols, target_days=365, max_concurrent=3):
        received_ohlcv_calls.append(list(symbols))
        return {"requested": len(symbols), "filled": len(symbols), "skipped_cooloff": 0, "errors": []}

    async def _mock_intraday(symbols, interval="30minute", max_concurrent=3):
        received_intraday_calls.append(list(symbols))
        return {"requested": len(symbols), "filled": len(symbols), "skipped_cooloff": 0, "errors": []}

    import asyncio as _asyncio
    import backend.api.persistence.backfill as _backfill_mod
    from backend.shared.helpers import mover_universe as _mu

    orig_ohlcv    = _backfill_mod.backfill_ohlcv_daily
    orig_intraday = _backfill_mod.backfill_intraday_today
    orig_sleep    = _asyncio.sleep
    orig_mwp      = _mu.mover_warm_pairs

    # Patch mover_warm_pairs to return test symbols, patch backfill functions,
    # patch asyncio.sleep to skip the 60 s settle delay.  DB + broker calls
    # inside the task will raise (no live DB in tests) but are caught by the
    # task's per-section try/except — only the mover path contributes here.
    def _fake_mwp():
        return iter(test_symbols)

    async def _fast_sleep(_n):
        pass

    _mu.mover_warm_pairs = _fake_mwp  # type: ignore[attr-defined]
    _asyncio.sleep = _fast_sleep  # type: ignore[attr-defined]
    _backfill_mod.backfill_ohlcv_daily    = _mock_ohlcv     # type: ignore[attr-defined]
    _backfill_mod.backfill_intraday_today = _mock_intraday  # type: ignore[attr-defined]

    try:
        # Patch is_any_segment_open at its definition site so the intraday
        # branch also fires (otherwise the task skips intraday when closed).
        with patch(
            "backend.shared.helpers.date_time_utils.is_any_segment_open",
            return_value=True,
        ):
            await _task_warm_backfill()
    finally:
        _mu.mover_warm_pairs = orig_mwp  # type: ignore[attr-defined]
        _asyncio.sleep = orig_sleep  # type: ignore[attr-defined]
        _backfill_mod.backfill_ohlcv_daily    = orig_ohlcv    # type: ignore[attr-defined]
        _backfill_mod.backfill_intraday_today = orig_intraday  # type: ignore[attr-defined]
        # Reset guard so other tests start clean.
        _task_warm_backfill._fired = False  # type: ignore[attr-defined]

    # Key assertion: backfill_ohlcv_daily was called and received at least the
    # mover-universe symbols (mover_warm_pairs was patched to our test list).
    assert received_ohlcv_calls, (
        "_task_warm_backfill must call backfill_ohlcv_daily at least once. "
        "Received zero calls — the task may have returned early or the "
        "mover_warm_pairs patch was not picked up."
    )
    all_syms = {s for batch in received_ohlcv_calls for s in batch}
    for sym in test_symbols:
        assert sym in all_syms, (
            f"Symbol {sym} from mover_warm_pairs must appear in the symbols "
            f"passed to backfill_ohlcv_daily. Got: {sorted(all_syms)}"
        )


# ── Stale code: backfill imports in background.py are at call time (deferred) ──

def test_stale_backfill_imports_are_deferred():
    """Backfill helpers must be imported at call time (deferred), not at module top-level.
    After the _task_warm_backfill decomposition, the imports live in the sub-functions
    _backfill_run_ohlcv and _backfill_run_intraday — check both.
    """
    from backend.api.background import _backfill_run_ohlcv, _backfill_run_intraday

    src_ohlcv = inspect.getsource(_backfill_run_ohlcv)
    src_intra = inspect.getsource(_backfill_run_intraday)

    assert "from backend.api.persistence.backfill import" in src_ohlcv, (
        "_backfill_run_ohlcv must import backfill_ohlcv_daily inside the function body."
    )
    assert "from backend.api.persistence.backfill import" in src_intra, (
        "_backfill_run_intraday must import backfill_intraday_today inside the function body."
    )
