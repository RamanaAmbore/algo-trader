"""
Grammar token CRUD — admin-only endpoints for the agent grammar catalog.

Routes:
  GET    /api/admin/grammar/tokens?grammar=<kind>    list all tokens, optionally filtered by grammar_kind
  POST   /api/admin/grammar/tokens                    create a custom (non-system) token
  GET    /api/admin/grammar/tokens/{id}               read a single token
  PATCH  /api/admin/grammar/tokens/{id}               update — system tokens may only toggle is_active
  DELETE /api/admin/grammar/tokens/{id}               delete — system tokens cannot be deleted
  POST   /api/admin/grammar/reload                    rebuild the in-memory Registry (duplicate of the
                                                       /api/algo/grammar/reload entry point for admin UIs
                                                       that live under /admin)

These drive the future admin "Grammar" page (three sub-tabs: condition /
notify / action). Every mutation also calls REGISTRY.reload() so edits
take effect without a service restart.
"""

from litestar import Controller, get, post, patch, delete
from litestar.exceptions import HTTPException, NotFoundException
from sqlalchemy import select

from backend.api.rbac import cap_guard
from backend.api.database import async_session
from backend.api.models import GrammarToken
from backend.api.schemas import GrammarTokenOut, GrammarTokenCreate, GrammarTokenPatch
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _to_out(row: GrammarToken) -> GrammarTokenOut:
    return GrammarTokenOut(
        id=row.id, grammar_kind=row.grammar_kind, token_kind=row.token_kind,
        token=row.token, value_type=row.value_type, units=row.units,
        description=row.description or "", resolver=row.resolver,
        params_schema=row.params_schema, enum_values=row.enum_values,
        template_body=row.template_body,
        is_system=row.is_system, is_active=row.is_active,
    )


async def _reload_registry():
    """Best-effort reload so an edit lights up the running engine."""
    try:
        from backend.api.algo.grammar_registry import REGISTRY
        await REGISTRY.reload()
    except Exception as e:
        logger.error(f"Grammar registry reload failed: {e}")


class GrammarTokenController(Controller):
    path = "/api/admin/grammar"
    # NOTE: no controller-level `guards = [admin_guard]`. Litestar merges
    # controller guards with handler guards (additive), so a per-handler
    # auth_or_demo_guard CANNOT relax a controller-level admin_guard —
    # both still run. Instead, each write handler below carries an
    # explicit `guards=[cap_guard("manage_grammar_tokens")]`, and each read handler carries
    # `guards=[cap_guard("view_agents_catalog")]`. The two reads are demo-readable so
    # the Tokens page works for anonymous visitors walking the showcase;
    # the catalog rows carry no secrets (grammar_kind / token_kind /
    # token / resolver-path / params_schema / template_body).

    # ── List ───────────────────────────────────────────────────────────────
    @get("/tokens", guards=[cap_guard("view_agents_catalog")])
    async def list_tokens(self, grammar: str | None = None) -> list[GrammarTokenOut]:
        """
        Return every token, optionally filtered by grammar_kind
        ("condition" / "notify" / "action"). Sorted system-first, then by
        token name so the list is stable across reloads.
        """
        async with async_session() as s:
            stmt = select(GrammarToken)
            if grammar:
                stmt = stmt.where(GrammarToken.grammar_kind == grammar)
            stmt = stmt.order_by(
                GrammarToken.grammar_kind.asc(),
                GrammarToken.token_kind.asc(),
                GrammarToken.token.asc(),
            )
            rows = (await s.execute(stmt)).scalars().all()
        return [_to_out(r) for r in rows]

    # ── Read one ───────────────────────────────────────────────────────────
    @get("/tokens/{token_id:int}", guards=[cap_guard("view_agents_catalog")])
    async def get_token(self, token_id: int) -> GrammarTokenOut:
        async with async_session() as s:
            row = await s.get(GrammarToken, token_id)
        if not row:
            raise NotFoundException(detail=f"grammar_token id={token_id} not found")
        return _to_out(row)

    # ── Create ─────────────────────────────────────────────────────────────
    @post("/tokens", guards=[cap_guard("manage_grammar_tokens")])
    async def create_token(self, data: GrammarTokenCreate) -> GrammarTokenOut:
        """
        Create a non-system token. The (grammar_kind, token_kind, token)
        triple must be unique — duplicates surface as a 409.
        """
        async with async_session() as s:
            # Pre-flight uniqueness check so we can return a friendly 409
            dup = await s.execute(select(GrammarToken).where(
                GrammarToken.grammar_kind == data.grammar_kind,
                GrammarToken.token_kind   == data.token_kind,
                GrammarToken.token        == data.token,
            ))
            if dup.scalar_one_or_none():
                raise HTTPException(
                    status_code=409,
                    detail=f"token '{data.token}' already exists for "
                           f"{data.grammar_kind}/{data.token_kind}",
                )
            row = GrammarToken(
                grammar_kind=data.grammar_kind, token_kind=data.token_kind,
                token=data.token,
                value_type=data.value_type, units=data.units,
                description=data.description or "",
                resolver=data.resolver,
                params_schema=data.params_schema, enum_values=data.enum_values,
                template_body=data.template_body,
                is_system=False, is_active=data.is_active,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        await _reload_registry()
        return _to_out(row)

    # ── Update ─────────────────────────────────────────────────────────────
    @patch("/tokens/{token_id:int}", guards=[cap_guard("manage_grammar_tokens")])
    async def patch_token(self, token_id: int, data: GrammarTokenPatch) -> GrammarTokenOut:
        """
        Update an existing token.

        System tokens (is_system=True) are restricted to is_active toggles —
        everything else is rewritten from the code-level seed on the next
        startup. This protects the operator from editing a resolver path and
        silently breaking the engine at the next boot.
        """
        async with async_session() as s:
            row = await s.get(GrammarToken, token_id)
            if not row:
                raise NotFoundException(detail=f"grammar_token id={token_id} not found")

            if row.is_system:
                if data.is_active is not None and data.is_active != row.is_active:
                    row.is_active = data.is_active
                # Any other field on a system token is a no-op by design.
            else:
                if data.value_type    is not None: row.value_type    = data.value_type
                if data.units         is not None: row.units         = data.units
                if data.description   is not None: row.description   = data.description
                if data.resolver      is not None: row.resolver      = data.resolver
                if data.params_schema is not None: row.params_schema = data.params_schema
                if data.enum_values   is not None: row.enum_values   = data.enum_values
                if data.template_body is not None: row.template_body = data.template_body
                if data.is_active     is not None: row.is_active     = data.is_active

            await s.commit()
            await s.refresh(row)

        await _reload_registry()
        return _to_out(row)

    # ── Delete ─────────────────────────────────────────────────────────────
    @delete("/tokens/{token_id:int}", guards=[cap_guard("manage_grammar_tokens")])
    async def delete_token(self, token_id: int) -> None:
        async with async_session() as s:
            row = await s.get(GrammarToken, token_id)
            if not row:
                raise NotFoundException(detail=f"grammar_token id={token_id} not found")
            if row.is_system:
                raise HTTPException(
                    status_code=400,
                    detail="system tokens cannot be deleted — deactivate instead",
                )
            await s.delete(row)
            await s.commit()
        await _reload_registry()

    # ── Reload ─────────────────────────────────────────────────────────────
    @post("/reload", guards=[cap_guard("manage_grammar_tokens")])
    async def reload(self) -> dict:
        """Hot-rebuild the Grammar Registry dispatch table."""
        from backend.api.algo.grammar_registry import REGISTRY
        await REGISTRY.reload()
        return {
            "metrics":   len(REGISTRY.metrics),
            "scopes":    len(REGISTRY.scopes),
            "operators": len(REGISTRY.operators),
            "channels":  len(REGISTRY.channels),
            "formats":   len(REGISTRY.formats),
            "templates": len(REGISTRY.templates),
            "actions":   len(REGISTRY.actions),
        }
