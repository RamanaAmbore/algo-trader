"""
Agents API routes and WebSocket.

WS   /ws/algo             — real-time event stream
"""

import asyncio
import json

from litestar import WebSocket
from litestar import websocket as ws_handler
from litestar.exceptions import WebSocketDisconnect

from backend.api.models import AlgoEvent
from backend.api.persistence.event_queue import EventQueue as _EventQueue
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Module-level state — shared across requests
_ws_clients: set[asyncio.Queue] = set()

# EventQueue for AlgoEvent rows (WS broadcast event log).
# Replaces the old _persist_buffer + _persist_flush_loop pattern with
# the canonical EventQueue so AlgoEvent flush shares the same class as
# AgentEvent / AlgoOrderEvent / McpAudit.
algo_event_queue: _EventQueue = _EventQueue(
    AlgoEvent,
    name="algo_event",
    batch_size=500,
    flush_interval_s=1.0,
    max_queue=10_000,
    on_full="drop",
)


def _broadcast_event(event_type: str, detail: dict = None):
    """
    Push event to all WebSocket clients and enqueue it for persistence.
    The EventQueue flush fires every second so a burst of agent fires
    collapses into one bulk INSERT instead of spawning a task per event.
    """
    msg = json.dumps({"event": event_type, **(detail or {})})
    for q in list(_ws_clients):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass
    # Fire-and-forget enqueue — coroutine is scheduled on the running loop.
    asyncio.ensure_future(algo_event_queue.enqueue(
        event_type=event_type,
        detail=json.dumps(detail) if detail else None,
    ))


def start_persist_flush() -> None:
    """Start the algo_event_queue flush task. Safe to call multiple times."""
    asyncio.ensure_future(algo_event_queue.start())


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
