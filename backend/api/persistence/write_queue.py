"""
Write-queue module — two bounded asyncio queues drain in parallel worker
coroutines so OHLCV bar persistence is off the hot read path.

Two queues (not one with fanout) so disk-write failures never pile up
DB writes and each can have its own batching policy.

Producer API  : enqueue_disk(payload), enqueue_db(payload)
Lifespan API  : start(), stop()
Health surface: get_health()
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ── Queues ────────────────────────────────────────────────────────────────────

disk_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=5_000)
db_queue:   asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)

# ── Health counters (module-level, written only by workers) ───────────────────

_disk_dropped:      int   = 0
_disk_last_flush:   float = 0.0   # epoch seconds
_disk_last_batch:   int   = 0

_db_dropped:        int   = 0
_db_last_flush:     float = 0.0
_db_last_batch:     int   = 0

# ── Worker task handles ───────────────────────────────────────────────────────

_disk_task: asyncio.Task | None = None
_db_task:   asyncio.Task | None = None


# ── Producer API ──────────────────────────────────────────────────────────────

def enqueue_disk(payload: dict) -> None:
    global _disk_dropped
    try:
        disk_queue.put_nowait(payload)
    except asyncio.QueueFull:
        _disk_dropped += 1
        logger.warning(
            f"write_queue: disk_queue full (dropped={_disk_dropped}); "
            "next read will re-fetch from broker"
        )


def enqueue_db(payload: dict) -> None:
    global _db_dropped
    try:
        db_queue.put_nowait(payload)
    except asyncio.QueueFull:
        _db_dropped += 1
        logger.warning(
            f"write_queue: db_queue full (dropped={_db_dropped}); "
            "next read will re-fetch from broker"
        )


# ── Lifespan API ─────────────────────────────────────────────────────────────

async def start() -> None:
    """Capture the running loop (so sync-thread callers can schedule
    work on it via run_coroutine_threadsafe), then spawn the two
    worker coroutines."""
    global _disk_task, _db_task, _main_loop
    _main_loop = asyncio.get_running_loop()
    from backend.api.persistence.cache_worker import run as _cache_run
    from backend.api.persistence.db_worker   import run as _db_run
    _disk_task = asyncio.create_task(_cache_run(), name="persist-cache-worker")
    _db_task   = asyncio.create_task(_db_run(),   name="persist-db-worker")
    logger.info("write_queue: disk + db workers started")


# Captured at start() so sync-thread callers (e.g. _get_today_token_map
# called from a broker fetch running in asyncio.to_thread) can schedule
# coroutines on the main loop without calling the deprecated
# asyncio.get_event_loop() from a non-running-loop context.
_main_loop: asyncio.AbstractEventLoop | None = None


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop captured at start(), or None if start() never ran
    (test / import-only contexts). Callers must handle the None case."""
    return _main_loop


async def stop() -> None:
    deadline = 2.0   # seconds to drain before cancelling
    for q, label in ((disk_queue, "disk"), (db_queue, "db")):
        try:
            await asyncio.wait_for(q.join(), timeout=deadline)
        except asyncio.TimeoutError:
            logger.warning(
                f"write_queue: {label}_queue did not drain within "
                f"{deadline}s on shutdown — remaining items dropped"
            )
    for task in (_disk_task, _db_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info("write_queue: workers stopped")


# ── Health surface ────────────────────────────────────────────────────────────

def get_health() -> dict[str, Any]:
    """Snapshot of queue depth + worker liveness. `last_flush_epoch` is
    the wall-clock epoch (seconds) when the worker last completed a
    batch — operator can subtract from `now` to see how stale the last
    flush is. `worker_alive` is false if the worker task has died
    (supervisor lost) so the dashboard surfaces it without polling logs.
    """
    def _alive(task: asyncio.Task | None) -> bool:
        return bool(task is not None and not task.done())
    return {
        "disk_queue": {
            "depth":             disk_queue.qsize(),
            "dropped":           _disk_dropped,
            "last_flush_epoch":  round(_disk_last_flush, 3) if _disk_last_flush else None,
            "last_batch_size":   _disk_last_batch,
            "worker_alive":      _alive(_disk_task),
        },
        "db_queue": {
            "depth":             db_queue.qsize(),
            "dropped":           _db_dropped,
            "last_flush_epoch":  round(_db_last_flush, 3) if _db_last_flush else None,
            "last_batch_size":   _db_last_batch,
            "worker_alive":      _alive(_db_task),
        },
    }


# ── Internal helpers used by workers to update health state ──────────────────

def _record_disk_flush(batch_size: int) -> None:
    global _disk_last_flush, _disk_last_batch
    _disk_last_flush = time.time()
    _disk_last_batch = batch_size


def _record_db_flush(batch_size: int) -> None:
    global _db_last_flush, _db_last_batch
    _db_last_flush = time.time()
    _db_last_batch = batch_size
