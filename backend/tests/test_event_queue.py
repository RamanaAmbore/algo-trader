"""
Tests for EventQueue — generic bounded async event queue.

Five dimensions:
1. SSOT  — queue inserts N rows; batch_size drives flush count; graceful stop
2. Perf  — concurrent producers don't race; all items committed
3. Stale — no inline session.add() in flush path (bulk executemany only)
4. Reuse — EventQueue is the single import used in events.py / order_events.py
5. UX    — queue-full policies (drop vs sync), DB failure re-queues items

Does NOT mock broker API calls (not applicable here).
"""

from __future__ import annotations

import asyncio
import json
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio


# ── Minimal SQLite-backed fixture ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def sqlite_factory():
    """In-memory SQLite with a simple test table, yields (session_factory, Model)."""
    from sqlalchemy import Column, Integer, String, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )

    class _Base(DeclarativeBase):
        pass

    class _Event(_Base):
        __tablename__ = "test_events"
        id      = Column(Integer, primary_key=True, autoincrement=True)
        kind    = Column(String(32), nullable=False, default="")
        message = Column(String(500), nullable=False, default="")
        ts      = Column(DateTime, nullable=False,
                         default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    yield factory, _Event

    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


# ── SSOT: basic enqueue → flush ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_100_items_all_committed(sqlite_factory):
    """Enqueue 100 items → after flush all 100 rows appear in DB."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", flush_interval_s=0.05, max_queue=200, session_factory=factory)
    await q.start()
    for i in range(100):
        await q.enqueue(kind="placed", message=f"msg-{i}")
    await asyncio.sleep(0.15)   # two flush cycles
    await q.stop()

    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    assert len(rows) == 100


@pytest.mark.asyncio
async def test_batch_size_drives_flush_count(sqlite_factory):
    """1500 items with batch_size=500 → 3 flush cycles needed, all land."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", batch_size=500, flush_interval_s=0.05,
                   max_queue=2000, session_factory=factory)
    await q.start()
    for i in range(1500):
        await q.enqueue(kind="chase_modify", message=f"m-{i}")
    await asyncio.sleep(0.40)   # enough for ≥3 cycles
    await q.stop()

    async with factory() as s:
        count = len((await s.execute(select(Model))).scalars().all())
    assert count == 1500


@pytest.mark.asyncio
async def test_graceful_stop_flushes_remaining(sqlite_factory):
    """Items enqueued right before stop() are not lost."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", flush_interval_s=60.0,   # won't fire on its own
                   max_queue=200, session_factory=factory)
    await q.start()
    for i in range(10):
        await q.enqueue(kind="fill", message=f"fill-{i}")
    await q.stop()   # must flush the 10 items synchronously

    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_stop_drains_multiple_batches(sqlite_factory):
    """stop() must flush ALL items even when queue depth > batch_size.

    With batch_size=5 and 12 items queued, stop() must make 3 flush
    passes (5 + 5 + 2) — not just one.  A single await self._flush()
    would leave 7 items silently on the floor.
    """
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    BATCH = 5
    TOTAL = 12   # > batch_size; requires ≥3 flush passes

    q = EventQueue(Model, name="t", batch_size=BATCH,
                   flush_interval_s=60.0,   # background task won't fire
                   max_queue=200, session_factory=factory)
    await q.start()
    for i in range(TOTAL):
        await q.enqueue(kind="placed", message=f"m-{i}")
    # Confirm nothing has been flushed yet (interval=60 s)
    async with factory() as s:
        pre_count = len((await s.execute(select(Model))).scalars().all())
    assert pre_count == 0, "nothing should flush before stop()"

    await q.stop()

    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    assert len(rows) == TOTAL, (
        f"stop() must drain all {TOTAL} items across multiple batches; "
        f"only {len(rows)} committed"
    )


@pytest.mark.asyncio
async def test_stop_drains_large_queue_db_healthy(sqlite_factory):
    """Drain loop must not abort after 3 batches when DB is healthy (progress-tracking fix)."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    BATCH = 200
    TOTAL = 700  # requires 4 flushes — previously lost last batch under the buggy counter
    q = EventQueue(Model, name="t", batch_size=BATCH, flush_interval_s=60.0,
                   max_queue=1000, session_factory=factory)
    await q.start()
    for i in range(TOTAL):
        await q.enqueue(val=i, label=f"x{i}")
    await q.stop()

    async with factory() as session:
        rows = (await session.execute(select(Model))).scalars().all()
    assert len(rows) == TOTAL, (
        f"drain must commit all {TOTAL} items across {TOTAL // BATCH + 1} batches; "
        f"only {len(rows)} committed — counter likely increments on progress, not failure"
    )


# ── enqueue_nowait sync fast-path ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_nowait_commits_items(sqlite_factory):
    """enqueue_nowait() appends to the deque; flush commits normally."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", flush_interval_s=60.0, max_queue=50, session_factory=factory)
    # enqueue_nowait is a plain sync call — no await needed
    for i in range(5):
        q.enqueue_nowait(kind="placed", message=f"sync-{i}")
    assert len(q._queue) == 5

    await q.stop()   # drains the queue

    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_enqueue_nowait_drops_on_full():
    """enqueue_nowait() on a full queue increments dropped, never raises."""
    from backend.api.persistence.event_queue import EventQueue

    class _FakeModel:
        __tablename__ = "fake"

    q = EventQueue(_FakeModel, name="t", max_queue=3, on_full="sync",
                   flush_interval_s=60.0)
    for _ in range(3):
        q.enqueue_nowait(kind="x", message="ok")
    # Overflow — enqueue_nowait must NOT attempt a sync DB insert
    q.enqueue_nowait(kind="x", message="overflow")

    assert q._dropped == 1
    assert len(q._queue) == 3   # no extra item snuck in


# ── Queue-full policies ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_full_drop_increments_counter(sqlite_factory):
    """on_full='drop': overflow items are dropped + counter incremented."""
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", max_queue=5, on_full="drop",
                   flush_interval_s=60.0, session_factory=factory)
    # Fill the queue
    for i in range(5):
        await q.enqueue(kind="placed", message="ok")
    # These three overflow
    for i in range(3):
        await q.enqueue(kind="placed", message="overflow")

    assert q._dropped == 3
    assert len(q._queue) == 5   # nothing extra got in


@pytest.mark.asyncio
async def test_queue_full_sync_inserts_overflow_row(sqlite_factory):
    """on_full='sync': overflow row lands in DB immediately via sync path."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", max_queue=2, on_full="sync",
                   flush_interval_s=60.0, session_factory=factory)
    await q.enqueue(kind="placed", message="a")
    await q.enqueue(kind="placed", message="b")
    # Third item overflows → sync insert
    await q.enqueue(kind="placed", message="overflow-sync")

    # Sync path inserted the overflow row directly; queue still has 2
    assert q._dropped == 1
    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    # The sync row was committed immediately
    assert len(rows) == 1
    assert rows[0].message == "overflow-sync"


# ── DB failure → re-queue ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_failure_requeues_items():
    """On flush DB error, items are re-queued for the next cycle."""
    from unittest.mock import AsyncMock, MagicMock

    # Make async_session raise on the first call, succeed on the second.
    call_count = 0

    class _FakeModel:
        __tablename__ = "fake"

    broken_cm = MagicMock()
    broken_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    broken_cm.__aexit__  = AsyncMock(return_value=False)

    from backend.api.persistence.event_queue import EventQueue
    q = EventQueue(_FakeModel, name="t", flush_interval_s=60.0, max_queue=100)
    await q.enqueue(kind="placed", message="x")
    assert len(q._queue) == 1

    with patch("backend.api.persistence.event_queue.async_session",
               return_value=broken_cm):
        await q._flush()

    # Item must be back in the queue after DB failure
    assert len(q._queue) == 1


# ── Concurrent producers ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_producers_no_race(sqlite_factory):
    """10 producers × 10 enqueues → all 100 rows committed, no item lost."""
    from sqlalchemy import select
    factory, Model = sqlite_factory
    from backend.api.persistence.event_queue import EventQueue

    q = EventQueue(Model, name="t", flush_interval_s=0.05,
                   batch_size=200, max_queue=500, session_factory=factory)

    async def _produce(n: int) -> None:
        for i in range(10):
            await q.enqueue(kind="error", message=f"p{n}-{i}")

    await q.start()
    await asyncio.gather(*[_produce(n) for n in range(10)])
    await asyncio.sleep(0.20)
    await q.stop()

    async with factory() as s:
        rows = (await s.execute(select(Model))).scalars().all()
    assert len(rows) == 100


# ── Reuse: module-level singletons exist in consumers ────────────────────────

def test_agent_event_queue_singleton_defined():
    """events.py must expose an `agent_event_queue` EventQueue instance."""
    from backend.api.algo import events
    from backend.api.persistence.event_queue import EventQueue
    assert hasattr(events, "agent_event_queue")
    assert isinstance(events.agent_event_queue, EventQueue)


def test_order_event_queue_singleton_defined():
    """order_events.py must expose an `order_event_queue` EventQueue instance."""
    from backend.api.algo import order_events
    from backend.api.persistence.event_queue import EventQueue
    assert hasattr(order_events, "order_event_queue")
    assert isinstance(order_events.order_event_queue, EventQueue)


def test_mcp_audit_queue_singleton_defined():
    """research.py must expose an `mcp_audit_queue` EventQueue instance."""
    from backend.api.routes import research
    from backend.api.persistence.event_queue import EventQueue
    assert hasattr(research, "mcp_audit_queue")
    assert isinstance(research.mcp_audit_queue, EventQueue)


def test_algo_ws_uses_event_queue():
    """algo.py _broadcast_event must use EventQueue, not a raw list buffer."""
    from backend.api.routes import algo
    from backend.api.persistence.event_queue import EventQueue
    # The old _persist_buffer list should be replaced by an EventQueue instance
    assert hasattr(algo, "algo_event_queue")
    assert isinstance(algo.algo_event_queue, EventQueue)
    # Old raw-list buffer must not exist
    assert not hasattr(algo, "_persist_buffer"), \
        "_persist_buffer raw list should be removed; use algo_event_queue instead"
