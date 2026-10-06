"""Tagged log storage: INFO and above land in log_events, batched off the logging thread.

The handler runs on the QueueListener thread, so it only enqueues. A main-loop task
drains the queue in batches. A full queue drops the record and counts it, so logging
never blocks. Storage failures go to stderr, never to the logger, so they cannot loop
back into the queue.
"""
import asyncio
import json
import logging
import queue
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from backend.shared.helpers.text_clean import to_plain

_QUEUE_MAX = 5000
_BATCH_MAX = 500
_FLUSH_S = 2.0
_PRUNE_EVERY_S = 3600.0
_LEVEL_REFRESH_S = 60.0
_COALESCE_S = 0.05
_MAX_TAGS = 20
_MAX_MESSAGE = 4000
_SKIP_PREFIXES = (
    "backend.shared.helpers.log_store",
    "backend.shared.helpers.alert_utils",
    "backend.shared.helpers.mail_utils",
    "backend.shared.helpers.error_alerts",
    "backend.api.algo.event_agents",
    "backend.api.persistence.write_queue",
    "sqlalchemy",
    "asyncio",
    "httpx",
    "urllib3",
)
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "tags"}

PROCESS = "api"

# Branch of the caller whose request is being served (set by the conn service
# from the X-Ramboq-Branch header). Records carry it as `origin`, so dev-driven
# work handled by the shared conn process never alerts from prod.
ORIGIN_BRANCH: ContextVar[str | None] = ContextVar("ramboq_origin_branch", default=None)


class OriginFilter(logging.Filter):
    """Stamp the calling request's origin onto each record, in the calling thread."""

    def filter(self, record: logging.LogRecord) -> bool:
        origin = ORIGIN_BRANCH.get()
        if origin is not None and not hasattr(record, "origin"):
            record.origin = origin
        return True

_loop: asyncio.AbstractEventLoop | None = None
_wake: asyncio.Event | None = None
_warned_tags: set[str] = set()


def _check_owner_tags(owner_tags) -> None:
    from backend.api.algo.grammar_registry import REGISTRY
    known = REGISTRY.log_tags
    if not known:
        return
    for tag in owner_tags:
        if tag not in known and tag not in _warned_tags:
            _warned_tags.add(tag)
            sys.stderr.write(f"log_store: tag '{tag}' is not in the tag catalog\n")


def _wake_writer() -> None:
    loop, wake = _loop, _wake
    if loop is None or wake is None:
        return
    try:
        loop.call_soon_threadsafe(wake.set)
    except RuntimeError:
        pass


def tags_for(record: logging.LogRecord) -> list[str]:
    """Level plus the owner's tags. A record without owner tags is tagged with its logger's last name."""
    owner = getattr(record, "tags", None)
    tags = [record.levelname.lower()]
    if owner:
        owner = [str(t) for t in owner]
        _check_owner_tags(owner)
        tags.extend(owner)
    else:
        tags.append(record.name.rsplit(".", 1)[-1])
    out: list[str] = []
    for tag in tags:
        if tag and tag not in out:
            out.append(tag)
    return out[:_MAX_TAGS]


def row_for(record: logging.LogRecord) -> dict:
    extra = {
        k: v for k, v in record.__dict__.items()
        if k not in _STANDARD_ATTRS and not k.startswith("_")
    }
    return {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc),
        "process": PROCESS,
        "level": record.levelname,
        "logger": record.name[:120],
        "message": to_plain(record.getMessage())[:_MAX_MESSAGE],
        "tags": tags_for(record),
        "extra": json.loads(json.dumps(extra, default=str)) if extra else None,
    }


class LogStoreHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self.level or record.name.startswith(_SKIP_PREFIXES):
                return
            self._q.put_nowait(row_for(record))
            _wake_writer()
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    def drain(self, limit: int) -> list[dict]:
        rows: list[dict] = []
        while len(rows) < limit:
            try:
                rows.append(self._q.get_nowait())
            except queue.Empty:
                break
        return rows


HANDLER = LogStoreHandler()


def _default_session_factory():
    from backend.api.database import async_session
    return async_session


async def insert_rows(rows: list[dict], session_factory=None) -> None:
    from sqlalchemy import insert
    from backend.api.models import LogEvent
    factory = session_factory or _default_session_factory()
    async with factory() as session:
        await session.execute(insert(LogEvent), rows)
        await session.commit()


async def prune(days: int, session_factory=None) -> None:
    from sqlalchemy import delete
    from backend.api.models import LogEvent
    factory = session_factory or _default_session_factory()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with factory() as session:
        await session.execute(delete(LogEvent).where(LogEvent.ts < cutoff))
        await session.commit()


async def flush_once(handler: LogStoreHandler = HANDLER, session_factory=None, dispatch=None) -> int:
    rows = handler.drain(_BATCH_MAX)
    if not rows:
        return 0
    if dispatch is not None:
        await dispatch(rows)
    try:
        await insert_rows(rows, session_factory)
    except Exception as e:
        handler.dropped += len(rows)
        sys.stderr.write(f"log_store: insert of {len(rows)} rows failed: {e}\n")
        return 0
    return len(rows)


def _min_level() -> int:
    from backend.shared.helpers.settings import get_int
    return get_int("log.db_min_level", logging.INFO)


def _event_dispatcher():
    from backend.api.algo.event_agents import dispatch_rows
    return dispatch_rows


async def run_forever(handler: LogStoreHandler = HANDLER) -> None:
    last_prune = 0.0
    last_level = 0.0
    while True:
        now = time.monotonic()
        try:
            if now - last_level >= _LEVEL_REFRESH_S:
                handler.setLevel(_min_level())
                last_level = now
            written = await flush_once(handler, dispatch=_event_dispatcher())
            if written == _BATCH_MAX:
                continue
            if now - last_prune >= _PRUNE_EVERY_S:
                from backend.shared.helpers.settings import get_int
                await prune(get_int("log.retention_days", 7))
                last_prune = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sys.stderr.write(f"log_store: writer error: {e}\n")
        try:
            await asyncio.wait_for(_wake.wait(), _FLUSH_S)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
        await asyncio.sleep(_COALESCE_S)


_task: asyncio.Task | None = None


async def start(process: str) -> None:
    global _task, PROCESS, _loop, _wake
    PROCESS = process
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    _task = asyncio.create_task(run_forever(), name="log_store")


_MAX_DRAIN_ROUNDS = 50


async def stop() -> None:
    """Stop the writer, then drain what is still queued so those records are stored and dispatched."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    for _ in range(_MAX_DRAIN_ROUNDS):
        if HANDLER._q.empty():
            break
        try:
            if await flush_once(HANDLER, dispatch=_event_dispatcher()) == 0:
                break
        except Exception as e:
            sys.stderr.write(f"log_store: shutdown drain failed: {e}\n")
            break
