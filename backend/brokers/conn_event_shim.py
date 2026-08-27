"""Shared lazy-import shim for emitting broker connection events.

Both broker_apis.py and dhan.py need to fire connection events without a
hard import on conn_events (which owns the DB session factory and must
only be imported inside the conn_service process). This module provides
one canonical implementation used by both.
"""


def _emit_conn_event(
    account: str,
    broker_id: str,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Lazy-import shim — emit a connection event without a hard dependency
    on conn_events. Silently swallows all exceptions so broker-layer code
    never crashes due to event emission failures."""
    try:
        from backend.brokers.service.conn_events import _emit_conn_event as _fire
        _fire(account, broker_id, event_type, detail)
    except Exception:
        pass
