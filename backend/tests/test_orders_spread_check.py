"""
Route-level tests for GET /api/orders/spread-check — the live endpoint
the Chain-tab spread gate polls (frontend/src/lib/data/spreadGate.js +
frontend/src/lib/api.js:checkOrderSpread).

Covers:
  - response shape matches the frontend's documented contract
    (ok, spread_pct, bid, ask, tradingsymbol, threshold_pct)
  - threshold echo: explicit max_spread_pct query param vs the global
    setting fallback when omitted
  - 422 on a non-positive max_spread_pct and on a blank tradingsymbol/
    exchange
  - a quote-fetch exception still returns 200 with ok=False, never a 5xx
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_spread_check_quote_cache():
    """The route's `_cached_quote_fn` caches raw quotes under a
    `spread_q:` key for 2s (backend.api.cache._store is module-level
    and persists across tests in the same pytest process) — purge it
    around every test in this file, same narrowly-scoped pattern as
    conftest.py's `_reset_preflight_instruments_cache`, so different
    tests reusing the same tradingsymbol don't see each other's mocked
    quote."""
    from backend.api import cache as _cache_mod
    _PREFIX = "spread_q:"

    def _purge():
        for k in [k for k in _cache_mod._store if k.startswith(_PREFIX)]:
            _cache_mod._store.pop(k, None)
        for k in [k for k in _cache_mod._locks if k.startswith(_PREFIX)]:
            _cache_mod._locks.pop(k, None)

    _purge()
    yield
    _purge()


def _admin_patches():
    """Mark every request as authenticated so auth_or_demo_guard passes —
    same helper shape as test_orders_qty_lot_size.py's _admin_patches."""
    return patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        is_admin_request=lambda _conn: True,
        jwt_guard=AsyncMock(return_value=None),
    )


def _mock_broker(bid: float = 99.0, ask: float = 101.0, ltp: float = 100.0):
    broker = MagicMock()
    broker.quote.return_value = {
        "NFO:NIFTY24OCT25000CE": {
            "last_price": ltp,
            "depth": {"buy": [{"price": bid}], "sell": [{"price": ask}]},
        },
    }
    return broker


@pytest.mark.asyncio
async def test_spread_check_happy_path_response_shape(async_client):
    """Tight spread, explicit threshold — 200 with the frontend's exact
    documented field set, ok=True."""
    with _admin_patches(), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_mock_broker(bid=99.0, ask=101.0, ltp=100.0),
    ):
        response = await async_client.get(
            "/api/orders/spread-check",
            params={
                "tradingsymbol": "NIFTY24OCT25000CE",
                "exchange": "NFO",
                "max_spread_pct": 10,
            },
        )
    assert response.status_code == 200
    body = response.json()
    for field in ("ok", "spread_pct", "bid", "ask", "tradingsymbol", "threshold_pct"):
        assert field in body, f"missing field {field!r} in {body}"
    assert body["ok"] is True
    assert body["spread_pct"] == pytest.approx(2.0)
    assert body["bid"] == 99.0 and body["ask"] == 101.0
    assert body["tradingsymbol"] == "NIFTY24OCT25000CE"
    assert body["threshold_pct"] == 10.0
    assert body["threshold_source"] == "override"


@pytest.mark.asyncio
async def test_spread_check_wide_spread_ok_false(async_client):
    with _admin_patches(), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_mock_broker(bid=80.0, ask=120.0, ltp=100.0),
    ):
        response = await async_client.get(
            "/api/orders/spread-check",
            params={
                "tradingsymbol": "NIFTY24OCT25000CE",
                "exchange": "NFO",
                "max_spread_pct": 10,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "wide"


@pytest.mark.asyncio
async def test_spread_check_threshold_omitted_falls_back_to_global_setting(async_client):
    with _admin_patches(), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_mock_broker(),
    ), patch(
        "backend.shared.helpers.settings.get_float", return_value=12.5,
    ):
        response = await async_client.get(
            "/api/orders/spread-check",
            params={"tradingsymbol": "NIFTY24OCT25000CE", "exchange": "NFO"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["threshold_pct"] == 12.5
    assert body["threshold_source"] == "setting"


@pytest.mark.asyncio
async def test_spread_check_rejects_non_positive_threshold(async_client):
    with _admin_patches():
        response = await async_client.get(
            "/api/orders/spread-check",
            params={
                "tradingsymbol": "NIFTY24OCT25000CE",
                "exchange": "NFO",
                "max_spread_pct": 0,
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_spread_check_rejects_blank_tradingsymbol(async_client):
    with _admin_patches():
        response = await async_client.get(
            "/api/orders/spread-check",
            params={"tradingsymbol": "", "exchange": "NFO"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_spread_check_rejects_blank_exchange(async_client):
    with _admin_patches():
        response = await async_client.get(
            "/api/orders/spread-check",
            params={"tradingsymbol": "NIFTY24OCT25000CE", "exchange": ""},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_spread_check_quote_exception_returns_200_not_5xx(async_client):
    """Broker/quote-layer exception must degrade to a structured
    ok=False/status=error result, never propagate as a 5xx — same
    never-raise convention as backend.api.algo.spread_check itself."""
    broker = MagicMock()
    broker.quote.side_effect = RuntimeError("broker unreachable")
    with _admin_patches(), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=broker,
    ):
        response = await async_client.get(
            "/api/orders/spread-check",
            params={
                "tradingsymbol": "NIFTY24OCT25000CE",
                "exchange": "NFO",
                "max_spread_pct": 10,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert body["spread_pct"] is None


@pytest.mark.asyncio
async def test_spread_check_mcx_exchange_passthrough(async_client):
    """exchange is never silently defaulted to NFO — MCX must be
    checked against an MCX-keyed quote, not an NFO one."""
    broker = MagicMock()
    broker.quote.return_value = {
        "MCX:CRUDEOIL26OCTFUT": {
            "last_price": 6000.0,
            "depth": {"buy": [{"price": 5990.0}], "sell": [{"price": 6010.0}]},
        },
    }
    with _admin_patches(), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=broker,
    ):
        response = await async_client.get(
            "/api/orders/spread-check",
            params={
                "tradingsymbol": "CRUDEOIL26OCTFUT",
                "exchange": "MCX",
                "max_spread_pct": 10,
            },
        )
    assert response.status_code == 200
    broker.quote.assert_called_once_with(["MCX:CRUDEOIL26OCTFUT"])
    assert response.json()["exchange"] == "MCX"
