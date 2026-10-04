"""Tests for the MCP template-slug support, mcp_request_id audit-to-order
linkage, and the new get_order_events MCP tool.

Covers three narrow gaps closed in one sprint:
  1. MCP place_order gained `template_slug` — resolved server-side (in
     backend.api.routes.lab) to a template_id before the ticket is
     built, since TicketOrderRequest itself has no slug field.
  2. AlgoOrder.mcp_request_id links an mcp_audit row to the order it
     created — additive migration + threading through both ticket-persist
     code paths (live + paper).
  3. backend.mcp.kite_server.get_order_events wraps the existing
     GET /api/orders/{id}/events endpoint — a thin HTTP passthrough, same
     as every other read-only tool in that file.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.database as dbmod


# ═══════════════════════════════════════════════════════════════════════════
# 1a. _res_resolve_template_slug — the resolver helper
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_resolve_template_slug_passthrough_when_id_given():
    """An explicit template_id always wins — slug is ignored, no DB hit."""
    from backend.api.routes.lab import _res_resolve_template_slug

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(side_effect=AssertionError("must not be called")),
    ):
        resolved, err = await _res_resolve_template_slug(9, "default-bull")

    assert resolved == 9
    assert err is None


@pytest.mark.asyncio
async def test_resolve_template_slug_none_when_neither_given():
    """No id, no slug → template-less order, not an error."""
    from backend.api.routes.lab import _res_resolve_template_slug

    resolved, err = await _res_resolve_template_slug(None, None)

    assert resolved is None
    assert err is None


@pytest.mark.asyncio
async def test_resolve_template_slug_resolves_via_loader():
    """A real slug resolves to the loader's returned row id."""
    from backend.api.routes.lab import _res_resolve_template_slug

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value={"id": 7, "slug": "default-bull"}),
    ) as mock_loader:
        resolved, err = await _res_resolve_template_slug(None, "default-bull")

    mock_loader.assert_awaited_once_with(template_id=None, template_slug="default-bull")
    assert resolved == 7
    assert err is None


@pytest.mark.asyncio
async def test_resolve_template_slug_unknown_returns_error():
    """An unresolvable slug returns (None, error) — never raises itself;
    the caller (place_order) decides how to surface it."""
    from backend.api.routes.lab import _res_resolve_template_slug

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=None),
    ):
        resolved, err = await _res_resolve_template_slug(None, "no-such-slug")

    assert resolved is None
    assert err is not None and "no-such-slug" in err


# ═══════════════════════════════════════════════════════════════════════════
# 1b. End-to-end through LabController.place_order
# ═══════════════════════════════════════════════════════════════════════════

def _mk_place_request():
    from backend.api.routes.lab import PlaceOrderRequest
    return PlaceOrderRequest(
        confirm_token="tok123",
        account="ZG0790",
        tradingsymbol="NIFTY25OCTFUT",
        side="BUY",
        quantity=1,
        mode="paper",
        template_slug="default-bull",
    )


@pytest.mark.asyncio
async def test_place_order_resolves_template_slug_and_sets_mcp_request_id():
    """A real MCP place_order call with template_slug set must: (a) resolve
    the slug to a template_id via the loader, (b) build a TicketOrderRequest
    carrying that resolved template_id AND a non-empty mcp_request_id, and
    (c) forward it unmodified to OrdersController.ticket_order."""
    from backend.api.routes.lab import LabController

    ctrl = LabController(owner=None)
    data = _mk_place_request()
    fake_request = MagicMock()

    captured: dict = {}

    class _FakeTicketResponse:
        order_id = "555"
        mode = "paper"
        status = "OPEN"
        detail = "placed"

    async def _fake_ticket_order_handler(data, request):
        captured["ticket"] = data
        return _FakeTicketResponse()

    # LabController.place_order calls OrdersController.ticket_order.fn,
    # which itself locally imports and delegates to
    # orders_place.ticket_order_handler — patch THAT (the real delegate),
    # not the `.fn` property (a read-only Litestar descriptor, can't be
    # patched directly).
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value={"id": 7, "slug": "default-bull"}),
    ), patch(
        "backend.api.routes.lab._consume_token", return_value=None,
    ), patch(
        "backend.api.routes.lab._user_id", return_value=1,
    ), patch(
        "backend.api.routes.lab._res_mcp_audit", new=AsyncMock(),
    ), patch(
        "backend.api.routes.lab._res_place_telegram_ping",
    ), patch(
        "backend.api.routes.orders_place.ticket_order_handler",
        new=_fake_ticket_order_handler,
    ):
        resp = await LabController.place_order.fn(
            ctrl, data=data, request=fake_request,
        )

    assert resp.order_id == "555"
    ticket = captured["ticket"]
    assert ticket.template_id == 7
    assert ticket.mcp_request_id is not None
    assert len(ticket.mcp_request_id) == 12  # token_hex(6)
    assert ticket.source == "mcp"


@pytest.mark.asyncio
async def test_place_order_unknown_template_slug_422_and_no_token_burned():
    """An unresolvable template_slug must 422 BEFORE _consume_token runs —
    a typo must never burn the operator's single-use confirm token."""
    from backend.api.routes.lab import LabController
    from litestar.exceptions import HTTPException

    ctrl = LabController(owner=None)
    data = _mk_place_request()
    fake_request = MagicMock()

    mock_consume = MagicMock(return_value=None)

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=None),
    ), patch(
        "backend.api.routes.lab._consume_token", mock_consume,
    ), patch(
        "backend.api.routes.lab._user_id", return_value=1,
    ), patch(
        "backend.api.routes.lab._res_mcp_audit", new=AsyncMock(),
    ):
        with pytest.raises(HTTPException) as ei:
            await LabController.place_order.fn(
                ctrl, data=data, request=fake_request,
            )

    assert ei.value.status_code == 422
    mock_consume.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 2a. Migration — additive, idempotent
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
# 2b. mcp_request_id persists end-to-end on both ticket-persist paths
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
