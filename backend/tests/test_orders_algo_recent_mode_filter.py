"""
Fix: `/api/orders/algo/recent` mode whitelist was missing "draft".

Before the fix, `mode in ("live", "sim", "paper", "replay", "shadow")` was
False for `mode="draft"`, so the `.where(AlgoOrder.mode == mode)` filter was
never applied — a caller asking specifically for draft rows silently got
every row back, regardless of mode. This test seeds one draft row and one
live row and asserts `mode=draft` returns only the draft row.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import patch

pytestmark = pytest.mark.asyncio


# ── In-process SQLite DB for isolation (mirrors test_order_events.py) ───────

@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Async SQLAlchemy session factory backed by an in-process SQLite DB.

    Only declares the columns `_chase_row_to_info` + `_fetch_child_order_ids`
    actually touch, avoiding Postgres-only types (JSONB, server-side FKs to
    tables we don't create here) that SQLite can't compile.
    """
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

    # Monkey-patch the real model class so the route's local import
    # (`from backend.api.models import AlgoOrder`) resolves to our
    # SQLite-compatible variant for the duration of this fixture.
    import backend.api.models as _models
    orig_order = _models.AlgoOrder
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]

    yield factory

    _models.AlgoOrder = orig_order  # type: ignore[assignment]

    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_draft_and_live_rows(sqlite_session_factory):
    """Insert one draft-mode row and one live-mode row; return their ids."""
    from backend.api.models import AlgoOrder

    async with sqlite_session_factory() as s:
        draft_row = AlgoOrder(
            account="ZG0790", symbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="BUY", quantity=50, status="OPEN",
            engine="manual", mode="draft", detail="draft row",
        )
        live_row = AlgoOrder(
            account="ZG0790", symbol="NIFTY25APRFUT", exchange="NFO",
            transaction_type="SELL", quantity=50, status="OPEN",
            engine="live", mode="live", detail="live row",
        )
        s.add(draft_row)
        s.add(live_row)
        await s.commit()
        return draft_row.id, live_row.id


@pytest.mark.asyncio
async def test_algo_recent_mode_draft_returns_only_draft_rows(
    async_client, sqlite_session_factory, seeded_draft_and_live_rows
):
    """mode=draft must filter to draft-only rows, not fall through and
    return every row unfiltered (the pre-fix bug)."""
    draft_id, live_id = seeded_draft_and_live_rows

    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        response = await async_client.get(
            "/api/orders/algo/recent?mode=draft",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    ids = [r["id"] for r in rows]
    modes = {r["mode"] for r in rows}

    assert draft_id in ids
    assert live_id not in ids
    assert modes == {"draft"}


@pytest.mark.asyncio
async def test_algo_recent_mode_all_still_returns_every_row(
    async_client, sqlite_session_factory, seeded_draft_and_live_rows
):
    """Sanity check: the default mode="all" path is untouched by this fix
    and still returns both rows."""
    draft_id, live_id = seeded_draft_and_live_rows

    with patch("backend.api.database.async_session", sqlite_session_factory), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        response = await async_client.get(
            "/api/orders/algo/recent",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200, response.text
    ids = [r["id"] for r in response.json()]
    assert draft_id in ids
    assert live_id in ids
