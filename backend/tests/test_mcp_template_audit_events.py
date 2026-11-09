"""Tests for the MCP mcp_request_id audit-to-order linkage, and the
get_order_events MCP tool.

Covers two narrow gaps closed in one sprint:
  1. AlgoOrder.mcp_request_id links an mcp_audit row to the order it
     created — additive migration + threading through both ticket-persist
     code paths (live + paper).
  2. backend.mcp.kite_server.get_order_events wraps the existing
     GET /api/orders/{id}/events endpoint — a thin HTTP passthrough, same
     as every other read-only tool in that file.

(The original item 1 — MCP place_order's `template_slug` support — was
removed along with the parameter itself; Bracket/Template-GTT removal,
T3.6, 2026-11. See test_bracket_removal_t3_6.py for its own-absence
coverage.)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.database as dbmod


# Section 1 (_res_resolve_template_slug + end-to-end MCP place_order
# template_slug coverage) removed entirely (Bracket/Template-GTT
# removal, T3.6, 2026-11) — `_res_resolve_template_slug`, `PlaceOrderRequest
# .template_slug`, and `TicketOrderRequest.template_id` were all removed
# in this sub-sprint. See test_bracket_removal_t3_6.py for the
# replacement coverage (confirms the parameter/attribute/function no
# longer exist, and that `McpController.place_order` still forwards
# mcp_request_id correctly with no template concept at all).


# ═══════════════════════════════════════════════════════════════════════════
# 1. Migration — additive, idempotent
# ═══════════════════════════════════════════════════════════════════════════

class _FakeResult:
    def fetchone(self): return None
    def fetchall(self): return []
    def scalar(self): return None
    def scalar_one_or_none(self): return None
    def all(self): return []
    def first(self): return None
    def scalars(self): return self
    def __iter__(self): return iter(())


class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult()

    async def run_sync(self, fn, *a, **k):
        return None


def test_migrate_algo_orders_mcp_request_id_statement_shape():
    """The migration step issues a plain ADD COLUMN IF NOT EXISTS under a
    bounded lock_timeout — no CONCURRENTLY (would crash inside init_db's
    transaction)."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_algo_orders_mcp_request_id(conn))

    assert not any("CONCURRENTLY" in s for s in conn.executed)
    joined = "\n".join(conn.executed)
    assert "ADD COLUMN IF NOT EXISTS" in joined
    assert "mcp_request_id" in joined
    assert "algo_orders" in joined
    assert any("lock_timeout" in s for s in conn.executed)


def test_migrate_algo_orders_mcp_request_id_idempotent_rerun():
    """Running the step twice (fresh DB + already-migrated DB) never
    raises — IF NOT EXISTS makes every statement safely re-runnable."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_algo_orders_mcp_request_id(conn))
    asyncio.run(dbmod._migrate_algo_orders_mcp_request_id(conn))  # must not raise
    assert len([s for s in conn.executed if "mcp_request_id" in s]) == 2


def test_init_db_calls_mcp_request_id_migration_inside_begin_block():
    """Structural guard: init_db's source must call
    _migrate_algo_orders_mcp_request_id(conn) INSIDE the engine.begin()
    block, after the sprint1a columns step."""
    import inspect
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()

    begin_idx = next(i for i, l in enumerate(lines) if "engine.begin() as conn:" in l)
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    sprint1a_idx = next(
        i for i, l in enumerate(lines) if "_migrate_algo_orders_sprint1a_columns(conn)" in l
    )
    mcp_idx = next(
        i for i, l in enumerate(lines) if "_migrate_algo_orders_mcp_request_id(conn)" in l
    )
    mcp_indent = len(lines[mcp_idx]) - len(lines[mcp_idx].lstrip())

    assert mcp_idx > sprint1a_idx, "mcp_request_id migration must run after sprint1a columns"
    assert mcp_indent > begin_indent, "mcp_request_id migration must run INSIDE engine.begin()"


# ═══════════════════════════════════════════════════════════════════════════
# 2. mcp_request_id persists end-to-end on both ticket-persist paths
# ═══════════════════════════════════════════════════════════════════════════

class _FakeAlgoOrderRow:
    """Captures AlgoOrder(**kwargs) construction without touching a DB."""
    _captured: dict = {}

    def __init__(self, **kwargs):
        _FakeAlgoOrderRow._captured.clear()
        _FakeAlgoOrderRow._captured.update(kwargs)
        self.id = 4242


class _FakeAsyncSession:
    def __init__(self):
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass

    async def execute(self, *a, **k):
        return _FakeResult()


@pytest.mark.asyncio
async def test_ticket_persist_live_algo_order_carries_mcp_request_id():
    from backend.api.routes.orders_place import _ticket_persist_live_algo_order
    from backend.api.schemas import TicketOrderRequest

    data = TicketOrderRequest(
        mode="live", side="BUY", tradingsymbol="NIFTY25OCTFUT", quantity=1,
        account="ZG0790", source="mcp", mcp_request_id="abc123def456",
    )
    fake_request = MagicMock()
    fake_request.scope = {"state": {}}  # no request_id → skip idempotency branch

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeAsyncSession()), \
         patch("backend.api.models.AlgoOrder", side_effect=_FakeAlgoOrderRow), \
         patch("backend.api.algo.agent_engine.get_agent_id_by_slug", new=AsyncMock(return_value=1)):
        row_id = await _ticket_persist_live_algo_order(
            data, fake_request, "ZG0790", "NIFTY25OCTFUT", "BUY", 50,
        )

    assert row_id == 4242
    assert _FakeAlgoOrderRow._captured.get("mcp_request_id") == "abc123def456"
    assert _FakeAlgoOrderRow._captured.get("source") == "mcp"


@pytest.mark.asyncio
async def test_opp_paper_persist_row_carries_mcp_request_id():
    from backend.api.routes.orders_place import _opp_paper_persist_row
    from backend.api.schemas import TicketOrderRequest

    data = TicketOrderRequest(
        mode="paper", side="SELL", tradingsymbol="NIFTY25OCTFUT", quantity=1,
        account="ZG0790", source="mcp", mcp_request_id="feedface0001",
    )
    fake_request = MagicMock()
    fake_request.scope = {"state": {}}

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeAsyncSession()), \
         patch("backend.api.models.AlgoOrder", side_effect=_FakeAlgoOrderRow), \
         patch("backend.api.algo.agent_engine.get_agent_id_by_slug", new=AsyncMock(return_value=1)):
        row_id = await _opp_paper_persist_row(
            data, fake_request, "ZG0790", "NIFTY25OCTFUT", "SELL", 50,
        )

    assert row_id == 4242
    assert _FakeAlgoOrderRow._captured.get("mcp_request_id") == "feedface0001"
    assert _FakeAlgoOrderRow._captured.get("source") == "mcp"


@pytest.mark.asyncio
async def test_ticket_persist_omits_mcp_request_id_for_non_mcp_ticket():
    """Baseline (unchanged) behaviour: a normal operator ticket (no
    mcp_request_id) persists NULL — never a stray default."""
    from backend.api.routes.orders_place import _ticket_persist_live_algo_order
    from backend.api.schemas import TicketOrderRequest

    data = TicketOrderRequest(
        mode="live", side="BUY", tradingsymbol="NIFTY25OCTFUT", quantity=1,
        account="ZG0790",
    )
    fake_request = MagicMock()
    fake_request.scope = {"state": {}}

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeAsyncSession()), \
         patch("backend.api.models.AlgoOrder", side_effect=_FakeAlgoOrderRow), \
         patch("backend.api.algo.agent_engine.get_agent_id_by_slug", new=AsyncMock(return_value=1)):
        await _ticket_persist_live_algo_order(
            data, fake_request, "ZG0790", "NIFTY25OCTFUT", "BUY", 50,
        )

    assert _FakeAlgoOrderRow._captured.get("mcp_request_id") is None
    assert _FakeAlgoOrderRow._captured.get("source") == "ticket"


# ═══════════════════════════════════════════════════════════════════════════
# 3. get_order_events MCP tool — thin HTTP passthrough
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_order_events_calls_rest_endpoint_and_passes_through():
    """get_order_events must hit the SAME REST endpoint
    (GET /api/orders/{id}/events) the OrderBook UI uses, and return its
    JSON unmodified (same shape as the REST handler's own response)."""
    import backend.mcp.kite_server as ks

    fake_rows = [
        {"id": 1, "order_id": 99, "ts": "2026-10-04T08:00:00+00:00",
         "kind": "fill", "message": "filled", "payload_json": None},
        {"id": 2, "order_id": 99, "ts": "2026-10-04T08:00:05+00:00",
         "kind": "template_attach", "message": "attached", "payload_json": "{}"},
    ]

    with patch.object(ks, "_get", new=AsyncMock(return_value=fake_rows)) as mock_get:
        result = await ks.get_order_events(99)

    mock_get.assert_awaited_once_with("/api/orders/99/events")
    assert result["events"] == fake_rows
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_get_order_events_empty_result_shape():
    """No events yet (brand-new order) → empty list, count 0, no crash."""
    import backend.mcp.kite_server as ks

    with patch.object(ks, "_get", new=AsyncMock(return_value=[])):
        result = await ks.get_order_events(1)

    assert result == {"events": [], "count": 0}


@pytest.mark.asyncio
async def test_get_order_events_matches_rest_handler_shape_for_known_order():
    """Cross-check against the real REST handler's own return shape
    (AlgoOrderEventInfo fields) for a known order id, with the DB layer
    mocked — confirms get_order_events' dict keys (id/order_id/ts/kind/
    message/payload_json) line up with what the handler actually emits."""
    from backend.api.routes.orders import OrdersController
    from backend.api.routes.orders_helpers import AlgoOrderEventInfo

    class _FakeEventRow:
        def __init__(self, id, order_id, ts, kind, message, payload_json):
            self.id = id
            self.order_id = order_id
            self.ts = ts
            self.kind = kind
            self.message = message
            self.payload_json = payload_json

    import datetime as _dt
    fake_rows = [
        _FakeEventRow(1, 99, _dt.datetime(2026, 10, 4, 8, 0, 0, tzinfo=_dt.timezone.utc),
                      "fill", "filled", None),
    ]

    class _FakeScalars:
        def __init__(self, rows):
            self._rows = rows
        def all(self):
            return self._rows

    class _FakeExecResult:
        def __init__(self, rows):
            self._rows = rows
        def scalars(self):
            return _FakeScalars(self._rows)

    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def execute(self, stmt):
            return _FakeExecResult(fake_rows)

    ctrl = OrdersController(owner=None)
    fake_request = MagicMock()

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeSession()), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        rest_result = await OrdersController.order_events.fn(
            ctrl, order_id=99, request=fake_request,
        )

    assert isinstance(rest_result, list) and len(rest_result) == 1
    assert isinstance(rest_result[0], AlgoOrderEventInfo)

    # get_order_events' passthrough dict keys match these REST field names.
    rest_keys = set(AlgoOrderEventInfo.__struct_fields__)
    assert rest_keys == {"id", "order_id", "ts", "kind", "message", "payload_json"}
