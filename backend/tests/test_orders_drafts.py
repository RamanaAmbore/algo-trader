"""
Tests for server-side order drafts (2026-10).

Drafts were session-only client-side state (frontend/src/lib/data/
payoffDrafts.svelte.js) with zero backend awareness. This adds real
AlgoOrder rows (mode='draft', status='OPEN') via a new CRUD surface
(POST/GET/DELETE/PATCH /api/orders/drafts[/{id}]), plus an optional
draft_id field on TicketOrderRequest so the real /ticket submission path
deletes the originating draft ONLY on confirmed success, never on failure.

Covers:
  1. POST /drafts creates a mode='draft' row.
  2. GET /drafts returns only draft rows (not live/paper).
  3. GET /drafts masks account for non-admin, shows real account for admin.
  4. DELETE /drafts/{id} on a real draft removes it.
  5. DELETE /drafts/{id} on a mode='live' row is refused (404), row intact
     — regression guard against "accidental delete-any-order".
  6. PATCH /drafts/{id} updates only the supplied fields.
  7. Routing: /api/orders/drafts and /api/orders/drafts/{id} resolve to the
     draft handlers, not the generic /{order_id:str} PUT/DELETE handlers.
  8. GET /chases/active excludes mode='draft' rows (leak fix).
  9. ticket_order_handler + draft_id: confirmed SUCCESS deletes the draft.
  10. ticket_order_handler + draft_id: FAILURE leaves the draft untouched
      (the actual regression guard for the bug this feature fixes).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock

pytestmark = pytest.mark.asyncio


# ── In-process SQLite DB for isolation (mirrors test_order_events.py /
#    test_orders_algo_recent_mode_filter.py) ─────────────────────────────────

@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Async SQLAlchemy session factory backed by an in-process SQLite DB.
    Declares every column the draft CRUD routes + _chase_row_to_info +
    _fetch_child_order_ids touch."""
    from sqlalchemy import Column, Integer, String, Text, Float, Numeric, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from datetime import datetime, timezone

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id                  = Column(Integer, primary_key=True, autoincrement=True)
        account              = Column(String(32), nullable=False)
        symbol               = Column(String(64), nullable=False)
        exchange             = Column(String(8),  nullable=False, default="NFO")
        transaction_type     = Column(String(4),  nullable=False)
        quantity             = Column(Integer,    nullable=False)
        initial_price        = Column(Numeric(16, 4), nullable=True)
        current_limit        = Column(Numeric(16, 4), nullable=True)
        fill_price           = Column(Numeric(16, 4), nullable=True)
        attempts             = Column(Integer,    nullable=False, default=0)
        status               = Column(String(16), nullable=False, default="OPEN")
        engine               = Column(String(16), nullable=False, default="manual")
        mode                 = Column(String(8),  nullable=False, default="live")
        broker_order_id      = Column(String(32), nullable=True)
        detail               = Column(Text,       nullable=True)
        created_at           = Column(DateTime,   nullable=False,
                                       default=lambda: datetime.now(timezone.utc))
        target_pct           = Column(Numeric(8, 4), nullable=True)
        target_abs           = Column(Numeric(16, 4), nullable=True)
        parent_order_id      = Column(Integer, nullable=True)
        basket_tag           = Column(String(64), nullable=True)
        template_id          = Column(Integer, nullable=True)
        attached_gtts_json   = Column(Text, nullable=True)
        filled_quantity      = Column(Integer, nullable=False, default=0)
        interval_seconds     = Column(Integer, nullable=True)
        last_attempt_at      = Column(Float, nullable=True)
        next_attempt_at      = Column(Float, nullable=True)
        source               = Column(String(32), nullable=True)
        agent_id             = Column(Integer, nullable=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    orig_order = _models.AlgoOrder
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]

    yield factory

    _models.AlgoOrder = orig_order  # type: ignore[assignment]

    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_rows(sqlite_session_factory):
    """One draft row, one live row with a broker_order_id (so it would be
    a legitimate /chases/active hit on its own)."""
    from backend.api.models import AlgoOrder

    async with sqlite_session_factory() as s:
        draft_row = AlgoOrder(
            account="ZG0790", symbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="BUY", quantity=50, status="OPEN",
            engine="manual", mode="draft", detail="[DRAFT] seed",
        )
        live_row = AlgoOrder(
            account="ZG0790", symbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="SELL", quantity=50, status="OPEN",
            engine="live", mode="live", detail="live row",
            broker_order_id="BO-1",
        )
        s.add(draft_row)
        s.add(live_row)
        await s.commit()
        return draft_row.id, live_row.id


# ═══════════════════════════════════════════════════════════════════════════
# 1-3 · POST / GET /drafts
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_draft_persists_mode_draft_row(async_client, sqlite_session_factory):
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.post(
            "/api/orders/drafts",
            json={
                "symbol": "niftyfut", "transaction_type": "buy",
                "quantity": 50, "exchange": "NFO", "price": 123.45,
            },
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 201 or resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    assert isinstance(draft_id, int)

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one()
        assert row.mode == "draft"
        assert row.status == "OPEN"
        assert row.symbol == "NIFTYFUT"
        assert row.transaction_type == "BUY"
        assert row.account == ""  # omitted account -> stored as ""


@pytest.mark.asyncio
async def test_create_draft_rejects_bad_quantity(async_client, sqlite_session_factory):
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.post(
            "/api/orders/drafts",
            json={"symbol": "NIFTYFUT", "transaction_type": "BUY", "quantity": 0},
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_drafts_returns_only_draft_rows(
    async_client, sqlite_session_factory, seeded_rows,
):
    draft_id, live_id = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        resp = await async_client.get(
            "/api/orders/drafts", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    ids = [r["id"] for r in rows]
    assert draft_id in ids
    assert live_id not in ids
    assert {r["mode"] for r in rows} == {"draft"}


@pytest.mark.asyncio
async def test_list_drafts_masks_account_for_non_admin(
    async_client, sqlite_session_factory, seeded_rows,
):
    draft_id, _ = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=False):
        resp = await async_client.get(
            "/api/orders/drafts", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["id"] == draft_id)
    assert row["account"] != "ZG0790"
    assert row["account"].endswith("####")


@pytest.mark.asyncio
async def test_list_drafts_shows_real_account_for_admin(
    async_client, sqlite_session_factory, seeded_rows,
):
    draft_id, _ = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        resp = await async_client.get(
            "/api/orders/drafts", headers={"Authorization": "Bearer test"},
        )
    row = next(r for r in resp.json() if r["id"] == draft_id)
    assert row["account"] == "ZG0790"


# ═══════════════════════════════════════════════════════════════════════════
# 4-5 · DELETE /drafts/{id}
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_delete_draft_removes_row(async_client, sqlite_session_factory, seeded_rows):
    draft_id, _ = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.delete(
            f"/api/orders/drafts/{draft_id}", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": draft_id, "deleted": True}

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert row is None


@pytest.mark.asyncio
async def test_delete_draft_refuses_non_draft_row(
    async_client, sqlite_session_factory, seeded_rows,
):
    """DELETE /drafts/{id} on a mode='live' row must 404 and leave the row
    untouched — this is the guard against becoming a delete-any-order
    endpoint."""
    _, live_id = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.delete(
            f"/api/orders/drafts/{live_id}", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404, resp.text

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == live_id))).scalar_one_or_none()
        assert row is not None
        assert row.mode == "live"


@pytest.mark.asyncio
async def test_delete_draft_missing_id_404(async_client, sqlite_session_factory):
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.delete(
            "/api/orders/drafts/999999", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 6 · PATCH /drafts/{id}
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_patch_draft_updates_only_supplied_fields(
    async_client, sqlite_session_factory, seeded_rows,
):
    draft_id, _ = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        resp = await async_client.patch(
            f"/api/orders/drafts/{draft_id}",
            json={"quantity": 75},
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quantity"] == 75
    # untouched fields survive
    assert body["symbol"] == "NIFTY25APRFUT"
    assert body["transaction_type"] == "BUY"
    assert body["account"] == "ZG0790"


@pytest.mark.asyncio
async def test_patch_draft_refuses_non_draft_row(
    async_client, sqlite_session_factory, seeded_rows,
):
    _, live_id = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await async_client.patch(
            f"/api/orders/drafts/{live_id}",
            json={"quantity": 99},
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 7 · Routing collision check
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_drafts_route_does_not_collide_with_generic_order_id_route(
    async_client, sqlite_session_factory, seeded_rows,
):
    """GET/DELETE/PATCH on /drafts[/{id}] must resolve to the draft
    handlers, not fall through to PUT/DELETE /{order_id:str} (modify_order /
    cancel_order), which would require admin + a live broker call."""
    draft_id, _ = seeded_rows
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        resp = await async_client.get(
            "/api/orders/drafts", headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 200
    # cancel_order (DELETE /{order_id:str}) requires a `account` query
    # param and admin; if routing mis-resolved "drafts/<id>" there the
    # response shape would differ entirely (CancelOrderResponse vs our
    # {"id":..., "deleted":...} dict).
    with patch("backend.api.database.async_session", sqlite_session_factory):
        del_resp = await async_client.delete(
            f"/api/orders/drafts/{draft_id}", headers={"Authorization": "Bearer test"},
        )
    assert del_resp.status_code == 200
    assert set(del_resp.json().keys()) == {"id", "deleted"}


# ═══════════════════════════════════════════════════════════════════════════
# 8 · /chases/active excludes drafts (leak fix)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chases_active_excludes_draft_rows(sqlite_session_factory, seeded_rows):
    """Regression guard: before the fix, list_active_chases' query had no
    mode filter and _rco_reconcile_active_rows only special-cased
    paper/live, so a draft row (status='OPEN' by construction) would sit
    in the operator's live in-flight-chase panel forever."""
    from backend.api.routes.orders import OrdersController

    draft_id, live_id = seeded_rows
    mock_request = MagicMock()
    controller = OrdersController.__new__(OrdersController)

    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders._chase_snapshot_paper_open_ids",
               return_value=set()), \
         patch("backend.api.routes.orders._chase_snapshot_broker_status_by_id",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        rows = await OrdersController.list_active_chases.fn(controller, request=mock_request)

    ids = [r.id for r in rows]
    assert live_id in ids
    assert draft_id not in ids


# ═══════════════════════════════════════════════════════════════════════════
# 9-10 · ticket_order_handler draft_id cleanup hook
# ═══════════════════════════════════════════════════════════════════════════

def _patched_ticket_gates(mode: str, account: str, sym: str, qty: int, lot_size: int):
    """Context manager stack patching every gate ticket_order_handler runs
    BEFORE dispatching to _ticket_place_live/_ticket_place_paper, so these
    tests exercise only the draft_id cleanup hook added around that
    dispatch — not the full validation/broker pipeline. Mirrors the
    pattern in test_template_findings.py
    (test_offsetting_position_clears_template_at_submit)."""
    from contextlib import ExitStack

    async def _fake_validate(d, req):
        return "BUY", sym, qty, lot_size

    def _fake_account(d):
        return account

    async def _fake_capacity(*a, **kw):
        pass

    async def _fake_gate(d, s):
        pass

    stack = ExitStack()
    stack.enter_context(patch(
        "backend.api.routes.orders_place._ticket_validate_input", new=_fake_validate))
    stack.enter_context(patch(
        "backend.api.routes.orders_place._ticket_validate_account", new=_fake_account))
    stack.enter_context(patch(
        "backend.api.routes.orders_place._ticket_enforce_lot_and_fat_finger",
        new=AsyncMock(return_value=None)))
    stack.enter_context(patch(
        "backend.api.routes.orders_place._enforce_capacity_guard", new=_fake_capacity))
    stack.enter_context(patch(
        "backend.api.routes.orders_place._ticket_gate_market_hours_and_align_price",
        new=_fake_gate))
    stack.enter_context(patch(
        "backend.shared.helpers.settings.get_bool", return_value=False))
    stack.enter_context(patch(
        "backend.shared.helpers.utils.config", {"deploy_branch": "dev"}))
    return stack


@pytest.mark.asyncio
async def test_ticket_success_with_draft_id_deletes_draft(
    sqlite_session_factory, seeded_rows,
):
    from backend.api.schemas import TicketOrderRequest, TicketOrderResponse
    from backend.api.routes.orders_place import ticket_order_handler

    draft_id, _ = seeded_rows
    data = TicketOrderRequest(
        mode="paper", side="BUY", tradingsymbol="NIFTY25APRFUT",
        quantity=50, exchange="NFO", account="ZG0790",
        draft_id=draft_id,
    )

    with _patched_ticket_gates("paper", "ZG0790", "NIFTY25APRFUT", 50, 1), \
         patch("backend.api.routes.orders_place._ticket_place_paper",
               new=AsyncMock(return_value=TicketOrderResponse(
                   order_id="123", mode="paper", status="OPEN", detail="ok",
               ))), \
         patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await ticket_order_handler(data, MagicMock())

    assert resp.order_id == "123"

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert row is None, "draft row must be deleted after a CONFIRMED successful placement"


@pytest.mark.asyncio
async def test_ticket_failure_with_draft_id_leaves_draft_untouched(
    sqlite_session_factory, seeded_rows,
):
    """The actual regression guard the task calls out: the draft used to
    be deleted client-side before the placement result was known. Now the
    backend is the single source of truth — a failed placement must NEVER
    delete the draft."""
    from litestar.exceptions import HTTPException
    from backend.api.schemas import TicketOrderRequest
    from backend.api.routes.orders_place import ticket_order_handler

    draft_id, _ = seeded_rows
    data = TicketOrderRequest(
        mode="paper", side="BUY", tradingsymbol="NIFTY25APRFUT",
        quantity=50, exchange="NFO", account="ZG0790",
        draft_id=draft_id,
    )

    async def _fake_place_paper_raises(*a, **kw):
        raise HTTPException(status_code=400, detail="broker rejected")

    with _patched_ticket_gates("paper", "ZG0790", "NIFTY25APRFUT", 50, 1), \
         patch("backend.api.routes.orders_place._ticket_place_paper",
               new=_fake_place_paper_raises), \
         patch("backend.api.database.async_session", sqlite_session_factory):
        with pytest.raises(HTTPException):
            await ticket_order_handler(data, MagicMock())

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert row is not None, "draft row must survive a FAILED placement"
        assert row.mode == "draft"


@pytest.mark.asyncio
async def test_ticket_success_without_draft_id_is_a_noop(
    sqlite_session_factory, seeded_rows,
):
    """Sanity check: when draft_id is None (the overwhelming majority of
    /ticket calls), the cleanup hook must not touch any row."""
    from backend.api.schemas import TicketOrderRequest, TicketOrderResponse
    from backend.api.routes.orders_place import ticket_order_handler

    draft_id, _ = seeded_rows
    data = TicketOrderRequest(
        mode="paper", side="BUY", tradingsymbol="NIFTY25APRFUT",
        quantity=50, exchange="NFO", account="ZG0790",
    )

    with _patched_ticket_gates("paper", "ZG0790", "NIFTY25APRFUT", 50, 1), \
         patch("backend.api.routes.orders_place._ticket_place_paper",
               new=AsyncMock(return_value=TicketOrderResponse(
                   order_id="456", mode="paper", status="OPEN", detail="ok",
               ))), \
         patch("backend.api.database.async_session", sqlite_session_factory):
        resp = await ticket_order_handler(data, MagicMock())

    assert resp.order_id == "456"

    from backend.api.models import AlgoOrder
    from sqlalchemy import select
    async with sqlite_session_factory() as s:
        row = (await s.execute(select(AlgoOrder).where(AlgoOrder.id == draft_id))).scalar_one_or_none()
        assert row is not None, "unrelated draft must survive when draft_id wasn't supplied"
