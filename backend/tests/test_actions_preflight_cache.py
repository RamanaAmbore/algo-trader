"""R1 regression test — `run_preflight`'s instruments(exchange) fetch
must be cached behind a short TTL instead of downloading the full
exchange dump on every call (every debounced margin-preview keystroke
AND every ticket placement, pre-fix).

Uses the same broker-stub pattern as `test_preflight.py`. Relies on
the `_reset_preflight_instruments_cache` autouse fixture in
`conftest.py` to purge the `preflight_instr:` cache keys between tests
(test isolation — several preflight test modules reuse the same
account/exchange with different mocked instruments() content).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_broker_stub() -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    broker.basket_order_margins.return_value = [{"initial": {"total": 10000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500000.0},
        "commodity": {"enabled": True, "net": 500000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _conns_with(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def test_preflight_trim_instruments_keeps_only_needed_fields():
    """R1 memory-safety follow-up: the cached value must be trimmed to
    only tradingsymbol/freeze_qty/lot_size — caching the full raw
    exchange dump (tens of thousands of rows × ~20 keys, never
    proactively evicted by `get_or_fetch`) would grow unbounded memory
    the same way the untrimmed instruments cache did pre-OOM-fix."""
    from backend.api.algo.actions_preflight import _preflight_trim_instruments

    raw = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange": "NFO",
        "instrument_token": 12345,
        "instrument_type": "FUT",
        "segment": "NFO-FUT",
        "freeze_qty": 6000,
        "lot_size": 50,
        "tick_size": 0.05,
        "expiry": "2025-04-24",
        "name": "NIFTY",
    }]

    trimmed = _preflight_trim_instruments(raw)

    assert trimmed == [{"tradingsymbol": "NIFTY25APRFUT", "freeze_qty": 6000, "lot_size": 50}]
    assert set(trimmed[0].keys()) == {"tradingsymbol", "freeze_qty", "lot_size"}, (
        "extra broker fields (instrument_token, segment, tick_size, "
        "expiry, name, ...) must not be retained in the cached value"
    )


def test_preflight_trim_instruments_empty_and_none_safe():
    from backend.api.algo.actions_preflight import _preflight_trim_instruments

    assert _preflight_trim_instruments(None) == []
    assert _preflight_trim_instruments([]) == []


@pytest.mark.asyncio
async def test_r1_preflight_instruments_fetch_cached_within_ttl():
    """Two run_preflight calls for the same account/exchange within the
    5s TTL window must hit the broker's instruments() endpoint exactly
    once — the second call is served from the `preflight_instr:` cache."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    conns = _conns_with("ZG0790")
    order = {
        "exchange":      "NFO",
        "tradingsymbol": "NIFTY25APRFUT",
        "quantity":      50,
        "order_type":    "LIMIT",
        "product":       "NRML",
        "variety":       "regular",
        "side":          "BUY",
        "price":         22000.0,
    }

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)):
        r1 = await run_preflight("ZG0790", order)
        r2 = await run_preflight("ZG0790", order)

    assert r1["ok"] is True
    assert r2["ok"] is True
    assert broker.instruments.call_count == 1, (
        f"expected exactly 1 broker.instruments() call across 2 "
        f"run_preflight calls within the TTL window (2nd is a cache "
        f"hit), got {broker.instruments.call_count} — R1 regression."
    )


@pytest.mark.asyncio
async def test_r1_preflight_instruments_cache_keyed_per_exchange():
    """Different exchanges must NOT share a cache entry — each gets its
    own broker.instruments(exchange) call the first time it's seen."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub()
    broker.instruments.side_effect = lambda exchange=None: [{
        "tradingsymbol": "NIFTY25APRFUT" if exchange == "NFO" else "CRUDEOIL25AUGFUT",
        "exchange":      exchange,
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    conns = _conns_with("ZG0790")

    nfo_order = {
        # quantity=100 matches the get_lot_size=100 mock below (a valid
        # 1-lot multiple) so the G1 lot-multiple guard doesn't
        # short-circuit before the instruments() fan-out fires.
        "exchange": "NFO", "tradingsymbol": "NIFTY25APRFUT", "quantity": 100,
        "order_type": "LIMIT", "product": "NRML", "variety": "regular",
        "side": "BUY", "price": 22000.0,
    }
    mcx_order = {
        "exchange": "MCX", "tradingsymbol": "CRUDEOIL25AUGFUT", "quantity": 100,
        "order_type": "LIMIT", "product": "NRML", "variety": "regular",
        "side": "BUY", "price": 5000.0,
    }

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        await run_preflight("ZG0790", nfo_order)
        await run_preflight("ZG0790", mcx_order)

    assert broker.instruments.call_count == 2, (
        "NFO and MCX must each get their own cache entry (keyed "
        "account:exchange) — a shared key would serve MCX's freeze_qty "
        "check with NFO's dump or vice versa."
    )
