"""Broker-connection event queue.

Owned by the conn_service process. The EventQueue writes batches of
BrokerConnectionEvent rows to the DB every 5 s (or when 200 rows
accumulate). Callers use the fire-and-forget `_emit_conn_event` helper
which enqueues without blocking — if the queue is full the event is
silently dropped (on_full="drop").

Import guard: this module imports from backend.api so it MUST NOT be
imported at the top level of any module that loads in the main API
process before the DB engine is initialised. Consumers inside
conn_service call it directly; consumers in connections.py and
broker_apis.py reach it via a lazy import wrapper.
"""

from backend.api.database import shared_async_session
from backend.api.models import BrokerConnectionEvent
from backend.api.persistence.event_queue import EventQueue

broker_conn_event_queue: EventQueue = EventQueue(
    BrokerConnectionEvent,
    name="broker_conn",
    batch_size=200,
    flush_interval_s=5.0,
    max_queue=5_000,
    on_full="drop",
    session_factory=shared_async_session,
)


def _emit_conn_event(
    account: str,
    broker_id: str,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Enqueue one broker-connection event. Fire-and-forget.

    Safe to call from sync context (uses enqueue_nowait). Silently
    discards if the queue has not been started yet (task is None or
    done) or if the queue is full.
    """
    try:
        if not broker_conn_event_queue.get_health().get("worker_alive"):
            return
        broker_conn_event_queue.enqueue_nowait(
            account=account,
            broker_id=broker_id,
            event_type=event_type,
            detail=detail,
        )
    except Exception:
        pass
