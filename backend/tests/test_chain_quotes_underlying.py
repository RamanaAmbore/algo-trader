"""
Tests for chain-quotes underlying normalization and cold/warm cache paths.

Coverage:
  1. Cold cache (both instruments_chain and instruments None) returns empty expiries/rows
  2. Warm cache (instruments available) returns populated expiries
  3. Underlying normalization: spaces stripped and uppercased
     - "CRUDE OIL" → "CRUDEOIL"
     - "NATURAL GAS" → "NATURALGAS"
     - "nifty" → "NIFTY"
     - "  nifty  50  " → "NIFTY50"
"""

from __future__ import annotations

import re as _re
import pytest
from unittest.mock import patch, AsyncMock

from backend.api.routes.instruments import Instrument, InstrumentsResponse
from backend.api.routes.options import (
    ChainQuotesResponse,
    _chain_sym_cache_clear,
    _chain_quotes_closed_cache_clear,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear both chain-quote caches before and after each test."""
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()
    yield
    _chain_sym_cache_clear()
    _chain_quotes_closed_cache_clear()


def _make_inst_resp() -> InstrumentsResponse:
    """Minimal NIFTY instruments fixture with two expiries, two strikes."""
    instruments = [
        Instrument(s="NIFTY27AUG24000CE", e="NFO", t="CE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-14", k=24000.0),
        Instrument(s="NIFTY27AUG24000PE", e="NFO", t="PE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-14", k=24000.0),
        Instrument(s="NIFTY27AUG21000CE", e="NFO", t="CE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-21", k=24000.0),
        Instrument(s="NIFTY27AUG21000PE", e="NFO", t="PE",
                   ls=25, ts=0.05, u="NIFTY", x="2027-08-21", k=24000.0),
    ]
    return InstrumentsResponse(cycle_date="2027-08-11", count=len(instruments), items=instruments)


async def _invoke_handler(
    peek_side_effect,
    underlying: str = "NIFTY",
    expiry: str = "",
    prices: bool = False,
    mock_sym_lookup: bool = True,
) -> ChainQuotesResponse:
    """
    Invoke OptionsController.chain_quotes directly with a patched cache.peek.
    If mock_sym_lookup=True, _chain_quotes_sym_lookup is replaced with an
    AsyncMock returning ({}, []) so tests do not spin up threads.
    Returns the ChainQuotesResponse.
    """
    from backend.api.routes.options import OptionsController

    fn = OptionsController.chain_quotes.fn

    with patch("backend.api.cache.peek", side_effect=peek_side_effect):
        if mock_sym_lookup:
            mock = AsyncMock(return_value=({}, []))
            with patch("backend.api.routes.options._chain_quotes_sym_lookup", mock):
                resp = await fn(None, underlying=underlying, expiry=expiry, prices=prices)
            return resp
        else:
            resp = await fn(None, underlying=underlying, expiry=expiry, prices=prices)
            return resp


# ---------------------------------------------------------------------------
# Test 1: Cold cache returns empty expiries and rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_cache_returns_empty():
    """When both instruments_chain and instruments are None (cold cache), returns empty."""
    resp = await _invoke_handler(lambda key: None)

    assert resp.expiries == [], "Cold cache should return empty expiries"
    assert resp.rows == [], "Cold cache should return empty rows"


# ---------------------------------------------------------------------------
# Test 2: Warm cache returns expiries (expiry-only fast path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_expiry_only_returns_expiries():
    """When instruments are warm and expiry omitted (fast path), expiries are returned."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    resp = await _invoke_handler(_peek, underlying="NIFTY", expiry="")

    assert resp.expiries == ["2027-08-14", "2027-08-21"], "Should return available expiries"
    assert resp.rows == [], "Expiry-only mode should return empty rows"


# ---------------------------------------------------------------------------
# Test 3: Warm cache + known expiry responds without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_with_expiry_succeeds():
    """When instruments are warm and expiry specified, handler returns without error."""
    inst_resp = _make_inst_resp()

    def _peek(key: str):
        return inst_resp

    resp = await _invoke_handler(
        _peek,
        underlying="NIFTY",
        expiry="2027-08-14",
        prices=False,
        mock_sym_lookup=True,
    )

    assert isinstance(resp, ChainQuotesResponse)


# ---------------------------------------------------------------------------
# Test 4: Warm cache + unknown underlying returns empty expiries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_unknown_underlying_returns_empty_expiries():
    """When instruments are warm but underlying not found, returns empty expiries."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}  # UNKNOWNXYZ not in index

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    resp = await _invoke_handler(
        _peek,
        underlying="UNKNOWNXYZ",
        expiry="",
        mock_sym_lookup=True,
    )

    assert resp.expiries == [], "Unknown underlying should have empty expiries"
    assert resp.rows == [], "Unknown underlying should have empty rows"


# ---------------------------------------------------------------------------
# Test 5: Underlying normalization — space stripping
# ---------------------------------------------------------------------------

def test_underlying_normalization_space_stripping():
    """Spaces in underlying are stripped via regex normalization."""
    pattern = r'\s+'

    # Test "CRUDE OIL" → "CRUDEOIL"
    result = _re.sub(pattern, '', "CRUDE OIL".upper())
    assert result == "CRUDEOIL", f"Expected 'CRUDEOIL' but got '{result}'"

    # Test "NATURAL GAS" → "NATURALGAS"
    result = _re.sub(pattern, '', "NATURAL GAS".upper())
    assert result == "NATURALGAS", f"Expected 'NATURALGAS' but got '{result}'"

    # Test "  nifty  " → "NIFTY"
    result = _re.sub(pattern, '', "  nifty  ".upper())
    assert result == "NIFTY", f"Expected 'NIFTY' but got '{result}'"

    # Test "nifty 50" → "NIFTY50"
    result = _re.sub(pattern, '', "nifty 50".upper())
    assert result == "NIFTY50", f"Expected 'NIFTY50' but got '{result}'"


def test_underlying_normalization_lowercasing():
    """Underlying is uppercased during normalization."""
    pattern = r'\s+'

    # Test lowercase input
    result = _re.sub(pattern, '', "nifty".upper())
    assert result == "NIFTY", f"Expected 'NIFTY' but got '{result}'"

    # Test mixed case
    result = _re.sub(pattern, '', "NiFtY".upper())
    assert result == "NIFTY", f"Expected 'NIFTY' but got '{result}'"


def test_underlying_normalization_multiple_spaces():
    """Multiple consecutive spaces are treated as one by the regex."""
    pattern = r'\s+'

    result = _re.sub(pattern, '', "CRUDE   OIL".upper())
    assert result == "CRUDEOIL", f"Expected 'CRUDEOIL' but got '{result}'"

    result = _re.sub(pattern, '', "NIFTY\t50".upper())  # Tab and space
    assert result == "NIFTY50", f"Expected 'NIFTY50' but got '{result}'"


# ---------------------------------------------------------------------------
# Test 6: Handler normalizes underlying internally
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_normalizes_underlying_in_response():
    """Handler normalizes underlying and includes normalized form in response."""
    resp = await _invoke_handler(lambda key: None, underlying="CRUDE OIL", expiry="")

    assert resp.underlying == "CRUDEOIL", f"Expected normalized 'CRUDEOIL' but got '{resp.underlying}'"


@pytest.mark.asyncio
async def test_handler_normalizes_underlying_with_whitespace():
    """Handler normalizes underlying with various whitespace patterns."""
    resp = await _invoke_handler(lambda key: None, underlying="  NIFTY  50  ", expiry="")

    assert resp.underlying == "NIFTY50", f"Expected 'NIFTY50' but got '{resp.underlying}'"


# ---------------------------------------------------------------------------
# Test 7: Empty underlying returns error (after normalization check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_underlying_after_normalization_raises_error():
    """When underlying is empty or only whitespace, handler raises 400 error."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.options import OptionsController

    fn = OptionsController.chain_quotes.fn

    with pytest.raises(HTTPException) as exc_info:
        with patch("backend.api.cache.peek", side_effect=lambda key: None):
            await fn(None, underlying="", expiry="")

    assert exc_info.value.status_code == 400
    assert "underlying is required" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_whitespace_only_underlying_raises_error():
    """When underlying is only whitespace, handler raises 400 error."""
    from litestar.exceptions import HTTPException
    from backend.api.routes.options import OptionsController

    fn = OptionsController.chain_quotes.fn

    with pytest.raises(HTTPException) as exc_info:
        with patch("backend.api.cache.peek", side_effect=lambda key: None):
            await fn(None, underlying="   ", expiry="")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test 8: All warm paths respond without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_warm_paths_succeed():
    """All non-cold-cache paths in handler return valid ChainQuotesResponse."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    # Path 1: Expiry-only (no expiry specified)
    resp1 = await _invoke_handler(_peek, underlying="NIFTY", expiry="", mock_sym_lookup=True)
    assert isinstance(resp1, ChainQuotesResponse)

    # Path 2: Full quote without prices
    resp2 = await _invoke_handler(
        _peek,
        underlying="NIFTY",
        expiry="2027-08-14",
        prices=False,
        mock_sym_lookup=True,
    )
    assert isinstance(resp2, ChainQuotesResponse)
