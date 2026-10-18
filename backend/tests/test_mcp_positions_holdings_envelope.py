"""get_positions() / get_holdings() (backend/mcp/kite_server.py) must unwrap
the real API's envelope response (PositionsResponse/HoldingsResponse —
`rows`/`summary`/`refreshed_at`/`as_of`/`stale_accounts`[/`symbol_summary`]),
not treat the whole envelope as the row list.

2026-10-17 live-verified bug: a real `get_positions` call against an
8-row book returned `count: 6` (the number of ENVELOPE keys, not rows) and
`positions` was the envelope dict itself, not a flat row list. `_get()` is
mocked directly here (the lowest-level HTTP helper both tools call) rather
than the full httpx transport, since this bug is purely about unwrapping
the already-parsed JSON body — see test_mcp_token_provider.py for the
fuller HTTP-mock convention used for auth-layer tests.
"""
from __future__ import annotations

import pytest

import backend.mcp.kite_server as kite_server


async def _async_ret(value):
    """`_get`/`_post` are async — a plain lambda returning a value directly
    can't be awaited. Monkeypatch sites pass a lambda that calls this."""
    return value


_ENVELOPE = {
    "rows": [
        {"account": "ZG0790", "tradingsymbol": "NIFTY26OCT25000CE", "quantity": 75},
        {"account": "ZG0790", "tradingsymbol": "NIFTY26OCT25500PE", "quantity": -75},
        {"account": "ZJ6294", "tradingsymbol": "CRUDEOIL26OCTFUT", "quantity": 10},
    ],
    "summary": [{"account": "ZG0790"}, {"account": "ZJ6294"}],
    "refreshed_at": "2026-10-17T06:00:00Z",
    "as_of": None,
    "stale_accounts": [],
    "symbol_summary": [],
}


@pytest.mark.asyncio
async def test_get_positions_unwraps_rows_not_envelope_keys(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret(_ENVELOPE))
    out = await kite_server.get_positions()
    assert out["count"] == 3
    assert out["positions"] == _ENVELOPE["rows"]


@pytest.mark.asyncio
async def test_get_positions_account_filter_applies_to_unwrapped_rows(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret(_ENVELOPE))
    out = await kite_server.get_positions(account="zg0790")
    assert out["count"] == 2
    assert all(r["account"] == "ZG0790" for r in out["positions"])


@pytest.mark.asyncio
async def test_get_positions_handles_missing_rows_key_without_throwing(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret({}))
    out = await kite_server.get_positions()
    assert out == {"positions": [], "count": 0}


@pytest.mark.asyncio
async def test_get_positions_handles_none_response_without_throwing(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret(None))
    out = await kite_server.get_positions()
    assert out == {"positions": [], "count": 0}


_HOLDINGS_ENVELOPE = {
    "rows": [
        {"account": "ZG0790", "tradingsymbol": "RELIANCE", "quantity": 10},
        {"account": "ZJ6294", "tradingsymbol": "TCS", "quantity": 5},
    ],
    "summary": [{"account": "ZG0790"}],
    "refreshed_at": "2026-10-17T06:00:00Z",
    "as_of": None,
    "stale_accounts": [],
}


@pytest.mark.asyncio
async def test_get_holdings_unwraps_rows_not_envelope_keys(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret(_HOLDINGS_ENVELOPE))
    out = await kite_server.get_holdings()
    assert out["count"] == 2
    assert out["holdings"] == _HOLDINGS_ENVELOPE["rows"]


@pytest.mark.asyncio
async def test_get_holdings_account_filter_applies_to_unwrapped_rows(monkeypatch):
    monkeypatch.setattr(kite_server, "_get", lambda path, params=None: _async_ret(_HOLDINGS_ENVELOPE))
    out = await kite_server.get_holdings(account="ZJ6294")
    assert out["count"] == 1
    assert out["holdings"][0]["tradingsymbol"] == "TCS"
