"""
Unified log feed.

GET /api/logs/unified?limit=50&kinds=…&accounts=…&since=ISO

Merges algo_order_events (order lifecycle) and agent_events (rule fires)
into one flat list sorted newest-first. Frontend surfaces use this single
endpoint instead of fetching the two streams separately and merging
client-side.

Kept separate from orders.py to avoid the controller growing further.
"""

import re

import msgspec
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import auth_or_demo_guard, is_admin_request
from backend.api.database import async_session
from backend.api.models import Agent, AgentEvent, AlgoOrder, AlgoOrderEvent
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_column

import pandas as pd

logger = get_logger(__name__)


class UnifiedLogRow(msgspec.Struct):
    """One row in the merged log feed.

    source ∈ 'order' | 'agent'
    kind   ∈ placed / chase_modify / fill / unfill / reject /
              preflight_ok / preflight_block / cancel / postback   (order)
            ∈ agent_fire / agent_match / agent_action_success /
              agent_action_error / agent_skipped / agent_paused     (agent)
    """
    id: int
    source: str           # 'order' | 'agent'
    ts: str               # ISO-8601 UTC
    kind: str
    message: str
    order_id: int | None
    agent_slug: str | None
    account: str | None
    payload_json: str | None


_ACCOUNT_RE = re.compile(r'\b([A-Z]{2})\d{4,8}\b')


def _mask_account(val: str | None) -> str | None:
    if val is None:
        return None
    return _ACCOUNT_RE.sub(r'\1####', val)


def _mask_payload(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _ACCOUNT_RE.sub(r'\1####', raw)


class LogsController(Controller):
    path = "/api/logs"
    guards = [auth_or_demo_guard]

    @get("/unified")
    async def unified_log(
        self,
        request: Request,
        limit: int = 50,
        kinds: str = "",
        accounts: str = "",
        since: str = "",
    ) -> list[UnifiedLogRow]:
        """Merged order-event + agent-event stream, newest-first.

        Query params:
          limit    — max rows returned (cap 200, default 50)
          kinds    — comma-separated kind filter; empty = all
          accounts — comma-separated account ids; matches row.account or
                     payload_json content; empty = all
          since    — ISO timestamp; only rows newer than this
        """
        from datetime import datetime, timezone
        from sqlalchemy import desc, select, or_, and_
        from sqlalchemy.orm import joinedload

        limit = max(1, min(limit, 200))
        kind_set = {k.strip() for k in kinds.split(",") if k.strip()} if kinds else set()
        acct_set = {a.strip() for a in accounts.split(",") if a.strip()} if accounts else set()
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid since= value: {since!r}")

        do_mask = not is_admin_request(request)
        mask = _mask_account if do_mask else (lambda x: x)
        mask_p = _mask_payload if do_mask else (lambda x: x)

        # ── order events ──────────────────────────────────────────────
        async with async_session() as s:
            oe_q = (
                select(AlgoOrderEvent, AlgoOrder.account)
                .join(AlgoOrder, AlgoOrderEvent.order_id == AlgoOrder.id)
                .order_by(desc(AlgoOrderEvent.ts))
                .limit(limit * 2)          # fetch more, filter + interleave below
            )
            if since_dt:
                oe_q = oe_q.where(AlgoOrderEvent.ts > since_dt)
            if kind_set:
                oe_q = oe_q.where(AlgoOrderEvent.kind.in_(kind_set))
            oe_rows = (await s.execute(oe_q)).all()

            # ── agent events + slug join ──────────────────────────────
            ae_q = (
                select(AgentEvent, Agent.slug)
                .join(Agent, AgentEvent.agent_id == Agent.id)
                .order_by(desc(AgentEvent.timestamp))
                .limit(limit * 2)
            )
            if since_dt:
                ae_q = ae_q.where(AgentEvent.timestamp > since_dt)
            if kind_set:
                # Map agent event_type to unified kind — same names used here
                ae_q = ae_q.where(AgentEvent.event_type.in_(kind_set))
            ae_rows = (await s.execute(ae_q)).all()

        # ── build unified rows ────────────────────────────────────────
        rows: list[UnifiedLogRow] = []

        for oe, account in oe_rows:
            acct_val = mask(account)
            if acct_set:
                raw = account or ""
                payload_raw = oe.payload_json or ""
                if not any(a in raw or a in payload_raw for a in acct_set):
                    continue
            rows.append(UnifiedLogRow(
                id=oe.id,
                source="order",
                ts=oe.ts.isoformat() if oe.ts else "",
                kind=oe.kind,
                message=oe.message or "",
                order_id=oe.order_id,
                agent_slug=None,
                account=acct_val,
                payload_json=mask_p(oe.payload_json),
            ))

        for ae, slug in ae_rows:
            kind = ae.event_type or "agent_fire"
            if kind_set and kind not in kind_set:
                continue
            # Extract account from detail / trigger_condition if present
            acct_raw = None
            detail_str = ae.detail or ae.trigger_condition or ""
            m = re.search(r'\b([A-Z]{2}\d{4,8})\b', detail_str)
            if m:
                acct_raw = m.group(1)
            if acct_set and acct_raw and acct_raw not in acct_set:
                continue
            rows.append(UnifiedLogRow(
                id=ae.id,
                source="agent",
                ts=ae.timestamp.isoformat() if ae.timestamp else "",
                kind=kind,
                message=ae.detail or ae.trigger_condition or "",
                order_id=None,
                agent_slug=slug,
                account=mask(acct_raw) if acct_raw else None,
                payload_json=None,
            ))

        # Sort merged list newest-first, cap to limit
        rows.sort(key=lambda r: r.ts, reverse=True)
        return rows[:limit]
