"""
Test for batch_quote virtual MCX root key subscription fix.

Validates that when the frontend sends a virtual MCX root key (e.g., "MCX:CRUDEOIL")
and key_map.input_to_broker resolves it to a front-month contract (e.g., "MCX:CRUDEOIL26OCTFUT"),
the seen_pairs passed to _subscribe_batch_universe_to_ticker contains the RESOLVED symbol
(CRUDEOIL26OCTFUT), NOT the original bare root (CRUDEOIL).

Quality dimensions:
  1. SSOT        — batch_quote builds seen_pairs correctly using key_map.input_to_broker
                   for ticker subscribe (lines 792-794 in quote.py).
  2. Performance — _subscribe_batch_universe_to_ticker is called with correct pairs;
                   no redundant broker calls for subscription setup.
  3. Stale code  — batch_quote handler directly builds seen_pairs using the resolver.
  4. Reuse       — _subscribe_batch_universe_to_ticker is the single subscription point.
  5. UX          — virtual roots resolve to their actual contracts before ticker subscribe.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_batch_quote_virtual_mcx_root_subscription():
    """
    When batch_quote receives a virtual MCX root key (MCX:CRUDEOIL) that resolves
    to a front-month contract (MCX:CRUDEOIL26OCTFUT), the ticker subscription must
    use the RESOLVED contract symbol, not the bare root.

    Test setup:
      1. Mock key_map so "MCX:CRUDEOIL" → "MCX:CRUDEOIL26OCTFUT"
      2. Mock broker.quote() to return valid data for the resolved key
      3. Mock _subscribe_batch_universe_to_ticker to capture its arguments
      4. Mock market-open so the live path runs
      5. Verify seen_pairs contains ("MCX", "CRUDEOIL26OCTFUT")
    """
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    # ── Setup: key_map resolves virtual root to front-month contract ─────────────
    class FakeKeyMap:
        input_to_broker = {
            "MCX:CRUDEOIL": "MCX:CRUDEOIL26OCTFUT",  # Virtual root → resolved contract
        }
        broker_keys = ["MCX:CRUDEOIL26OCTFUT"]

    key_map = FakeKeyMap()

    # ── Broker returns valid quote for the resolved contract ──────────────────────
    broker_quote_response = {
        "MCX:CRUDEOIL26OCTFUT": {
            "last_price": 6100.0,
            "volume": 5000,
            "oi": 1200,
            "ohlc": {"open": 6050.0, "close": 6000.0, "high": 6150.0, "low": 5950.0},
            "depth": {
                "buy": [{"price": 6099.0, "quantity": 100, "orders": 5}],
                "sell": [{"price": 6101.0, "quantity": 150, "orders": 7}],
            },
        }
    }

    # ── Capture the seen_pairs argument to _subscribe_batch_universe_to_ticker ────
    captured_seen_pairs = None

    async def _mock_subscribe(seen_pairs):
        nonlocal captured_seen_pairs
        captured_seen_pairs = seen_pairs

    with patch(
        "backend.api.routes.quote._all_exchanges_closed",
        return_value=False,  # Market is open → live path
    ), patch(
        "backend.api.algo.symbol_resolver.resolve_market_data_keys",
        new=AsyncMock(return_value=key_map),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
    ) as mock_broker_registry, patch(
        "backend.api.routes.quote._subscribe_batch_universe_to_ticker",
        side_effect=_mock_subscribe,
    ), patch(
        "backend.api.routes.quote._record_live_batch_lkg",
    ):
        # Configure broker mock
        mock_broker = MagicMock()
        mock_broker.quote = MagicMock(return_value=broker_quote_response)
        mock_broker_registry.return_value = mock_broker

        # Mock asyncio.to_thread to run synchronously in tests
        async def _fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
            # Call the batch_quote handler
            handler_fn = QuoteController.batch_quote.fn
            request = BatchQuoteRequest(keys=["MCX:CRUDEOIL"])
            response = await handler_fn(None, request)

    # ── Verify the response contains the original key in the row ─────────────────
    assert len(response.items) == 1, "Should have one response row for MCX:CRUDEOIL"
    row = response.items[0]
    assert row.exchange == "MCX", f"Expected exchange MCX, got {row.exchange}"
    assert row.tradingsymbol == "CRUDEOIL", (
        f"Response row must use original symbol CRUDEOIL, got {row.tradingsymbol}"
    )
    assert row.ltp == 6100.0, f"Expected LTP 6100.0, got {row.ltp}"

    # ── CRITICAL: Verify seen_pairs uses RESOLVED contract for subscription ──────
    assert captured_seen_pairs is not None, "_subscribe_batch_universe_to_ticker was not called"
    assert len(captured_seen_pairs) == 1, (
        f"Expected 1 pair in seen_pairs, got {len(captured_seen_pairs)}"
    )

    exch, sym = captured_seen_pairs[0]
    assert exch.upper() == "MCX", (
        f"Expected exchange MCX in subscription pair, got {exch}"
    )
    assert sym.upper() == "CRUDEOIL26OCTFUT", (
        f"Expected RESOLVED symbol CRUDEOIL26OCTFUT in subscription, got {sym} — "
        f"this is the critical fix: subscription must use resolved contract, not bare root"
    )


@pytest.mark.asyncio
async def test_batch_quote_multiple_keys_virtual_and_direct():
    """
    When batch_quote receives a mix of virtual roots and direct contracts,
    only the virtual roots should be resolved; direct contracts should pass through.

    Test setup:
      1. "MCX:CRUDEOIL" → "MCX:CRUDEOIL26OCTFUT" (virtual root)
      2. "NSE:RELIANCE" → "NSE:RELIANCE" (direct, no resolution)
      3. Verify seen_pairs contains both resolved symbols
    """
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    class FakeKeyMap:
        input_to_broker = {
            "MCX:CRUDEOIL": "MCX:CRUDEOIL26OCTFUT",
            "NSE:RELIANCE": "NSE:RELIANCE",
        }
        broker_keys = ["MCX:CRUDEOIL26OCTFUT", "NSE:RELIANCE"]

    key_map = FakeKeyMap()

    broker_quote_response = {
        "MCX:CRUDEOIL26OCTFUT": {
            "last_price": 6100.0,
            "volume": 5000,
            "oi": 1200,
            "ohlc": {"open": 6050.0, "close": 6000.0},
            "depth": {"buy": [], "sell": []},
        },
        "NSE:RELIANCE": {
            "last_price": 2900.0,
            "volume": 123456,
            "oi": 0,
            "ohlc": {"open": 2880.0, "close": 2850.0},
            "depth": {"buy": [], "sell": []},
        },
    }

    captured_seen_pairs = None

    async def _mock_subscribe(seen_pairs):
        nonlocal captured_seen_pairs
        captured_seen_pairs = seen_pairs

    with patch(
        "backend.api.routes.quote._all_exchanges_closed",
        return_value=False,
    ), patch(
        "backend.api.algo.symbol_resolver.resolve_market_data_keys",
        new=AsyncMock(return_value=key_map),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
    ) as mock_broker_registry, patch(
        "backend.api.routes.quote._subscribe_batch_universe_to_ticker",
        side_effect=_mock_subscribe,
    ), patch(
        "backend.api.routes.quote._record_live_batch_lkg",
    ):
        mock_broker = MagicMock()
        mock_broker.quote = MagicMock(return_value=broker_quote_response)
        mock_broker_registry.return_value = mock_broker

        async def _fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=_fake_to_thread):
            handler_fn = QuoteController.batch_quote.fn
            request = BatchQuoteRequest(keys=["MCX:CRUDEOIL", "NSE:RELIANCE"])
            response = await handler_fn(None, request)

    # ── Response rows use original symbols ────────────────────────────────────────
    assert len(response.items) == 2, f"Expected 2 rows, got {len(response.items)}"
    crudeoil_row = next(
        (r for r in response.items if r.tradingsymbol == "CRUDEOIL"),
        None,
    )
    reliance_row = next(
        (r for r in response.items if r.tradingsymbol == "RELIANCE"),
        None,
    )
    assert crudeoil_row is not None
    assert reliance_row is not None

    # ── Subscription uses RESOLVED symbols ────────────────────────────────────────
    assert captured_seen_pairs is not None
    assert len(captured_seen_pairs) == 2, (
        f"Expected 2 pairs in seen_pairs, got {len(captured_seen_pairs)}"
    )

    # Extract pairs by exchange + symbol
    pairs_dict = {(exch.upper(), sym.upper()): True for exch, sym in captured_seen_pairs}

    # Virtual root must resolve to contract for subscription
    assert ("MCX", "CRUDEOIL26OCTFUT") in pairs_dict, (
        f"Expected resolved contract ('MCX', 'CRUDEOIL26OCTFUT') in subscription, "
        f"got {captured_seen_pairs}"
    )

    # Direct contract passes through unchanged
    assert ("NSE", "RELIANCE") in pairs_dict, (
        f"Expected direct symbol ('NSE', 'RELIANCE') in subscription, "
        f"got {captured_seen_pairs}"
    )
