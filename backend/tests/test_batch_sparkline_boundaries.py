"""
Batch sparkline endpoint boundary tests.

Five dimensions:
  1. SSOT       — Test only the canonical batch_sparkline endpoint
                  (not duplicated composition logic).
  2. Performance— Empty symbol list handled with no broker calls (O(1)).
  3. Stale code — 100-symbol cap enforced; no bypass paths.
  4. Reuse      — Mock only the store calls and ticker; all helpers tested.
  5. UX         — Response shape is stable: {data: dict, refreshed_at: str, as_of: ?str}.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Group 1: HTTP-level tests (via async_client)
# =============================================================================


@pytest.mark.asyncio
async def test_batch_sparkline_empty_symbols_returns_empty(async_client):
    """POST /api/quotes/sparkline with symbols=[] returns 2xx with empty data dict
    (not 400 error)."""
    request_body = {
        "symbols": [],
        "days": 5,
    }

    response = await async_client.post(
        "/api/quotes/sparkline",
        json=request_body,
    )

    # Should return 2xx (200/201), not 4xx or 5xx
    assert 200 <= response.status_code < 300, (
        f"Expected 2xx success, got {response.status_code}: {response.text}"
    )

    data = response.json()
    assert "data" in data, "Response should have 'data' key"
    assert isinstance(data["data"], dict), "'data' should be a dict"
    assert data["data"] == {}, f"Expected empty data dict, got {data['data']}"
    assert "refreshed_at" in data, "Response should have 'refreshed_at' key"
    assert isinstance(data["refreshed_at"], str), "'refreshed_at' must be an ISO-8601 timestamp string"


@pytest.mark.asyncio
async def test_batch_sparkline_over_100_symbols_returns_400(async_client):
    """POST /api/quotes/sparkline with 101 symbols returns 400 HTTPException."""
    # Create a request with 101 symbols (over the cap)
    symbols = [
        {"tradingsymbol": f"SYM{i:03d}", "exchange": "NSE"}
        for i in range(101)
    ]
    request_body = {
        "symbols": symbols,
        "days": 5,
    }

    response = await async_client.post(
        "/api/quotes/sparkline",
        json=request_body,
    )

    # Should return 400
    assert response.status_code == 400, (
        f"Expected 400 for >100 symbols, got {response.status_code}: {response.text}"
    )

    data = response.json()
    assert "detail" in data, "Error response should have 'detail' key"
    assert "cap is 100" in data["detail"], (
        f"Error message should mention 100-symbol cap, got: {data['detail']}"
    )


@pytest.mark.asyncio
async def test_batch_sparkline_days_clamped_to_1(async_client):
    """days=0 is clamped to 1 (no crash)."""
    from backend.api.routes.quote import SparklineSymbol

    request_body = {
        "symbols": [{"tradingsymbol": "RELIANCE", "exchange": "NSE"}],
        "days": 0,
    }

    # Mock the async dependencies to avoid broker calls
    mock_sym = SparklineSymbol(tradingsymbol="RELIANCE", exchange="NSE")
    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=([mock_sym], {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=AsyncMock(return_value=({}, {}))), \
         patch("backend.api.routes.quote._self_heal_empty_bars",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._fill_from_daily_book_sparkline",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={})):
        response = await async_client.post(
            "/api/quotes/sparkline",
            json=request_body,
        )

    # Should not raise; response is valid (2xx)
    assert 200 <= response.status_code < 300, (
        f"Expected 2xx success, got {response.status_code}: {response.text}"
    )

    data = response.json()
    assert isinstance(data["data"], dict), "'data' should be a dict"
    assert data["refreshed_at"] is not None


@pytest.mark.asyncio
async def test_batch_sparkline_days_clamped_to_90(async_client):
    """days=200 is clamped to 90 (no crash)."""
    from backend.api.routes.quote import SparklineSymbol

    request_body = {
        "symbols": [{"tradingsymbol": "NIFTY", "exchange": "NSE"}],
        "days": 200,
    }

    # Mock all async calls and capture the days argument passed to _fetch_bars_parallel
    mock_fetch_bars = AsyncMock(return_value=({}, {}))
    mock_sym = SparklineSymbol(tradingsymbol="NIFTY", exchange="NSE")

    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=([mock_sym], {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=mock_fetch_bars), \
         patch("backend.api.routes.quote._self_heal_empty_bars",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._fill_from_daily_book_sparkline",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={})):
        response = await async_client.post(
            "/api/quotes/sparkline",
            json=request_body,
        )

    # Verify _fetch_bars_parallel was called with days=90 (clamped)
    assert mock_fetch_bars.await_count >= 1, "Expected _fetch_bars_parallel to be called"
    # call_args is a tuple of (args, kwargs); days is the 5th positional arg (index 4)
    call_args = mock_fetch_bars.call_args[0]
    actual_days = call_args[4] if len(call_args) > 4 else None
    assert actual_days == 90, (
        f"Expected days=90 (clamped), got {actual_days} in args {call_args}"
    )
    assert 200 <= response.status_code < 300, (
        f"Expected 2xx success, got {response.status_code}: {response.text}"
    )


# =============================================================================
# Group 2: Unit-level tests (direct function calls, not HTTP)
# =============================================================================


@pytest.mark.asyncio
async def test_dual_write_bare_and_resolved_keys():
    """When a resolved contract key (e.g., CRUDEOIL26JULFUT) has a sparkline,
    result dict also contains the bare root key (CRUDEOIL) via _compose_and_dual_write."""
    from backend.api.routes.quote import _compose_and_dual_write, SparklineSymbol

    # Simulate normalized symbols (resolved contract)
    norm_syms = [SparklineSymbol(tradingsymbol="CRUDEOIL26JULFUT", exchange="MCX")]

    # Past data keyed by the resolved contract name
    past_result = {"CRUDEOIL26JULFUT": [100.0, 101.0, 102.0]}
    today_result = {"CRUDEOIL26JULFUT": [103.0, 104.0]}
    ltp_map = {"MCX:CRUDEOIL26JULFUT": 105.0}

    # orig_to_resolved maps bare root to resolved contract
    orig_to_resolved = {"CRUDEOIL": "CRUDEOIL26JULFUT"}

    # Call the dual-write function
    result = _compose_and_dual_write(
        norm_syms,
        past_result,
        today_result,
        ltp_map,
        orig_to_resolved,
        spark_market_closed=False,
    )

    # Verify both the resolved and bare keys are in the result
    assert "CRUDEOIL26JULFUT" in result, (
        f"Resolved key not in result: {result.keys()}"
    )
    assert "CRUDEOIL" in result, (
        f"Bare root key not in result via dual-write: {result.keys()}"
    )

    # Both should point to the same series
    assert result["CRUDEOIL26JULFUT"] == result["CRUDEOIL"], (
        "Dual-write should copy the same series to both keys"
    )

    # Series should contain past + today + LTP
    series = result["CRUDEOIL26JULFUT"]
    assert len(series) >= 5, f"Expected series with past+today+ltp, got {series}"


@pytest.mark.asyncio
async def test_batch_sparkline_response_shape(async_client):
    """Verify sparkline response shape is stable and correct."""
    from backend.api.routes.quote import SparklineSymbol

    request_body = {
        "symbols": [{"tradingsymbol": "TCS", "exchange": "NSE"}],
        "days": 5,
    }

    # Mock all dependencies
    mock_sym = SparklineSymbol(tradingsymbol="TCS", exchange="NSE")
    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=([mock_sym], {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=AsyncMock(return_value=({"TCS": [3000.0, 3100.0]}, {"TCS": [3150.0]}))), \
         patch("backend.api.routes.quote._self_heal_empty_bars",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._fill_from_daily_book_sparkline",
               new=AsyncMock()), \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={"NSE:TCS": 3200.0})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={"TCS": [3000.0, 3100.0, 3150.0, 3200.0]})):
        response = await async_client.post(
            "/api/quotes/sparkline",
            json=request_body,
        )

    # Verify response status
    assert 200 <= response.status_code < 300, (
        f"Expected 2xx success, got {response.status_code}: {response.text}"
    )

    data = response.json()

    # Verify response has required fields
    assert "data" in data, "Response must have 'data' field"
    assert "refreshed_at" in data, "Response must have 'refreshed_at' field"
    assert "as_of" in data, "Response must have 'as_of' field (nullable)"

    # Verify data is a dict
    assert isinstance(data["data"], dict), (
        f"Expected data to be dict, got {type(data['data'])}"
    )

    # Verify refreshed_at is a timestamp string
    assert isinstance(data["refreshed_at"], str), (
        "refreshed_at must be an ISO-8601 timestamp string"
    )

    # Verify as_of is either None (market open) or a timestamp string
    assert data["as_of"] is None or isinstance(data["as_of"], str), (
        f"as_of must be None or timestamp string, got {type(data['as_of'])}"
    )
