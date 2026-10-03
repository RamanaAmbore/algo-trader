"""Sprint 2a fix — paper-chase Kill no longer mislabeled as an MCP action.

`PaperTradeEngine._safe_update_algo_order_cancel` (paper.py) used to
hard-code `payload={"source": "mcp"}` and "... via MCP" in its cancel
event + AlgoOrder.detail string regardless of which caller invoked it.
It's called from BOTH the real MCP cancel path
(`research.py:_res_cancel_paper`) AND the ordinary operator Kill button
(`orders.py:_rco_kill_paper_mode` → `cancel_paper_order`), so every
manual Kill was indistinguishable from an MCP-initiated cancel in the
event log.

Fix: `cancel_paper_order(algo_order_id, *, source="operator")` threads
the real caller through to `_safe_update_algo_order_cancel` and
`_paper_cancel_fanout`. The MCP route now explicitly passes
`source="mcp"`; the operator Kill path (default kwarg) gets
`source="operator"`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _StubQuoteSource:
    def bid_ask_for_order(self, order):
        return 99.0, 99.0

    def prefetch_for(self, orders):
        pass

    def on_fill(self, order):
        pass


def _make_order(order_id: int = 1) -> dict:
    return {
        "algo_order_id": order_id,
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 50,
        "limit_price": 100.0,
        "exchange": "NFO",
        "agent_slug": "test-agent",
        "action_type": "place_order",
        "status": "OPEN",
        "attempts": 0,
    }


def _make_algo_order_row(order: dict) -> MagicMock:
    row = MagicMock()
    row.id = order["algo_order_id"]
    row.status = "OPEN"
    row.attempts = 0
    row.detail = ""
    return row


def _patch_db(row: MagicMock):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row))
    )
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_session)


# ─────────────────────────────────────────────────────────────────────────
# 1. cancel_paper_order threads `source` through to the DB-update coroutine
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_paper_order_default_source_is_operator():
    """The ordinary Kill button (orders.py's _rco_kill_paper_mode) calls
    cancel_paper_order(row.id) with no source kwarg — must default to
    'operator', not 'mcp'."""
    from backend.api.algo.paper import PaperTradeEngine

    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")
    order = _make_order(order_id=10)
    engine._open_orders = [dict(order)]

    captured_kwargs = {}

    async def _fake_db_update(order_dict, *, source="operator"):
        captured_kwargs["source"] = source

    with patch.object(engine, "_safe_update_algo_order_cancel", _fake_db_update):
        result = engine.cancel_paper_order(10)
        await __import__("asyncio").sleep(0.05)

    assert result is True
    assert captured_kwargs["source"] == "operator"


@pytest.mark.asyncio
async def test_cancel_paper_order_mcp_source_threaded_through():
    """The MCP cancel_order route explicitly passes source='mcp'."""
    from backend.api.algo.paper import PaperTradeEngine

    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")
    order = _make_order(order_id=11)
    engine._open_orders = [dict(order)]

    captured_kwargs = {}

    async def _fake_db_update(order_dict, *, source="operator"):
        captured_kwargs["source"] = source

    with patch.object(engine, "_safe_update_algo_order_cancel", _fake_db_update):
        result = engine.cancel_paper_order(11, source="mcp")
        await __import__("asyncio").sleep(0.05)

    assert result is True
    assert captured_kwargs["source"] == "mcp"


@pytest.mark.asyncio
async def test_cancel_paper_order_default_event_note_not_mislabeled_as_mcp():
    """The synchronous _record_event note built inside cancel_paper_order
    itself must not say 'via MCP' for the default (operator) path."""
    from backend.api.algo.paper import PaperTradeEngine

    captured_events = []
    engine = PaperTradeEngine(
        quote_source=_StubQuoteSource(), label="test",
        on_event=lambda evt: captured_events.append(evt),
    )
    order = _make_order(order_id=12)
    engine._open_orders = [dict(order)]

    with patch.object(engine, "_safe_update_algo_order_cancel", AsyncMock()):
        engine.cancel_paper_order(12)
        await __import__("asyncio").sleep(0.05)

    assert len(captured_events) == 1
    note = captured_events[0]["note"]
    assert "via MCP" not in note, (
        f"operator-initiated Kill must not be labeled 'via MCP', got: {note!r}"
    )


@pytest.mark.asyncio
async def test_cancel_paper_order_mcp_event_note_still_says_via_mcp():
    """A genuine MCP-initiated cancel must still say 'via MCP'."""
    from backend.api.algo.paper import PaperTradeEngine

    captured_events = []
    engine = PaperTradeEngine(
        quote_source=_StubQuoteSource(), label="test",
        on_event=lambda evt: captured_events.append(evt),
    )
    order = _make_order(order_id=13)
    engine._open_orders = [dict(order)]

    with patch.object(engine, "_safe_update_algo_order_cancel", AsyncMock()):
        engine.cancel_paper_order(13, source="mcp")
        await __import__("asyncio").sleep(0.05)

    assert len(captured_events) == 1
    note = captured_events[0]["note"]
    assert "via MCP" in note


# ─────────────────────────────────────────────────────────────────────────
# 2. _safe_update_algo_order_cancel — DB detail / event payload / fanout
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safe_update_cancel_operator_source_labels_correctly():
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=55)
    row = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    write_event_mock = AsyncMock()
    fanout_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", write_event_mock),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._safe_update_algo_order_cancel(order, source="operator")

    assert "via MCP" not in row.detail, (
        f"operator Kill must not stamp 'via MCP' into AlgoOrder.detail, got: {row.detail!r}"
    )
    write_event_mock.assert_called_once()
    _, _, message = write_event_mock.call_args.args
    assert "via MCP" not in message
    assert write_event_mock.call_args.kwargs["payload"] == {"source": "operator"}

    fanout_mock.assert_called_once()
    assert "via MCP" not in fanout_mock.call_args.kwargs["status_message"]


@pytest.mark.asyncio
async def test_safe_update_cancel_mcp_source_still_labels_via_mcp():
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=56)
    row = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    write_event_mock = AsyncMock()
    fanout_mock = MagicMock()

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", write_event_mock),
        patch("backend.api.routes.orders._postback_broadcast_fanout", fanout_mock),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._safe_update_algo_order_cancel(order, source="mcp")

    assert "via MCP" in row.detail
    write_event_mock.assert_called_once()
    _, _, message = write_event_mock.call_args.args
    assert "via MCP" in message
    assert write_event_mock.call_args.kwargs["payload"] == {"source": "mcp"}

    fanout_mock.assert_called_once()
    assert "via MCP" in fanout_mock.call_args.kwargs["status_message"]


@pytest.mark.asyncio
async def test_safe_update_cancel_default_source_is_operator():
    """Calling without an explicit source kwarg (defensive — real callers
    always pass it explicitly via cancel_paper_order) still defaults safe,
    not to 'mcp'."""
    from backend.api.algo.paper import PaperTradeEngine

    order = _make_order(order_id=57)
    row = _make_algo_order_row(order)
    engine = PaperTradeEngine(quote_source=_StubQuoteSource(), label="test")

    with (
        patch("backend.api.database.async_session", _patch_db(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", MagicMock()),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        await engine._safe_update_algo_order_cancel(order)

    assert "via MCP" not in row.detail


# ─────────────────────────────────────────────────────────────────────────
# 3. MCP route call site explicitly passes source="mcp"
# ─────────────────────────────────────────────────────────────────────────

def test_mcp_cancel_route_passes_source_mcp_explicitly():
    import inspect
    from backend.api.routes import research as research_mod

    source = inspect.getsource(research_mod._res_cancel_paper)
    assert 'cancel_paper_order(algo_order_id, source="mcp")' in source, (
        "the MCP cancel_order route must explicitly pass source=\"mcp\" to "
        "cancel_paper_order so the event log correctly distinguishes it "
        "from an ordinary operator Kill"
    )
