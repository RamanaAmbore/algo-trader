"""
2026-10 audit fix: `/quote/batch` used to hit the broker's `quote()`
REST call directly on every request, with no cache or in-flight
coalescing — against Kite's ~1 req/s budget, multiple independent
frontend pollers requesting the SAME symbol set within the same
second each triggered their own broker round-trip.

Covers `_get_cached_batch_quote` (backend/api/routes/quote.py):
  - Concurrent identical-key-set requests coalesce onto ONE broker call.
  - A cached result is served within the TTL window without a new call.
  - Different key sets never share a cache entry (each gets its own
    broker call).
  - A broker exception is NOT cached — the very next call retries.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from backend.api.cache import invalidate_all
from backend.api.routes.quote import _get_cached_batch_quote


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_all()
    yield
    invalidate_all()


@pytest.mark.asyncio
async def test_concurrent_identical_keys_coalesce_to_one_broker_call():
    call_count = 0

    def _quote(keys):
        nonlocal call_count
        call_count += 1
        return {k: {"last_price": 100.0} for k in keys}

    mock_broker = MagicMock()
    mock_broker.quote = MagicMock(side_effect=_quote)

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        results = await asyncio.gather(
            _get_cached_batch_quote(["NSE:RELIANCE", "NSE:INFY"]),
            _get_cached_batch_quote(["NSE:INFY", "NSE:RELIANCE"]),  # same set, different order
        )

    assert call_count == 1, (
        f"expected exactly one broker.quote() call for two concurrent "
        f"identical-key requests, got {call_count}"
    )
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_cached_result_served_within_ttl_without_new_call():
    call_count = 0

    def _quote(keys):
        nonlocal call_count
        call_count += 1
        return {k: {"last_price": 200.0} for k in keys}

    mock_broker = MagicMock()
    mock_broker.quote = MagicMock(side_effect=_quote)

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        first = await _get_cached_batch_quote(["NSE:TCS"])
        second = await _get_cached_batch_quote(["NSE:TCS"])

    assert call_count == 1, "second call within TTL must be served from cache"
    assert first == second


@pytest.mark.asyncio
async def test_different_key_sets_do_not_share_cache_entry():
    call_count = 0

    def _quote(keys):
        nonlocal call_count
        call_count += 1
        return {k: {"last_price": 50.0} for k in keys}

    mock_broker = MagicMock()
    mock_broker.quote = MagicMock(side_effect=_quote)

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        await _get_cached_batch_quote(["NSE:WIPRO"])
        await _get_cached_batch_quote(["NSE:HDFC"])

    assert call_count == 2, "distinct key sets must each trigger their own broker call"


@pytest.mark.asyncio
async def test_broker_exception_is_not_cached_next_call_retries():
    call_count = 0

    def _quote(keys):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("broker timeout")
        return {k: {"last_price": 300.0} for k in keys}

    mock_broker = MagicMock()
    mock_broker.quote = MagicMock(side_effect=_quote)

    with patch("backend.brokers.registry.get_market_data_broker",
               return_value=mock_broker):
        with pytest.raises(RuntimeError):
            await _get_cached_batch_quote(["NSE:SBIN"])

        # Second call must retry (not serve a cached failure).
        result = await _get_cached_batch_quote(["NSE:SBIN"])

    assert call_count == 2, "a failed fetch must never be cached"
    assert result["NSE:SBIN"]["last_price"] == 300.0
