"""
Audit log read endpoint.

GET /api/admin/audit — paginated query of the audit_log table with
filters by actor / action / target / date range. Gated by the
`view_audit` capability (admin / risk / ops). Writes happen entirely
via the AuditMiddleware; this controller is read-only.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import msgspec
from litestar import Controller, get
from sqlalchemy import select, and_, desc

from backend.api.database import async_session
from backend.api.models import AuditLog
from backend.api.rbac import cap_guard


class AuditRow(msgspec.Struct):
    id: int
    actor_user_id: Optional[int]
    actor_username: str
    actor_role: str
    action: str
    category: Optional[str]
    method: str
    path: str
    target_type: Optional[str]
    target_id: Optional[str]
    status_code: int
    summary: Optional[str]
    request_id: str
    client_ip: Optional[str]
    user_agent: Optional[str]
    created_at: str


class AuditResponse(msgspec.Struct):
    rows: list[AuditRow]
    total: int
    limit: int
    offset: int


class AuditController(Controller):
    path = "/api/admin/audit"
    # `view_audit` covers admin / risk / ops — the three roles that
    # have legitimate reason to read the forensic trail. Trader +
    # observer + demo intentionally excluded.

    @get("/", guards=[cap_guard("view_audit")])
    async def list_audit(
        self,
        actor:        Optional[str] = None,
        action:       Optional[str] = None,
        category:     Optional[str] = None,
        target_type:  Optional[str] = None,
        target_id:    Optional[str] = None,
        since_hours:  Optional[int] = None,
        status_code:  Optional[int] = None,
        limit:        int = 50,
        offset:       int = 0,
    ) -> AuditResponse:
        """Filtered audit query. All filters are optional + AND-combined.

        - actor: matches actor_username (exact, case-insensitive).
        - action: substring match on the action label (`POST /api/...`).
        - target_type / target_id: exact match on the structured
          target field — useful for "what happened to broker ZG0790".
        - since_hours: rows newer than N hours (defaults to all-time).
        - status_code: exact match on the HTTP status code.

        Limit is capped at 500 server-side; offset is unbounded but
        the UI uses 50/page so large offsets shouldn't happen on the
        hot path.
        """
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))

        conditions = []
        if actor:
            conditions.append(AuditLog.actor_username.ilike(actor.strip()))
        if action:
            conditions.append(AuditLog.action.ilike(f"%{action.strip()}%"))
        if category:
            # Category supports comma-separated OR (e.g. 'order.fill,order.place')
            # so a "show all order events" pill in the UI can pass multiple
            # categories in one call.
            cats = [c.strip() for c in category.split(",") if c.strip()]
            if cats:
                conditions.append(AuditLog.category.in_(cats))
        if target_type:
            conditions.append(AuditLog.target_type == target_type.strip())
        if target_id:
            conditions.append(AuditLog.target_id == target_id.strip())
        if since_hours and since_hours > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
            conditions.append(AuditLog.created_at >= cutoff)
        if status_code:
            conditions.append(AuditLog.status_code == int(status_code))

        async with async_session() as session:
            # Count + page in two queries so the UI can render a
            # "showing 1-50 of 1247" footer without scanning the
            # full table.
            count_q = select(AuditLog.id)
            page_q = select(AuditLog)
            if conditions:
                count_q = count_q.where(and_(*conditions))
                page_q  = page_q.where(and_(*conditions))
            total = (await session.execute(
                select(AuditLog.id.distinct()).where(and_(*conditions)) if conditions
                else select(AuditLog.id)
            )).scalars().all()
            total_count = len(total)

            rows = (await session.execute(
                page_q.order_by(desc(AuditLog.created_at))
                      .limit(limit).offset(offset)
            )).scalars().all()

        return AuditResponse(
            rows=[
                AuditRow(
                    id=r.id,
                    actor_user_id=r.actor_user_id,
                    actor_username=r.actor_username,
                    actor_role=r.actor_role,
                    action=r.action,
                    category=r.category,
                    method=r.method,
                    path=r.path,
                    target_type=r.target_type,
                    target_id=r.target_id,
                    status_code=r.status_code,
                    summary=r.summary,
                    request_id=r.request_id,
                    client_ip=r.client_ip,
                    user_agent=r.user_agent,
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ],
            total=total_count,
            limit=limit,
            offset=offset,
        )
