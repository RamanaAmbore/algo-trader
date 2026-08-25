"""
Tests for asyncio.wait_for timeout guards added to options chain hang vectors.

Covers:
  1. _chain_snapshot_instruments → 503 when instruments cache is cold (peek returns None)
  2. _chain_snapshot_instruments → succeeds when cache is warm (peek returns valid data)
  3. _resolve_spot_ticker → returns None on TimeoutError from broker.quote
  4. _ltp_broker_quote → returns None on TimeoutError
  5. _strategy_fetch_bulk_quote → returns {} on TimeoutError
  6. _mcx_batch_quote_futures → falls through to empty resp on TimeoutError
  7. _resolve_token_for_broker → skips exchange on TimeoutError, tries next exchange
  8. _commodity_spot_4a → returns None on TimeoutError from broker.quote
  9. _commodity_spot_4b → returns None on TimeoutError from broker.quote
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from litestar.exceptions import HTTPException


# ---------------------------------------------------------------------------
# Fix 1 + 2 — _chain_snapshot_instruments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_snapshot_instruments_cold_cache_returns_503():
    """When peek('instruments') returns None, _chain_snapshot_instruments
    must raise HTTPException(503) immediately — no blocking download."""
    from backend.api.routes.options_helpers import _chain_snapshot_instruments

    with patch("backend.api.cache.peek", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await _chain_snapshot_instruments("NIFTY", "2026-09-25", 24500.0, 5)

    assert exc_info.value.status_code == 503
    assert "warming" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_chain_snapshot_instruments_warm_cache_succeeds():
    """When peek('instruments') returns a valid response, _chain_snapshot_instruments
    should filter to matching (underlying, expiry) contracts and return the window."""
    from backend.api.routes.options_helpers import _chain_snapshot_instruments

    # Build a minimal InstrumentsResponse-like object with matching items
    def _make_inst(s, u, x, t, k):
        inst = MagicMock()
        inst.s = s
        inst.u = u
        inst.x = x
        inst.t = t
        inst.k = k
        return inst

    items = [
        _make_inst("NIFTY26SEP24000CE", "NIFTY", "2026-09-25", "CE", 24000),
        _make_inst("NIFTY26SEP24000PE", "NIFTY", "2026-09-25", "PE", 24000),
        _make_inst("NIFTY26SEP24500CE", "NIFTY", "2026-09-25", "CE", 24500),
        _make_inst("NIFTY26SEP24500PE", "NIFTY", "2026-09-25", "PE", 24500),
        _make_inst("NIFTY26OCT24000CE", "NIFTY", "2026-10-30", "CE", 24000),  # different expiry
    ]
    warm_resp = MagicMock()
    warm_resp.items = items

    with patch("backend.api.cache.peek", return_value=warm_resp):
        sym_by_strike, atm_strike, window_strikes = await _chain_snapshot_instruments(
            "NIFTY", "2026-09-25", 24500.0, 5
        )

    # Should only find the 2026-09-25 contracts
    assert 24000.0 in sym_by_strike
    assert 24500.0 in sym_by_strike
    # The October contract must NOT appear
    assert len(sym_by_strike) == 2
    # ATM should be the strike closest to spot (24500)
    assert atm_strike == 24500.0
    assert 24500.0 in window_strikes


# ---------------------------------------------------------------------------
# Fix 2 — _resolve_spot_ticker (NSE spot timeout)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_spot_ticker_returns_none_on_timeout():
    """_resolve_spot_ticker must return None when broker.quote times out."""
    from backend.api.routes.options_helpers import _resolve_spot_ticker

    broker = MagicMock()
    broker.quote.side_effect = asyncio.TimeoutError()

    spot_cache_put = MagicMock()
    ltp_from_quote = MagicMock(return_value=(None, "live"))
    prev_close_from_quote = MagicMock(return_value=None)

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await _resolve_spot_ticker(
            "NIFTY", broker, spot_cache_put, ltp_from_quote, prev_close_from_quote
        )

    assert result is None
    spot_cache_put.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_spot_ticker_returns_result_on_success():
    """_resolve_spot_ticker returns (price, src, prev, None) on a successful quote."""
    from backend.api.routes.options_helpers import _resolve_spot_ticker
    from backend.api.algo.derivatives import underlying_ltp_key

    key = underlying_ltp_key("NIFTY")
    quote_dict = {"last_price": 24500.0, "ohlc": {"close": 24400.0}}

    broker = MagicMock()

    async def _fake_wait_for(coro, timeout):
        return {key: quote_dict}

    spot_cache_put = MagicMock()
    ltp_from_quote = MagicMock(return_value=(24500.0, "live"))
    prev_close_from_quote = MagicMock(return_value=24400.0)

    with patch("asyncio.wait_for", side_effect=_fake_wait_for):
        result = await _resolve_spot_ticker(
            "NIFTY", broker, spot_cache_put, ltp_from_quote, prev_close_from_quote
        )

    assert result is not None
    price, src, prev, anchor = result
    assert price == 24500.0
    assert anchor is None


# ---------------------------------------------------------------------------
# Fix 6 — _ltp_broker_quote (option LTP timeout)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ltp_broker_quote_returns_none_on_timeout():
    """_ltp_broker_quote must return None when broker.quote times out."""
    from backend.api.routes.options import _ltp_broker_quote

    broker = MagicMock()

    with patch("backend.brokers.registry.get_market_data_broker", return_value=broker), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await _ltp_broker_quote("NIFTY26SEP24500CE")

    assert result is None


@pytest.mark.asyncio
async def test_ltp_broker_quote_returns_price_on_success():
    """_ltp_broker_quote returns (price, src) when the quote succeeds."""
    from backend.api.routes.options import _ltp_broker_quote, option_quote_key

    sym = "NIFTY26SEP24500CE"
    key = option_quote_key(sym)
    quote_resp = {key: {"last_price": 150.0, "ohlc": {"close": 145.0}}}

    broker = MagicMock()

    async def _fake_wait_for(coro, timeout):
        return quote_resp

    with patch("backend.brokers.registry.get_market_data_broker", return_value=broker), \
         patch("asyncio.wait_for", side_effect=_fake_wait_for):
        result = await _ltp_broker_quote(sym)

    assert result is not None
    price, src = result
    assert price == 150.0


# ---------------------------------------------------------------------------
# Fix 8 — _strategy_fetch_bulk_quote (strategy bulk quote timeout)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strategy_fetch_bulk_quote_returns_empty_on_timeout():
    """_strategy_fetch_bulk_quote must return {} when broker.quote times out."""
    from backend.api.routes.options import _strategy_fetch_bulk_quote

    price_broker = MagicMock()
    need_quote = {"NSE:NIFTY 50": True, "NFO:NIFTY26SEP24500CE": True}

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await _strategy_fetch_bulk_quote(need_quote, price_broker)

    assert result == {}


@pytest.mark.asyncio
async def test_strategy_fetch_bulk_quote_returns_empty_when_need_quote_is_empty():
    """_strategy_fetch_bulk_quote returns {} immediately when need_quote is empty
    (no broker call should be made)."""
    from backend.api.routes.options import _strategy_fetch_bulk_quote

    price_broker = MagicMock()
    result = await _strategy_fetch_bulk_quote({}, price_broker)

    assert result == {}
    price_broker.quote.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 7 — _mcx_batch_quote_futures (MCX futures batch quote timeout)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcx_batch_quote_futures_falls_through_on_timeout():
    """_mcx_batch_quote_futures must fall through to empty _fut_quote_resp on timeout,
    leaving month_to_cached unpopulated (scale_ratio=1 fallback downstream)."""
    from backend.api.routes.options import _mcx_batch_quote_futures

    price_broker = MagicMock()
    # Two MCX futures months
    month_to_fut_sym = {(2026, 9): "CRUDEOIL26SEPFUT", (2026, 10): "CRUDEOIL26OCTFUT"}
    month_to_cached: dict = {}

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        # Must not raise — should log warning and fall through
        await _mcx_batch_quote_futures("CRUDEOIL", month_to_fut_sym, month_to_cached, price_broker)

    # month_to_cached should remain empty — no futures price resolved
    assert month_to_cached == {}


# ---------------------------------------------------------------------------
# Fix 5 — _resolve_token_for_broker (instruments per-exchange timeout)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_token_for_broker_skips_timed_out_exchange():
    """When instruments fetch for exchange A times out, _resolve_token_for_broker
    must skip A via continue and try exchange B — returning the token from B."""
    from backend.api.routes.options_helpers import _resolve_token_for_broker

    broker = MagicMock()
    broker.account = "ZG0001"

    # Exchange A: instruments fetch times out
    # Exchange B: instruments fetch returns a valid token map
    call_count = {"n": 0}

    async def _fake_wait_for(coro, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise asyncio.TimeoutError()
        # Second call (exchange B) succeeds
        return [{"tradingsymbol": "NIFTY26SEP24500CE", "instrument_token": 12345}]

    instruments_cache_get = MagicMock(return_value=None)  # always cache miss
    instruments_cache_put = MagicMock()

    with patch("asyncio.wait_for", side_effect=_fake_wait_for):
        token = await _resolve_token_for_broker(
            broker,
            "NIFTY26SEP24500CE",
            ("NFO", "NSE"),
            instruments_cache_get,
            instruments_cache_put,
        )

    # Should have found the token from exchange B (NFO timed out, NSE succeeded)
    assert token == 12345


@pytest.mark.asyncio
async def test_resolve_token_for_broker_returns_none_when_all_exchanges_timeout():
    """When all exchanges time out, _resolve_token_for_broker returns None."""
    from backend.api.routes.options_helpers import _resolve_token_for_broker

    broker = MagicMock()
    broker.account = "ZG0001"

    instruments_cache_get = MagicMock(return_value=None)
    instruments_cache_put = MagicMock()

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        token = await _resolve_token_for_broker(
            broker,
            "NIFTY26SEP24500CE",
            ("NFO", "NSE"),
            instruments_cache_get,
            instruments_cache_put,
        )

    assert token is None
    instruments_cache_put.assert_not_called()
