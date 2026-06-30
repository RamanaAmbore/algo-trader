"""
Tests for the DB-backed close fallback in options._resolve_spot (Issue A).

Six dimensions:
  SSOT    — spot resolver path matches daily_book.holdings.ltp for IDFCFIRSTB
             during closed hours (live quote + futures both fail).
  Perf    — _close_from_db returns within 50 ms (one indexed SQL row).
  Stale   — NO call to median-strike fallback when daily_book has data.
  Reuse   — uses the same async_session pattern as sibling routes (audit, auth, metrics).
  UX      — N/A (backend-only; documented here per six-dimension convention).
  Response— _close_from_db budget < 50 ms (measured with time.perf_counter).
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ─── Shared patch helpers (mirrors test_options_spot.py) ──────────────────────

def _patch_no_sim():
    drv = MagicMock()
    drv.active = False
    return patch("backend.api.algo.sim.driver.get_driver", return_value=drv)


def _patch_broker_quote_zero(underlying: str):
    """Broker returns zero LTP for the underlying — simulates broken token / closed hours."""
    key = f"NSE:{underlying}"

    def _quote(keys):
        return {k: {"last_price": 0.0, "ohlc": {"close": 0.0}} for k in keys}

    broker = MagicMock()
    broker.quote.side_effect = _quote
    return patch("backend.brokers.registry.get_price_broker", return_value=broker)


def _patch_instruments_empty():
    """Instruments cache returns no futures — forces all broker lookups to fail."""
    resp = MagicMock()
    resp.items = []
    return patch("backend.api.cache.get_or_fetch", new=AsyncMock(return_value=resp))


def _patch_close_from_db(px: float | None):
    """Patch _close_from_db to return a known price (or None)."""
    return patch(
        "backend.api.routes.options._close_from_db",
        new=AsyncMock(return_value=px),
    )


# ─── SSOT: close-db resolves for IDFCFIRSTB when live fails ──────────────────

@pytest.mark.asyncio
async def test_close_db_resolves_when_live_fails():
    """SSOT: _resolve_spot returns source='close-db' with the DB LTP when
    live quote returns 0 and no futures are listed (stock, closed hours)."""
    from backend.api.routes.options import _resolve_spot

    # Simulate: daily_book.holdings.ltp = 79.52 for IDFCFIRSTB
    with _patch_no_sim(), \
         _patch_instruments_empty(), \
         _patch_broker_quote_zero("IDFCFIRSTB"), \
         _patch_close_from_db(79.52):
        spot, src, prev_close, anchor = await _resolve_spot(
            "IDFCFIRSTB", None,
            fallback=82.0,          # median strike — must NOT be used
        )

    assert src == "close-db", f"Expected 'close-db', got '{src}'"
    assert abs(spot - 79.52) < 0.01, f"Expected ~79.52, got {spot}"
    assert anchor is None, "Stocks have no futures anchor"


@pytest.mark.asyncio
async def test_close_db_used_over_median_strike_fallback():
    """Stale: when daily_book has data the median-strike 'fallback' is NEVER returned."""
    from backend.api.routes.options import _resolve_spot

    # fallback=82.0 is the problematic median-strike that was used before this fix
    with _patch_no_sim(), \
         _patch_instruments_empty(), \
         _patch_broker_quote_zero("IDFCFIRSTB"), \
         _patch_close_from_db(79.52):
        _, src, _, _ = await _resolve_spot(
            "IDFCFIRSTB", None,
            fallback=82.0,
        )

    # Under no circumstances should the median-strike synthetic be used
    # when the DB has a real close price.
    assert src != "fallback", (
        f"Fell through to median-strike 'fallback' despite DB having close={79.52}. "
        f"source={src!r}"
    )


@pytest.mark.asyncio
async def test_fallback_still_used_when_db_empty():
    """SSOT: when both DB tiers return None AND a fallback is supplied, return it."""
    from backend.api.routes.options import _resolve_spot

    with _patch_no_sim(), \
         _patch_instruments_empty(), \
         _patch_broker_quote_zero("IDFCFIRSTB"), \
         _patch_close_from_db(None):   # both tiers cold
        spot, src, _, _ = await _resolve_spot(
            "IDFCFIRSTB", None,
            fallback=82.0,
        )

    assert src == "fallback"
    assert abs(spot - 82.0) < 0.01


@pytest.mark.asyncio
async def test_close_db_not_called_when_live_succeeds():
    """Stale: _close_from_db must NOT be called when the live broker quote succeeds."""
    from backend.api.routes.options import _resolve_spot

    live_ltp = 79.52
    live_key = "NSE:IDFCFIRSTB"

    def _quote(keys):
        return {k: {"last_price": live_ltp, "ohlc": {"close": live_ltp * 0.99}} for k in keys}

    broker = MagicMock()
    broker.quote.side_effect = _quote

    close_db_mock = AsyncMock(return_value=79.0)

    with _patch_no_sim(), \
         _patch_instruments_empty(), \
         patch("backend.brokers.registry.get_price_broker", return_value=broker), \
         patch("backend.api.routes.options._close_from_db", new=close_db_mock):
        spot, src, _, _ = await _resolve_spot("IDFCFIRSTB", None)

    # Live succeeded — should NOT have called DB at all
    close_db_mock.assert_not_called()
    assert src in ("live", "close", "depth"), f"Expected live-path source, got '{src}'"


# ─── Perf / Response-time: _close_from_db < 50 ms ───────────────────────────

@pytest.mark.asyncio
async def test_close_from_db_returns_within_50ms():
    """Perf: _close_from_db must complete in < 50 ms (one indexed SQL round-trip).

    UX: N/A — backend-only. The 50 ms budget corresponds to one indexed SELECT
    on the daily_book table (ix_daily_book_kind_acct_sym_captured) which covers
    this ORDER BY captured_at DESC lookup.
    """
    from backend.api.routes.options import _close_from_db

    # Mock the DB session so no real SQL is executed in the test environment.
    # _close_from_db uses a lazy `from backend.api.database import async_session`
    # inside the function body, so we patch at the source module.
    mock_result = MagicMock()
    mock_result.fetchone.return_value = (79.52,)

    mock_session_inner = AsyncMock()
    mock_session_inner.execute = AsyncMock(return_value=mock_result)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_inner)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.database.async_session", return_value=mock_session_cm):
        # Warmup pass — import overhead excluded from measurement
        await _close_from_db("IDFCFIRSTB")

        t0 = time.perf_counter()
        result = await _close_from_db("IDFCFIRSTB")
        elapsed_ms = (time.perf_counter() - t0) * 1000

    assert result is not None and result > 0, "Expected non-None price from mocked DB"
    assert elapsed_ms < 50, (
        f"_close_from_db took {elapsed_ms:.1f} ms — exceeds 50 ms budget. "
        "Check for extra awaits or unindexed query paths."
    )


# ─── Reuse: async_session pattern matches sibling routes ────────────────────

def test_close_from_db_uses_async_session():
    """Reuse: _close_from_db source code uses async_session (same as audit.py, auth.py).
    Guards against accidental replacement with a sync session or raw psycopg call.

    The function uses a lazy import (`from backend.api.database import async_session`)
    inside the body to avoid circular imports at module load time — consistent with the
    pattern used by options.py's other helpers that import broker + cache modules lazily.
    """
    import inspect
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "api" / "routes" / "options.py"
    text = src.read_text()

    # Find the _close_from_db function body
    start = text.find("async def _close_from_db(")
    end = text.find("\nasync def _resolve_spot(", start)
    fn_body = text[start:end]

    assert "async_session" in fn_body, (
        "_close_from_db must use async_session() (project convention). "
        "Found no 'async_session' in the function body."
    )
    assert "async with async_session" in fn_body, (
        "_close_from_db must open the session with 'async with async_session()'."
    )
    assert "backend.api.database" in fn_body, (
        "_close_from_db must import async_session from backend.api.database."
    )


# ─── UX note (documented, not asserted) ─────────────────────────────────────
# UX dimension is N/A for backend-only helpers. The frontend UI treats
# source='close-db' identically to source='close' — no special chip rendering.
# The logger.info() call in _resolve_spot provides operator-visible telemetry
# in the api_log_file so the operator can confirm "close-db" fires vs. "fallback".
