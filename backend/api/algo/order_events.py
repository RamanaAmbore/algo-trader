"""
Append-only per-order event log helper.

One async function — `write_event` — that any callsite can fire-and-forget.
Errors are logged at WARNING and swallowed so a DB hiccup never bubbles into
the order-placement hot path.

`write_event` enqueues into `order_event_queue` (started at app startup)
which batches inserts every 1 s rather than opening a session per row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Module-level EventQueue for AlgoOrderEvent rows.
from backend.api.persistence.event_queue import EventQueue as _EventQueue


def _make_order_event_queue() -> _EventQueue:
    from backend.api.models import AlgoOrderEvent
    return _EventQueue(
        AlgoOrderEvent,
        name="order_event",
        batch_size=500,
        flush_interval_s=1.0,
        max_queue=10_000,
        on_full="drop",
    )


order_event_queue: _EventQueue = _make_order_event_queue()

VALID_KINDS = frozenset({
    "placed", "chase_modify", "fill", "unfill", "reject", "cancel",
    "postback", "margin_check", "preflight_ok", "preflight_block", "error",
})


async def write_event(
    order_id: int,
    kind: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert one row into algo_order_events.

    Tolerant of DB failures — logs a WARNING and returns without raising.
    Safe to call from any async context.

    Args:
        order_id: FK to algo_orders.id — the order this event belongs to.
        kind:     One of the VALID_KINDS literals (unknown values are stored
                  as-is with a debug warning so new kinds don't break callers).
        message:  Human-readable one-liner, max 500 chars.
        payload:  Optional structured detail (limit, fill_price, broker_response,
                  slippage, …). JSON-encoded into payload_json column.
    """
    if kind not in VALID_KINDS:
        logger.debug(f"order_events.write_event: unknown kind '{kind}' for order {order_id}")

    payload_json: str | None = None
    if payload:
        try:
            payload_json = json.dumps(payload, default=str)
        except Exception as enc_err:
            logger.debug(f"order_events: payload encode failed for order {order_id}: {enc_err}")

    # Clamp message to column length.
    message = str(message)[:500]

    try:
        await order_event_queue.enqueue(
            order_id=order_id,
            ts=datetime.now(timezone.utc),
            kind=kind,
            message=message,
            payload_json=payload_json,
        )
        logger.info(f"[order_event] order={order_id} kind={kind} — {message}")

    except Exception as db_err:
        logger.warning(
            f"order_events.write_event failed (order={order_id}, kind={kind}): {db_err}"
        )
