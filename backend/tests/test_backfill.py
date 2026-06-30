"""
test_backfill.py

Covers backend/api/persistence/backfill.py — the coverage backfill helpers.

Six quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — backfill_ohlcv_daily calls get_or_fetch_daily(bypass_cache=True),
                   not a parallel broker path.  backfill_intraday_today calls
                   get_or_fetch_intraday(bypass_cache=True).

  2. Performance — 50 symbols × 365 days each completes in < 10 s with a mocked
                   store (no real I/O).  Measured via time.perf_counter().

  3. Stale code  — assert no inline broker.historical_data call in backfill.py
                   (always goes via store).  Source-grep guard.

  4. Reuse       — backfill.py imports and uses _RATE_LIMIT_COOLOFF from
                   backend.brokers.registry.  Source-grep guard.

  5. UX          — N/A (backend module; no UI surface).

  6. Response    — time.perf_counter() budget on the helper's outer loop (< 10 s
                   for 50 symbols with mocked store, < 30 s for 300 symbols).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_bars(count: int) -> list[dict]:
    """Return `count` synthetic OHLCVBar-compatible dicts."""
    today = date.today()
    bars = []
    for i in range(count):
        d = today - timedelta(days=count - i)
        bars.append({
            "date":   d.isoformat(),
            "open":   100.0,
            "high":   105.0,
            "low":    98.0,
            "close":  102.0,
            "volume": 10_000,
        })
    return bars


def _make_intraday_bars(count: int) -> list[dict]:
    return [
        {"bar_ts": f"2026-06-30T{9 + i // 2:02d}:{30 * (i % 2):02d}:00+00:00",
         "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 500}
        for i in range(count)
    ]


# ── Dimension 3: Stale code — no inline broker.historical_data call ────────────

def test_stale_no_inline_broker_historical_data():
    """backfill.py must NOT contain a direct broker.historical_data() call
    in any of its function bodies.  All broker fetches must go through the
    store (get_or_fetch_daily / get_or_fetch_intraday).  A direct call would
    bypass the three-tier cache + write-back pipeline and diverge from the
    canonical path.

    We check only the public function bodies (not the module docstring which
    may legitimately mention 'historical_data' in a budget/rate-limit comment).
    """
    import inspect
    from backend.api.persistence import backfill as _bf

    for fn_name in ("backfill_ohlcv_daily", "backfill_intraday_today",
                    "_count_db_bars", "_price_broker_in_cooloff",
                    "_any_broker_in_cooloff"):
        fn = getattr(_bf, fn_name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        # Detect direct .historical_data( calls — not import or comment references.
        import re
        # Match "broker.historical_data(" or ".historical_data(" patterns.
        matches = re.findall(r'\.\s*historical_data\s*\(', src)
        assert not matches, (
            f"backfill.{fn_name} must not call broker.historical_data() directly. "
            "All fetches must go through get_or_fetch_daily / get_or_fetch_intraday "
            "so the three-tier cache + write-back pipeline is always used. "
            f"Found: {matches}"
        )


# ── Dimension 4: Reuse — _RATE_LIMIT_COOLOFF from registry ────────────────────

def test_reuse_rate_limit_cooloff_from_registry():
    """backfill.py must import _RATE_LIMIT_COOLOFF from backend.brokers.registry.
    This ensures the cooloff check uses the canonical registry dict and is not
    a parallel re-invention that could diverge from PriceBroker._try().
    """
    from pathlib import Path
    src = (
        Path(__file__).parent.parent / "api" / "persistence" / "backfill.py"
    ).read_text()

    assert "_RATE_LIMIT_COOLOFF" in src, (
        "backfill.py must reference _RATE_LIMIT_COOLOFF from backend.brokers.registry. "
        "This is the canonical rate-limit dict used by PriceBroker._try(); "
        "checking it here prevents a cascade when the broker is cooling off."
    )
    assert "backend.brokers.registry" in src, (
        "backfill.py must import from backend.brokers.registry to access "
        "_RATE_LIMIT_COOLOFF.  Any parallel re-invention would drift from "
        "the registry's _mark_rate_limited / _is_rate_limited lifecycle."
    )


# ── Dimension 1: SSOT — calls get_or_fetch_daily(bypass_cache=True) ────────────

@pytest.mark.asyncio
async def test_ssot_ohlcv_calls_get_or_fetch_daily_bypass():
    """backfill_ohlcv_daily must call get_or_fetch_daily with bypass_cache=True.
    Under-covered symbols must trigger exactly one such call per symbol.
    Well-covered symbols (>= 70% of target_days) must be skipped.

    We patch at the ohlcv_store module level because backfill imports the
    function via a deferred `from ... import` inside its function body.
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    calls: list[dict] = []

    async def _mock_get_or_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        calls.append({"sym": sym, "exch": exch, "bypass_cache": bypass_cache})
        return _make_bars(50)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        # Patch at the ohlcv_store module so the deferred import inside
        # backfill_ohlcv_daily picks it up.
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_get_or_fetch,
    ):
        result = await backfill_ohlcv_daily(
            [("NIFTY50", "NSE"), ("RELIANCE", "NSE")],
            target_days=100,
            max_concurrent=2,
        )

    assert result["requested"] == 2, f"Expected 2 requested; got {result}"
    assert result["filled"] == 2, (
        f"Both symbols have 0 bars (< 70% of 100) — both must be fetched. "
        f"Got filled={result['filled']}"
    )
    # Every call must have bypass_cache=True.
    bypass_values = [c["bypass_cache"] for c in calls]
    assert all(v is True for v in bypass_values), (
        f"All get_or_fetch_daily calls must have bypass_cache=True. "
        f"Got: {bypass_values}"
    )


@pytest.mark.asyncio
async def test_ssot_well_covered_symbol_skipped():
    """A symbol that already has >= 70% of target_days bars must be skipped
    (no get_or_fetch_daily call).
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    calls: list[str] = []

    async def _mock_get_or_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        calls.append(sym)
        return _make_bars(50)

    target_days = 100
    # 75 bars = 75% of 100 → above the 70% threshold → skip.
    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=75),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_get_or_fetch,
    ):
        result = await backfill_ohlcv_daily(
            [("BEL", "NSE")],
            target_days=target_days,
            max_concurrent=1,
        )

    assert result["filled"] == 0, (
        f"Symbol with 75% coverage should be SKIPPED (>= 70% threshold). "
        f"Got filled={result['filled']}"
    )
    assert calls == [], (
        f"get_or_fetch_daily must NOT be called for a well-covered symbol. "
        f"Got calls: {calls}"
    )


@pytest.mark.asyncio
async def test_ssot_intraday_calls_get_or_fetch_intraday_bypass():
    """backfill_intraday_today must call get_or_fetch_intraday with bypass_cache=True."""
    from backend.api.persistence.backfill import backfill_intraday_today

    calls: list[dict] = []

    async def _mock_get_or_fetch_intraday(sym, exch, on_date, interval="30minute", bypass_cache=None):
        calls.append({"sym": sym, "bypass_cache": bypass_cache, "interval": interval})
        return _make_intraday_bars(10)

    with patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        # Patch at the intraday_store module (deferred import target).
        "backend.api.persistence.intraday_store.get_or_fetch_intraday",
        new=_mock_get_or_fetch_intraday,
    ):
        result = await backfill_intraday_today(
            [("NIFTY50", "NSE"), ("GOLD", "MCX")],
            interval="30minute",
            max_concurrent=2,
        )

    assert result["requested"] == 2
    assert result["filled"] == 2
    assert all(c["bypass_cache"] is True for c in calls), (
        f"All calls must have bypass_cache=True; got: {[c['bypass_cache'] for c in calls]}"
    )
    assert all(c["interval"] == "30minute" for c in calls), (
        f"All calls must use interval='30minute'; got: {[c['interval'] for c in calls]}"
    )


# ── Cooloff skip tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ssot_cooloff_skips_not_retries():
    """When the broker is in rate-limit cool-off, symbols must be skipped
    (skipped_cooloff incremented) — not retried.  filled must be 0.
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    fetch_calls: list[str] = []

    async def _mock_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        fetch_calls.append(sym)
        return _make_bars(10)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=True,   # all brokers in cooloff
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_fetch,
    ):
        result = await backfill_ohlcv_daily(
            [("IDFCFIRSTB", "NSE"), ("CRUDEOIL26JUL6500CE", "MCX")],
            target_days=365,
            max_concurrent=2,
        )

    assert result["skipped_cooloff"] == 2, (
        f"Both symbols must be skipped when broker is in cooloff. "
        f"Got skipped_cooloff={result['skipped_cooloff']}"
    )
    assert result["filled"] == 0, (
        f"filled must be 0 when broker is in cooloff. Got filled={result['filled']}"
    )
    assert fetch_calls == [], (
        f"get_or_fetch_daily must NOT be called when broker is in cooloff. "
        f"Got calls: {fetch_calls}"
    )


@pytest.mark.asyncio
async def test_ssot_cooloff_intraday_skips_not_retries():
    """Same cooloff contract for backfill_intraday_today."""
    from backend.api.persistence.backfill import backfill_intraday_today

    fetch_calls: list[str] = []

    async def _mock_fetch(sym, exch, on_date, interval="30minute", bypass_cache=None):
        fetch_calls.append(sym)
        return _make_intraday_bars(5)

    with patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=True,
    ), patch(
        "backend.api.persistence.intraday_store.get_or_fetch_intraday",
        new=_mock_fetch,
    ):
        result = await backfill_intraday_today(
            [("NIFTY50", "NSE")],
            interval="30minute",
            max_concurrent=1,
        )

    assert result["skipped_cooloff"] == 1
    assert result["filled"] == 0
    assert fetch_calls == [], "Must not call the store when in cooloff"


# ── Dimension 2 + 6: Performance — 50 symbols in < 10 s with mocked store ──────

@pytest.mark.asyncio
async def test_perf_50_symbols_daily_under_10s():
    """50 symbols × 365 days each must complete in < 10 s with a mocked
    store (pure-async, no real I/O).  This verifies the concurrency gate
    (Semaphore) doesn't serialise all 50 symbols sequentially in a way
    that would time out in prod with a 3-req/s budget.
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    symbols = [(f"SYM{i:03d}", "NSE") for i in range(50)]

    async def _mock_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        await asyncio.sleep(0)   # yield control — simulate async I/O
        return _make_bars(50)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_fetch,
    ):
        t0 = time.perf_counter()
        result = await backfill_ohlcv_daily(symbols, target_days=365, max_concurrent=5)
        elapsed = time.perf_counter() - t0

    assert elapsed < 10.0, (
        f"50 symbols with mocked store must complete in < 10 s. "
        f"Got {elapsed:.2f} s — concurrency gate may be blocking."
    )
    assert result["filled"] == 50, (
        f"All 50 under-covered symbols must be filled. Got filled={result['filled']}"
    )


@pytest.mark.asyncio
async def test_perf_300_symbols_daily_under_30s():
    """300 symbols with mocked store must complete in < 30 s.
    This is the full-universe (300-cap) scenario.
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    symbols = [(f"SYM{i:03d}", "NSE") for i in range(300)]

    async def _mock_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        await asyncio.sleep(0)
        return _make_bars(20)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_fetch,
    ):
        t0 = time.perf_counter()
        result = await backfill_ohlcv_daily(symbols, target_days=365, max_concurrent=5)
        elapsed = time.perf_counter() - t0

    assert elapsed < 30.0, (
        f"300 symbols with mocked store must complete in < 30 s. "
        f"Got {elapsed:.2f} s."
    )
    assert result["filled"] == 300


# ── Summary dict shape ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_dict_shape():
    """backfill_ohlcv_daily and backfill_intraday_today must return a dict
    with exactly the keys: requested, filled, skipped_cooloff, errors.
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily, backfill_intraday_today

    async def _no_op_daily(sym, exch, from_d, to_d, bypass_cache=None):
        return _make_bars(10)

    async def _no_op_intraday(sym, exch, on_date, interval="30minute", bypass_cache=None):
        return _make_intraday_bars(5)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_no_op_daily,
    ), patch(
        "backend.api.persistence.intraday_store.get_or_fetch_intraday",
        new=_no_op_intraday,
    ):
        r_daily = await backfill_ohlcv_daily([("X", "NSE")], target_days=100)
        r_intra = await backfill_intraday_today([("X", "NSE")])

    expected_keys = {"requested", "filled", "skipped_cooloff", "errors"}
    assert set(r_daily.keys()) == expected_keys, (
        f"backfill_ohlcv_daily must return dict with keys {expected_keys}. "
        f"Got: {set(r_daily.keys())}"
    )
    assert set(r_intra.keys()) == expected_keys, (
        f"backfill_intraday_today must return dict with keys {expected_keys}. "
        f"Got: {set(r_intra.keys())}"
    )
    assert isinstance(r_daily["errors"], list)
    assert isinstance(r_intra["errors"], list)


# ── Error isolation — one bad symbol does not abort the rest ──────────────────

@pytest.mark.asyncio
async def test_error_isolation_one_bad_symbol():
    """When one symbol's fetch raises an exception, the rest of the symbols
    must still be processed and the error must be captured in result['errors'].
    """
    from backend.api.persistence.backfill import backfill_ohlcv_daily

    good_calls: list[str] = []

    async def _mock_fetch(sym, exch, from_d, to_d, bypass_cache=None):
        if sym == "BAD":
            raise RuntimeError("simulated broker failure")
        good_calls.append(sym)
        return _make_bars(10)

    with patch(
        "backend.api.persistence.backfill._count_db_bars",
        new=AsyncMock(return_value=0),
    ), patch(
        "backend.api.persistence.backfill._price_broker_in_cooloff",
        return_value=False,
    ), patch(
        "backend.api.persistence.ohlcv_store.get_or_fetch_daily",
        new=_mock_fetch,
    ):
        result = await backfill_ohlcv_daily(
            [("GOOD1", "NSE"), ("BAD", "NSE"), ("GOOD2", "NSE")],
            target_days=100,
            max_concurrent=3,
        )

    assert result["filled"] == 2, (
        f"2 good symbols must be filled despite 1 error. Got filled={result['filled']}"
    )
    assert result["requested"] == 3
    assert len(result["errors"]) == 1, (
        f"Exactly 1 error expected. Got: {result['errors']}"
    )
    bad_sym, bad_exch, bad_msg = result["errors"][0]
    assert bad_sym == "BAD"
    assert "simulated broker failure" in bad_msg
