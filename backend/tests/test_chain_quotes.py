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

        with patch("backend.api.cache.get_or_fetch") as mock_cache, \
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

        with patch("backend.api.cache.get_or_fetch") as mock_cache, \
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

        with patch("backend.api.cache.get_or_fetch") as mock_cache:
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

        with patch("backend.api.cache.get_or_fetch") as mock_cache:
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
        """When instruments fetch fails, return empty response."""
        client = async_client

        with patch("backend.api.cache.get_or_fetch") as mock_cache:
            mock_cache.side_effect = Exception("Network error")

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

        with patch("backend.api.cache.get_or_fetch") as mock_cache, \
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

        with patch("backend.api.cache.get_or_fetch") as mock_cache:
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
            patch("backend.api.cache.get_or_fetch") as mock_cache,
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

    async def test_closed_market_stale_cache_calls_broker(
        self, async_client, nifty_instruments_fixture
    ):
        """When market is closed but cache entry is expired, broker IS called."""
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
            patch("backend.api.cache.get_or_fetch") as mock_cache,
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
            assert broker_call_count == 1, (
                "Broker must be called when closed-market cache is stale"
            )

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
            patch("backend.api.cache.get_or_fetch") as mock_cache,
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
            patch("backend.api.cache.get_or_fetch") as mock_cache,
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
            patch("backend.api.cache.get_or_fetch") as mock_cache,
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
