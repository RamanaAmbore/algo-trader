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


def _validate_body_shape(kind: str, body) -> None:
    """Stage 1 — sanity-check the body matches its kind. Notify must be
    a list of {channel, enabled} dicts; condition must be a dict (leaf
    or composite). Raises HTTPException(400) on bad shape."""
    if kind == "notify":
        if not isinstance(body, list):
            raise HTTPException(status_code=400,
                detail="notify fragment body must be a list of channel entries")
        for i, entry in enumerate(body):
            if not isinstance(entry, dict) or "channel" not in entry:
                raise HTTPException(status_code=400,
                    detail=f"notify fragment body[{i}] missing 'channel'")
    elif kind == "condition":
        if not isinstance(body, dict):
            raise HTTPException(status_code=400,
                detail="condition fragment body must be a dict (leaf or composite)")
    else:
        raise HTTPException(status_code=400,
            detail=f"kind must be one of {sorted(_VALID_KINDS)}")


async def _reload_registry() -> None:
    try:
        from backend.api.algo.template_registry import REGISTRY
        await REGISTRY.reload()
    except Exception as e:
        logger.error(f"TemplateRegistry reload failed: {e}")


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
                detail=f"Fragment {frag_id} not found")
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
                    detail=f"Fragment {data.kind}/{name} already exists")
            row = AgentTemplate(
                kind=data.kind, name=name, body=data.body,
                description=data.description or "",
                is_system=False, is_active=True,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
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
                    detail=f"Fragment {frag_id} not found")
            # System fragments may only toggle is_active — body + description
            # are owned by code seeds.
            if row.is_system:
                if data.body is not None or data.description is not None:
                    raise HTTPException(status_code=400,
                        detail="System fragments can only toggle is_active; "
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
        await _reload_registry()
        return _to_out(row)

    @delete("/{frag_id:int}", guards=[cap_guard("manage_own_agents")], status_code=200)
    async def delete_fragment(self, frag_id: int) -> dict:
        async with async_session() as s:
            row = (await s.execute(
                select(AgentTemplate).where(AgentTemplate.id == frag_id)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Fragment {frag_id} not found")
            if row.is_system:
                raise HTTPException(status_code=400,
                    detail="Cannot delete system fragments. Toggle is_active "
                           "off instead.")
            await s.delete(row)
            await s.commit()
        await _reload_registry()
        return {"detail": f"Fragment {frag_id} deleted"}

    @post("/reload", guards=[cap_guard("manage_own_agents")], status_code=200)
    async def reload_fragments(self) -> dict:
        await _reload_registry()
        return {"detail": "Fragment registry reloaded"}
