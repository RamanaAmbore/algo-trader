"""
Agents API routes and WebSocket.

WS   /ws/algo             — real-time event stream
"""

import asyncio
import json

from litestar import WebSocket
from litestar import websocket as ws_handler
from litestar.exceptions import WebSocketDisconnect

from backend.api.database import async_session
from backend.api.models import AlgoEvent
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Module-level state — shared across requests
_ws_clients: set[asyncio.Queue] = set()


def _broadcast_event(event_type: str, detail: dict = None):
    """
    Push event to all WebSocket clients and queue it for persistence.
    The persistence writer flushes the buffer every second so a burst of
    agent fires collapses into one batched INSERT instead of spawning a
    bare `create_task` per event (which used to accumulate unbounded
    under high tick cadences).
    """
    msg = json.dumps({"event": event_type, **(detail or {})})
    for q in list(_ws_clients):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass
    _persist_buffer.append((event_type, detail))


# Event rows accumulate here and are flushed to algo_events every second
# by `_persist_flush_loop()` (started on app startup via `start_persist_flush`).
_persist_buffer: list[tuple[str, dict | None]] = []
_persist_flush_task: asyncio.Task | None = None


async def _persist_flush_loop(interval: float = 1.0) -> None:
    """Drain _persist_buffer once per second with a single commit."""
    while True:
        try:
            await asyncio.sleep(interval)
            if not _persist_buffer:
                continue
            batch = _persist_buffer[:]
            _persist_buffer.clear()
            try:
                async with async_session() as session:
                    for event_type, detail in batch:
                        session.add(AlgoEvent(
                            event_type=event_type,
                            detail=json.dumps(detail) if detail else None,
                        ))
                    await session.commit()
            except Exception as e:
                logger.warning(f"Algo: batched event persist failed ({len(batch)} rows): {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Algo: persist-flush loop hiccup: {e}")


def start_persist_flush() -> None:
    """Kick off the background persist loop. Safe to call multiple times."""
    global _persist_flush_task
    if _persist_flush_task is None or _persist_flush_task.done():
        _persist_flush_task = asyncio.create_task(
            _persist_flush_loop(), name="algo-persist-flush",
        )


# ---------------------------------------------------------------------------
# WebSocket — real-time event stream
# ---------------------------------------------------------------------------

@ws_handler("/ws/algo")
async def algo_ws_handler(socket: WebSocket) -> None:
    await socket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _ws_clients.add(queue)
    logger.info(f"Algo WS: client connected, total={len(_ws_clients)}")

    async def _send_loop():
        while True:
            msg = await queue.get()
            await socket.send_data(msg)

    async def _recv_loop():
        while True:
            data = await socket.receive_data(mode="text")
            if data == "ping":
                await socket.send_data("pong")

    send_task = asyncio.create_task(_send_loop())
    recv_task = asyncio.create_task(_recv_loop())

    try:
        _done, pending = await asyncio.wait(
            [send_task, recv_task], return_when=asyncio.FIRST_EXCEPTION,
        )
        for t in pending:
            t.cancel()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.discard(queue)
        logger.info(f"Algo WS: client disconnected, total={len(_ws_clients)}")
        try:
            await socket.close()
        except Exception:
            pass
