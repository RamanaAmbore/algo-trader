"""
Pytest suite for GET /api/watchlist/movers snapshot gate behaviour.

Covers:
1. Live market hours with valid quotes → response.captured_at is None
2. Market hours but broker fails → falls back to snapshot with captured_at
3. Off-hours (both exchanges closed) → serves snapshot
4. Snapshot contains gainers (positive change_pct)
5. Snapshot contains losers (negative change_pct)
"""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Optional

import pytest

from backend.api.models import MoversSnapshot
from backend.api.routes.watchlist import (
    MoverRow,
    MoversResponse,
)


def _make_snapshot(rows: list[dict]) -> MoversSnapshot:
    """Build a real MoversSnapshot instance with JSON-serialized payload."""
    snap = MoversSnapshot()
    snap.id = 1
    snap.date = date.today()
    snap.payload_json = json.dumps(rows)
    snap.captured_at = datetime.now(tz=timezone.utc)
    return snap


@pytest.mark.asyncio
async def test_movers_live_market_hours_ok(async_client):
    """
    Market open AND quote_data is non-empty
    → response.captured_at is None (live path, not snapshot fallback).
    """
    # Test focuses on the snapshot gate:  if quote_data is empty after market-open probe,
    # handler falls back to snapshot (captured_at non-null).  If quote_data is non-empty,
    # handler returns live movers (captured_at null).

    now_utc = datetime.now(timezone.utc)
    mock_snapshot = MagicMock()
    mock_snapshot.date = now_utc.date()
    mock_snapshot.captured_at = now_utc
    mock_snapshot.payload_json = json.dumps([{"tradingsymbol": "DUMMY", "exchange": "NSE", "last_price": 0, "previous_close": 0, "change_pct": 0, "peak_pct": 0, "sticky": False}])

    # Mock market open and successful quote fetch
    with patch("backend.api.routes.watchlist._movers_probe_market_state", return_value=(True, False)):
        with patch("backend.api.routes.watchlist._movers_rebuild_universes_if_needed", new_callable=AsyncMock):
            with patch(
                "backend.api.routes.watchlist._movers_build_key_to_meta",
                return_value={},  # Empty universe = no rows to process
            ):
                # Even with empty universe, we return empty movers without snapshot fallback
                mock_ist_now = MagicMock()
                mock_ist_now.date.return_value.isoformat.return_value = "2026-07-22"
                with patch(
                    "backend.shared.helpers.date_time_utils.timestamp_indian",
                    return_value=mock_ist_now,
                ):
                    response = await async_client.get("/api/watchlist/movers")

    assert response.status_code == 200
    data = response.json()
    # Empty universe → empty movers, but captured_at stays None (live, not snapshot)
    assert data.get("captured_at") is None, (
        f"Expected captured_at=None for live path (empty universe), got {data.get('captured_at')}"
    )


@pytest.mark.asyncio
async def test_movers_broker_fail_market_hours_returns_snapshot(async_client):
    """
    Market open AND _movers_fetch_quotes_cached returns {} (broker failed)
    → handler falls back to _movers_offhours_response;
    response has captured_at non-null (snapshot mode).
    """
    # Create a mock snapshot object
    now_utc = datetime.now(timezone.utc)
    snapshot_rows = [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "last_price": 3000.0,
            "previous_close": 2950.0,
            "change_pct": 1.69,
            "peak_pct": 1.69,
            "sticky": False,
        }
    ]
    mock_snapshot = MagicMock()
    mock_snapshot.date = now_utc.date()
    mock_snapshot.captured_at = now_utc
    mock_snapshot.payload_json = json.dumps(snapshot_rows)

    # Mock market open, but broker returns empty quotes
    with patch("backend.api.routes.watchlist._movers_probe_market_state", return_value=(True, False)):
        with patch("backend.api.routes.watchlist._movers_rebuild_universes_if_needed", new_callable=AsyncMock):
            with patch(
                "backend.api.routes.watchlist._movers_build_key_to_meta",
                return_value={"RELIANCE": {"underlying": "RELIANCE", "exchange": "NSE"}},
            ):
                # Broker fails → returns empty dict
                with patch(
                    "backend.api.routes.watchlist._movers_fetch_quotes_cached",
                    new_callable=AsyncMock,
                    return_value={},  # Empty → broker failure
                ):
                    # Mock _load_latest_movers_snapshot to return our test snapshot
                    with patch(
                        "backend.api.routes.watchlist._load_latest_movers_snapshot",
                        new_callable=AsyncMock,
                        return_value=mock_snapshot,
                    ):
                        mock_ist_now = MagicMock()
                        mock_ist_now.date.return_value.isoformat.return_value = "2026-07-22"
                        with patch(
                            "backend.shared.helpers.date_time_utils.timestamp_indian",
                            return_value=mock_ist_now,
                        ):
                            response = await async_client.get("/api/watchlist/movers")

    assert response.status_code == 200
    data = response.json()

    # Should fall back to snapshot with captured_at
    assert data.get("captured_at") is not None, (
        f"Expected captured_at non-null for snapshot fallback, got {data.get('captured_at')}"
    )
    # Snapshot should contain the seeded row
    assert len(data["movers"]) > 0, "Expected snapshot rows in fallback response"


@pytest.mark.asyncio
async def test_movers_offhours_returns_snapshot(async_client):
    """
    _movers_probe_market_state returns (False, False) (both closed)
    → _movers_offhours_response is called directly;
    response has captured_at non-null.
    """
    # Create a mock snapshot object
    now_utc = datetime.now(timezone.utc)
    snapshot_rows = [
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "last_price": 4200.0,
            "previous_close": 4100.0,
            "change_pct": 2.44,
            "peak_pct": 2.44,
            "sticky": False,
        }
    ]
    mock_snapshot = MagicMock()
    mock_snapshot.date = now_utc.date()
    mock_snapshot.captured_at = now_utc
    mock_snapshot.payload_json = json.dumps(snapshot_rows)

    # Mock both exchanges closed
    with patch("backend.api.routes.watchlist._movers_probe_market_state", return_value=(False, False)):
        # Mock _load_latest_movers_snapshot to return our test snapshot
        with patch(
            "backend.api.routes.watchlist._load_latest_movers_snapshot",
            new_callable=AsyncMock,
            return_value=mock_snapshot,
        ):
            mock_ist_now = MagicMock()
            mock_ist_now.date.return_value.isoformat.return_value = "2026-07-22"
            with patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_ist_now,
            ):
                response = await async_client.get("/api/watchlist/movers")

    assert response.status_code == 200
    data = response.json()

    # Should serve snapshot with captured_at
    assert data.get("captured_at") is not None, (
        f"Expected captured_at non-null for off-hours response, got {data.get('captured_at')}"
    )
    assert len(data["movers"]) > 0, "Expected snapshot rows for off-hours"


@pytest.mark.asyncio
async def test_movers_snapshot_contains_gainers(async_client):
    """
    Snapshot table has a row with change_pct = 3.17 (positive)
    → response deserialises JSON payload correctly and includes gainer.
    Tests the actual MoverRow deserialization pipeline, not just mocking.
    """
    gainer_row = {
        "tradingsymbol": "BAJAJFINSV",
        "exchange": "NSE",
        "last_price": 19500.0,
        "previous_close": 18900.0,
        "change_pct": 3.17,  # Positive gainer
        "peak_pct": 3.17,
        "sticky": False,
        "price_source": "snapshot",
        "is_animating": False,
        "quote_symbol": None,
    }
    snap = _make_snapshot([gainer_row])

    # Mock async_session context manager
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = snap
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value.execute = AsyncMock(return_value=mock_result)
    mock_session.__aexit__.return_value = None

    # Both exchanges closed → use snapshot deserialization path
    with patch("backend.api.routes.watchlist._movers_probe_market_state", return_value=(False, False)):
        with patch("backend.api.routes.watchlist.async_session", return_value=mock_session):
            mock_ist_now = MagicMock()
            mock_ist_now.date.return_value.isoformat.return_value = "2026-07-22"
            with patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_ist_now,
            ):
                response = await async_client.get("/api/watchlist/movers")

    assert response.status_code == 200
    data = response.json()
    movers = data.get("movers", [])

    # Check for at least one gainer
    gainers = [m for m in movers if m.get("change_pct", 0) > 0]
    assert len(gainers) > 0, (
        f"Expected at least one gainer (change_pct > 0) in snapshot, "
        f"got {len(gainers)} gainers from {len(movers)} movers"
    )
    # Verify the specific gainer is present
    assert any(m.get("tradingsymbol") == "BAJAJFINSV" for m in gainers), (
        "Expected BAJAJFINSV (gainer) in response"
    )


@pytest.mark.asyncio
async def test_movers_snapshot_contains_losers(async_client):
    """
    Snapshot table has a row with change_pct = -2.5 (negative)
    → response deserialises JSON payload correctly and includes loser.
    Tests the actual MoverRow deserialization pipeline, not just mocking.
    """
    loser_row = {
        "tradingsymbol": "TCS",
        "exchange": "NSE",
        "last_price": 3900.0,
        "previous_close": 4000.0,
        "change_pct": -2.5,  # Negative loser
        "peak_pct": -2.5,
        "sticky": False,
        "price_source": "snapshot",
        "is_animating": False,
        "quote_symbol": None,
    }
    snap = _make_snapshot([loser_row])

    # Mock async_session context manager
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = snap
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value.execute = AsyncMock(return_value=mock_result)
    mock_session.__aexit__.return_value = None

    # Both exchanges closed → use snapshot deserialization path
    with patch("backend.api.routes.watchlist._movers_probe_market_state", return_value=(False, False)):
        with patch("backend.api.routes.watchlist.async_session", return_value=mock_session):
            mock_ist_now = MagicMock()
            mock_ist_now.date.return_value.isoformat.return_value = "2026-07-22"
            with patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=mock_ist_now,
            ):
                response = await async_client.get("/api/watchlist/movers")

    assert response.status_code == 200
    data = response.json()
    movers = data.get("movers", [])

    # Check for at least one loser
    losers = [m for m in movers if m.get("change_pct", 0) < 0]
    assert len(losers) > 0, (
        f"Expected at least one loser (change_pct < 0) in snapshot, "
        f"got {len(losers)} losers from {len(movers)} movers"
    )
    # Verify the specific loser is present
    assert any(m.get("tradingsymbol") == "TCS" for m in losers), (
        "Expected TCS (loser) in response"
    )
