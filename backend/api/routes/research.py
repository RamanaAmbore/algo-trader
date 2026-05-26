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
from backend.api.models import Agent, ResearchThread
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


class PromoteRequest(msgspec.Struct):
    """Promote a research thread into a draft Agent (status=inactive).

    The agent ships disabled — the operator's next step is "Run in
    Simulator" from /agents to validate the condition tree before
    activating. Per industry pattern (Composer.trade, IBKR TraderGPT),
    no LLM-initiated draft is ever activated automatically.
    """
    name:             str
    conditions:       dict             # v2 grammar condition tree
    actions:          list = msgspec.field(default_factory=list)
    events:           list = msgspec.field(default_factory=list)
    description:      str = ""
    scope:            str = "total"     # total / per_account
    schedule:         str = "market_hours"
    cooldown_minutes: int = 30


class DraftInfo(msgspec.Struct):
    """Joined view: thread → its linked draft agent (status=inactive).

    Drives the Drafts tab on /admin/research. Excludes threads whose
    draft_agent_id is NULL, and threads whose linked agent has been
    activated (status=active) — those graduate out of the Drafts list
    so it always reflects "still pending review"."""
    thread_id:         int
    symbol:            str
    title:             str
    confidence:        str
    thesis_text:       str | None
    agent_id:          int
    agent_slug:        str
    agent_name:        str
    agent_status:      str
    agent_scope:       str
    agent_schedule:    str | None
    agent_cooldown:    int
    agent_trade_mode:  str | None
    created_at:        str
    updated_at:        str


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


_VALID_CONF  = {"bull", "bear", "neutral", "unsure"}
_VALID_SCOPE = {"total", "per_account"}


def _slugify(s: str) -> str:
    """Lower-kebab-case, ascii-safe. Used for auto-generating an Agent
    slug from a thread + name pair when the LLM doesn't supply one."""
    out = []
    prev_dash = True
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "draft"


async def _unique_slug(session, base: str) -> str:
    """Append -2, -3, … until the slug is unique in the agents table."""
    candidate = base
    n = 2
    while True:
        existing = await session.execute(select(Agent).where(Agent.slug == candidate))
        if existing.scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


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
        # Auto-title via Gemini Flash free tier when title is blank.
        # Defensive: the helper falls back to a deterministic stub if
        # genai is disabled / SDK missing / quota exhausted / parse
        # fails, so this never blocks thread creation.
        title = (data.title or "").strip()
        if not title:
            try:
                from backend.shared.helpers.genai_helpers import auto_title
                title = auto_title(sym, data.thesis_text)
            except Exception as e:
                logger.warning(f"auto_title raised (using stub): {e}")
                title = f"{sym} research"
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

    @post("/threads/{thread_id:int}/promote")
    async def promote_thread(self, thread_id: int, data: PromoteRequest) -> DraftInfo:
        """Promote a research thread into an inactive draft Agent.

        Creates a new agent row with status=inactive, sets
        thread.draft_agent_id = agent.id, returns the joined view. If
        the thread already has a draft_agent_id, returns 409 — operator
        must un-link first to avoid silently overwriting.

        Industry-standard safety: the agent ships INACTIVE and PAPER
        (trade_mode=paper) regardless of what the caller asks. Operator
        must explicitly flip status + trade_mode on /agents — this
        endpoint cannot create an active or live agent.
        """
        # Validate inputs first.
        if not data.name or not data.name.strip():
            raise HTTPException(status_code=400, detail="name is required")
        if data.scope not in _VALID_SCOPE:
            raise HTTPException(status_code=400,
                                detail=f"scope must be one of {sorted(_VALID_SCOPE)}")
        if not isinstance(data.conditions, dict) or not data.conditions:
            raise HTTPException(status_code=400, detail="conditions must be a non-empty dict")
        # Optional grammar dry-check — surface a precise error if the
        # condition tree references unknown tokens. Keeps the operator
        # from landing a broken draft in the Drafts tab.
        try:
            from backend.api.algo.agent_evaluator import validate as validate_condition
            errors = validate_condition(data.conditions)
            if errors:
                raise HTTPException(status_code=400,
                                    detail=f"condition validation: {'; '.join(errors)}")
        except HTTPException:
            raise
        except Exception as e:
            # Grammar registry not loaded yet — log + continue (the
            # /agents page's own validator will catch it before activate).
            logger.warning(f"promote: skipped grammar validation: {e}")

        async with async_session() as s:
            thread = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
            if not thread:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
            if thread.draft_agent_id:
                raise HTTPException(status_code=409,
                    detail=f"Thread already promoted to agent #{thread.draft_agent_id}")

            base = _slugify(f"{thread.symbol}-{data.name}")[:48]
            slug = await _unique_slug(s, base)
            agent = Agent(
                slug=slug,
                name=data.name.strip()[:128],
                description=data.description.strip()[:1024] if data.description else
                            f"Promoted from research thread #{thread.id} ({thread.symbol})",
                conditions=data.conditions,
                events=data.events,
                actions=data.actions,
                scope=data.scope,
                schedule=data.schedule,
                cooldown_minutes=max(1, int(data.cooldown_minutes or 30)),
                status="inactive",
                trade_mode="paper",
            )
            s.add(agent)
            await s.flush()           # populate agent.id
            thread.draft_agent_id = agent.id
            await s.commit()
            await s.refresh(thread)
            await s.refresh(agent)

        logger.info(
            f"research thread #{thread.id} promoted → agent #{agent.id} "
            f"slug={slug!r} (status=inactive, trade_mode=paper)"
        )
        return DraftInfo(
            thread_id=thread.id,
            symbol=thread.symbol,
            title=thread.title or "",
            confidence=thread.confidence or "unsure",
            thesis_text=thread.thesis_text,
            agent_id=agent.id,
            agent_slug=agent.slug,
            agent_name=agent.name,
            agent_status=agent.status,
            agent_scope=agent.scope,
            agent_schedule=agent.schedule,
            agent_cooldown=int(agent.cooldown_minutes or 0),
            agent_trade_mode=agent.trade_mode,
            created_at=thread.created_at.isoformat() if thread.created_at else "",
            updated_at=thread.updated_at.isoformat() if thread.updated_at else "",
        )

    @get("/drafts")
    async def list_drafts(self, limit: int = 200) -> list[DraftInfo]:
        """Threads with a linked draft Agent that's still inactive.

        Activating an agent on /agents naturally graduates it out of
        this list — no manual cleanup needed."""
        async with async_session() as s:
            rows = (await s.execute(
                select(ResearchThread, Agent)
                .join(Agent, Agent.id == ResearchThread.draft_agent_id)
                .where(ResearchThread.draft_agent_id.is_not(None))
                .where(Agent.status == "inactive")
                .order_by(ResearchThread.updated_at.desc())
                .limit(max(1, min(500, limit)))
            )).all()
        return [
            DraftInfo(
                thread_id=t.id, symbol=t.symbol, title=t.title or "",
                confidence=t.confidence or "unsure",
                thesis_text=t.thesis_text,
                agent_id=a.id, agent_slug=a.slug, agent_name=a.name,
                agent_status=a.status, agent_scope=a.scope,
                agent_schedule=a.schedule,
                agent_cooldown=int(a.cooldown_minutes or 0),
                agent_trade_mode=a.trade_mode,
                created_at=t.created_at.isoformat() if t.created_at else "",
                updated_at=t.updated_at.isoformat() if t.updated_at else "",
            )
            for (t, a) in rows
        ]
