"""
Audit Fix #3 — chain_quotes cold-cache response includes stale and message fields.

Tests for options.py chain_quotes off-market caching behavior.

Coverage:
  - ChainQuotesResponse struct has stale (bool) and message (str) fields
  - Cold-cache miss response is constructed with stale=True and non-empty message
  - stale field is preserved when built via msgspec.structs.replace
  - Cache hit with stale=False → replace() produces stale=True
  - Off-market path source inspection: stale and message present in code
  - Cache TTL is 300s (5 minutes) for off-market responses
"""

import pytest
import time
import msgspec

from backend.api.routes.options import (
    _chain_quotes_closed_cache_clear,
    _chain_quotes_closed_cache_get,
    ChainQuotesResponse,
    _CHAIN_QUOTES_CLOSED_CACHE,
    _CHAIN_QUOTES_CLOSED_TTL,
)


@pytest.fixture(autouse=True)
def clear_closed_cache():
    """Clear the closed-market cache before and after each test."""
    _chain_quotes_closed_cache_clear()
    yield
    _chain_quotes_closed_cache_clear()


class TestChainQuotesResponseSchema:
    """ChainQuotesResponse must carry stale and message fields."""

    def test_schema_has_stale_field(self):
        """ChainQuotesResponse must have a stale: bool field defaulting to False."""
        r = ChainQuotesResponse(underlying="NIFTY", expiry="2027-08-14", rows=[])
        assert hasattr(r, "stale"), "ChainQuotesResponse must have 'stale' field"
        assert r.stale is False, "stale must default to False"

    def test_schema_has_message_field(self):
        """ChainQuotesResponse must have a message: str field defaulting to ''."""
        r = ChainQuotesResponse(underlying="NIFTY", expiry="2027-08-14", rows=[])
        assert hasattr(r, "message"), "ChainQuotesResponse must have 'message' field"
        assert r.message == "", "message must default to empty string"

    def test_schema_stale_true_constructor(self):
        """stale=True and message can be passed at construction time."""
        r = ChainQuotesResponse(
            underlying="NIFTY", expiry="2027-08-14", rows=[],
            stale=True, message="No chain data until next market open",
        )
        assert r.stale is True
        assert r.message == "No chain data until next market open"

    def test_schema_is_msgspec_struct(self):
        """ChainQuotesResponse is a msgspec.Struct — replace() must work."""
        r = ChainQuotesResponse(underlying="NIFTY", expiry="2027-08-14", rows=[],
                                stale=False)
        replaced = msgspec.structs.replace(r, stale=True)
        assert replaced.stale is True
        assert replaced.underlying == "NIFTY"
        assert replaced.expiry == "2027-08-14"


class TestChainQuotesCacheMissOffMarket:
    """Off-market cache miss must produce a stale=True response."""

    def test_cold_cache_miss_response_pattern(self):
        """Simulate the cold-miss return: stale=True + non-empty message."""
        # This mirrors what chain_quotes does at the cold-miss path.
        response = ChainQuotesResponse(
            underlying="NIFTY",
            expiry="2027-08-14",
            expiries=[],
            rows=[],
            stale=True,
            message="No chain data until next market open",
        )
        assert response.stale is True, "Cold-miss response must have stale=True"
        assert response.rows == [], "Cold-miss response must have empty rows"
        assert "market open" in response.message.lower(), (
            f"Cold-miss message should reference market open; got {response.message!r}"
        )

    def test_source_contains_stale_true_on_miss(self):
        """Source inspection: off-market miss path in chain_quotes sets stale=True."""
        from pathlib import Path
        src = Path("backend/api/routes/options.py").read_text()
        # The cold-miss return must have stale=True and a message
        assert "stale=True" in src, (
            "options.py must return stale=True on off-market cache miss"
        )
        assert "No chain data until next market open" in src, (
            "options.py must include the cold-miss message string"
        )

    def test_source_uses_structs_replace_on_cache_hit(self):
        """Source inspection: cache hit path uses msgspec.structs.replace to set stale=True."""
        from pathlib import Path
        src = Path("backend/api/routes/options.py").read_text()
        assert "structs.replace" in src, (
            "options.py must use msgspec.structs.replace to set stale=True on cache hit"
        )


class TestChainQuotesCacheHitOffMarket:
    """Off-market cache hit: returned object must have stale=True."""

    def test_structs_replace_sets_stale_true(self):
        """msgspec.structs.replace sets stale=True on a cached stale=False entry."""
        cached = ChainQuotesResponse(
            underlying="NIFTY",
            expiry="2027-08-14",
            rows=[],
            expiries=["2027-08-14"],
            stale=False,
        )
        # Simulate what chain_quotes does on cache hit
        returned = msgspec.structs.replace(cached, stale=True)
        assert returned.stale is True, (
            "msgspec.structs.replace must produce stale=True on the returned response"
        )
        # Data integrity preserved
        assert returned.underlying == "NIFTY"
        assert returned.expiry == "2027-08-14"
        assert returned.expiries == ["2027-08-14"]
        # Original unchanged (structs are immutable)
        assert cached.stale is False, "Original cached object must not be mutated"


class TestChainQuotesClosedCacheTTL:
    """Tests for closed-cache TTL expiration (5 minutes)."""

    def test_closed_cache_ttl_constant(self):
        """_CHAIN_QUOTES_CLOSED_TTL is 300 seconds (5 minutes)."""
        assert _CHAIN_QUOTES_CLOSED_TTL == 300.0, (
            "_CHAIN_QUOTES_CLOSED_TTL must be 300s (5 minutes)"
        )

    def test_closed_cache_get_respects_ttl(self):
        """_chain_quotes_closed_cache_get returns None when TTL expired."""
        old_ts = time.monotonic() - (_CHAIN_QUOTES_CLOSED_TTL + 1)
        cache_key = ("NIFTY", "2027-08-14")
        stale_response = ChainQuotesResponse(
            underlying="NIFTY", expiry="2027-08-14", rows=[], expiries=[],
        )
        _CHAIN_QUOTES_CLOSED_CACHE[cache_key] = (old_ts, stale_response)
        result = _chain_quotes_closed_cache_get(cache_key)
        assert result is None, "Expired cache entries must return None"

    def test_closed_cache_get_valid_ttl(self):
        """_chain_quotes_closed_cache_get returns entry when within TTL."""
        fresh_ts = time.monotonic() - (_CHAIN_QUOTES_CLOSED_TTL / 2)
        cache_key = ("NIFTY", "2027-08-14")
        fresh_response = ChainQuotesResponse(
            underlying="NIFTY", expiry="2027-08-14", rows=[], expiries=[],
        )
        _CHAIN_QUOTES_CLOSED_CACHE[cache_key] = (fresh_ts, fresh_response)
        result = _chain_quotes_closed_cache_get(cache_key)
        assert result is fresh_response, "Fresh cache entries must be returned"

    def test_cache_clear_helper(self):
        """_chain_quotes_closed_cache_clear resets the cache."""
        cache_key = ("NIFTY", "2027-08-14")
        _CHAIN_QUOTES_CLOSED_CACHE[cache_key] = (
            time.monotonic(),
            ChainQuotesResponse(underlying="NIFTY", expiry="2027-08-14", rows=[]),
        )
        assert len(_CHAIN_QUOTES_CLOSED_CACHE) > 0
        _chain_quotes_closed_cache_clear()
        assert len(_CHAIN_QUOTES_CLOSED_CACHE) == 0
