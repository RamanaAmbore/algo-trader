"""Fill records: each FILLED transition is logged once, tagged orders, for the fill event agent.

The record carries the mode, so the agent (not this module) decides which fills alert.
Delivery lives in event_agents.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from backend.api.database import async_session
from backend.api.models import Agent
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def format_fill_message(row, when: datetime | None = None) -> tuple[str, str]:
    from backend.shared.helpers.date_time_utils import format_dual_tz
    side = str(row.transaction_type or "").upper() or "ORDER"
    qty = int(row.quantity or 0)
    symbol = str(row.symbol or "")
    title = f"Order filled: {side} {qty} {symbol}"
    # Source line (2026-10-15, revised 2026-10-17 — operator: "telegram
    # order fill alert does not show manual... any order triggered by an
    # agent... it's basically how it was triggered. presently it is
    # shown as a tag with the log") — the ORIGINAL 2026-10-15 fix read
    # `row.engine`, reasoning it mirrored the Agents log's own chip. That
    # was wrong: `engine` is the EXECUTION mode (sim/paper/live/replay/
    # shadow/expiry) — a manually-placed LIVE order gets `engine="live"`
    # (orders_place.py), never the literal string "manual", so "Source:
    # MANUAL" was structurally unreachable for any real order. The
    # Agents log's chip (LogPanel.svelte's `log-agent-chip`) actually
    # resolves `AlgoOrder.agent_id` -> the originating `agents` row's
    # `name` — NULL/the synthetic "Manual operator order" agent
    # (agent_engine.py's MANUAL_AGENT) for a human-placed order, the real
    # automation agent's name otherwise (e.g. "Chase close positions").
    # `agent_name` is resolved once, async, in `notify_fills()` below
    # (where a DB session is natural) and threaded through the log
    # record's `extra` dict, since `_render_fill` (event_agents.py)
    # rebuilds this `row` from THAT dict alone, long after the DB row
    # itself is gone.
    source = str(getattr(row, "agent_name", None) or "Manual operator order").upper()
    from backend.shared.helpers.utils import mask_account
    # Account masked (2026-10 audit fix) — this is the HIGHEST-VOLUME live
    # notification (every fill, Telegram+ntfy); matches the masking
    # convention every other correctly-masked alert path already uses.
    masked_account = mask_account(row.account) if row.account else "-"
    # Missing-vs-zero convention (CLAUDE.md): a genuinely absent fill
    # price must never render as a fabricated "0.00".
    fill_px = (f"{float(row.fill_price):.2f}" if row.fill_price is not None else "—")
    body = "\n".join([
        f"Account: {masked_account}",
        f"{side} {qty} {symbol} ({row.exchange or '-'}) @ {fill_px}",
        f"Product: {row.product or '-'}",
        f"Source: {source}",
        f"Order id: {row.id}",
        f"Time: {format_dual_tz(when or datetime.now(timezone.utc))}",
    ])
    return title, body


async def _resolve_agent_names(agent_ids: set) -> dict:
    """Batch-resolve `agents.id -> name` for every distinct agent_id in a
    fill batch, in ONE query — same {id: name} shape LogPanel.svelte's
    own agent chip ultimately reflects (via the API's `agent_name` field
    on order rows), so the Telegram Source line and the Agents log chip
    always agree on wording for the same order."""
    agent_ids = {a for a in agent_ids if a is not None}
    if not agent_ids:
        return {}
    try:
        async with async_session() as s:
            rows = (await s.execute(
                select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
            )).all()
        return {aid: name for aid, name in rows}
    except Exception as e:
        logger.warning(f"fill agent-name resolution failed for ids={agent_ids}: {e}")
        return {}


async def notify_fills(rows: list) -> None:
    names = await _resolve_agent_names({getattr(r, "agent_id", None) for r in rows})
    for row in rows:
        try:
            agent_id = getattr(row, "agent_id", None)
            logger.info(
                "order filled",
                extra={
                    "tags": ["orders"],
                    "alert_event": "filled",
                    "mode": str(getattr(row, "mode", "")),
                    "order_id": row.id,
                    "account": row.account,
                    "symbol": row.symbol,
                    "exchange": row.exchange,
                    "transaction_type": row.transaction_type,
                    "quantity": row.quantity,
                    "fill_price": row.fill_price,
                    "product": row.product,
                    "engine": getattr(row, "engine", None),
                    "agent_name": names.get(agent_id),
                },
            )
        except Exception as e:
            logger.warning(f"fill log failed for order_id={row.id}: {e}")
