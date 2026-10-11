"""
Agent template CRUD — admin endpoints for reusable notify/condition
sub-trees ($ref-able from inside an agent's events / conditions tree).

Operator vocabulary in v2.1+:
  • notify templates    — saved channel lists (telegram + email + log + …)
  • condition templates — saved condition sub-trees ($ref expands inline)
  • action templates    — RESERVED for a future stage

Routes (URL kept under /api/admin/fragments for back-compat with
pre-v2.1 callers; the underlying model + module are now AgentTemplate /
template_registry):

  GET    /api/admin/fragments[?kind=notify|condition]   list (active + inactive)
  GET    /api/admin/fragments/{id}                       read one
  POST   /api/admin/fragments                            create custom
  PATCH  /api/admin/fragments/{id}                       update — system rows toggle-only
  DELETE /api/admin/fragments/{id}                       custom only
  POST   /api/admin/fragments/reload                     rebuild in-memory cache

Every mutation calls TemplateRegistry.reload() so edits take effect
without a service restart — matches the grammar-token pattern.
"""

from __future__ import annotations

from datetime import datetime

import msgspec
from litestar import Controller, get, post, patch, delete
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.rbac import cap_guard
from backend.api.database import async_session
from backend.api.models import AgentTemplate
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── msgspec schemas ────────────────────────────────────────────────────

class FragmentOut(msgspec.Struct):
    id: int
    kind: str
    name: str
    body: list | dict
    description: str
    is_system: bool
    is_active: bool
    created_at: str | None = None
    updated_at: str | None = None


class FragmentCreate(msgspec.Struct):
    kind: str
    name: str
    body: list | dict
    description: str = ""


class FragmentPatch(msgspec.Struct):
    body: list | dict | None = None
    description: str | None = None
    is_active: bool | None = None


_VALID_KINDS = {"notify", "condition"}


def _to_out(row: AgentTemplate) -> FragmentOut:
    return FragmentOut(
        id=row.id, kind=row.kind, name=row.name, body=row.body,
        description=row.description or "", is_system=row.is_system,
        is_active=row.is_active,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _validate_condition_body(body: dict) -> None:
    """Stage 2 — dry-run a condition token's body through the SAME grammar
    validator `create_agent`/`update_agent` already use for an agent's own
    `conditions` tree (`agent_evaluator.validate()`). Without this, a Token
    saved via this route's own PATCH with an unknown metric, a malformed
    `quote.*` leaf missing the required `symbol` qualifier, or any other
    structurally-invalid condition tree was accepted with no error — every
    agent that references it via `{"$ref": <name>}` then silently evaluates
    to `[]` forever, with no indication anywhere of why.

    Sprint C (2026-10, Phase 2 "Automation redesign") retired the
    standalone TokensTab.svelte component that used to be this route's
    only caller for condition-kind edits — the new Templates kind-card
    (`/automation`) reads from the unified `kind='template'` Agent rows
    for display, but a condition-kind Token still has no CLI grammar
    (`agents.js`'s `compileTokenStmt` explicitly refuses `kind=condition`)
    and therefore no in-app Edit UI at all right now; this route is
    reachable only directly (`PATCH /api/admin/fragments/{id}`) until a
    future sprint extends the grammar. Notify-kind Tokens are unaffected
    — their Edit action on the Templates card routes through this same
    PATCH endpoint via the CLI decompile/recompile round-trip.

    Raises HTTPException(422) with the same `extra={"errors": errors}`
    shape `agents.py:_age_validate_threshold_conditions` already uses, for
    consistency across both save surfaces."""
    from backend.api.algo.agent_evaluator import validate as v2_validate
    errors = v2_validate(body)
    if errors:
        raise HTTPException(status_code=422,
            detail="; ".join(errors), extra={"errors": errors})


def _validate_body_shape(kind: str, body) -> None:
    """Stage 1 — sanity-check the body matches its kind. Notify must be
    a list of {channel, enabled} dicts; condition must be a dict (leaf
    or composite). Raises HTTPException(400) on bad shape. Condition
    bodies additionally run through `_validate_condition_body` (Stage 2)
    once the shape check passes — notify bodies are a channel list, not
    a condition tree, so they never go through the grammar validator."""
    if kind == "notify":
        if not isinstance(body, list):
            raise HTTPException(status_code=400,
                detail="notify token body must be a list of channel entries")
        for i, entry in enumerate(body):
            if not isinstance(entry, dict) or "channel" not in entry:
                raise HTTPException(status_code=400,
                    detail=f"notify token body[{i}] missing 'channel'")
    elif kind == "condition":
        if not isinstance(body, dict):
            raise HTTPException(status_code=400,
                detail="condition token body must be a dict (leaf or composite)")
        _validate_condition_body(body)
    else:
        raise HTTPException(status_code=400,
            detail=f"kind must be one of {sorted(_VALID_KINDS)}")


async def _reload_registry() -> None:
    try:
        from backend.api.algo.template_registry import REGISTRY
        await REGISTRY.reload()
    except Exception as e:
        logger.error(f"TemplateRegistry reload failed: {e}")


async def _load_fragment(frag_id: int):
    from backend.api.database import async_session
    async with async_session() as s:
        return (await s.execute(
            select(AgentTemplate).where(AgentTemplate.id == frag_id)
        )).scalar_one_or_none()


async def _load_bodies(kind: str) -> dict:
    from backend.api.database import async_session
    async with async_session() as s:
        rows = (await s.execute(
            select(AgentTemplate).where(AgentTemplate.kind == kind)
        )).scalars().all()
    return {r.name: r.body for r in rows}


async def _load_agents() -> list[dict]:
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        rows = (await s.execute(select(Agent))).scalars().all()
    return [{"slug": a.slug, "status": a.status, "conditions": a.conditions, "events": a.events}
            for a in rows]


# ── Controller ─────────────────────────────────────────────────────────

class AgentTemplateController(Controller):
    path = "/api/admin/fragments"

    @get("/", guards=[cap_guard("view_agents_catalog")])
    async def list_fragments(self, kind: str | None = None) -> list[FragmentOut]:
        async with async_session() as s:
            q = select(AgentTemplate).order_by(
                AgentTemplate.kind, AgentTemplate.name)
            if kind:
                if kind not in _VALID_KINDS:
                    raise HTTPException(status_code=400,
                        detail=f"kind must be one of {sorted(_VALID_KINDS)}")
                q = q.where(AgentTemplate.kind == kind)
            rows = (await s.execute(q)).scalars().all()
        return [_to_out(r) for r in rows]

    @get("/{frag_id:int}", guards=[cap_guard("view_agents_catalog")])
    async def get_fragment(self, frag_id: int) -> FragmentOut:
        async with async_session() as s:
            row = (await s.execute(
                select(AgentTemplate).where(AgentTemplate.id == frag_id)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404,
                detail=f"Token {frag_id} not found")
        return _to_out(row)

    @post("/", guards=[cap_guard("manage_own_agents")])
    async def create_fragment(self, data: FragmentCreate) -> FragmentOut:
        if data.kind not in _VALID_KINDS:
            raise HTTPException(status_code=400,
                detail=f"kind must be one of {sorted(_VALID_KINDS)}")
        # Slug-style name check — lowercase, hyphens, alphanumerics only.
        name = (data.name or "").strip().lower()
        if not name or not all(c.isalnum() or c == "-" for c in name):
            raise HTTPException(status_code=400,
                detail="name must be lowercase alphanumerics + hyphens")
        _validate_body_shape(data.kind, data.body)
        async with async_session() as s:
            existing = (await s.execute(
                select(AgentTemplate).where(
                    AgentTemplate.kind == data.kind,
                    AgentTemplate.name == name,
                )
            )).scalar_one_or_none()
            if existing:
                raise HTTPException(status_code=409,
                    detail=f"Token {data.kind}/{name} already exists")
            row = AgentTemplate(
                kind=data.kind, name=name, body=data.body,
                description=data.description or "",
                is_system=False, is_active=True,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            # Sprint D (Agent/Bracket/Template unification phase) — mirror
            # into the new kind='template' Agent row so $ref resolution
            # (now sourced from Agent rows, see template_registry.reload())
            # sees this fragment without waiting for a separate migration
            # pass. Best-effort — never raises, never blocks this response.
            from backend.api.algo.template_registry import mirror_fragment_upsert
            await mirror_fragment_upsert(s, row, force=True)
        await _reload_registry()
        return _to_out(row)

    @patch("/{frag_id:int}", guards=[cap_guard("manage_own_agents")])
    async def update_fragment(self, frag_id: int, data: FragmentPatch) -> FragmentOut:
        async with async_session() as s:
            row = (await s.execute(
                select(AgentTemplate).where(AgentTemplate.id == frag_id)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Token {frag_id} not found")
            # System tokens may only toggle is_active — body + description
            # are owned by code seeds.
            if row.is_system:
                if data.body is not None or data.description is not None:
                    raise HTTPException(status_code=400,
                        detail="System tokens can only toggle is_active; "
                               "body + description are owned by code seeds.")
            if data.body is not None:
                _validate_body_shape(row.kind, data.body)
                row.body = data.body
            if data.description is not None:
                row.description = data.description
            if data.is_active is not None:
                row.is_active = bool(data.is_active)
            await s.commit()
            await s.refresh(row)
            # Sprint D — propagate this edit to the mirrored Agent row.
            from backend.api.algo.template_registry import mirror_fragment_upsert
            await mirror_fragment_upsert(s, row, force=True)
        await _reload_registry()
        return _to_out(row)

    @get("/{frag_id:int}/references", guards=[cap_guard("view_agents_catalog")])
    async def fragment_references(self, frag_id: int) -> dict:
        from backend.api.algo.agent_evaluator import referencing_agents, fragments_reaching
        row = await _load_fragment(frag_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Token {frag_id} not found")
        bodies = await _load_bodies(row.kind)
        agents = await _load_agents()
        return {
            "fragment": {"id": row.id, "kind": row.kind, "name": row.name},
            "agents": referencing_agents(row.kind, row.name, bodies, agents),
            "fragments": sorted(fragments_reaching(row.name, bodies) - {row.name}),
        }

    @delete("/{frag_id:int}", guards=[cap_guard("manage_own_agents")], status_code=200)
    async def delete_fragment(self, frag_id: int) -> dict:
        async with async_session() as s:
            row = (await s.execute(
                select(AgentTemplate).where(AgentTemplate.id == frag_id)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Token {frag_id} not found")
            if row.is_system:
                raise HTTPException(status_code=400,
                    detail="Cannot delete system tokens. Toggle is_active "
                           "off instead.")
            frag_kind, frag_name = row.kind, row.name
            await s.delete(row)
            await s.commit()
            # Sprint D — remove the mirrored Agent row too.
            from backend.api.algo.template_registry import mirror_fragment_delete
            await mirror_fragment_delete(s, frag_kind, frag_name)
        await _reload_registry()
        return {"detail": f"Token {frag_id} deleted"}

    @post("/reload", guards=[cap_guard("manage_own_agents")], status_code=200)
    async def reload_fragments(self) -> dict:
        await _reload_registry()
        return {"detail": "Token registry reloaded"}
