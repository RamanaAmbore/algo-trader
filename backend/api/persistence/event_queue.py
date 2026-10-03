"""
Generic bounded async event queue for high-frequency append-only writers.

Replaces the per-row `async_session()` pattern used by AgentEvent,
AlgoOrderEvent and McpAudit with a coalescing batch INSERT that fires
every `flush_interval_s` seconds.

Usage
-----
  queue = EventQueue(
      MyModel,
      name="my_table",
      max_queue=10_000,
      on_full="drop",       # or "sync"
  )
  await queue.start()

  await queue.enqueue(col_a=1, col_b="x")   # O(1), non-blocking normally

  await queue.stop()   # flush remaining rows, cancel task

Health
------
  queue.get_health() -> dict with depth, dropped, last_flush_epoch,
                         last_batch_size, worker_alive.

on_full policy
--------------
  "drop" (default) — silently drop + increment counter. Suits high-cadence
      writers where a brief DB stall is tolerable. Agent events and order
      events use this.
  "sync" — fall back to a direct session.add() insert for the ONE overflowing
      row, with a WARNING log. Suits compliance / forensic writers (MCP audit)
      where every row must land.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Literal, Type

from sqlalchemy import insert
from sqlalchemy.exc import DataError, IntegrityError

from backend.api.database import async_session
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class EventQueue:
    """Bounded async queue with a background flush task.

    Parameters
    ----------
    table_model:
        SQLAlchemy ORM model class. Used for both the bulk `insert()`
        statement and the fallback `session.add()` on queue-full.
    name:
        Short label used in log messages and health keys.
    batch_size:
        Maximum rows per INSERT statement.
    flush_interval_s:
        Seconds between flush attempts when the queue is non-empty.
    max_queue:
        Maximum deque length. Additional items trigger `on_full` policy.
    on_full:
        "drop" — increment counter, log WARNING, discard item.
        "sync" — immediately insert the item in its own session (blocks
                  the caller's coroutine for one DB round-trip).
    """

    def __init__(
        self,
        table_model: Type[Any],
        *,
        name: str = "",
        batch_size: int = 500,
        flush_interval_s: float = 1.0,
        max_queue: int = 10_000,
        on_full: Literal["drop", "sync"] = "drop",
        session_factory=None,
    ) -> None:
        self.table_model    = table_model
        self.name           = name or table_model.__tablename__
        self.batch_size     = batch_size
        self.flush_interval = flush_interval_s
        self.max_queue      = max_queue
        self.on_full        = on_full
        self._session_factory = session_factory or async_session

        self._queue: deque[dict] = deque()
        self._task:  asyncio.Task | None = None

        # Health counters (written only by _flush / enqueue, read by get_health)
        self._dropped:        int   = 0
        self._last_flush:     float = 0.0
        self._last_batch:     int   = 0

    # ── Producer API ─────────────────────────────────────────────────────────

    async def enqueue(self, **kwargs: Any) -> None:
        """Add one row's keyword args to the queue.

        Non-blocking in the common case.  On queue-full: drops with a
        warning (on_full="drop") or performs a sync insert (on_full="sync").
        """
        if len(self._queue) >= self.max_queue:
            self._dropped += 1
            logger.warning(
                f"event_queue[{self.name}]: queue full "
                f"(dropped={self._dropped}, max={self.max_queue})"
            )
            if self.on_full == "sync":
                await self._sync_insert(kwargs)
            return
        self._queue.append(kwargs)

    def enqueue_nowait(self, **kwargs: Any) -> None:
        """Synchronous fast-path for drop-mode callers in non-async contexts.

        Safe to call from a sync function (e.g. a KiteTicker callback or a
        Litestar sync route handler) without requiring a running event loop.
        On queue-full: increments dropped counter and discards — ``on_full``
        "sync" is intentionally NOT honoured here because a sync DB round-trip
        from a non-async context would block the event loop thread.

        Only use this method when the caller is a ``def`` (not ``async def``)
        and cannot ``await``.  Prefer ``enqueue()`` everywhere else.
        """
        if len(self._queue) >= self.max_queue:
            self._dropped += 1
            logger.warning(
                f"event_queue[{self.name}]: queue full "
                f"(dropped={self._dropped}, max={self.max_queue})"
            )
            return
        self._queue.append(kwargs)

    # ── Lifespan API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush task. Safe to call multiple times."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._flush_loop(), name=f"event-queue-{self.name}"
            )
            logger.info(f"event_queue[{self.name}]: flush task started")

    async def stop(self) -> None:
        """Flush ALL remaining items (multiple batches if needed), then cancel task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Drain all remaining items — bounded to 3 consecutive failures to avoid
        # hanging on DB down, but does not penalise successful partial flushes.
        _drain_failures = 0
        while self._queue and _drain_failures < 3:
            prev_size = len(self._queue)
            await self._flush()
            if len(self._queue) >= prev_size:
                _drain_failures += 1
            else:
                _drain_failures = 0
        if self._queue:
            logger.warning(
                f"event_queue[{self.name}]: gave up draining after 3 consecutive failures "
                f"({len(self._queue)} items lost)"
            )
        logger.info(f"event_queue[{self.name}]: stopped, flushed remaining")

    # ── Health surface ────────────────────────────────────────────────────────

    def get_health(self) -> dict[str, Any]:
        return {
            "depth":            len(self._queue),
            "dropped":          self._dropped,
            "last_flush_epoch": round(self._last_flush, 3) if self._last_flush else None,
            "last_batch_size":  self._last_batch,
            "worker_alive":     self._task is not None and not self._task.done(),
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"event_queue[{self.name}]: flush loop error: {exc}")

    async def _flush(self) -> None:
        """Drain up to batch_size rows in one bulk INSERT + commit."""
        if not self._queue:
            return

        batch: list[dict] = []
        while self._queue and len(batch) < self.batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return

        try:
            async with self._session_factory() as session:
                await session.execute(insert(self.table_model), batch)
                await session.commit()
            self._last_flush = time.time()
            self._last_batch = len(batch)
        except (IntegrityError, DataError) as exc:
            # Bad data in ONE specific row (e.g. an FK pointing at a row
            # already deleted elsewhere, or a value too long/wrong-typed
            # for its column) — genuinely safe to isolate and drop that
            # row only. Every other exception below (connection-level,
            # transient) preserves the original whole-batch re-queue.
            logger.warning(
                f"event_queue[{self.name}]: bulk flush hit a bad-row error "
                f"({len(batch)} rows): {exc} — falling back to per-row insert"
            )
            await self._flush_rows_individually(batch)
        except Exception as exc:
            logger.warning(
                f"event_queue[{self.name}]: bulk flush failed "
                f"({len(batch)} rows): {exc}"
            )
            # Re-queue failed batch at the front so items aren't silently
            # lost on transient DB errors (next cycle will retry). This is
            # the ONLY path for connection-level failures (OperationalError,
            # InterfaceError, etc.) — McpAudit's on_full="sync" land-every-
            # row guarantee depends on a transient outage being retried,
            # not dropped, so this branch must never be weakened.
            self._queue.extendleft(reversed(batch))

    async def _flush_rows_individually(self, batch: list[dict]) -> None:
        """Per-row fallback after a bulk flush raised IntegrityError/DataError.

        Inserts rows one at a time, each in its own fresh session. A row
        that raises the SAME bad-data error class is logged and dropped —
        it's the one actually at fault. Any OTHER exception encountered
        mid-loop (e.g. a connection drop between rows) is NOT a bad-data
        error — that row and every remaining not-yet-attempted row are
        re-queued at the front (preserving the whole-batch retry guarantee
        for transient failures) and the loop stops immediately. Rows
        already committed earlier in the loop are never re-queued, so a
        retry can never re-insert a duplicate.
        """
        for i, row in enumerate(batch):
            try:
                async with self._session_factory() as session:
                    await session.execute(insert(self.table_model), [row])
                    await session.commit()
            except (IntegrityError, DataError) as row_exc:
                self._dropped += 1
                logger.warning(
                    f"event_queue[{self.name}]: dropping bad row "
                    f"({row_exc.__class__.__name__}): {row_exc}"
                )
                continue
            except Exception as other_exc:
                remaining = batch[i:]
                logger.warning(
                    f"event_queue[{self.name}]: per-row fallback hit a "
                    f"non-bad-data error ({other_exc.__class__.__name__}) — "
                    f"re-queuing this row and {len(remaining) - 1} remaining "
                    f"unattempted row(s) for retry: {other_exc}"
                )
                self._queue.extendleft(reversed(remaining))
                return

    async def _sync_insert(self, kwargs: dict) -> None:
        """Insert one row directly (queue-full sync fallback)."""
        try:
            async with self._session_factory() as session:
                session.add(self.table_model(**kwargs))
                await session.commit()
        except Exception as exc:
            logger.warning(
                f"event_queue[{self.name}]: sync-insert fallback failed: {exc}"
            )
