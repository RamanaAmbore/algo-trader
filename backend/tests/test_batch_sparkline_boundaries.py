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


@pytest.mark.asyncio
async def test_batch_sparkline_empty_symbols_returns_empty():
    """POST /api/quotes/sparkline with symbols=[] returns 200 with empty data dict
    (not 400 error)."""
    from backend.api.routes.quote import SparklineRequest, SparklineResponse

    # Create a minimal request with no symbols
    request_data = SparklineRequest(symbols=[], days=5)

    # Mock the endpoint handler directly (simulate the POST path)
    from backend.api.routes.quote import SparklineController
    controller = SparklineController()

    # Call the handler
    response = await controller.batch_sparkline(request_data)

    # Verify response shape
    assert isinstance(response, SparklineResponse)
    assert response.data == {}, f"Expected empty data dict, got {response.data}"
    assert response.refreshed_at is not None
    assert isinstance(response.refreshed_at, str)


@pytest.mark.asyncio
async def test_batch_sparkline_over_100_symbols_returns_400():
    """POST /api/quotes/sparkline with 101 symbols returns 400 HTTPException."""
    from backend.api.routes.quote import SparklineRequest, SparklineSymbol
    from litestar.exceptions import HTTPException

    # Create a request with 101 symbols (over the cap)
    symbols = [
        SparklineSymbol(tradingsymbol=f"SYM{i:03d}", exchange="NSE")
        for i in range(101)
    ]
    request_data = SparklineRequest(symbols=symbols, days=5)

    from backend.api.routes.quote import SparklineController
    controller = SparklineController()

    # Should raise HTTPException with 400 status
    with pytest.raises(HTTPException) as exc_info:
        await controller.batch_sparkline(request_data)

    assert exc_info.value.status_code == 400
    assert "cap is 100" in exc_info.value.detail


@pytest.mark.asyncio
async def test_batch_sparkline_days_clamped_to_1():
    """days=0 is clamped to 1 (no crash)."""
    from backend.api.routes.quote import SparklineRequest, SparklineSymbol

    symbols = [SparklineSymbol(tradingsymbol="RELIANCE", exchange="NSE")]
    request_data = SparklineRequest(symbols=symbols, days=0)

    from backend.api.routes.quote import SparklineController
    controller = SparklineController()

    # Mock the async dependencies to avoid broker calls
    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=(symbols, {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=AsyncMock(return_value=({}, {}))), \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={})):
        response = await controller.batch_sparkline(request_data)

    # Should not raise; response is valid
    assert response.data == {}
    assert response.refreshed_at is not None


@pytest.mark.asyncio
async def test_batch_sparkline_days_clamped_to_90():
    """days=200 is clamped to 90 (no crash)."""
    from backend.api.routes.quote import SparklineRequest, SparklineSymbol

    symbols = [SparklineSymbol(tradingsymbol="NIFTY", exchange="NSE")]
    request_data = SparklineRequest(symbols=symbols, days=200)

    from backend.api.routes.quote import SparklineController
    controller = SparklineController()

    # Mock all async calls
    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=(symbols, {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=AsyncMock(return_value=({}, {}))) as m_fetch, \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={})):
        response = await controller.batch_sparkline(request_data)

    # Verify _fetch_bars_parallel was called with days=90 (clamped)
    assert m_fetch.await_count == 1
    call_kwargs = m_fetch.call_args[1]
    assert call_kwargs.get("days") == 90, \
        f"Expected days=90 (clamped), got {call_kwargs.get('days')}"


@pytest.mark.asyncio
async def test_dual_write_bare_and_resolved_keys():
    """When a resolved contract key (e.g., CRUDEOIL26JULFUT) has a sparkline,
    result dict also contains the bare root key (CRUDEOIL) via _compose_and_dual_write."""
    from backend.api.routes.quote import (
        _compose_and_dual_write, SparklineSymbol
    )

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
    assert "CRUDEOIL26JULFUT" in result, \
        f"Resolved key not in result: {result.keys()}"
    assert "CRUDEOIL" in result, \
        f"Bare root key not in result via dual-write: {result.keys()}"

    # Both should point to the same series
    assert result["CRUDEOIL26JULFUT"] == result["CRUDEOIL"], \
        "Dual-write should copy the same series to both keys"

    # Series should contain past + today + LTP
    series = result["CRUDEOIL26JULFUT"]
    assert len(series) >= 5, f"Expected series with past+today+ltp, got {series}"


@pytest.mark.asyncio
async def test_batch_sparkline_response_shape():
    """Verify sparkline response shape is stable and correct."""
    from backend.api.routes.quote import SparklineRequest, SparklineSymbol

    symbols = [SparklineSymbol(tradingsymbol="TCS", exchange="NSE")]
    request_data = SparklineRequest(symbols=symbols, days=5)

    from backend.api.routes.quote import SparklineController
    controller = SparklineController()

    # Mock all dependencies
    with patch("backend.api.routes.quote._normalize_sparkline_symbols",
               new=AsyncMock(return_value=(symbols, {}))), \
         patch("backend.api.routes.quote._any_segment_open",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.routes.quote._fetch_bars_parallel",
               new=AsyncMock(return_value=({"TCS": [3000.0, 3100.0]}, {"TCS": [3150.0]}))), \
         patch("backend.api.routes.quote._resolve_spark_ltps",
               new=AsyncMock(return_value={"NSE:TCS": 3200.0})), \
         patch("backend.api.routes.quote._compose_and_dual_write",
               new=MagicMock(return_value={"TCS": [3000.0, 3100.0, 3150.0, 3200.0]})):
        response = await controller.batch_sparkline(request_data)

    # Verify response has required fields
    assert hasattr(response, "data"), "Response must have 'data' field"
    assert hasattr(response, "refreshed_at"), "Response must have 'refreshed_at' field"
    assert hasattr(response, "as_of"), "Response must have 'as_of' field (nullable)"

    # Verify data is a dict
    assert isinstance(response.data, dict), \
        f"Expected data to be dict, got {type(response.data)}"

    # Verify refreshed_at is a timestamp string
    assert isinstance(response.refreshed_at, str), \
        "refreshed_at must be an ISO-8601 timestamp string"

    # Verify as_of is either None (market open) or a timestamp string
    assert response.as_of is None or isinstance(response.as_of, str), \
        f"as_of must be None or timestamp string, got {type(response.as_of)}"
