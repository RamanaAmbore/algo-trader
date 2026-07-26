"""
AppMessage feed endpoint.

GET /api/messages
    Query params:
      tags    — comma-separated tag filter (OR / overlap match)
      limit   — max rows (default 50, max 200)
      since   — ISO-8601 UTC timestamp; return rows after this
      account — filter by exact account string

Returns newest-first. Requires auth (demo access allowed via auth_or_demo_guard).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import auth_or_demo_guard
from backend.api.database import async_session
from backend.api.models import AppMessage
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


class AppMessageRow(msgspec.Struct):
    """One row returned by GET /api/messages."""
    id:           int
    created_at:   str            # ISO-8601 UTC
    level:        str
    tags:         list[str]
    title:        Optional[str]
    body:         str
    account:      Optional[str]
    symbol:       Optional[str]
    data:         Optional[dict]
    retain_until: Optional[str]  # ISO date or None


async def list_messages(
    tags: Optional[str] = None,
    limit: Optional[int] = None,
    since: Optional[str] = None,
    account: Optional[str] = None,
) -> list[AppMessageRow]:
    """Return recent AppMessage rows newest-first.

    tags    — comma-separated; returns rows where tags overlap (OR semantics).
    limit   — clipped to [1, 500]; default 50.
    since   — ISO-8601 UTC or date string; rows with created_at > since only.
    account — exact match on account column.
    """
    _limit = min(max(1, limit or _DEFAULT_LIMIT), _MAX_LIMIT)

    tag_list: list[str] = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"since={since!r} is not a valid ISO-8601 timestamp",
            )

    try:
        async with async_session() as session:
            stmt = (
                select(AppMessage)
                .order_by(AppMessage.created_at.desc())
                .limit(_limit)
            )
            if tag_list:
                stmt = stmt.where(AppMessage.tags.overlap(tag_list))
            if since_dt is not None:
                stmt = stmt.where(AppMessage.created_at > since_dt)
            if account:
                stmt = stmt.where(AppMessage.account == account)

            rows = (await session.execute(stmt)).scalars().all()

        return [
            AppMessageRow(
                id=r.id,
                created_at=r.created_at.isoformat(),
                level=r.level,
                tags=r.tags or [],
                title=r.title,
                body=r.body,
                account=r.account,
                symbol=r.symbol,
                data=r.data,
                retain_until=(
                    r.retain_until.isoformat() if r.retain_until else None
                ),
            )
            for r in rows
        ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("MessagesController: list_messages failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


class MessagesController(Controller):
    path = "/api/messages"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_messages(
        self,
        tags: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        account: Optional[str] = None,
    ) -> list[AppMessageRow]:
        return await list_messages(
            tags=tags, limit=limit, since=since, account=account
        )
