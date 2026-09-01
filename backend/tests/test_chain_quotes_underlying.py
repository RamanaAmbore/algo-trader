"""
Tests for the chain-quotes `ready` flag and underlying normalization fix.

Coverage:
  1. ChainQuotesResponse.ready field defaults to False and is set to True on warm paths
  2. Cold cache (both instruments_chain and instruments None) returns ready=False
  3. Warm cache (instruments available) returns ready=True
  4. Underlying normalization: spaces stripped and uppercased
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
# Test 1: ChainQuotesResponse.ready field defaults to False
# ---------------------------------------------------------------------------

def test_chain_quotes_response_ready_field_defaults_false():
    """ChainQuotesResponse has ready field with default False."""
    resp = ChainQuotesResponse(underlying="NIFTY", expiry="", rows=[])
    assert resp.ready is False, "ChainQuotesResponse.ready should default to False"
    assert hasattr(resp, 'ready'), "ChainQuotesResponse must have a 'ready' field"


def test_chain_quotes_response_ready_can_be_set_true():
    """ChainQuotesResponse ready can be explicitly set to True."""
    resp = ChainQuotesResponse(underlying="NIFTY", expiry="", rows=[], ready=True)
    assert resp.ready is True, "ChainQuotesResponse.ready should be True when set"


# ---------------------------------------------------------------------------
# Test 2: Cold cache returns ready=False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_cache_returns_ready_false():
    """When both instruments_chain and instruments are None (cold cache), ready=False."""
    resp = await _invoke_handler(lambda key: None)

    assert resp.ready is False, "Cold cache should return ready=False"
    assert resp.expiries == [], "Cold cache should return empty expiries"
    assert resp.rows == [], "Cold cache should return empty rows"


# ---------------------------------------------------------------------------
# Test 3: Warm cache returns ready=True (expiry-only fast path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_expiry_only_returns_ready_true():
    """When instruments are warm and expiry omitted (fast path), ready=True."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    resp = await _invoke_handler(_peek, underlying="NIFTY", expiry="")

    assert resp.ready is True, "Warm fast-path should return ready=True"
    assert resp.expiries == ["2027-08-14", "2027-08-21"], "Should return available expiries"
    assert resp.rows == [], "Expiry-only mode should return empty rows"


# ---------------------------------------------------------------------------
# Test 4: Warm cache + known expiry returns ready=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_with_expiry_returns_ready_true():
    """When instruments are warm and expiry specified, ready=True even without prices."""
    inst_resp = _make_inst_resp()

    def _peek(key: str):
        return inst_resp

    resp = await _invoke_handler(
        _peek,
        underlying="NIFTY",
        expiry="2027-08-14",
        prices=False,  # Do not call broker for prices
        mock_sym_lookup=True,
    )

    assert resp.ready is True, "Warm cache with expiry should return ready=True"


# ---------------------------------------------------------------------------
# Test 5: Warm cache + unknown underlying returns ready=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_cache_unknown_underlying_returns_ready_true():
    """When instruments are warm but underlying not found, ready=True with empty expiries."""
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

    assert resp.ready is True, "Warm cache should return ready=True even for unknown underlying"
    assert resp.expiries == [], "Unknown underlying should have empty expiries"
    assert resp.rows == [], "Unknown underlying should have empty rows"


# ---------------------------------------------------------------------------
# Test 6: Underlying normalization — space stripping
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
# Test 7: Handler normalizes underlying internally
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_normalizes_underlying_in_response():
    """Handler normalizes underlying and includes normalized form in response."""
    resp = await _invoke_handler(lambda key: None, underlying="CRUDE OIL", expiry="")

    # Response underlying should be normalized
    assert resp.underlying == "CRUDEOIL", f"Expected normalized 'CRUDEOIL' but got '{resp.underlying}'"
    assert resp.ready is False, "Cold cache should return ready=False"


@pytest.mark.asyncio
async def test_handler_normalizes_underlying_with_whitespace():
    """Handler normalizes underlying with various whitespace patterns."""
    resp = await _invoke_handler(lambda key: None, underlying="  NIFTY  50  ", expiry="")

    assert resp.underlying == "NIFTY50", f"Expected 'NIFTY50' but got '{resp.underlying}'"


# ---------------------------------------------------------------------------
# Test 8: Empty underlying returns error (after normalization check)
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
# Test 9: ready=True set on all warm paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_warm_paths_set_ready_true():
    """All non-cold-cache paths in handler set ready=True."""
    inst_resp = _make_inst_resp()
    exp_index = {"NIFTY": ["2027-08-14", "2027-08-21"]}

    def _peek(key: str):
        if key == "instruments_chain_expiries":
            return exp_index
        return inst_resp

    # Path 1: Expiry-only (no expiry specified)
    resp1 = await _invoke_handler(_peek, underlying="NIFTY", expiry="", mock_sym_lookup=True)
    assert resp1.ready is True, "Expiry-only path should set ready=True"

    # Path 2: Full quote without prices
    resp2 = await _invoke_handler(
        _peek,
        underlying="NIFTY",
        expiry="2027-08-14",
        prices=False,
        mock_sym_lookup=True,
    )
    assert resp2.ready is True, "Full quote without prices should set ready=True"
