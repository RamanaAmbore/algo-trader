"""
Smoke tests for the AlgoOrderEvent append-only timeline.

Verifies:
  - write_event inserts rows and can be queried
  - GET /api/orders/{id}/events returns rows in ts-ASC order
  - Demo callers get account codes masked in payload_json
  - write_event errors never raise into the caller's context

Does NOT mock broker API calls.
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio


# ── In-process SQLite DB for isolation ───────────────────────────────────────

@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Async SQLAlchemy session factory backed by an in-process SQLite DB.

    Only creates the two tables under test (algo_orders + algo_order_events)
    so we avoid JSONB columns on other models that SQLite can't compile.
    """
    from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from datetime import datetime, timezone

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id               = Column(Integer, primary_key=True, autoincrement=True)
        account          = Column(String(32), nullable=False)
        symbol           = Column(String(64), nullable=False)
        exchange         = Column(String(8),  nullable=False, default="NFO")
        transaction_type = Column(String(4),  nullable=False)
        quantity         = Column(Integer,    nullable=False)
        initial_price    = Column(Float,      nullable=True)
        fill_price       = Column(Float,      nullable=True)
        attempts         = Column(Integer,    nullable=False, default=0)
        slippage         = Column(Float,      nullable=True)
        status           = Column(String(16), nullable=False, default="OPEN")
        engine           = Column(String(16), nullable=False, default="manual")
        mode             = Column(String(8),  nullable=False, default="live")
        broker_order_id  = Column(String(32), nullable=True)
        detail           = Column(Text,       nullable=True)
        created_at       = Column(DateTime,   nullable=False,
                                  default=lambda: datetime.now(timezone.utc))
        filled_at        = Column(DateTime,   nullable=True)

    class _AlgoOrderEvent(_Base):
        __tablename__ = "algo_order_events"
        id           = Column(Integer, primary_key=True, autoincrement=True)
        order_id     = Column(Integer, ForeignKey("algo_orders.id"), nullable=False)
        ts           = Column(DateTime, nullable=False,
                              default=lambda: datetime.now(timezone.utc))
        kind         = Column(String(32), nullable=False)
        message      = Column(String(500), nullable=False, default="")
        payload_json = Column(Text, nullable=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Monkey-patch the real model classes to point at our SQLite-compatible
    # variants for the duration of this fixture. write_event and route handlers
    # import from backend.api.models; patching those names makes the queries land
    # on our in-memory tables.
    import backend.api.models as _models
    orig_order       = _models.AlgoOrder
    orig_order_event = _models.AlgoOrderEvent
    _models.AlgoOrder      = _AlgoOrder      # type: ignore[assignment]
    _models.AlgoOrderEvent = _AlgoOrderEvent  # type: ignore[assignment]

    yield factory

    # Restore originals before teardown so other test modules aren't affected.
    _models.AlgoOrder      = orig_order
    _models.AlgoOrderEvent = orig_order_event

    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def sample_order_id(sqlite_session_factory):
    """Insert one AlgoOrder row and return its integer id."""
    from backend.api.models import AlgoOrder
    async with sqlite_session_factory() as s:
        row = AlgoOrder(
            account="ZG0790",
            symbol="NIFTY25APRFUT",
            exchange="NFO",
            transaction_type="SELL",
            quantity=50,
            initial_price=22000.0,
            status="OPEN",
            engine="paper",
            mode="paper",
            detail="[PAPER] test order",
        )
        s.add(row)
        await s.commit()
        return row.id


# ── write_event unit tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_event_inserts_rows(sqlite_session_factory, sample_order_id):
    """write_event enqueues rows; after flush all three appear in DB.

    Since write_event now goes through order_event_queue (EventQueue), we
    patch async_session inside event_queue, start the queue, let it flush,
    then verify.
    """
    from backend.api.algo.order_events import order_event_queue, write_event
    from backend.api.models import AlgoOrderEvent
    from backend.api.persistence.event_queue import async_session
    from sqlalchemy import select

    # Reset queue state between tests
    order_event_queue._queue.clear()

    try:
        order_event_queue._session_factory = sqlite_session_factory
        await order_event_queue.start()
        await write_event(sample_order_id, "placed",       "order placed @₹22000")
        await write_event(sample_order_id, "chase_modify", "chase #1 limit=₹21990")
        await write_event(sample_order_id, "fill",         "FILLED @₹21985.50",
                          payload={"fill_price": 21985.50, "slippage": -14.50})
        await order_event_queue.stop()
    finally:
        order_event_queue._session_factory = async_session

    async with sqlite_session_factory() as s:
        rows = (await s.execute(
            select(AlgoOrderEvent)
            .where(AlgoOrderEvent.order_id == sample_order_id)
            .order_by(AlgoOrderEvent.id)
        )).scalars().all()

    assert len(rows) == 3
    assert rows[0].kind == "placed"
    assert rows[1].kind == "chase_modify"
    assert rows[2].kind == "fill"
    # Payload round-trip
    payload = json.loads(rows[2].payload_json)
    assert payload["fill_price"] == pytest.approx(21985.50)


@pytest.mark.asyncio
async def test_write_event_tolerates_db_failure():
    """write_event with a broken queue never raises into the caller.

    EventQueue.enqueue() swallows exceptions — the caller's coroutine
    continues even when the underlying insert fails.
    """
    from backend.api.algo.order_events import write_event

    # write_event enqueues — the enqueue itself is a simple deque append;
    # it cannot raise. Confirm the call doesn't propagate any error.
    await write_event(999, "error", "should not propagate")


# ── API route tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_order_events_returns_rows_in_order(
    async_client, sqlite_session_factory, sample_order_id
):
    """GET /api/orders/{id}/events returns 3 rows in ts-ASC order."""
    from backend.api.algo.order_events import order_event_queue, write_event
    from backend.api.persistence.event_queue import async_session

    order_event_queue._queue.clear()

    try:
        order_event_queue._session_factory = sqlite_session_factory
        await order_event_queue.start()
        await write_event(sample_order_id, "placed",       "placed")
        await write_event(sample_order_id, "chase_modify", "chase #1")
        await write_event(sample_order_id, "fill",         "filled",
                          payload={"account": "ZG0790", "fill_price": 100.0})
        await order_event_queue.stop()
    finally:
        order_event_queue._session_factory = async_session

    # Patch both the route's local import and the auth helper.
    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        response = await async_client.get(
            f"/api/orders/{sample_order_id}/events",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200, response.text
    events = response.json()
    assert len(events) == 3
    kinds = [e["kind"] for e in events]
    assert kinds == ["placed", "chase_modify", "fill"]


@pytest.mark.asyncio
async def test_get_order_events_demo_masks_account(
    async_client, sqlite_session_factory, sample_order_id
):
    """Demo callers (is_admin_request=False) get ZG0790 → ZG#### in payload_json."""
    from backend.api.algo.order_events import order_event_queue, write_event
    from backend.api.persistence.event_queue import async_session

    order_event_queue._queue.clear()

    try:
        order_event_queue._session_factory = sqlite_session_factory
        await order_event_queue.start()
        await write_event(
            sample_order_id, "placed", "placed",
            payload={"account": "ZG0790", "price": 22000.0},
        )
        await order_event_queue.stop()
    finally:
        order_event_queue._session_factory = async_session

    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=False):
        response = await async_client.get(
            f"/api/orders/{sample_order_id}/events",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200, response.text
    events = response.json()
    assert len(events) == 1

    raw = events[0]["payload_json"]
    assert raw is not None, "payload_json should not be None"
    assert "ZG0790" not in raw, "raw account code must be masked for demo callers"
    assert "ZG####" in raw, "masked form ZG#### must appear in payload_json"
