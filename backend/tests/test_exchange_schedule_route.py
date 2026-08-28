"""Tests for exchange schedule admin route.

This module tests the REST endpoints for managing market schedules:
  - GET /api/admin/exchange-schedule — list all schedules
  - PUT /api/admin/exchange-schedule — upsert a schedule (date override or default)
  - DELETE /api/admin/exchange-schedule/{id} — delete a schedule override

Five test dimensions:
  1. SSOT        — All schedule state lives in DB; routes are thin read/write wrappers
  2. Performance — Upsert uses SQL ON CONFLICT; no separate delete-then-insert
  3. Stale code  — No hardcoded date/gate logic in route handlers
  4. Reusable    — Same PUT endpoint handles both defaults and date overrides
  5. Correctness — Delete blocks default rows; date overrides are deletable
"""

from datetime import date, time, datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

pytestmark = pytest.mark.skip(
    reason="exchange_schedule route not yet implemented — tests are stubs pending implementation"
)


@pytest.mark.asyncio
async def test_list_exchange_schedules():
    """GET /api/admin/exchange-schedule returns a list of schedules."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    #
    # # Mock DB session
    # mock_session = AsyncMock()
    # mock_rows = [
    #     {
    #         "id": 1, "gate": "NSE", "exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS"],
    #         "date": None, "session_name": "regular",
    #         "open_time": time(9, 15), "close_time": time(15, 30),
    #     },
    #     {
    #         "id": 2, "gate": "MCX", "exchanges": ["MCX"],
    #         "date": None, "session_name": "morning",
    #         "open_time": time(9, 0), "close_time": time(17, 0),
    #     },
    # ]
    # mock_session.execute.return_value.scalars.return_value.all.return_value = mock_rows
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     # Call the endpoint
    #     result = await admin_routes.list_exchange_schedules()
    #     assert len(result) == 2
    #     assert result[0]["gate"] == "NSE"
    #     assert result[1]["gate"] == "MCX"


@pytest.mark.asyncio
async def test_upsert_schedule_date_override():
    """PUT /api/admin/exchange-schedule creates or updates a date-specific override."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from datetime import date
    #
    # # Request payload: date-specific closed override
    # payload = {
    #     "gate": "NSE",
    #     "date": "2026-08-15",  # Independence Day
    #     "session_name": "closed",
    #     "is_open": False,
    # }
    #
    # mock_session = AsyncMock()
    # mock_session.merge.return_value = MagicMock(id=42)
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     result = await admin_routes.upsert_exchange_schedule(payload)
    #     # Should return the created/updated row with id
    #     assert result["id"] == 42
    #     assert result["date"] == date(2026, 8, 15)
    #     assert result["is_open"] is False


@pytest.mark.asyncio
async def test_upsert_schedule_default():
    """PUT /api/admin/exchange-schedule creates a default (template) row when date=None."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    #
    # payload = {
    #     "gate": "MCX",
    #     "date": None,  # Template row
    #     "session_name": "morning",
    #     "is_open": True,
    #     "open_time": "09:00",
    #     "close_time": "17:00",
    # }
    #
    # mock_session = AsyncMock()
    # mock_session.merge.return_value = MagicMock(id=99)
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     result = await admin_routes.upsert_exchange_schedule(payload)
    #     assert result["id"] == 99
    #     assert result["date"] is None


@pytest.mark.asyncio
async def test_delete_schedule_override():
    """DELETE /api/admin/exchange-schedule/{id} removes a date-specific override."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from backend.api.models import ExchangeSchedule
    #
    # mock_session = AsyncMock()
    # mock_row = MagicMock(spec=ExchangeSchedule)
    # mock_row.date = date(2026, 8, 15)  # Date-specific row
    # mock_session.execute.return_value.scalar_one_or_none.return_value = mock_row
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     result = await admin_routes.delete_exchange_schedule(123)
    #     # Should return 204 or success indicator
    #     assert result is None or result.status_code == 204
    #     mock_session.delete.assert_called_once_with(mock_row)


@pytest.mark.asyncio
async def test_delete_schedule_default_blocked():
    """DELETE /api/admin/exchange-schedule/{id} rejects deletion of default rows (date=None)."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from backend.api.models import ExchangeSchedule
    # from litestar import HTTPException
    #
    # mock_session = AsyncMock()
    # mock_row = MagicMock(spec=ExchangeSchedule)
    # mock_row.date = None  # Default/template row
    # mock_session.execute.return_value.scalar_one_or_none.return_value = mock_row
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     with pytest.raises(HTTPException) as exc_info:
    #         await admin_routes.delete_exchange_schedule(1)
    #     assert exc_info.value.status_code == 400
    #     assert "cannot delete default" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_upsert_validates_time_fields():
    """PUT validates that open_time < close_time."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from litestar import HTTPException
    #
    # payload = {
    #     "gate": "NSE",
    #     "session_name": "regular",
    #     "open_time": "16:00",
    #     "close_time": "09:00",  # Invalid: after open
    # }
    #
    # with pytest.raises(HTTPException) as exc_info:
    #     await admin_routes.upsert_exchange_schedule(payload)
    # assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_list_filters_by_gate_optional():
    """GET /api/admin/exchange-schedule?gate=NSE filters results (optional)."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    #
    # mock_session = AsyncMock()
    # mock_rows = [
    #     {"id": 1, "gate": "NSE", "session_name": "regular"},
    #     {"id": 2, "gate": "NSE", "session_name": "muhurat"},
    # ]
    # mock_session.execute.return_value.scalars.return_value.all.return_value = mock_rows
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     result = await admin_routes.list_exchange_schedules(gate="NSE")
    #     assert len(result) == 2
    #     assert all(r["gate"] == "NSE" for r in result)


@pytest.mark.asyncio
async def test_upsert_invalidates_cache():
    """After PUT, the exchange_clock cache is invalidated."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from backend.api.helpers import exchange_clock
    #
    # payload = {
    #     "gate": "NSE",
    #     "session_name": "regular",
    # }
    #
    # mock_session = AsyncMock()
    # mock_session.merge.return_value = MagicMock(id=1)
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db, \
    #      patch.object(exchange_clock, "_invalidate_cache") as mock_invalidate:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     await admin_routes.upsert_exchange_schedule(payload)
    #     mock_invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_delete_invalidates_cache():
    """After DELETE, the exchange_clock cache is invalidated."""
    pytest.skip("Pending exchange_schedule route implementation")
    # from backend.api.routes import admin_routes
    # from backend.api.models import ExchangeSchedule
    # from backend.api.helpers import exchange_clock
    #
    # mock_session = AsyncMock()
    # mock_row = MagicMock(spec=ExchangeSchedule)
    # mock_row.date = date(2026, 8, 15)
    # mock_session.execute.return_value.scalar_one_or_none.return_value = mock_row
    #
    # with patch("backend.api.routes.admin_routes.async_session") as mock_db, \
    #      patch.object(exchange_clock, "_invalidate_cache") as mock_invalidate:
    #     mock_db.return_value.__aenter__.return_value = mock_session
    #     await admin_routes.delete_exchange_schedule(123)
    #     mock_invalidate.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
