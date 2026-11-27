"""get_order_history() / get_trade_history() / get_funds_history() /
get_realized_pnl_history() / get_historical_tradebook() /
get_historical_ledger() / get_pnl_summary() (backend/mcp/kite_server.py)
— MCP wrappers over the seven `/api/admin/history/*` endpoints
(backend/api/routes/history.py). `_get` is mocked directly here, same
convention as test_mcp_positions_holdings_envelope.py — these MCP
tools are thin param-building + light-reshaping wrappers, so the
contract worth pinning is: which path is called, which params are
forwarded (and which are omitted when not passed), and that a
missing/None/empty upstream response degrades to an empty-but-well-
shaped result rather than throwing.
"""
from __future__ import annotations

import pytest

import backend.mcp.kite_server as kite_server


async def _async_ret(value):
    return value


def _capture(monkeypatch, response):
    """Patches kite_server._get to record (path, params) and return
    `response`. Returns the mutable `calls` list the test inspects."""
    calls: list[tuple[str, dict]] = []

    async def _fake_get(path, params=None):
        calls.append((path, params or {}))
        return response

    monkeypatch.setattr(kite_server, "_get", _fake_get)
    return calls


# ── get_order_history ──────────────────────────────────────────────────

_ORDER_HISTORY_RESPONSE = {
    "rows": [
        {"id": 101, "created_at": "2026-11-20T09:15:00+00:00", "account": "ZG0790",
         "symbol": "GOLDM26DECFUT", "exchange": "MCX", "transaction_type": "SELL",
         "quantity": 100, "filled_quantity": 100, "initial_price": 75000.0,
         "fill_price": 75010.0, "slippage": -10.0, "status": "FILLED",
         "mode": "live", "engine": "chase", "broker_order_id": "250001",
         "request_id": "req-1", "detail": None},
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
    "counts": {"FILLED": 1},
}


@pytest.mark.asyncio
async def test_get_order_history_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _ORDER_HISTORY_RESPONSE)
    await kite_server.get_order_history()
    assert calls[0][0] == "/api/admin/history/orders"


@pytest.mark.asyncio
async def test_get_order_history_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _ORDER_HISTORY_RESPONSE)
    out = await kite_server.get_order_history()
    assert out == _ORDER_HISTORY_RESPONSE


@pytest.mark.asyncio
async def test_get_order_history_forwards_all_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _ORDER_HISTORY_RESPONSE)
    await kite_server.get_order_history(
        from_date="2026-11-01", to_date="2026-11-20", accounts="ZG0790,DH3747",
        symbols="GOLDM26DECFUT", status="FILLED", mode="live", limit=10, offset=5,
    )
    params = calls[0][1]
    assert params["from_date"] == "2026-11-01"
    assert params["to_date"] == "2026-11-20"
    assert params["accounts"] == "ZG0790,DH3747"
    assert params["symbols"] == "GOLDM26DECFUT"
    assert params["status"] == "FILLED"
    assert params["mode"] == "live"
    assert params["limit"] == 10
    assert params["offset"] == 5


@pytest.mark.asyncio
async def test_get_order_history_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _ORDER_HISTORY_RESPONSE)
    await kite_server.get_order_history()
    params = calls[0][1]
    for key in ("from_date", "to_date", "accounts", "symbols", "status", "mode"):
        assert key not in params, f"{key} should be omitted when not passed"
    assert params["limit"] == 50
    assert params["offset"] == 0


@pytest.mark.asyncio
async def test_get_order_history_limit_is_clamped_to_500(monkeypatch):
    calls = _capture(monkeypatch, _ORDER_HISTORY_RESPONSE)
    await kite_server.get_order_history(limit=9999)
    assert calls[0][1]["limit"] == 500


@pytest.mark.asyncio
async def test_get_order_history_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_order_history()
    assert out == {"rows": [], "total": 0, "limit": 50, "offset": 0, "counts": {}}


# ── get_trade_history ───────────────────────────────────────────────────

_TRADE_HISTORY_RESPONSE = {
    "rows": [
        {"date": "2026-11-20", "account": "ZG0790", "segment": "commodity",
         "symbol": "GOLDM26DECFUT", "exchange": "MCX", "qty": 100,
         "avg_cost": 75000.0, "notional": 7500000.0, "captured_at": "2026-11-20T23:45:00+00:00"},
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
    "summary": {"total_notional": 7500000.0},
}


@pytest.mark.asyncio
async def test_get_trade_history_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _TRADE_HISTORY_RESPONSE)
    await kite_server.get_trade_history()
    assert calls[0][0] == "/api/admin/history/trades"


@pytest.mark.asyncio
async def test_get_trade_history_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _TRADE_HISTORY_RESPONSE)
    out = await kite_server.get_trade_history()
    assert out == _TRADE_HISTORY_RESPONSE


@pytest.mark.asyncio
async def test_get_trade_history_forwards_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _TRADE_HISTORY_RESPONSE)
    await kite_server.get_trade_history(
        from_date="2026-11-01", to_date="2026-11-20", accounts="ZG0790",
        symbols="GOLDM26DECFUT", limit=25, offset=10,
    )
    params = calls[0][1]
    assert params["from_date"] == "2026-11-01"
    assert params["to_date"] == "2026-11-20"
    assert params["accounts"] == "ZG0790"
    assert params["symbols"] == "GOLDM26DECFUT"
    assert params["limit"] == 25
    assert params["offset"] == 10


@pytest.mark.asyncio
async def test_get_trade_history_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _TRADE_HISTORY_RESPONSE)
    await kite_server.get_trade_history()
    params = calls[0][1]
    for key in ("from_date", "to_date", "accounts", "symbols"):
        assert key not in params


@pytest.mark.asyncio
async def test_get_trade_history_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_trade_history()
    assert out == {"rows": [], "total": 0, "limit": 50, "offset": 0, "summary": {}}


# ── get_funds_history ───────────────────────────────────────────────────

_FUNDS_HISTORY_RESPONSE = {
    "rows": [
        {"date": "2026-11-20", "account": "ZG0790", "segment": "commodity",
         "cash_available": 100000.0, "opening_balance": 95000.0,
         "debits_today": 2, "realised_m2m": 5000.0, "net": 100000.0,
         "cash_delta": 5000.0},
    ],
    "total": 1,
    "earliest_date": "2026-06-01",
}


@pytest.mark.asyncio
async def test_get_funds_history_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _FUNDS_HISTORY_RESPONSE)
    await kite_server.get_funds_history()
    assert calls[0][0] == "/api/admin/history/funds"


@pytest.mark.asyncio
async def test_get_funds_history_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _FUNDS_HISTORY_RESPONSE)
    out = await kite_server.get_funds_history()
    assert out == _FUNDS_HISTORY_RESPONSE


@pytest.mark.asyncio
async def test_get_funds_history_forwards_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _FUNDS_HISTORY_RESPONSE)
    await kite_server.get_funds_history(
        from_date="2026-09-01", to_date="2026-11-20", accounts="ZG0790,DH3747",
    )
    params = calls[0][1]
    assert params == {"from_date": "2026-09-01", "to_date": "2026-11-20",
                       "accounts": "ZG0790,DH3747"}


@pytest.mark.asyncio
async def test_get_funds_history_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _FUNDS_HISTORY_RESPONSE)
    await kite_server.get_funds_history()
    assert calls[0][1] == {}


@pytest.mark.asyncio
async def test_get_funds_history_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_funds_history()
    assert out == {"rows": [], "total": 0, "earliest_date": None}


# ── get_realized_pnl_history ────────────────────────────────────────────
# Unlike the other three, the real endpoint returns a bare list, not a
# {rows, total, ...} wrapper (per history.py's own docstring on
# list_realized_pnl) — the MCP tool wraps it into {rows, count} for a
# consistent tool-call shape, mirroring get_audit_recent/get_order_events.

_REALIZED_PNL_ROWS = [
    {"account": "Z1####", "segment": "commodities", "instrument_type": "option",
     "symbol_root": "GOLDM", "expiry": "2026-12-24", "realized_pnl": 45000.0},
    {"account": "Z1####", "segment": "equity", "instrument_type": "equity",
     "symbol_root": "RELIANCE", "expiry": None, "realized_pnl": 1200.5},
]


@pytest.mark.asyncio
async def test_get_realized_pnl_history_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _REALIZED_PNL_ROWS)
    await kite_server.get_realized_pnl_history()
    assert calls[0][0] == "/api/admin/history/realized-pnl"


@pytest.mark.asyncio
async def test_get_realized_pnl_history_wraps_bare_list_into_rows_and_count(monkeypatch):
    _capture(monkeypatch, _REALIZED_PNL_ROWS)
    out = await kite_server.get_realized_pnl_history()
    assert out == {"rows": _REALIZED_PNL_ROWS, "count": 2}


@pytest.mark.asyncio
async def test_get_realized_pnl_history_forwards_all_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _REALIZED_PNL_ROWS)
    await kite_server.get_realized_pnl_history(
        account="ZG0790", segment="commodities", instrument_type="option",
        symbol_root="GOLDM", expiry_from="2026-01-01", expiry_to="2026-12-31",
    )
    params = calls[0][1]
    assert params == {
        "account": "ZG0790", "segment": "commodities", "instrument_type": "option",
        "symbol_root": "GOLDM", "expiry_from": "2026-01-01", "expiry_to": "2026-12-31",
    }


@pytest.mark.asyncio
async def test_get_realized_pnl_history_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _REALIZED_PNL_ROWS)
    await kite_server.get_realized_pnl_history()
    assert calls[0][1] == {}


@pytest.mark.asyncio
async def test_get_realized_pnl_history_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_realized_pnl_history()
    assert out == {"rows": [], "count": 0}


@pytest.mark.asyncio
async def test_get_realized_pnl_history_handles_empty_list_without_throwing(monkeypatch):
    _capture(monkeypatch, [])
    out = await kite_server.get_realized_pnl_history()
    assert out == {"rows": [], "count": 0}


@pytest.mark.asyncio
async def test_get_realized_pnl_history_forwards_broker_when_set(monkeypatch):
    calls = _capture(monkeypatch, _REALIZED_PNL_ROWS)
    await kite_server.get_realized_pnl_history(broker="kite")
    assert calls[0][1]["broker"] == "kite"


@pytest.mark.asyncio
async def test_get_realized_pnl_history_omits_broker_when_unset(monkeypatch):
    calls = _capture(monkeypatch, _REALIZED_PNL_ROWS)
    await kite_server.get_realized_pnl_history()
    assert "broker" not in calls[0][1]


# ── get_historical_tradebook ────────────────────────────────────────────

_TRADEBOOK_RESPONSE = {
    "rows": [
        {"account": "ZJ####", "broker": "kite", "segment": "commodities",
         "symbol": "CRUDEOIL26APR10500CE", "exchange": "MCX",
         "instrument_type": "option", "symbol_root": "CRUDEOIL",
         "expiry": "2026-04-24", "console_expiry": "2026-04-16",
         "trade_date": "2026-04-02", "trade_id": "150707579",
         "order_id": "609215344302031", "buy_qty": 100.0, "sell_qty": None,
         "buy_value": 104240.0, "sell_value": None, "price": 1042.4,
         "trade_type": "buy", "order_execution_time": "2026-04-02T18:43:32"},
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


@pytest.mark.asyncio
async def test_get_historical_tradebook_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _TRADEBOOK_RESPONSE)
    await kite_server.get_historical_tradebook()
    assert calls[0][0] == "/api/admin/history/tradebook"


@pytest.mark.asyncio
async def test_get_historical_tradebook_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _TRADEBOOK_RESPONSE)
    out = await kite_server.get_historical_tradebook()
    assert out == _TRADEBOOK_RESPONSE


@pytest.mark.asyncio
async def test_get_historical_tradebook_forwards_all_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _TRADEBOOK_RESPONSE)
    await kite_server.get_historical_tradebook(
        from_date="2026-04-01", to_date="2026-04-30", account="ZJ0790",
        segment="commodities", instrument_type="option", symbol_root="CRUDEOIL",
        exchange="MCX", broker="kite", limit=25, offset=5,
    )
    params = calls[0][1]
    assert params == {
        "from_date": "2026-04-01", "to_date": "2026-04-30", "account": "ZJ0790",
        "segment": "commodities", "instrument_type": "option",
        "symbol_root": "CRUDEOIL", "exchange": "MCX", "broker": "kite",
        "limit": 25, "offset": 5,
    }


@pytest.mark.asyncio
async def test_get_historical_tradebook_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _TRADEBOOK_RESPONSE)
    await kite_server.get_historical_tradebook()
    params = calls[0][1]
    for key in ("from_date", "to_date", "account", "segment", "instrument_type",
                "symbol_root", "exchange", "broker"):
        assert key not in params
    assert params["limit"] == 50
    assert params["offset"] == 0


@pytest.mark.asyncio
async def test_get_historical_tradebook_limit_is_clamped_to_500(monkeypatch):
    calls = _capture(monkeypatch, _TRADEBOOK_RESPONSE)
    await kite_server.get_historical_tradebook(limit=9999)
    assert calls[0][1]["limit"] == 500


@pytest.mark.asyncio
async def test_get_historical_tradebook_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_historical_tradebook()
    assert out == {"rows": [], "total": 0, "limit": 50, "offset": 0}


# ── get_historical_ledger ───────────────────────────────────────────────

_LEDGER_RESPONSE = {
    "rows": [
        {"account": "ZJ####", "broker": "kite", "posting_date": "2026-04-01",
         "cost_center": "MCX-F&O - Z", "segment": "commodities",
         "voucher_type": "Delivery Voucher",
         "particulars": "Span margin reversed for MCX F&O",
         "debit": 0.0, "credit": 1416190.5, "net_balance": 190471.655934},
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
}


@pytest.mark.asyncio
async def test_get_historical_ledger_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _LEDGER_RESPONSE)
    await kite_server.get_historical_ledger()
    assert calls[0][0] == "/api/admin/history/ledger"


@pytest.mark.asyncio
async def test_get_historical_ledger_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _LEDGER_RESPONSE)
    out = await kite_server.get_historical_ledger()
    assert out == _LEDGER_RESPONSE


@pytest.mark.asyncio
async def test_get_historical_ledger_forwards_all_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _LEDGER_RESPONSE)
    await kite_server.get_historical_ledger(
        from_date="2026-04-01", to_date="2026-04-30", account="ZJ0790",
        segment="commodities", broker="kite", limit=10, offset=2,
    )
    params = calls[0][1]
    assert params == {
        "from_date": "2026-04-01", "to_date": "2026-04-30", "account": "ZJ0790",
        "segment": "commodities", "broker": "kite", "limit": 10, "offset": 2,
    }


@pytest.mark.asyncio
async def test_get_historical_ledger_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _LEDGER_RESPONSE)
    await kite_server.get_historical_ledger()
    params = calls[0][1]
    for key in ("from_date", "to_date", "account", "segment", "broker"):
        assert key not in params


@pytest.mark.asyncio
async def test_get_historical_ledger_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_historical_ledger()
    assert out == {"rows": [], "total": 0, "limit": 50, "offset": 0}


# ── get_pnl_summary ──────────────────────────────────────────────────────

_PNL_SUMMARY_RESPONSE = {
    "rows": [
        {"symbol_root": "GOLDM", "segment": "commodities", "trade_count": 3,
         "win_count": 1, "loss_count": 1, "flat_count": 1, "total_pnl": 30000.0,
         "win_rate": 0.3333333333333333, "avg_win": 50000.0, "avg_loss": -20000.0},
    ],
    "total": 1,
}


@pytest.mark.asyncio
async def test_get_pnl_summary_calls_the_real_endpoint_path(monkeypatch):
    calls = _capture(monkeypatch, _PNL_SUMMARY_RESPONSE)
    await kite_server.get_pnl_summary()
    assert calls[0][0] == "/api/admin/history/pnl-summary"


@pytest.mark.asyncio
async def test_get_pnl_summary_returns_response_unwrapped(monkeypatch):
    _capture(monkeypatch, _PNL_SUMMARY_RESPONSE)
    out = await kite_server.get_pnl_summary()
    assert out == _PNL_SUMMARY_RESPONSE


@pytest.mark.asyncio
async def test_get_pnl_summary_forwards_all_filters_when_set(monkeypatch):
    calls = _capture(monkeypatch, _PNL_SUMMARY_RESPONSE)
    await kite_server.get_pnl_summary(
        account="ZJ0790", segment="commodities", symbol_root="GOLDM",
        broker="kite", expiry_from="2026-01-01", expiry_to="2026-12-31",
    )
    params = calls[0][1]
    assert params == {
        "account": "ZJ0790", "segment": "commodities", "symbol_root": "GOLDM",
        "broker": "kite", "expiry_from": "2026-01-01", "expiry_to": "2026-12-31",
    }


@pytest.mark.asyncio
async def test_get_pnl_summary_omits_unset_optional_filters(monkeypatch):
    calls = _capture(monkeypatch, _PNL_SUMMARY_RESPONSE)
    await kite_server.get_pnl_summary()
    assert calls[0][1] == {}


@pytest.mark.asyncio
async def test_get_pnl_summary_handles_none_response_without_throwing(monkeypatch):
    _capture(monkeypatch, None)
    out = await kite_server.get_pnl_summary()
    assert out == {"rows": [], "total": 0}
