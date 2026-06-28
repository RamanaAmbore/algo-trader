"""
Tests for GET /api/admin/pnl/benchmarks

Covers:
  - Unknown symbol returns 422
  - Multi-symbol success path (mocked Kite)
  - Date validation (from > to returns 422)
"""

import asyncio
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers — build a minimal kite historical_data stub
# ---------------------------------------------------------------------------

async def _call_benchmarks(symbols: str, from_date: str, to_date: str):
    """Call the pnl_benchmarks handler's underlying async logic directly,
    bypassing Litestar's route decorator wrapping."""
    from backend.api.routes.admin import AdminController
    # The Litestar @get decorator wraps the method; access the original via __wrapped__
    # if available, otherwise grab the undecorated fn from the class dict.
    import inspect
    handler = AdminController.__dict__["pnl_benchmarks"]
    # Litestar stores the original coroutine as handler.fn
    fn = getattr(handler, "fn", None) or handler
    ctrl = AdminController.__new__(AdminController)
    return await fn(ctrl, from_date=from_date, to_date=to_date, symbols=symbols)


def _make_candles(from_date, to_date, start_close=10000.0, step=100.0):
    """Return a list of fake daily candles between from_date and to_date."""
    from datetime import timedelta, datetime

    candles = []
    current = from_date
    i = 0
    while current <= to_date:
        candles.append({
            "date": datetime(current.year, current.month, current.day, 0, 0, 0),
            "open": start_close + step * i,
            "high": start_close + step * i + 50,
            "low":  start_close + step * i - 50,
            "close": start_close + step * i,
            "volume": 1_000_000,
        })
        current += timedelta(days=1)
        i += 1
    return candles


# ---------------------------------------------------------------------------
# Unit tests — pure logic in admin.py (no HTTP needed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_symbol_raises_422():
    """Requesting a symbol not in BENCHMARK_TOKENS returns 422."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.admin import BENCHMARK_TOKENS, PnlBenchmarkResponse, _BENCHMARK_CACHE

    _BENCHMARK_CACHE.clear()
    with pytest.raises(HTTPException) as exc_info:
        await _call_benchmarks("MADE_UP_INDEX", "2026-01-01", "2026-01-31")
    assert exc_info.value.status_code == 422
    assert "Unknown benchmark" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_date_order_validation():
    """from > to should raise 422."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.admin import _BENCHMARK_CACHE

    _BENCHMARK_CACHE.clear()
    with pytest.raises(HTTPException) as exc_info:
        await _call_benchmarks("NIFTY 50", "2026-03-15", "2026-01-01")
    assert exc_info.value.status_code == 422
    assert "must be <=" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_multi_symbol_success():
    """Two symbols requested — both series are returned with correct pct math."""
    from backend.api.routes.admin import _BENCHMARK_CACHE

    _BENCHMARK_CACHE.clear()

    from_d = date(2026, 1, 2)
    to_d   = date(2026, 1, 4)

    nifty_candles  = _make_candles(from_d, to_d, start_close=22000.0, step=200.0)
    sensex_candles = _make_candles(from_d, to_d, start_close=72000.0, step=500.0)

    candle_map = {
        256265: nifty_candles,
        265:    sensex_candles,
    }

    # admin.py was refactored to call broker.historical_data (Broker ABC)
    # instead of broker.kite.historical_data. Mock the new path.
    mock_broker = MagicMock()
    mock_broker.historical_data = MagicMock(
        side_effect=lambda token, *a, **kw: candle_map.get(token, [])
    )

    with patch("backend.brokers.registry.get_price_broker", return_value=mock_broker), \
         patch("backend.api.routes.admin.get_price_broker", return_value=mock_broker, create=True):
        resp = await _call_benchmarks("NIFTY 50,SENSEX", "2026-01-02", "2026-01-04")

    assert resp.from_date == "2026-01-02"
    assert resp.to_date   == "2026-01-04"
    assert len(resp.series) == 2

    nifty_s = next(s for s in resp.series if s.symbol == "NIFTY 50")
    assert len(nifty_s.closes) == 3
    assert nifty_s.closes[0]["pct_change_from_start"] == 0.0
    assert nifty_s.closes[1]["pct_change_from_start"] == pytest.approx(
        (22200.0 / 22000.0 - 1.0) * 100.0, rel=1e-3
    )

    sensex_s = next(s for s in resp.series if s.symbol == "SENSEX")
    assert len(sensex_s.closes) == 3
    assert sensex_s.closes[0]["pct_change_from_start"] == 0.0

    _BENCHMARK_CACHE.clear()


@pytest.mark.asyncio
async def test_symbol_fetch_failure_is_graceful():
    """If the broker call fails for one symbol, the series has empty closes
    and no exception is raised."""
    from backend.api.routes.admin import _BENCHMARK_CACHE

    _BENCHMARK_CACHE.clear()

    # Same Broker-ABC pattern — mock broker.historical_data directly.
    mock_broker = MagicMock()
    mock_broker.historical_data = MagicMock(side_effect=RuntimeError("Kite down"))

    with patch("backend.brokers.registry.get_price_broker", return_value=mock_broker), \
         patch("backend.api.routes.admin.get_price_broker", return_value=mock_broker, create=True):
        resp = await _call_benchmarks("NIFTY 50", "2026-01-02", "2026-01-04")

    assert len(resp.series) == 1
    assert resp.series[0].closes == []

    _BENCHMARK_CACHE.clear()
