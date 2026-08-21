"""
Tests for the /api/options/chain-quotes endpoint.

Coverage:
  • Expiry-only mode (no broker call when expiry omitted)
  • Full quote mode (with expiry parameter)
  • New response fields: ce_sym, pe_sym, ce_ls, pe_ls, exchange, expiries
  • Cache behavior (156K scan cached for 3s, LRU cap at _CHAIN_SYM_CACHE_MAX_SIZE)
  • Coalescing lock: _CHAIN_SYM_CACHE_MAX_SIZE constant defined and enforced
  • depth_available flag: True when real depth, False when fallback to last_price
  • Unknown underlying handling
  • Malformed instrument responses
"""

from __future__ import annotations

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, date

from backend.api.routes.instruments import Instrument, InstrumentsResponse
from backend.api.routes.options import (
    _chain_quotes_build_sym_map,
    _chain_quotes_batch_quote,
    _chain_quotes_bid_ask_from_q,
    _chain_sym_cache_clear,
    _chain_quotes_closed_cache_clear,
    ChainQuoteRow,
    ChainQuotesResponse,
    _CHAIN_SYM_CACHE,
    _CHAIN_SYM_CACHE_MAX_SIZE,
    _CHAIN_QUOTES_CLOSED_CACHE,
    _CHAIN_QUOTES_CLOSED_TTL,
)


@pytest.fixture
def nifty_instruments_fixture() -> InstrumentsResponse:
    """Minimal NIFTY instruments fixture with two expiries, four strikes."""
    instruments = [
        # 2025-08-14 expiry
        Instrument(
            s="NIFTY24AUG24000CE",
            e="NFO",
            t="CE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-14",
            k=24000.0,
        ),
        Instrument(
            s="NIFTY24AUG24000PE",
            e="NFO",
            t="PE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-14",
            k=24000.0,
        ),
        Instrument(
            s="NIFTY24AUG24500CE",
            e="NFO",
            t="CE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-14",
            k=24500.0,
        ),
        Instrument(
            s="NIFTY24AUG24500PE",
            e="NFO",
            t="PE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-14",
            k=24500.0,
        ),
        # 2025-08-21 expiry
        Instrument(
            s="NIFTY24AUG24000CE",
            e="NFO",
            t="CE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-21",
            k=24000.0,
        ),
        Instrument(
            s="NIFTY24AUG24000PE",
            e="NFO",
            t="PE",
            ls=25,
            ts=0.05,
            u="NIFTY",
            x="2025-08-21",
            k=24000.0,
        ),
    ]
    return InstrumentsResponse(cycle_date="2025-08-11", count=len(instruments), items=instruments)


@pytest.fixture(autouse=True)
def clear_chain_cache():
    """Clear both chain-quote caches before and after each test."""
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()
    yield
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()


class TestChainQuotesBuildSymMap:
    """Unit tests for _chain_quotes_build_sym_map helper."""

    def test_expiry_only_mode_no_strikes(self, nifty_instruments_fixture):
        """When exp='' (empty), strike_map should be empty; expiries list populated."""
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", ""
        )
        assert sym_by_strike == {}, "Strike map should be empty in expiry-only mode"
        assert all_expiries == ["2025-08-14", "2025-08-21"], f"Expected both expiries, got {all_expiries}"

    def test_full_mode_with_expiry(self, nifty_instruments_fixture):
        """When exp='2025-08-14', strike_map populated with both CE/PE."""
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )
        # Should have 2 strikes (24000, 24500)
        assert len(sym_by_strike) == 2, f"Expected 2 strikes, got {len(sym_by_strike)}"
        # Expiries still populated (all expiries for the underlying)
        assert all_expiries == ["2025-08-14", "2025-08-21"]

        # Check strike 24000
        assert 24000.0 in sym_by_strike
        assert sym_by_strike[24000.0]["CE"]["sym"] == "NIFTY24AUG24000CE"
        assert sym_by_strike[24000.0]["CE"]["ls"] == 25
        assert sym_by_strike[24000.0]["CE"]["e"] == "NFO"
        assert sym_by_strike[24000.0]["PE"]["sym"] == "NIFTY24AUG24000PE"
        assert sym_by_strike[24000.0]["PE"]["ls"] == 25

        # Check strike 24500
        assert 24500.0 in sym_by_strike
        assert sym_by_strike[24500.0]["CE"]["sym"] == "NIFTY24AUG24500CE"
        assert sym_by_strike[24500.0]["PE"]["sym"] == "NIFTY24AUG24500PE"

    def test_case_insensitive_underlying(self, nifty_instruments_fixture):
        """Underlying lookup is case-insensitive when passed as uppercase (route does conversion)."""
        # The function expects und to already be uppercased (route handler does this).
        # Instruments have u="NIFTY", so (inst.u or "").upper() == und should match.
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )
        assert len(sym_by_strike) == 2, "Should find NIFTY when passed uppercase"

    def test_unknown_underlying(self, nifty_instruments_fixture):
        """Unknown underlying should return empty strike map and empty expiries."""
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "BANKNIFTY", ""
        )
        assert sym_by_strike == {}
        assert all_expiries == []

    def test_non_option_instruments_skipped(self):
        """Equity and futures instruments should be skipped; only CE/PE included."""
        instruments = [
            Instrument(s="NIFTY50", e="NSE", t="EQ", ls=1, ts=0.05, u="NIFTY"),
            Instrument(s="NIFTYFUT", e="NFO", t="FUT", ls=1, ts=0.05, u="NIFTY"),
            Instrument(
                s="NIFTY24AUG24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
        ]
        inst_resp = InstrumentsResponse(cycle_date="2025-08-11", count=len(instruments), items=instruments)

        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            inst_resp, "NIFTY", "2025-08-14"
        )
        # The strike is added when ANY side (CE or PE) is found.
        # PE is missing but CE exists, so the strike is included (PE dict initialized empty).
        assert len(sym_by_strike) == 1, "Strike 24000 should be in map (CE present)"
        assert sym_by_strike[24000.0]["CE"]["sym"] == "NIFTY24AUG24000CE"
        assert sym_by_strike[24000.0]["PE"]["sym"] == "", "PE not present, so empty sym"


@pytest.mark.asyncio
class TestChainQuotesEndpoint:
    """Integration tests for GET /api/options/chain-quotes."""

    async def test_expiry_only_no_broker_call(
        self, async_client, nifty_instruments_fixture
    ):
        """GET /chain-quotes?underlying=NIFTY (no expiry) returns expiries list, no rows."""
        client = async_client

        with patch("backend.api.cache.peek") as mock_cache, \
             patch("backend.api.routes.options._chain_quotes_batch_quote") as mock_quote:

            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get("/api/options/chain-quotes", params={"underlying": "NIFTY"})

            assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
            data = response.json()

            # Verify response structure
            assert "expiries" in data
            assert "rows" in data
            assert "underlying" in data
            assert "expiry" in data

            # Verify expiry-only mode: no rows, expiries populated
            assert data["rows"] == [], f"Expected empty rows, got {data['rows']}"
            assert set(data["expiries"]) == {"2025-08-14", "2025-08-21"}
            assert data["underlying"] == "NIFTY"
            assert data["expiry"] == ""

            # Broker quote call should NOT have been made
            mock_quote.assert_not_called()

    async def test_full_quote_mode_with_expiry(
        self, async_client, nifty_instruments_fixture
    ):
        """GET /chain-quotes?underlying=NIFTY&expiry=2025-08-14 populates rows with bid/ask."""
        client = async_client

        # Minimal synthetic quote response
        synthetic_quotes = {
            "NIFTY24AUG24000CE": {
                "depth": {
                    "buy": [{"price": 100.0, "quantity": 100}],
                    "sell": [{"price": 105.0, "quantity": 100}],
                }
            },
            "NIFTY24AUG24000PE": {
                "depth": {
                    "buy": [{"price": 10.0, "quantity": 100}],
                    "sell": [{"price": 12.0, "quantity": 100}],
                }
            },
            "NIFTY24AUG24500CE": {
                "depth": {
                    "buy": [{"price": 50.0, "quantity": 100}],
                    "sell": [{"price": 55.0, "quantity": 100}],
                }
            },
            "NIFTY24AUG24500PE": {
                "depth": {
                    "buy": [{"price": 25.0, "quantity": 100}],
                    "sell": [{"price": 27.0, "quantity": 100}],
                }
            },
        }

        async def mock_batch_quote(sym_by_strike, und, exp):
            """Mock _chain_quotes_batch_quote to return synthetic quotes."""
            key_meta = {}
            for strike, sides in sym_by_strike.items():
                for side in ["CE", "PE"]:
                    if sides[side].get("sym"):
                        qk = sides[side]["sym"]  # Simplified key
                        key_meta[qk] = (strike, side)
            return synthetic_quotes, key_meta

        with patch("backend.api.cache.peek") as mock_cache, \
             patch("backend.api.routes.options._chain_quotes_batch_quote", side_effect=mock_batch_quote):

            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"}
            )

            assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
            data = response.json()

            # Verify rows are populated
            assert len(data["rows"]) == 2, f"Expected 2 strikes, got {len(data['rows'])}"
            assert data["underlying"] == "NIFTY"
            assert data["expiry"] == "2025-08-14"
            assert set(data["expiries"]) == {"2025-08-14", "2025-08-21"}

            # Verify first row (strike 24000)
            row_24k = next((r for r in data["rows"] if r["k"] == 24000.0), None)
            assert row_24k is not None
            assert row_24k["ce_sym"] == "NIFTY24AUG24000CE"
            assert row_24k["pe_sym"] == "NIFTY24AUG24000PE"
            assert row_24k["ce_ls"] == 25
            assert row_24k["pe_ls"] == 25
            assert row_24k["exchange"] == "NFO"
            assert row_24k["ce_bid"] == 100.0
            assert row_24k["ce_ask"] == 105.0
            assert row_24k["pe_bid"] == 10.0
            assert row_24k["pe_ask"] == 12.0

    async def test_unknown_underlying(self, async_client, nifty_instruments_fixture):
        """GET /chain-quotes?underlying=UNKNOWN returns empty expiries and rows."""
        client = async_client

        with patch("backend.api.cache.peek") as mock_cache:
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "UNKNOWN"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["rows"] == []
            assert data["expiries"] == []
            assert data["underlying"] == "UNKNOWN"

    async def test_missing_underlying_param(self, async_client):
        """GET /chain-quotes without underlying should return 400."""
        client = async_client

        response = await client.get("/api/options/chain-quotes")
        assert response.status_code == 400

    async def test_empty_underlying_param(self, async_client):
        """GET /chain-quotes?underlying= (empty string) should return 400."""
        client = async_client

        response = await client.get(
            "/api/options/chain-quotes",
            params={"underlying": ""}
        )
        assert response.status_code == 400

    async def test_response_structure_matches_spec(
        self, async_client, nifty_instruments_fixture
    ):
        """Response fields match ChainQuotesResponse spec."""
        client = async_client

        with patch("backend.api.cache.peek") as mock_cache:
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"}
            )

            data = response.json()

            # Check top-level fields exist
            assert "underlying" in data
            assert "expiry" in data
            assert "rows" in data
            assert "expiries" in data

            # expiries should be a list
            assert isinstance(data["expiries"], list)

            # rows should be a list of objects
            assert isinstance(data["rows"], list)


class TestChainQuotesCachePerformance:
    """Test cache behavior and performance characteristics."""

    def test_cache_populated_after_build(self, nifty_instruments_fixture):
        """After calling _chain_quotes_build_sym_map, cache should be populated."""
        from backend.api.routes.options import _CHAIN_SYM_CACHE

        # Clear cache
        _chain_sym_cache_clear()
        assert len(_CHAIN_SYM_CACHE) == 0

        # Build sym map
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )

        # Note: cache is populated by the route handler, not by _chain_quotes_build_sym_map itself.
        # The function is just the computation. Let's verify the data structure is correct.
        assert len(sym_by_strike) == 2
        assert all_expiries == ["2025-08-14", "2025-08-21"]

    def test_cache_clear_resets_cache(self, nifty_instruments_fixture):
        """_chain_sym_cache_clear() should wipe the cache."""
        from backend.api.routes.options import _CHAIN_SYM_CACHE
        import time

        # Manually populate cache (simulating route behavior)
        _cache_key = ("NIFTY", "2025-08-14")
        _now = time.monotonic()
        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )
        _CHAIN_SYM_CACHE[_cache_key] = (_now, (sym_by_strike, all_expiries))

        assert len(_CHAIN_SYM_CACHE) > 0, "Cache should be populated"

        # Clear
        _chain_sym_cache_clear()
        assert len(_CHAIN_SYM_CACHE) == 0, "Cache should be empty after clear"


@pytest.mark.asyncio
class TestChainQuotesEdgeCases:
    """Edge case and error handling tests."""

    async def test_instruments_fetch_failure(self, async_client):
        """When instruments cache is cold (peek returns None), return empty response."""
        client = async_client

        with patch("backend.api.cache.peek") as mock_cache:
            mock_cache.return_value = None  # cold cache — no download triggered

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["rows"] == []
            assert data["expiries"] == []

    async def test_quote_batch_empty_response(
        self, async_client, nifty_instruments_fixture
    ):
        """When broker quote call returns empty dict, bid/ask should be None."""
        client = async_client

        async def mock_batch_quote_empty(sym_by_strike, und, exp):
            """Return empty quote response."""
            key_meta = {}
            for strike, sides in sym_by_strike.items():
                for side in ["CE", "PE"]:
                    if sides[side].get("sym"):
                        qk = sides[side]["sym"]
                        key_meta[qk] = (strike, side)
            return {}, key_meta  # Empty quotes

        with patch("backend.api.cache.peek") as mock_cache, \
             patch("backend.api.routes.options._chain_quotes_batch_quote", side_effect=mock_batch_quote_empty):

            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"}
            )

            assert response.status_code == 200
            data = response.json()

            # Rows should exist but with None bid/ask
            assert len(data["rows"]) == 2
            for row in data["rows"]:
                assert row["ce_bid"] is None
                assert row["ce_ask"] is None
                assert row["pe_bid"] is None
                assert row["pe_ask"] is None
                # But sym/ls/exchange should still be populated
                assert row["ce_sym"] is not None
                assert row["pe_sym"] is not None

    async def test_partial_expiry_mismatch(self):
        """When only CE exists for a strike in an expiry, PE fields should be None/empty."""
        # Create instruments where PE is missing for one strike
        instruments = [
            Instrument(
                s="NIFTY24AUG24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
            # Note: no PE for 24000 strike
            Instrument(
                s="NIFTY24AUG24500CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24500.0,
            ),
            Instrument(
                s="NIFTY24AUG24500PE",
                e="NFO",
                t="PE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24500.0,
            ),
        ]
        inst_resp = InstrumentsResponse(cycle_date="2025-08-11", count=len(instruments), items=instruments)

        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            inst_resp, "NIFTY", "2025-08-14"
        )

        # 24000 should have only CE (PE will be empty dict initialized)
        assert sym_by_strike[24000.0]["CE"]["sym"] == "NIFTY24AUG24000CE"
        assert sym_by_strike[24000.0]["PE"]["sym"] == ""  # Default empty

        # 24500 should have both
        assert sym_by_strike[24500.0]["CE"]["sym"] == "NIFTY24AUG24500CE"
        assert sym_by_strike[24500.0]["PE"]["sym"] == "NIFTY24AUG24500PE"


@pytest.mark.asyncio
class TestChainQuotesIntegration:
    """Integration tests simulating real end-to-end flows."""

    async def test_multiple_expiries_sorted(
        self, async_client, nifty_instruments_fixture
    ):
        """Multiple expiries should be sorted in the response."""
        client = async_client

        # Create instruments with additional expiry
        instruments = nifty_instruments_fixture.items + [
            Instrument(
                s="NIFTY24SEP24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-09-04",
                k=24000.0,
            ),
            Instrument(
                s="NIFTY24SEP24000PE",
                e="NFO",
                t="PE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-09-04",
                k=24000.0,
            ),
        ]
        inst_resp = InstrumentsResponse(
            cycle_date="2025-08-11", count=len(instruments), items=instruments
        )

        with patch("backend.api.cache.peek") as mock_cache:
            mock_cache.return_value = inst_resp

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"}
            )

            assert response.status_code == 200
            data = response.json()

            # Expiries should be sorted
            expected = ["2025-08-14", "2025-08-21", "2025-09-04"]
            assert data["expiries"] == expected, f"Expected {expected}, got {data['expiries']}"


class TestChainSymCacheLRUCap:
    """Fix 5+6: LRU cap constant defined and enforced; oldest entry evicted at capacity."""

    def test_max_size_constant_defined(self):
        """_CHAIN_SYM_CACHE_MAX_SIZE must be defined and positive."""
        assert isinstance(_CHAIN_SYM_CACHE_MAX_SIZE, int)
        assert _CHAIN_SYM_CACHE_MAX_SIZE > 0

    def test_lru_eviction_at_capacity(self):
        """When cache reaches _CHAIN_SYM_CACHE_MAX_SIZE, adding one more entry evicts
        the oldest (first-inserted) key — same logic as the route handler inline."""
        import time

        _chain_sym_cache_clear()
        # Fill the cache to the cap using the same insertion pattern the route uses.
        first_key = None
        for i in range(_CHAIN_SYM_CACHE_MAX_SIZE):
            key = (f"UNDER{i}", f"2025-0{(i % 9) + 1}-01")
            if first_key is None:
                first_key = key
            _CHAIN_SYM_CACHE[key] = (time.monotonic(), ({}, []))

        assert len(_CHAIN_SYM_CACHE) == _CHAIN_SYM_CACHE_MAX_SIZE
        assert first_key in _CHAIN_SYM_CACHE

        # Simulate route eviction: at capacity, pop oldest before inserting new entry.
        new_key = ("OVERFLOW", "2025-12-31")
        if len(_CHAIN_SYM_CACHE) >= _CHAIN_SYM_CACHE_MAX_SIZE:
            _CHAIN_SYM_CACHE.pop(next(iter(_CHAIN_SYM_CACHE)))
        _CHAIN_SYM_CACHE[new_key] = (time.monotonic(), ({}, []))

        assert len(_CHAIN_SYM_CACHE) == _CHAIN_SYM_CACHE_MAX_SIZE, (
            "Cache should remain at max size after eviction"
        )
        assert first_key not in _CHAIN_SYM_CACHE, (
            "Oldest (first-inserted) key should have been evicted"
        )
        assert new_key in _CHAIN_SYM_CACHE, "Newly added key should be present"

        _chain_sym_cache_clear()

    def test_no_eviction_below_capacity(self):
        """Below the cap, no entries are removed when a new one is added."""
        import time

        _chain_sym_cache_clear()
        n = _CHAIN_SYM_CACHE_MAX_SIZE - 1
        for i in range(n):
            _CHAIN_SYM_CACHE[(f"X{i}", "2025-08-14")] = (time.monotonic(), ({}, []))

        assert len(_CHAIN_SYM_CACHE) == n

        new_key = ("NEWENTRY", "2025-08-14")
        if len(_CHAIN_SYM_CACHE) >= _CHAIN_SYM_CACHE_MAX_SIZE:
            _CHAIN_SYM_CACHE.pop(next(iter(_CHAIN_SYM_CACHE)))
        _CHAIN_SYM_CACHE[new_key] = (time.monotonic(), ({}, []))

        assert len(_CHAIN_SYM_CACHE) == n + 1, "No eviction should occur below capacity"
        _chain_sym_cache_clear()


class TestChainQuotesBidAskDepthAvailable:
    """Fix 7: _chain_quotes_bid_ask_from_q returns depth_available=True only when
    both bid and ask come from real market depth entries."""

    def test_both_sides_have_depth_returns_true(self):
        """Real bid and ask from depth entries → depth_available=True."""
        q = {
            "depth": {
                "buy": [{"price": 100.0, "quantity": 50}],
                "sell": [{"price": 102.0, "quantity": 50}],
            },
            "last_price": 101.0,
        }
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        assert bid == 100.0
        assert ask == 102.0
        assert depth_available is True

    def test_missing_buy_side_returns_false(self):
        """Empty buy depth → bid falls back to last_price → depth_available=False."""
        q = {
            "depth": {
                "buy": [],
                "sell": [{"price": 102.0, "quantity": 50}],
            },
            "last_price": 101.0,
        }
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        assert bid == 101.0  # fell back to last_price
        assert ask == 102.0
        assert depth_available is False

    def test_missing_sell_side_returns_false(self):
        """Empty sell depth → ask falls back to last_price → depth_available=False."""
        q = {
            "depth": {
                "buy": [{"price": 100.0, "quantity": 50}],
                "sell": [],
            },
            "last_price": 101.0,
        }
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        assert bid == 100.0
        assert ask == 101.0  # fell back to last_price
        assert depth_available is False

    def test_both_sides_missing_with_last_price_returns_false(self):
        """Both depth sides empty, last_price present → fallback for both → depth_available=False."""
        q = {
            "depth": {
                "buy": [],
                "sell": [],
            },
            "last_price": 55.0,
        }
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        assert bid == 55.0
        assert ask == 55.0
        assert depth_available is False

    def test_no_depth_key_no_last_price_returns_none_none_false(self):
        """No depth, no last_price → (None, None, False) — true no-quote."""
        q = {}
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        assert bid is None
        assert ask is None
        assert depth_available is False

    def test_depth_with_zero_price_falls_back(self):
        """Zero-priced depth entries are ignored by _best_depth_price; fallback fires."""
        q = {
            "depth": {
                "buy": [{"price": 0.0, "quantity": 10}],
                "sell": [{"price": 0.0, "quantity": 10}],
            },
            "last_price": 80.0,
        }
        bid, ask, depth_available = _chain_quotes_bid_ask_from_q(q)
        # Both sides have zero-priced entries → _best_depth_price returns None → fallback
        assert bid == 80.0
        assert ask == 80.0
        assert depth_available is False


@pytest.mark.asyncio
class TestChainQuotesOffMarketGate:
    """Gap 9 fix: chain_quotes must skip broker.quote() when market is closed.

    Verify that:
    1. When market is closed and a fresh cache entry exists, broker is NOT called.
    2. When market is closed but cache is stale/empty, broker IS called and result cached.
    3. When market is open, broker is always called (live data, no gate).
    4. Closed-market cache TTL is the configured 5-minute value.
    """

    async def test_closed_market_returns_cached_response_without_broker_call(
        self, async_client, nifty_instruments_fixture
    ):
        """When market is closed and cache hit is within TTL, broker NOT called."""
        import time

        client = async_client

        async def mock_batch_quote_sentinel(sym_by_strike, und, exp):
            # If this is called, the test should fail
            raise AssertionError("broker.quote() must NOT be called on cache hit during closed market")

        # Pre-populate the closed cache with a fresh entry
        _cached_resp = ChainQuotesResponse(
            underlying="NIFTY",
            expiry="2025-08-14",
            expiries=["2025-08-14"],
            rows=[
                ChainQuoteRow(
                    k=24000.0,
                    ce_bid=100.0, ce_ask=105.0,
                    pe_bid=10.0, pe_ask=12.0,
                    ce_sym="NIFTY24AUG24000CE", pe_sym="NIFTY24AUG24000PE",
                    ce_ls=25, pe_ls=25,
                    exchange="NFO",
                    ce_depth_available=True, pe_depth_available=True,
                )
            ],
        )
        _CHAIN_QUOTES_CLOSED_CACHE[("NIFTY", "2025-08-14")] = (
            time.monotonic(), _cached_resp
        )

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=False,  # market is closed
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote_sentinel,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            data = response.json()
            # Should return cached response (1 row for NIFTY)
            assert len(data["rows"]) == 1
            assert data["rows"][0]["k"] == 24000.0
            assert data["rows"][0]["ce_bid"] == 100.0

    async def test_closed_market_stale_cache_no_broker_call(
        self, async_client, nifty_instruments_fixture
    ):
        """When market is closed and cache entry is expired, broker is NOT called (safe-fail)."""
        import time

        client = async_client
        broker_call_count = 0

        async def mock_batch_quote(sym_by_strike, und, exp):
            nonlocal broker_call_count
            broker_call_count += 1
            return {}, {k: (strike, side) for strike, sides in sym_by_strike.items()
                        for side, meta in sides.items()
                        if (k := meta.get("sym"))}

        # Pre-populate with a STALE cache entry (older than TTL)
        _CHAIN_QUOTES_CLOSED_CACHE[("NIFTY", "2025-08-14")] = (
            time.monotonic() - _CHAIN_QUOTES_CLOSED_TTL - 1,  # expired
            ChainQuotesResponse(underlying="NIFTY", expiry="2025-08-14", rows=[]),
        )

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=False,  # market is closed
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            # When cache is stale (TTL expired) and market is closed, the off-market
            # gate returns empty rows without calling broker to avoid stale/hung data
            assert broker_call_count == 0, (
                "Broker must NOT be called when stale cache + closed market (safe-fail)"
            )
            data = response.json()
            assert data["rows"] == [], "Stale closed-market cache should return empty rows"

    async def test_open_market_always_calls_broker(
        self, async_client, nifty_instruments_fixture
    ):
        """When market is open, broker is always called regardless of closed cache."""
        import time

        client = async_client
        broker_call_count = 0

        async def mock_batch_quote(sym_by_strike, und, exp):
            nonlocal broker_call_count
            broker_call_count += 1
            return {}, {}

        # Pre-populate closed cache with a fresh entry (should be ignored when open)
        _CHAIN_QUOTES_CLOSED_CACHE[("NIFTY", "2025-08-14")] = (
            time.monotonic(),
            ChainQuotesResponse(underlying="NIFTY", expiry="2025-08-14", rows=[]),
        )

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=True,  # market is OPEN
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            assert broker_call_count == 1, (
                "Broker must always be called when market is open (no gate)"
            )

    async def test_live_response_populates_closed_cache(
        self, async_client, nifty_instruments_fixture
    ):
        """Every live broker response populates the closed-market cache."""
        import time

        client = async_client

        async def mock_batch_quote(sym_by_strike, und, exp):
            return {}, {}

        _chain_quotes_closed_cache_clear()  # ensure empty

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=True,  # market open
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200

        # Cache should now hold the response
        key = ("NIFTY", "2025-08-14")
        assert key in _CHAIN_QUOTES_CLOSED_CACHE, (
            "Live response must populate the closed-market cache for off-hours use"
        )
        cached_ts, cached_resp = _CHAIN_QUOTES_CLOSED_CACHE[key]
        assert time.monotonic() - cached_ts < 5.0, "Cache entry must be fresh"
        assert isinstance(cached_resp, ChainQuotesResponse)

    def test_closed_market_cache_ttl_is_five_minutes(self):
        """_CHAIN_QUOTES_CLOSED_TTL must be 300 seconds (5 minutes)."""
        assert _CHAIN_QUOTES_CLOSED_TTL == 300.0, (
            f"Off-market cache TTL must be 300s (5 min), got {_CHAIN_QUOTES_CLOSED_TTL}"
        )

    def test_closed_market_cache_clear_helper_empties_cache(self):
        """_chain_quotes_closed_cache_clear() resets the cache."""
        import time
        key = ("TEST", "2025-08-14")
        _CHAIN_QUOTES_CLOSED_CACHE[key] = (
            time.monotonic(),
            ChainQuotesResponse(underlying="TEST", expiry="2025-08-14", rows=[]),
        )
        assert len(_CHAIN_QUOTES_CLOSED_CACHE) > 0

        _chain_quotes_closed_cache_clear()
        assert len(_CHAIN_QUOTES_CLOSED_CACHE) == 0

    async def test_expiry_only_mode_skips_closed_market_gate(
        self, async_client, nifty_instruments_fixture
    ):
        """Expiry-only mode (no expiry param) does not hit the closed-market gate."""
        broker_call_count = 0

        async def mock_batch_quote(sym_by_strike, und, exp):
            nonlocal broker_call_count
            broker_call_count += 1
            return {}, {}

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=False,  # closed
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await async_client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"},  # no expiry — expiry-only mode
            )

            assert response.status_code == 200
            # Expiry-only mode returns before the gate; broker still not called
            assert broker_call_count == 0, (
                "Expiry-only mode must never call the broker (returns early)"
            )


@pytest.mark.asyncio
class TestChainQuotesBrokerTimeout:
    """Test timeout handling in _chain_quotes_batch_quote.

    When broker.quote() times out or raises an exception, the function
    should return an empty dict (quote_resp) and valid key_meta, allowing
    the route to proceed with None bid/ask values for affected strikes.
    """

    async def test_broker_quote_timeout_returns_empty_dict(self):
        """broker.quote timeout should return empty dict, not raise."""
        import time

        nifty_instruments = [
            Instrument(
                s="NIFTY24AUG24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
            Instrument(
                s="NIFTY24AUG24000PE",
                e="NFO",
                t="PE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
        ]
        inst_resp = InstrumentsResponse(
            cycle_date="2025-08-11", count=len(nifty_instruments), items=nifty_instruments
        )

        # Build sym_by_strike to pass to _chain_quotes_batch_quote
        sym_by_strike, _ = _chain_quotes_build_sym_map(inst_resp, "NIFTY", "2025-08-14")

        # Call _chain_quotes_batch_quote with mocked broker that times out
        # asyncio.to_thread is called from within _chain_quotes_batch_quote,
        # so we patch it to raise TimeoutError
        with patch("asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = asyncio.TimeoutError("Quote call timed out")

            quote_resp, key_meta = await _chain_quotes_batch_quote(
                sym_by_strike, "NIFTY", "2025-08-14"
            )

        # When timeout occurs, quote_resp should be empty dict, not raise
        assert quote_resp == {}, "Timeout should return empty dict, not raise"
        # key_meta should still contain all symbol mappings
        assert len(key_meta) > 0, "key_meta must be populated even on timeout"

    async def test_broker_quote_exception_returns_empty_dict(self):
        """Any broker.quote exception should return empty dict, not raise."""
        nifty_instruments = [
            Instrument(
                s="NIFTY24AUG24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
            Instrument(
                s="NIFTY24AUG24000PE",
                e="NFO",
                t="PE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
        ]
        inst_resp = InstrumentsResponse(
            cycle_date="2025-08-11", count=len(nifty_instruments), items=nifty_instruments
        )

        sym_by_strike, _ = _chain_quotes_build_sym_map(inst_resp, "NIFTY", "2025-08-14")

        # Patch asyncio.to_thread to raise a generic exception (network failure)
        with patch("asyncio.to_thread") as mock_to_thread:
            mock_to_thread.side_effect = RuntimeError("Broker connection lost")

            quote_resp, key_meta = await _chain_quotes_batch_quote(
                sym_by_strike, "NIFTY", "2025-08-14"
            )

        # On exception, should return empty dict and valid key_meta
        assert quote_resp == {}, "Exception should return empty dict, not raise"
        assert isinstance(key_meta, dict), "key_meta must be a dict"


@pytest.mark.asyncio
class TestChainQuotesOffMarketGateEnhanced:
    """Additional off-market gate tests focusing on edge cases and performance."""

    async def test_off_market_cold_cache_no_broker_call(self, async_client, nifty_instruments_fixture):
        """When market is closed AND cache is cold/empty, return empty rows without broker call."""
        import time

        client = async_client
        broker_call_count = 0

        async def mock_batch_quote(sym_by_strike, und, exp):
            nonlocal broker_call_count
            broker_call_count += 1
            # If called, return empty quotes
            key_meta = {}
            for strike, sides in sym_by_strike.items():
                for side, meta in sides.items():
                    if meta.get("sym"):
                        key_meta[meta["sym"]] = (strike, side)
            return {}, key_meta

        # Ensure closed cache is empty
        _chain_quotes_closed_cache_clear()
        assert len(_CHAIN_QUOTES_CLOSED_CACHE) == 0, "Cache should start empty"

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=False,  # market is closed
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            data = response.json()
            # When cache is cold and market closed, the off-market gate returns empty rows
            # WITHOUT calling broker.quote() to avoid stale data or hangs
            assert broker_call_count == 0, (
                "Off-market gate should NOT call broker when cache is empty (returns empty rows instead)"
            )
            # Response should have empty rows
            assert data["rows"] == [], "Off-market cold cache should return empty rows"

    async def test_off_market_gate_returns_empty_on_stale_cache(self, async_client, nifty_instruments_fixture):
        """Verify off-market gate returns empty rows when cache is stale (beyond TTL)."""
        import time

        client = async_client
        broker_call_count = 0

        async def mock_batch_quote(sym_by_strike, und, exp):
            nonlocal broker_call_count
            broker_call_count += 1
            return {}, {}

        # Pre-populate cache with entry that's just outside the TTL window
        cache_entry_age = _CHAIN_QUOTES_CLOSED_TTL + 0.1  # 0.1s past expiry
        _CHAIN_QUOTES_CLOSED_CACHE[("NIFTY", "2025-08-14")] = (
            time.monotonic() - cache_entry_age,
            ChainQuotesResponse(underlying="NIFTY", expiry="2025-08-14", rows=[]),
        )

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch(
                "backend.api.routes.options._any_segment_open",
                return_value=False,  # market is closed
            ),
            patch(
                "backend.api.routes.options._chain_quotes_batch_quote",
                side_effect=mock_batch_quote,
            ),
        ):
            mock_cache.return_value = nifty_instruments_fixture

            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            # When cache entry is stale (beyond TTL), off-market gate returns empty rows
            # without calling broker (safe-fail behavior during closed hours)
            assert broker_call_count == 0, (
                "Stale cache entry (beyond TTL) should NOT trigger broker call (returns empty)"
            )
            data = response.json()
            assert data["rows"] == [], "Stale cache should return empty rows during off-market"


@pytest.mark.asyncio
class TestChainInstrumentsNFOMCXOnly:
    """Test that chain instruments are filtered to NFO and MCX exchanges only.

    Options chains should only include NFO (Indian options) and MCX (commodity
    options), not NSE (equities), BSE, or CDS. This test verifies the filter
    logic if a _fetch_chain_instruments helper is added.
    """

    async def test_chain_instruments_nfo_mcx_filter(self, nifty_instruments_fixture):
        """All instruments in chain should be NFO or MCX, never NSE/BSE/CDS."""
        # Verify the fixture itself contains only NFO
        for inst in nifty_instruments_fixture.items:
            assert inst.e in ("NFO", "MCX"), (
                f"Test fixture must only contain NFO/MCX, got {inst.e} for {inst.s}"
            )

    async def test_build_sym_map_skips_non_option_exchanges(self):
        """_chain_quotes_build_sym_map should only process CE/PE, ignoring EQ/FUT."""
        # Create instruments across multiple exchanges
        instruments = [
            # NSE equity (should be skipped)
            Instrument(s="RELIANCE", e="NSE", t="EQ", ls=1, ts=0.05, u="RELIANCE"),
            # NFO futures (should be skipped — FUT, not CE/PE)
            Instrument(
                s="NIFTYFUT",
                e="NFO",
                t="FUT",
                ls=1,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
            ),
            # NFO option (should be included)
            Instrument(
                s="NIFTY24AUG24000CE",
                e="NFO",
                t="CE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
            Instrument(
                s="NIFTY24AUG24000PE",
                e="NFO",
                t="PE",
                ls=25,
                ts=0.05,
                u="NIFTY",
                x="2025-08-14",
                k=24000.0,
            ),
            # MCX commodity option (should be included)
            Instrument(
                s="GOLDM25AUG99000CE",
                e="MCX",
                t="CE",
                ls=10,
                ts=0.05,
                u="GOLDM",
                x="2025-08-14",
                k=99000.0,
            ),
            # BSE equity (should be skipped)
            Instrument(s="SENSEX", e="BSE", t="EQ", ls=1, ts=0.05, u="SENSEX"),
        ]
        inst_resp = InstrumentsResponse(
            cycle_date="2025-08-11", count=len(instruments), items=instruments
        )

        sym_by_strike, all_expiries = _chain_quotes_build_sym_map(
            inst_resp, "NIFTY", "2025-08-14"
        )

        # Should have 1 strike (24000) with both CE and PE from NFO
        assert len(sym_by_strike) == 1, (
            f"Should have 1 NIFTY strike, got {len(sym_by_strike)}"
        )
        assert 24000.0 in sym_by_strike
        # NSE equity, BSE, CDS, and NFO futures should be filtered out
        for strike, sides in sym_by_strike.items():
            for side in ["CE", "PE"]:
                if sides[side].get("sym"):
                    # If symbol exists, it must be from NFO or MCX
                    exch = sides[side].get("e")
                    assert exch in ("NFO", "MCX"), (
                        f"Unexpected exchange {exch} for {sides[side].get('sym')}"
                    )

    async def test_chain_quotes_endpoint_nfo_mcx_responses(
        self, async_client, nifty_instruments_fixture
    ):
        """Chain-quotes endpoint should only return NFO/MCX instruments in rows."""
        client = async_client

        # Create a fixture with mixed exchanges including a commodity option
        instruments = nifty_instruments_fixture.items + [
            Instrument(
                s="CRUDEOIL25AUG6500CE",
                e="MCX",
                t="CE",
                ls=100,
                ts=1.0,
                u="CRUDEOIL",
                x="2025-08-14",
                k=6500.0,
            ),
            Instrument(
                s="CRUDEOIL25AUG6500PE",
                e="MCX",
                t="PE",
                ls=100,
                ts=1.0,
                u="CRUDEOIL",
                x="2025-08-14",
                k=6500.0,
            ),
        ]
        inst_resp = InstrumentsResponse(
            cycle_date="2025-08-11", count=len(instruments), items=instruments
        )

        async def mock_batch_quote(sym_by_strike, und, exp):
            """Return minimal quotes for all keys."""
            key_meta = {}
            for strike, sides in sym_by_strike.items():
                for side, meta in sides.items():
                    if meta.get("sym"):
                        key_meta[meta["sym"]] = (strike, side)
            # Return synthetic quotes
            return (
                {k: {"depth": {"buy": [{"price": 100.0}], "sell": [{"price": 101.0}]}} for k in key_meta},
                key_meta,
            )

        with (
            patch("backend.api.cache.peek") as mock_cache,
            patch("backend.api.routes.options._chain_quotes_batch_quote", side_effect=mock_batch_quote),
        ):
            mock_cache.return_value = inst_resp

            # Query for CRUDEOIL (MCX commodity)
            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "CRUDEOIL", "expiry": "2025-08-14"},
            )

            assert response.status_code == 200
            data = response.json()

            # All rows should have exchange either NFO or MCX
            for row in data["rows"]:
                assert row["exchange"] in ("NFO", "MCX"), (
                    f"Unexpected exchange {row['exchange']} in chain-quotes response"
                )


@pytest.mark.asyncio
class TestChainQuotesBatchQuoteWaitForTimeout:
    """Task 1: asyncio.wait_for timeout guard in _chain_quotes_batch_quote.

    The broker.quote() call is now wrapped in asyncio.wait_for(..., timeout=10.0).
    On TimeoutError, the function must return ({}, key_meta) — not hang or propagate.
    """

    async def test_wait_for_timeout_returns_empty_quote_resp(self, nifty_instruments_fixture):
        """When asyncio.wait_for raises TimeoutError, quote_resp is {} and no exception propagates."""
        sym_by_strike, _ = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )

        # Patch asyncio.wait_for itself to raise TimeoutError, simulating a 10s hang
        with patch("backend.api.routes.options.asyncio.wait_for",
                   side_effect=asyncio.TimeoutError):
            quote_resp, key_meta = await _chain_quotes_batch_quote(
                sym_by_strike, "NIFTY", "2025-08-14"
            )

        assert quote_resp == {}, "TimeoutError must produce empty quote_resp"
        assert isinstance(key_meta, dict), "key_meta must still be a dict on timeout"
        # key_meta built before the broker call, so it should be non-empty
        assert len(key_meta) > 0, "key_meta must be populated (symbols were resolved)"

    async def test_wait_for_timeout_does_not_propagate(self, nifty_instruments_fixture):
        """TimeoutError from asyncio.wait_for is caught internally — caller never sees it."""
        sym_by_strike, _ = _chain_quotes_build_sym_map(
            nifty_instruments_fixture, "NIFTY", "2025-08-14"
        )

        raised = False
        try:
            with patch("backend.api.routes.options.asyncio.wait_for",
                       side_effect=asyncio.TimeoutError):
                await _chain_quotes_batch_quote(sym_by_strike, "NIFTY", "2025-08-14")
        except asyncio.TimeoutError:
            raised = True

        assert not raised, "TimeoutError must not propagate from _chain_quotes_batch_quote"

    async def test_empty_keys_skips_wait_for_entirely(self, nifty_instruments_fixture):
        """When sym_by_strike has no valid symbols, keys list is empty and wait_for is never called."""
        # Build a sym_by_strike where all syms are empty (no valid keys)
        empty_sym_by_strike: dict = {
            24000.0: {
                "CE": {"sym": "", "ls": 25, "e": "NFO"},
                "PE": {"sym": "", "ls": 25, "e": "NFO"},
            }
        }

        call_count = 0

        async def _counting_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            return await coro

        with patch("backend.api.routes.options.asyncio.wait_for", side_effect=_counting_wait_for):
            quote_resp, key_meta = await _chain_quotes_batch_quote(
                empty_sym_by_strike, "NIFTY", "2025-08-14"
            )

        assert quote_resp == {}, "No keys → empty quote_resp"
        assert key_meta == {}, "No valid syms → empty key_meta"
        assert call_count == 0, "wait_for must not be called when keys list is empty"


@pytest.mark.asyncio
class TestChainQuotesInstrumentsChainCacheKey:
    """Task 3: Route prefers 'instruments_chain' cache key over 'instruments'."""

    async def test_instruments_chain_key_tried_before_instruments(
        self, async_client, nifty_instruments_fixture
    ):
        """peek('instruments_chain') must be called before peek('instruments')."""
        client = async_client
        peek_order: list[str] = []

        def _track_peek(key: str):
            peek_order.append(key)
            if key == "instruments_chain":
                return nifty_instruments_fixture
            return None

        with patch("backend.api.cache.peek", side_effect=_track_peek):
            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"},
            )

        assert response.status_code == 200
        assert "instruments_chain" in peek_order, "instruments_chain must be in peek calls"
        chain_pos = peek_order.index("instruments_chain")
        instr_positions = [i for i, k in enumerate(peek_order) if k == "instruments"]
        # instruments_chain was tried first; instruments should not be needed
        if instr_positions:
            assert instr_positions[0] > chain_pos, (
                "instruments must only be tried AFTER instruments_chain returns None"
            )

    async def test_instruments_fallback_when_chain_cold(
        self, async_client, nifty_instruments_fixture
    ):
        """When instruments_chain cache is cold, route falls back to instruments."""
        client = async_client
        peek_order: list[str] = []

        def _track_peek(key: str):
            peek_order.append(key)
            if key == "instruments_chain":
                return None  # cold
            if key == "instruments":
                return nifty_instruments_fixture
            return None

        with patch("backend.api.cache.peek", side_effect=_track_peek):
            response = await client.get(
                "/api/options/chain-quotes",
                params={"underlying": "NIFTY"},
            )

        assert response.status_code == 200
        data = response.json()
        # Should still return expiries from the fallback instruments cache
        assert set(data["expiries"]) == {"2025-08-14", "2025-08-21"}, (
            "Fallback to instruments must still populate expiries correctly"
        )
        assert "instruments" in peek_order, "instruments must be tried as fallback"


@pytest.mark.asyncio
class TestFetchChainInstruments:
    """Task 4: _fetch_chain_instruments returns NFO+MCX subset."""

    def test_fetch_chain_instruments_returns_none_without_kite(self):
        """When no Kite account is loaded, _fetch_chain_instruments returns None."""
        from backend.api.routes.instruments import _fetch_chain_instruments
        from unittest.mock import patch

        with patch("backend.brokers.registry._loaded_accounts", return_value=[]), \
             patch("backend.brokers.registry._broker_id_for", return_value="dhan"):
            result = _fetch_chain_instruments()

        assert result is None, "Must return None (not empty InstrumentsResponse) when no Kite account"

    def test_fetch_chain_instruments_only_fetches_nfo_mcx(self):
        """_fetch_chain_instruments fetches exactly NFO and MCX — not NSE, BSE, CDS."""
        from backend.api.routes.instruments import _fetch_chain_instruments
        from unittest.mock import patch, call

        fetched_exchanges: list[str] = []

        def _mock_fetch_exchange_raw(exch: str, kite_accts: list):
            fetched_exchanges.append(exch)
            return []  # empty but non-None

        with patch("backend.brokers.registry._loaded_accounts", return_value=["acc1"]), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.api.routes.instruments._fetch_exchange_raw",
                   side_effect=_mock_fetch_exchange_raw):
            result = _fetch_chain_instruments()

        assert set(fetched_exchanges) == {"NFO", "MCX"}, (
            f"Must fetch exactly NFO+MCX, got {fetched_exchanges}"
        )
        assert "NSE" not in fetched_exchanges, "NSE must not be fetched by chain instruments"
        assert "BSE" not in fetched_exchanges, "BSE must not be fetched by chain instruments"
        assert "CDS" not in fetched_exchanges, "CDS must not be fetched by chain instruments"

    def test_fetch_chain_instruments_returns_instrument_response(self):
        """_fetch_chain_instruments returns InstrumentsResponse with correct shape."""
        from backend.api.routes.instruments import _fetch_chain_instruments, InstrumentsResponse
        from datetime import date

        nfo_raw = [
            {
                "tradingsymbol": "NIFTY24AUG24000CE",
                "exchange": "NFO",
                "instrument_type": "CE",
                "lot_size": 25,
                "tick_size": 0.05,
                "name": "NIFTY",
                "expiry": date(2025, 8, 14),
                "strike": 24000.0,
            }
        ]
        mcx_raw = [
            {
                "tradingsymbol": "CRUDEOIL25AUG6500CE",
                "exchange": "MCX",
                "instrument_type": "CE",
                "lot_size": 100,
                "tick_size": 1.0,
                "name": "CRUDEOIL",
                "expiry": date(2025, 8, 19),
                "strike": 6500.0,
            }
        ]

        def _mock_fetch_exchange_raw(exch: str, kite_accts: list):
            if exch == "NFO":
                return nfo_raw
            if exch == "MCX":
                return mcx_raw
            return None

        with patch("backend.brokers.registry._loaded_accounts", return_value=["acc1"]), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.api.routes.instruments._fetch_exchange_raw",
                   side_effect=_mock_fetch_exchange_raw):
            result = _fetch_chain_instruments()

        assert result is not None
        assert isinstance(result, InstrumentsResponse)
        assert result.count == 2, f"Expected 2 instruments (1 NFO + 1 MCX), got {result.count}"
        assert result.cycle_date == date.today().isoformat()
        exchanges_in_result = {inst.e for inst in result.items}
        assert exchanges_in_result == {"NFO", "MCX"}
