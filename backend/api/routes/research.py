"""
`/api/research/*` — research-thread CRUD for the /admin/research page.

A research thread captures one MCP-driven session ("Research RELIANCE")
with its transcript, the synthesized thesis, and (after promotion) the
draft Agent it produced. Read by the Lab page; written by the MCP server
(via the operator's JWT) as the chat unfolds.

No GenAI is invoked from this route — it just persists what the operator's
Claude Code session sends back. The transcript is opaque JSONB; the route
doesn't parse tool calls or LLM tokens. That keeps this layer cheap and
free of any LLM-provider dependency.
"""

from __future__ import annotations

from datetime import datetime

import msgspec
from litestar import Controller, Request, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, delete as sa_delete

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import ResearchThread
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────

class ThreadInfo(msgspec.Struct):
    id:                int
    symbol:            str
    title:             str
    thesis_text:       str | None
    confidence:        str            # bull / bear / neutral / unsure
    transcript:        list           # opaque list of {role, content, tool_calls?, ...}
    draft_agent_id:    int | None
    created_by_user_id: int | None
    created_at:        str
    updated_at:        str


class ThreadSummary(msgspec.Struct):
    """Lightweight row for the thread-list rail — no transcript blob."""
    id:                int
    symbol:            str
    title:             str
    confidence:        str
    draft_agent_id:    int | None
    transcript_len:    int
    created_at:        str
    updated_at:        str


class ThreadCreate(msgspec.Struct):
    symbol:            str
    title:             str = ""
    thesis_text:       str | None = None
    confidence:        str = "unsure"
    transcript:        list = msgspec.field(default_factory=list)


class ThreadUpdate(msgspec.Struct):
    title:             str | None = None
    thesis_text:       str | None = None
    confidence:        str | None = None
    transcript:        list | None = None
    draft_agent_id:    int | None = None


# ── Helpers ───────────────────────────────────────────────────────────

def _to_info(row: ResearchThread) -> ThreadInfo:
    return ThreadInfo(
        id=row.id,
        symbol=row.symbol,
        title=row.title or "",
        thesis_text=row.thesis_text,
        confidence=row.confidence or "unsure",
        transcript=row.transcript or [],
        draft_agent_id=row.draft_agent_id,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _to_summary(row: ResearchThread) -> ThreadSummary:
    return ThreadSummary(
        id=row.id,
        symbol=row.symbol,
        title=row.title or "",
        confidence=row.confidence or "unsure",
        draft_agent_id=row.draft_agent_id,
        transcript_len=len(row.transcript or []),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


_VALID_CONF = {"bull", "bear", "neutral", "unsure"}


def _user_id(connection) -> int | None:
    payload = getattr(connection.state, "token_payload", {}) or {}
    sub = payload.get("sub")
    try:
        return int(sub) if sub is not None else None
    except (TypeError, ValueError):
        return None


# ── Controller ────────────────────────────────────────────────────────

class ResearchController(Controller):
    path   = "/api/research"
    guards = [admin_guard]

    @get("/threads")
    async def list_threads(self, symbol: str | None = None, limit: int = 100) -> list[ThreadSummary]:
        async with async_session() as s:
            q = select(ResearchThread).order_by(ResearchThread.updated_at.desc())
            if symbol:
                q = q.where(ResearchThread.symbol == symbol.upper())
            q = q.limit(max(1, min(500, limit)))
            rows = (await s.execute(q)).scalars().all()
        return [_to_summary(r) for r in rows]

    @get("/threads/{thread_id:int}")
    async def get_thread(self, thread_id: int) -> ThreadInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        return _to_info(row)

    @post("/threads")
    async def create_thread(self, data: ThreadCreate, request: Request) -> ThreadInfo:
        sym = (data.symbol or "").upper().strip()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        if data.confidence not in _VALID_CONF:
            raise HTTPException(status_code=400,
                                detail=f"confidence must be one of {sorted(_VALID_CONF)}")
        title = (data.title or "").strip() or f"{sym} research"
        async with async_session() as s:
            row = ResearchThread(
                symbol=sym,
                title=title[:256],
                thesis_text=data.thesis_text,
                confidence=data.confidence,
                transcript=data.transcript or [],
                created_by_user_id=_user_id(request),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        logger.info(f"research thread created: id={row.id} sym={sym}")
        return _to_info(row)

    @patch("/threads/{thread_id:int}")
    async def update_thread(self, thread_id: int, data: ThreadUpdate) -> ThreadInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
            if data.title is not None:
                row.title = data.title[:256]
            if data.thesis_text is not None:
                row.thesis_text = data.thesis_text
            if data.confidence is not None:
                if data.confidence not in _VALID_CONF:
                    raise HTTPException(status_code=400,
                                        detail=f"confidence must be one of {sorted(_VALID_CONF)}")
                row.confidence = data.confidence
            if data.transcript is not None:
                row.transcript = data.transcript
            if data.draft_agent_id is not None:
                row.draft_agent_id = data.draft_agent_id
            row.updated_at = datetime.utcnow()
            await s.commit()
            await s.refresh(row)
        return _to_info(row)

    @delete("/threads/{thread_id:int}", status_code=204)
    async def delete_thread(self, thread_id: int) -> None:
        async with async_session() as s:
            result = await s.execute(
                sa_delete(ResearchThread).where(ResearchThread.id == thread_id)
            )
            await s.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
