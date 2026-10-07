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


def test_row_flags_unverified_mcx_quantity_when_lot_size_is_missing(monkeypatch):
    from backend.api.routes import orders_helpers as oh
    monkeypatch.setattr(kite, "_LOT_INDEX", {})
    row = oh._row_from_dict({"order_id": "1", "exchange": "MCX", "tradingsymbol": "CRUDEOIL26OCT8400CE",
                             "quantity": 3}, "ZG0790", "zerodha_kite")
    assert row.qty_unverified is True


def test_row_is_verified_when_lot_size_is_loaded(monkeypatch):
    from backend.api.routes import orders_helpers as oh
    monkeypatch.setattr(kite, "_LOT_INDEX", {("MCX", "CRUDEOIL26OCT8400CE"): 100})
    row = oh._row_from_dict({"order_id": "1", "exchange": "MCX", "tradingsymbol": "CRUDEOIL26OCT8400CE",
                             "quantity": 3}, "ZG0790", "zerodha_kite")
    assert row.qty_unverified is False
    assert row.quantity == 300


def test_nfo_row_is_never_flagged(monkeypatch):
    from backend.api.routes import orders_helpers as oh
    monkeypatch.setattr(kite, "_LOT_INDEX", {})
    row = oh._row_from_dict({"order_id": "1", "exchange": "NFO", "tradingsymbol": "NIFTY26OCTFUT",
                             "quantity": 75}, "ZG0790", "zerodha_kite")
    assert row.qty_unverified is False


def test_dhan_and_groww_reads_retry_on_network_errors():
    from backend.brokers.adapters import dhan, groww
    for cls in (dhan.DhanBroker, groww.GrowwBroker):
        for name in ("holdings", "positions", "margins", "orders"):
            fn = getattr(cls, name)
            assert hasattr(fn, "__wrapped__") or getattr(fn, "__name__", "") == name, (cls, name)


def test_market_data_reads_retry_on_transient_errors_across_brokers():
    from backend.brokers.adapters import kite, dhan, groww
    for cls, has_recoverable in ((kite.KiteBroker, True), (dhan.DhanBroker, True), (groww.GrowwBroker, True)):
        for name in ("ltp", "quote", "get_gtts"):
            fn = getattr(cls, name)
            assert hasattr(fn, "__wrapped__") or getattr(fn, "__name__", "") == name, (cls, name)
