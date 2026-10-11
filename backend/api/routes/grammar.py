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
    # Sprint 17 — `mutually_exclusive_groups` is never stored on the
    # grammar_tokens row (no DB column for it, by design — see
    # grammar.py's _ACTION_MUTEX_GROUPS comment); it's looked up live from
    # the in-memory, code-reviewed YAML catalog, scoped to system action
    # tokens only (a custom/operator-authored token can never carry one).
    mutex_groups = None
    if row.is_system and row.grammar_kind == "action":
        from backend.api.algo.grammar import get_action_mutex_groups
        mutex_groups = get_action_mutex_groups(row.token)
    return GrammarTokenOut(
        id=row.id, grammar_kind=row.grammar_kind, token_kind=row.token_kind,
        token=row.token, value_type=row.value_type, units=row.units,
        description=row.description or "", resolver=row.resolver,
        params_schema=row.params_schema, enum_values=row.enum_values,
        template_body=row.template_body,
        is_system=row.is_system, is_active=row.is_active,
        mutually_exclusive_groups=mutex_groups,
    )


# Sprint 10 — merge active AgentTemplate ("Token") rows into the same
# catalog shape fetchGrammarTokens('condition')/('notify') already
# returns, so the frontend's existing buildCatalog() picks them up with
# zero fetch-side changes. A condition-kind Template behaves like a
# metric (it stands where a leaf would inside WHEN); a notify-kind
# Template behaves like a channel (it stands where a channel would
# inside ALERT). See GrammarTokenOut.is_fragment_ref for the marker a
# caller uses to tell a merged Template row apart from a real token.
_FRAGMENT_TOKEN_KIND = {"condition": "metric", "notify": "channel"}


async def _fragment_catalog_rows(grammar: str | None) -> list[GrammarTokenOut]:
    """Active AgentTemplate rows for `grammar`, shaped as GrammarTokenOut.

    Deliberately scoped to grammar in {"condition", "notify"} ONLY — the
    exact two calls agents.js's fetchAgentCatalog makes. An unfiltered
    call (grammar=None — what the admin Tokens CRUD page at /admin/tokens
    always sends, filtering by tab client-side) never merges these in:
    AgentTemplate rows live in a separate table (agent_fragments) with
    their own id-space, and that admin page mutates rows by id
    (toggle/patch/delete) straight against this same /tokens/{id} path.
    Merging there would risk an operator action against a merged row
    silently landing on an unrelated real grammar token that happens to
    share the same numeric id. Returning [] for any other grammar value
    (including "action", which AgentTemplate doesn't support) is correct.
    """
    if grammar not in _FRAGMENT_TOKEN_KIND:
        return []
    from backend.api.models import AgentTemplate

    token_kind = _FRAGMENT_TOKEN_KIND[grammar]
    async with async_session() as s:
        rows = (await s.execute(
            select(AgentTemplate).where(
                AgentTemplate.kind == grammar,
                AgentTemplate.is_active == True,  # noqa: E712
            )
        )).scalars().all()
    return [
        GrammarTokenOut(
            # Negative id namespace: AgentTemplate.id lives in a distinct
            # table (agent_fragments) and could otherwise numerically
            # collide with a real GrammarToken.id. A negative id can never
            # match a real (autoincrement, positive) GrammarToken row, so
            # any stray PATCH/DELETE against it 404s instead of mutating
            # an unrelated token.
            id=-row.id,
            grammar_kind=grammar,
            token_kind=token_kind,
            token=row.name,
            description=row.description or "",
            is_system=row.is_system,
            is_active=True,
            is_fragment_ref=True,
        )
        for row in rows
    ]


def _merge_fragment_rows(
    base_rows: list[GrammarTokenOut], fragment_rows: list[GrammarTokenOut],
) -> list[GrammarTokenOut]:
    """Append `fragment_rows` to `base_rows`, skipping (and logging) any
    Template whose name collides with an existing real token's name in
    the same (grammar_kind, token_kind) bucket — buildCatalog() keys its
    maps by lowercased token name, last-write-wins, so an unguarded
    collision would let a Template silently shadow a real metric/channel."""
    if not fragment_rows:
        return base_rows
    taken = {(r.token_kind, r.token.lower()) for r in base_rows}
    for frow in fragment_rows:
        key = (frow.token_kind, frow.token.lower())
        if key in taken:
            logger.warning(
                f"grammar catalog: Token '{frow.token}' "
                f"({frow.grammar_kind}/{frow.token_kind}) collides with an "
                f"existing token of the same name — skipping merged row"
            )
            continue
        base_rows.append(frow)
        taken.add(key)
    return base_rows


# Sprint 15 — project `backend.api.algo.grammar.EXPOSED_COLUMNS` (the
# per-scope exposed-column registry backing `scope.attribute` dot-access,
# e.g. `positions.total.pnl <= -50000`) into the same catalog shape
# fetchGrammarTokens('condition') already returns, so every exposed
# column is browsable/discoverable in the exact same place metrics/
# scopes/channels/actions and (since Sprint 10) Tokens/fragments already
# are. Sibling to `_fragment_catalog_rows`/`_merge_fragment_rows` above —
# same gating + merge pattern, deliberately duplicated rather than shared
# since the two sources (DB-backed AgentTemplate rows vs. an in-memory
# Python registry) have nothing else in common.
#
# Token shape (the frontend agent in this sprint depends on this EXACT
# shape): `token = "<scope_token>.<exposed_name>"`, e.g.
# `"positions.total.pnl"`. Chosen over a bare `exposed_name` with scope
# carried as separate metadata because (a) `GrammarTokenOut` has no spare
# field for it today — adding one would ripple into every other token
# kind for a Sprint-15-only need — and (b) the SAME exposed_name
# legitimately repeats across multiple scopes (`pnl` appears under
# positions.total, positions.any_acct, holdings.total, ...), so a bare
# name would collide with itself across scopes AND with the real `pnl`
# metric token. The compound string is unambiguous to parse: `exposed_name`
# is guaranteed dot-free (identifier regex, enforced by
# backend/tests/test_grammar_exposed_columns.py) and every real scope
# token is a known, enumerable set, so a consumer splits on the LAST dot
# only after failing to match the whole string against a known scope
# chain — exactly the "longest scope-chain match first, then column read"
# rule the dot-attribute grammar itself needs for
# `positions.expiring_today.mcx_unhedged` (a real multi-segment SCOPE)
# vs. `positions.total.pnl` (a scope + column read).
_EXPOSED_COLUMN_ID_BASE = -2_000_000


def _exposed_column_catalog_rows(grammar: str | None) -> list[GrammarTokenOut]:
    """Synthesize one `GrammarTokenOut` per (scope, exposed_name) entry in
    the Sprint 15 registry. Gated to `grammar == 'condition'` ONLY — these
    are WHEN-side reads (every entry's `grammar_kind` is hard-coded
    'condition'); an unfiltered call (`grammar=None`, the admin Tokens CRUD
    page) must never see these synthetic rows, same id-space-collision
    rationale as `_fragment_catalog_rows`'s own `grammar=None` short-circuit.

    Negative id namespace, offset far below `_fragment_catalog_rows`' own
    `-row.id` range (AgentTemplate ids are small DB autoincrement values)
    so the two synthetic-row sources can never collide on id even though
    both use negative ids to stay clear of real (positive) GrammarToken
    ids. Ids are deterministic across calls/process restarts — derived
    from a FIXED, alphabetically-sorted walk order, not registry dict
    iteration order — so they are stable for a given registry content.
    """
    if grammar != "condition":
        return []
    from backend.api.algo.grammar import EXPOSED_COLUMNS, EXPOSED_COLUMN_SCOPE_PROVENANCE

    rows: list[GrammarTokenOut] = []
    idx = 0
    for scope_token in sorted(EXPOSED_COLUMNS.keys()):
        columns = EXPOSED_COLUMNS[scope_token]
        ctx_attr, fn_name = EXPOSED_COLUMN_SCOPE_PROVENANCE[scope_token]
        for exposed_name in sorted(columns.keys()):
            entry = columns[exposed_name]
            rows.append(GrammarTokenOut(
                id=_EXPOSED_COLUMN_ID_BASE - idx,
                grammar_kind="condition",
                token_kind="column",
                token=f"{scope_token}.{exposed_name}",
                value_type=entry["type"],
                description=(
                    f"{entry['label']}. Sourced from DataFrame column "
                    f"'{entry['from_column']}' on {ctx_attr}, populated by "
                    f"backend.api.algo.grammar.{fn_name}."
                ),
                label=entry["label"],
                is_system=True,
                is_active=True,
            ))
            idx += 1
    return rows


def _merge_exposed_column_rows(
    base_rows: list[GrammarTokenOut], column_rows: list[GrammarTokenOut],
) -> list[GrammarTokenOut]:
    """Append `column_rows` to `base_rows`, skipping (and logging) any
    collision with an existing token of the same (token_kind, token) in
    the same bucket — sibling to `_merge_fragment_rows`, identical
    gating/merge/collision-skip pattern. A collision is not expected in
    practice (every column token is scope-qualified, e.g.
    'positions.total.pnl', and `token_kind='column'` is a brand-new kind
    no other row ever uses) but the guard is kept for parity and so a
    future registry addition can never silently shadow an existing row."""
    if not column_rows:
        return base_rows
    taken = {(r.token_kind, r.token.lower()) for r in base_rows}
    for crow in column_rows:
        key = (crow.token_kind, crow.token.lower())
        if key in taken:
            logger.warning(
                f"grammar catalog: exposed column '{crow.token}' "
                f"({crow.grammar_kind}/{crow.token_kind}) collides with an "
                f"existing token of the same name — skipping merged row"
            )
            continue
        base_rows.append(crow)
        taken.add(key)
    return base_rows


# Sprint 18 — the kind='event' agent render action's own optional
# sibling params (currently just `gate`) are declared in a SEPARATE
# top-level YAML section (`event_actions.render.params_schema`,
# `grammar.get_render_action_params_schema()`) — deliberately NOT part
# of the `action_type` token family (see that accessor's own docstring
# for why: merging it in would make `is_known_action_type("render")`
# true). Nothing in `list_tokens()` exposed this schema to the frontend
# before this addition, which is what blocked the agent CLI decompiler
# from recognizing a real seeded agent's action-level `gate` key at all.
# Synthesized here as its own `GrammarTokenOut` row, gated to
# `grammar == "action"` ONLY — the exact fetchGrammarTokens('action')
# call `fetchAgentCatalog()` already makes, so no new frontend network
# call is needed. `token_kind="render_action"` (NEVER "action_type") so
# `buildCatalog()`'s action-type bucket — and by extension
# `is_known_action_type`-gated DO-clause validation — never sees it;
# this is a sibling catalog entry for a structurally different
# (kind='event'-only) action shape, not a dispatchable DO action.
# Negative id namespace well clear of `_fragment_catalog_rows`'
# `-row.id` (small DB autoincrement values) and
# `_exposed_column_catalog_rows`' `-2_000_000`-based range, so none of
# the three synthetic-row sources can ever collide on id.
_RENDER_ACTION_ID = -3_000_000


def _render_action_catalog_rows(grammar: str | None) -> list[GrammarTokenOut]:
    """One synthetic `GrammarTokenOut` row for the render action's own
    params_schema, or `[]` when `grammar != "action"` (including the
    admin Tokens CRUD page's unfiltered `grammar=None` call, which must
    never see synthetic rows — same rule `_fragment_catalog_rows`/
    `_exposed_column_catalog_rows` already follow) or when the YAML
    section declares no schema at all."""
    if grammar != "action":
        return []
    from backend.api.algo.grammar import get_render_action_params_schema
    schema = get_render_action_params_schema()
    if not schema:
        return []
    return [GrammarTokenOut(
        id=_RENDER_ACTION_ID,
        grammar_kind="action",
        token_kind="render_action",
        token="render",
        description="kind='event' agent render-action optional params (e.g. gate).",
        params_schema=schema,
        is_system=True,
        is_active=True,
    )]


async def _reload_registry():
    """Best-effort reload so an edit lights up the running engine."""
    try:
        from backend.api.algo.grammar_registry import REGISTRY
        await REGISTRY.reload()
    except Exception as e:
        logger.error(f"Grammar registry reload failed: {e}")


def _apply_custom_token_patch(row: GrammarToken, data) -> None:
    """Copy all patchable fields from `data` onto a non-system token row.

    Each conditional assignment reduces a CC point in patch_token; grouping
    them here keeps the method within B-grade and names the intent clearly.
    System tokens follow a different (is_active-only) path — this helper
    must never be called for them.
    """
    if data.value_type    is not None: row.value_type    = data.value_type
    if data.units         is not None: row.units         = data.units
    if data.description   is not None: row.description   = data.description
    if data.resolver      is not None: row.resolver      = data.resolver
    if data.params_schema is not None: row.params_schema = data.params_schema
    if data.enum_values   is not None: row.enum_values   = data.enum_values
    if data.template_body is not None: row.template_body = data.template_body
    if data.is_active     is not None: row.is_active     = data.is_active


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

        When `grammar` is "condition" or "notify" — the exact two calls
        the agent CLI's catalog fetcher makes — active AgentTemplate
        ("Token") rows are merged in too, marked `is_fragment_ref=True`
        (see `_fragment_catalog_rows`). An unfiltered call never merges
        them (see that function's docstring for why).

        When `grammar == "condition"` specifically, the Sprint 15
        per-scope exposed-column registry (`EXPOSED_COLUMNS` in
        `backend.api.algo.grammar`) is also projected in as
        `token_kind='column'` rows (see `_exposed_column_catalog_rows`) —
        one per real `scope.attribute` pair the dot-access grammar can
        read (e.g. `positions.total.pnl`). Same unfiltered-never-merges
        rule applies.

        When `grammar == "action"` specifically, Sprint 18's render-action
        params_schema (`event_actions.render.params_schema` in the YAML
        catalog) is projected in as one `token_kind='render_action'` row
        (see `_render_action_catalog_rows`) — the kind='event' agent CLI
        decompiler reads this to recognize the render action's own extra
        keys (e.g. `gate`). Same unfiltered-never-merges rule applies.
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
        out = [_to_out(r) for r in rows]
        fragment_rows = await _fragment_catalog_rows(grammar)
        out = _merge_fragment_rows(out, fragment_rows)
        column_rows = _exposed_column_catalog_rows(grammar)
        out = _merge_exposed_column_rows(out, column_rows)
        out.extend(_render_action_catalog_rows(grammar))
        return out

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
                _apply_custom_token_patch(row, data)

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
