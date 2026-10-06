"""ensure_lot_index warms the lot-size index before the orders list reads it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.brokers.adapters import kite


@pytest.fixture
def clean_index(monkeypatch):
    monkeypatch.setattr(kite, "_LOT_INDEX", {})
    monkeypatch.setattr(kite, "_LOT_INDEX_STAMP", None)


@pytest.mark.asyncio
async def test_ensure_lot_index_populates_mcx_lot_size(clean_index):
    resp = SimpleNamespace(items=[SimpleNamespace(e="MCX", s="CRUDEOIL26OCT8400CE", ls=100)])
    with patch("backend.api.cache.get_or_fetch", new=AsyncMock(return_value=resp)), \
         patch("backend.api.routes.instruments._fetch_instruments", new=AsyncMock()), \
         patch("backend.api.routes.instruments._TTL_SECONDS", 60):
        assert await kite.ensure_lot_index() is True
    assert kite._LOT_INDEX[("MCX", "CRUDEOIL26OCT8400CE")] == 100


@pytest.mark.asyncio
async def test_ensure_lot_index_reports_failure_without_raising(clean_index):
    with patch("backend.api.cache.get_or_fetch", new=AsyncMock(side_effect=RuntimeError("down"))), \
         patch("backend.api.routes.instruments._fetch_instruments", new=AsyncMock()), \
         patch("backend.api.routes.instruments._TTL_SECONDS", 60):
        assert await kite.ensure_lot_index() is False


@pytest.mark.asyncio
async def test_get_lot_size_falls_back_to_stale_value_when_refresh_fails(clean_index, monkeypatch):
    monkeypatch.setitem(kite._LOT_INDEX, ("MCX", "GOLDM26OCTFUT"), 10)
    with patch.object(kite, "ensure_lot_index", new=AsyncMock(return_value=False)):
        assert await kite.get_lot_size("MCX", "GOLDM26OCTFUT") == 10
        assert await kite.get_lot_size("MCX", "UNKNOWN") == 0
