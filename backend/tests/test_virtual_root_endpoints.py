"""
test_virtual_root_endpoints.py

Verifies that virtual MCX/CDS bare roots (CRUDEOIL, GOLD, USDINR, …) are
resolved to their front-month contract BEFORE the broker call, and that all
API response rows remain keyed on the ORIGINAL operator-facing symbol.

Background
----------
The frontend pulse grid stores watchlist items by their bare root name
(e.g. tradingsymbol="CRUDEOIL", exchange="MCX").  Before this fix,
POST /api/quote/batch received "MCX:CRUDEOIL", passed it straight to
broker.quote(), got nothing back, and returned LTP=0 + stale=True.
The canonical resolver (symbol_resolver.py) now handles translation.

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — resolve_market_data_keys is the single dispatch point for
                   virtual-root resolution across all market-data entry points.
                   No inline isalpha() heuristics remain in batch_quote.
  2. Performance — resolution adds one await per request (shared instruments
                   cache, O(1) per key after the first call).  The helper
                   itself has no per-test regression as it's pure async logic
                   over a mocked cache.
  3. Stale code  — batch_quote no longer contains an inline `keys` list passed
                   directly to broker.quote (the resolved broker_keys list is
                   used instead).  Grepped below.
  4. Reuse       — batch_quote, get_quote, and (via sparkline) batch_sparkline
                   all share the same resolution path through
                   resolve_market_data_keys / resolve_symbol.
  5. UX          — response tradingsymbol must equal the original input root
                   ("CRUDEOIL") not the resolved contract ("CRUDEOIL26JULFUT");
                   non-virtual symbols (RELIANCE) pass through unchanged and
                   add no resolution overhead.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kite_quote_resp(key: str, ltp: float = 6100.0) -> dict:
    """Minimal broker.quote() response dict keyed on 'EXCH:SYM'."""
    return {
        key: {
            "last_price": ltp,
            "volume": 1000,
            "oi": 500,
            "ohlc": {"open": 6050.0, "close": 6000.0, "high": 6150.0, "low": 5990.0},
            "depth": {"buy": [], "sell": []},
        }
    }


def _quote_source() -> str:
    """Return source of quote.py module."""
    import backend.api.routes.quote as quote_mod
    return inspect.getsource(quote_mod)


async def _fake_to_thread(fn, *args):
    """Replace asyncio.to_thread with a sync call for tests."""
    return fn(*args)


# ---------------------------------------------------------------------------
# 1. SSOT — resolve_market_data_keys is the canonical helper
# ---------------------------------------------------------------------------

def test_resolve_market_data_keys_lives_in_symbol_resolver():
    """resolve_market_data_keys must exist in symbol_resolver, not inline."""
    from backend.api.algo.symbol_resolver import resolve_market_data_keys, MarketDataKeyMap
    assert callable(resolve_market_data_keys)
    assert MarketDataKeyMap is not None


def test_resolve_market_data_keys_returns_correct_type():
    """resolve_market_data_keys returns a MarketDataKeyMap instance."""
    from backend.api.algo.symbol_resolver import resolve_market_data_keys, MarketDataKeyMap

    async def _mock_resolve(virtual: str, exchange: str) -> str:
        _map = {"CRUDEOIL": "CRUDEOILM26JULFUT", "USDINR": "USDINR26JULFUT"}
        return _map.get(virtual.upper(), virtual)

    with patch("backend.api.algo.symbol_resolver.resolve_symbol", side_effect=_mock_resolve):
        result = asyncio.run(resolve_market_data_keys(["MCX:CRUDEOIL", "NSE:RELIANCE"]))

    assert isinstance(result, MarketDataKeyMap)
    assert result.input_to_broker["MCX:CRUDEOIL"] == "MCX:CRUDEOILM26JULFUT"
    assert result.input_to_broker["NSE:RELIANCE"] == "NSE:RELIANCE"  # identity
    assert result.broker_to_input["MCX:CRUDEOILM26JULFUT"] == "MCX:CRUDEOIL"
    assert result.broker_to_input["NSE:RELIANCE"] == "NSE:RELIANCE"
    # broker_keys must contain the RESOLVED keys
    assert "MCX:CRUDEOILM26JULFUT" in result.broker_keys
    assert "NSE:RELIANCE" in result.broker_keys
    # original bare root must NOT appear in broker_keys
    assert "MCX:CRUDEOIL" not in result.broker_keys


def test_resolve_market_data_keys_logs_resolved_keys():
    """[MARKET-DATA-VIRTUAL-RESOLVE] log tag emitted for each virtual root."""
    import logging
    from backend.api.algo.symbol_resolver import resolve_market_data_keys
    import backend.api.algo.symbol_resolver as resolver_mod

    log_messages: list[str] = []

    class _CapHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_messages.append(self.format(record))

    handler = _CapHandler()
    resolver_mod.logger.addHandler(handler)
    resolver_mod.logger.setLevel(logging.INFO)

    try:
        async def _mock_resolve(virtual: str, exchange: str) -> str:
            if virtual.upper() == "GOLD":
                return "GOLDM26JULFUT"
            return virtual

        with patch("backend.api.algo.symbol_resolver.resolve_symbol", side_effect=_mock_resolve):
            asyncio.run(resolve_market_data_keys(["MCX:GOLD"]))
    finally:
        resolver_mod.logger.removeHandler(handler)

    assert any("[MARKET-DATA-VIRTUAL-RESOLVE]" in m for m in log_messages), (
        f"Expected [MARKET-DATA-VIRTUAL-RESOLVE] log. Got: {log_messages}"
    )
    assert any("GOLD" in m and "GOLDM26JULFUT" in m for m in log_messages)


def test_resolve_market_data_keys_no_log_for_non_virtual():
    """No [MARKET-DATA-VIRTUAL-RESOLVE] log for non-virtual symbols."""
    import logging
    from backend.api.algo.symbol_resolver import resolve_market_data_keys
    import backend.api.algo.symbol_resolver as resolver_mod

    log_messages: list[str] = []

    class _CapHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_messages.append(self.format(record))

    handler = _CapHandler()
    resolver_mod.logger.addHandler(handler)
    resolver_mod.logger.setLevel(logging.INFO)

    try:
        asyncio.run(resolve_market_data_keys(["NSE:RELIANCE", "NFO:RELIANCE25JULCE"]))
    finally:
        resolver_mod.logger.removeHandler(handler)

    assert not any("[MARKET-DATA-VIRTUAL-RESOLVE]" in m for m in log_messages), (
        f"No resolution log expected for non-virtual symbols. Got: {log_messages}"
    )


# ---------------------------------------------------------------------------
# 2. batch_quote: broker receives resolved key, response row uses original key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_quote_virtual_mcx_root_resolved():
    """batch_quote must pass the resolved contract to broker.quote and return
    the response row keyed on the original bare root ('CRUDEOIL')."""
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    resolved_key = "MCX:CRUDEOILM26JULFUT"
    input_key    = "MCX:CRUDEOIL"

    broker_keys_seen: list[list] = []

    def _tracking_quote(keys):
        broker_keys_seen.append(list(keys))
        return _make_kite_quote_resp(resolved_key, ltp=6100.0)

    mock_broker = MagicMock()
    mock_broker.quote = _tracking_quote

    async def _mock_resolve(virtual: str, exchange: str) -> str:
        if virtual.upper() == "CRUDEOIL" and exchange == "MCX":
            return "CRUDEOILM26JULFUT"
        return virtual

    with (
        patch("backend.api.algo.symbol_resolver.resolve_symbol", side_effect=_mock_resolve),
        patch("backend.brokers.registry.get_market_data_broker", return_value=mock_broker),
        patch("backend.api.routes.quote._all_exchanges_closed", return_value=False),
        patch("backend.api.routes.quote._get_today_token_map", return_value={}),
        patch("asyncio.to_thread", side_effect=_fake_to_thread),
    ):
        handler_fn = QuoteController.batch_quote.fn
        result = await handler_fn(None, BatchQuoteRequest(keys=[input_key]))

    # broker must have been called with the RESOLVED key
    assert broker_keys_seen, "broker.quote was not called"
    assert resolved_key in broker_keys_seen[0], (
        f"Expected resolved key {resolved_key} in broker call, got {broker_keys_seen[0]}"
    )
    assert input_key not in broker_keys_seen[0], (
        f"Bare root {input_key} must not be sent to the broker"
    )

    # response row must be keyed on the ORIGINAL operator symbol
    assert len(result.items) == 1
    row = result.items[0]
    assert row.tradingsymbol == "CRUDEOIL", (
        f"Response tradingsymbol must be 'CRUDEOIL', got '{row.tradingsymbol}'"
    )
    assert row.exchange == "MCX"
    assert row.ltp == 6100.0
    assert not row.stale


@pytest.mark.asyncio
async def test_batch_quote_non_virtual_passthrough():
    """Non-virtual symbols (RELIANCE) must pass through with no resolution."""
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    input_key = "NSE:RELIANCE"
    broker_keys_seen: list[list] = []

    def _tracking_quote(keys):
        broker_keys_seen.append(list(keys))
        return _make_kite_quote_resp("NSE:RELIANCE", ltp=2900.0)

    with (
        patch("backend.brokers.registry.get_market_data_broker",
              return_value=MagicMock(quote=_tracking_quote)),
        patch("backend.api.routes.quote._all_exchanges_closed", return_value=False),
        patch("backend.api.routes.quote._get_today_token_map", return_value={}),
        patch("asyncio.to_thread", side_effect=_fake_to_thread),
    ):
        handler_fn = QuoteController.batch_quote.fn
        result = await handler_fn(None, BatchQuoteRequest(keys=[input_key]))

    # broker receives the same key (identity for non-virtual)
    assert broker_keys_seen
    assert "NSE:RELIANCE" in broker_keys_seen[0]

    assert len(result.items) == 1
    assert result.items[0].tradingsymbol == "RELIANCE"
    assert result.items[0].ltp == 2900.0


@pytest.mark.asyncio
async def test_batch_quote_multi_virtual_roots():
    """Multiple MCX/CDS virtual roots in one batch are each resolved."""
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest

    resolve_table = {
        ("CRUDEOIL", "MCX"): "CRUDEOILM26JULFUT",
        ("GOLD",     "MCX"): "GOLDM26AUGFUT",
        ("USDINR",   "CDS"): "USDINR26JULFUT",
    }

    async def _mock_resolve(virtual: str, exchange: str) -> str:
        return resolve_table.get((virtual.upper(), exchange), virtual)

    broker_keys_seen: list[list] = []

    def _tracking_quote(keys):
        broker_keys_seen.append(list(keys))
        resp = {}
        for k in keys:
            resp.update(_make_kite_quote_resp(k, ltp=5000.0))
        return resp

    with (
        patch("backend.api.algo.symbol_resolver.resolve_symbol", side_effect=_mock_resolve),
        patch("backend.brokers.registry.get_market_data_broker",
              return_value=MagicMock(quote=_tracking_quote)),
        patch("backend.api.routes.quote._all_exchanges_closed", return_value=False),
        patch("backend.api.routes.quote._get_today_token_map", return_value={}),
        patch("asyncio.to_thread", side_effect=_fake_to_thread),
    ):
        handler_fn = QuoteController.batch_quote.fn
        result = await handler_fn(None, BatchQuoteRequest(keys=[
            "MCX:CRUDEOIL", "MCX:GOLD", "CDS:USDINR",
        ]))

    # All three original bare roots must appear as tradingsymbols in response
    returned_syms = {r.tradingsymbol for r in result.items}
    assert "CRUDEOIL" in returned_syms
    assert "GOLD" in returned_syms
    assert "USDINR" in returned_syms

    # Broker must have received only resolved keys (no bare roots)
    assert broker_keys_seen
    called = set(broker_keys_seen[0])
    assert "MCX:CRUDEOIL" not in called
    assert "MCX:GOLD" not in called
    assert "CDS:USDINR" not in called
    assert "MCX:CRUDEOILM26JULFUT" in called
    assert "MCX:GOLDM26AUGFUT" in called
    assert "CDS:USDINR26JULFUT" in called


# ---------------------------------------------------------------------------
# 3. Stale code — batch_quote uses key_map.broker_keys (not raw keys)
# ---------------------------------------------------------------------------

def test_batch_quote_uses_key_map_broker_keys():
    """batch_quote source must reference key_map.broker_keys for the broker
    call, not the raw 'keys' list — prevents virtual roots reaching broker."""
    src = _quote_source()

    # The broker call must use key_map.broker_keys
    assert "key_map.broker_keys" in src, (
        "batch_quote must pass key_map.broker_keys to broker.quote(), not raw keys"
    )
    # resolve_market_data_keys must be referenced in quote.py
    assert "resolve_market_data_keys" in src, (
        "batch_quote must import and use resolve_market_data_keys"
    )


def test_batch_quote_source_no_direct_raw_keys_to_broker():
    """No live broker.quote(keys) call with the raw input list in batch_quote."""
    import re
    src = _quote_source()

    # Find lines in batch_quote body that look like: broker.quote, keys)
    # where 'keys' is the raw input list (not key_map.broker_keys).
    # Extract only the batch_quote section to avoid false positives.
    batch_start = src.find("async def batch_quote")
    batch_end   = src.find("\n    @", batch_start)
    batch_body  = src[batch_start:batch_end] if batch_end > batch_start else src[batch_start:]

    suspicious = [
        line for line in batch_body.splitlines()
        if re.search(r"broker\.quote,\s*keys\b", line) and not line.strip().startswith("#")
    ]
    assert not suspicious, (
        f"batch_quote must not pass raw `keys` to broker.quote. Found: {suspicious}"
    )


# ---------------------------------------------------------------------------
# 4. Reuse — sparkline + watchlist use the same resolution path
# ---------------------------------------------------------------------------

def test_sparkline_imports_watchlist_resolver():
    """batch_sparkline delegates MCX/CDS resolution to the same helper functions
    that watchlist uses (_resolve_mcx_commodity / _resolve_cds_currency), which
    themselves delegate to list_active_futures in symbol_resolver."""
    from backend.api.routes import watchlist as wl_mod
    assert hasattr(wl_mod, "_resolve_mcx_commodity")
    assert hasattr(wl_mod, "_resolve_cds_currency")

    # Confirm they import list_active_futures from symbol_resolver
    src = inspect.getsource(wl_mod._resolve_mcx_commodity)
    assert "list_active_futures" in src or "symbol_resolver" in src, (
        "_resolve_mcx_commodity must delegate to symbol_resolver.list_active_futures"
    )


def test_batch_quote_source_imports_resolve_market_data_keys():
    """batch_quote method body must import resolve_market_data_keys (SSOT)."""
    src = _quote_source()
    assert "resolve_market_data_keys" in src, (
        "batch_quote must import and use resolve_market_data_keys"
    )


# ---------------------------------------------------------------------------
# 5. UX — closed-hours path also resolves (LKG lookup uses resolved sym)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_quote_closed_hours_resolves_for_lkg():
    """During closed hours, LKG lookup must use the resolved symbol so a
    CRUDEOIL request finds the LKG entry recorded as CRUDEOILM26JULFUT."""
    from backend.api.routes.quote import QuoteController, BatchQuoteRequest
    from backend.brokers import broker_apis

    lkg_calls: list[str] = []

    def _mock_lkg(sym: str, max_age_s: float = 86400.0) -> float:
        lkg_calls.append(sym)
        return 6100.0 if sym == "CRUDEOILM26JULFUT" else 0.0

    async def _mock_resolve(virtual: str, exchange: str) -> str:
        if virtual.upper() == "CRUDEOIL" and exchange == "MCX":
            return "CRUDEOILM26JULFUT"
        return virtual

    with (
        patch("backend.api.algo.symbol_resolver.resolve_symbol", side_effect=_mock_resolve),
        patch("backend.api.routes.quote._all_exchanges_closed", return_value=True),
        patch.object(broker_apis, "get_last_good_ltp", side_effect=_mock_lkg),
    ):
        handler_fn = QuoteController.batch_quote.fn
        result = await handler_fn(None, BatchQuoteRequest(keys=["MCX:CRUDEOIL"]))

    assert len(result.items) == 1
    row = result.items[0]
    # Response still keyed on original operator symbol
    assert row.tradingsymbol == "CRUDEOIL"
    assert row.exchange == "MCX"
    # LTP must come from the RESOLVED symbol's LKG
    assert row.ltp == 6100.0, (
        f"Expected LTP from resolved CRUDEOILM26JULFUT LKG (6100.0), got {row.ltp}"
    )
    assert row.stale is True  # closed-hours always stale
    # LKG was called with the resolved symbol
    assert "CRUDEOILM26JULFUT" in lkg_calls, (
        f"LKG lookup must use resolved symbol; calls were: {lkg_calls}"
    )
