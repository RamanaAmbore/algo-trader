"""
Wave 2 of the "Unify DSL token registration" plan — resolver-import
validation on the custom grammar-token CRUD endpoints.

Before this change, `POST /api/admin/grammar/tokens` (`create_token`) and
`PATCH /api/admin/grammar/tokens/{id}` (`patch_token`) accepted ANY
`resolver` dotted-path string with zero validation that it actually
imports. `GrammarRegistry.reload()`'s per-row loader (`grammar_registry.
py:_load_one_token`) treats an import failure as a SKIP (WARNING logged,
counted in `skipped`), not a hard error — so a typo'd or nonexistent
resolver path would successfully create/update with a 200/201 response,
and the token would silently never dispatch at runtime. This is the
plan's own stated "critical safety boundary".

Covers:
  - `_validate_resolver_importable()` — the new shared helper — rejects a
    nonexistent module, a nonexistent attribute, and a malformed
    (no-dot) dotted path; accepts None/empty (plenty of real token kinds
    legitimately carry no resolver) and a real resolver.
  - `create_token` — a bad resolver 400s BEFORE any DB write (no
    session.add / session.commit); a real resolver creates normally and
    triggers a registry reload.
  - `patch_token` — a bad resolver 400s on an existing custom token with
    the row's `resolver` field left untouched and no commit; a real
    resolver on a genuine field-changing patch succeeds.
  - The pre-existing duplicate-(grammar_kind, token_kind, token) 409
    rejection still fires, and still fires BEFORE resolver validation
    (dup-check is cheaper and should win when both would fail).
  - `seed_grammar_tokens()`'s own existing-rows query filters strictly on
    `is_system == True` (structural protection — proves a custom
    (`is_system=False`) token is never read, let alone mutated, by a
    bulk YAML reseed) plus a behavioral companion showing a custom-keyed
    row is never written to even if it leaked into the query result.

No new DB column/migration — `is_system=False` is already sufficient
provenance to protect a custom token from `seed_grammar_tokens()`.

Five quality dimensions:
  1. SSOT        — one shared `_validate_resolver_importable()` helper,
                   called from both create_token and patch_token; no
                   duplicated import-and-catch logic.
  2. Correctness — covers nonexistent module, nonexistent attribute,
                   malformed path, None/empty short-circuit, and a real
                   resolver, on both create and patch.
  3. Performance — validation is a single import attempt, no DB round
                   trip; runs before any session write.
  4. Reuse       — follows the existing async_session-mock pattern from
                   test_grammar_catalog_render_action.py (`.fn()` direct
                   controller calls) and test_grammar_registry.py (real
                   reload() against a mocked DB session).
  5. UX          — the 400 detail names the bad resolver string and the
                   underlying import error, so an operator (or the
                   future MCP-authoring flow) gets an actionable message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.algo import grammar
from backend.api.routes import grammar as g
from backend.api.schemas import GrammarTokenCreate, GrammarTokenPatch


# ── A known-real resolver, already migrated in Wave 1 ──────────────────────
_REAL_RESOLVER = "backend.api.algo.grammar_tokens.metric_cash._metric_cash"
_REAL_RESOLVER_2 = "backend.api.algo.grammar_tokens.metric_collateral._metric_collateral"


def _grammar_token_row(**kw):
    defaults = dict(
        id=1, grammar_kind="condition", token_kind="metric", token="custom_metric_xyz",
        value_type=None, units=None, description="", resolver=None,
        params_schema=None, enum_values=None, template_body=None,
        is_system=False, is_active=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_session_mock(dup_row=None):
    """Mirrors test_grammar_catalog_render_action.py's `_session_ctx`
    pattern, extended to track `add`/`commit`/`refresh` calls so tests
    can assert whether a write actually happened."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = dup_row

    async def _execute(stmt, *a, **kw):
        return mock_result

    async def _refresh(obj):
        # Mimics the DB assigning a real autoincrement id on commit/refresh
        # — a real GrammarToken() built by create_token() starts with
        # id=None (never set at construction) and would otherwise stay
        # None through this mocked DB round-trip.
        if getattr(obj, "id", None) is None:
            obj.id = 123

    session = AsyncMock()
    session.execute = _execute
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


def _make_get_session_mock(row):
    """Session mock for patch_token/delete_token — `s.get(GrammarToken,
    id)` returns `row` directly (no SELECT statement involved)."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=row)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


# ═══════════════════════════════════════════════════════════════════════════
#  _validate_resolver_importable — the shared helper
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateResolverImportable:
    def test_none_is_noop(self):
        g._validate_resolver_importable(None)  # must not raise

    def test_empty_string_is_noop(self):
        g._validate_resolver_importable("")  # must not raise

    def test_rejects_nonexistent_module(self):
        with pytest.raises(HTTPException) as exc_info:
            g._validate_resolver_importable(
                "backend.api.algo.this_module_does_not_exist_xyz.some_fn"
            )
        assert exc_info.value.status_code == 400
        assert "this_module_does_not_exist_xyz" in str(exc_info.value.detail)

    def test_rejects_nonexistent_attribute(self):
        with pytest.raises(HTTPException) as exc_info:
            g._validate_resolver_importable(
                "backend.api.algo.grammar_registry.this_attr_does_not_exist_xyz"
            )
        assert exc_info.value.status_code == 400

    def test_rejects_malformed_path_no_dot(self):
        with pytest.raises(HTTPException) as exc_info:
            g._validate_resolver_importable("notadottedpath")
        assert exc_info.value.status_code == 400

    def test_accepts_real_resolver(self):
        g._validate_resolver_importable(_REAL_RESOLVER)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════
#  create_token
# ═══════════════════════════════════════════════════════════════════════════

class TestCreateTokenResolverValidation:
    @pytest.mark.asyncio
    async def test_rejects_unimportable_resolver_before_any_write(self, monkeypatch):
        ctx, session = _make_session_mock(dup_row=None)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        reload_called = AsyncMock()
        monkeypatch.setattr(g, "_reload_registry", reload_called)

        data = GrammarTokenCreate(
            grammar_kind="condition", token_kind="metric", token="bad_custom_metric",
            resolver="backend.api.algo.nonexistent_module_xyz.fn",
        )
        with pytest.raises(HTTPException) as exc_info:
            await g.GrammarTokenController.create_token.fn(None, data=data)

        assert exc_info.value.status_code == 400
        session.add.assert_not_called()
        session.commit.assert_not_called()
        reload_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_real_resolver_and_reloads_registry(self, monkeypatch):
        ctx, session = _make_session_mock(dup_row=None)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        reload_called = AsyncMock()
        monkeypatch.setattr(g, "_reload_registry", reload_called)

        data = GrammarTokenCreate(
            grammar_kind="condition", token_kind="metric", token="good_custom_metric",
            resolver=_REAL_RESOLVER,
        )
        out = await g.GrammarTokenController.create_token.fn(None, data=data)

        session.add.assert_called_once()
        session.commit.assert_awaited_once()
        reload_called.assert_awaited_once()
        assert out.resolver == _REAL_RESOLVER
        assert out.is_system is False
        assert out.token == "good_custom_metric"

    @pytest.mark.asyncio
    async def test_duplicate_token_409_wins_even_with_bad_resolver(self, monkeypatch):
        """The pre-existing uniqueness check must still fire — and fire
        BEFORE resolver validation runs — when both would reject."""
        dup = _grammar_token_row(token="dup_metric")
        ctx, session = _make_session_mock(dup_row=dup)
        monkeypatch.setattr(g, "async_session", lambda: ctx)

        data = GrammarTokenCreate(
            grammar_kind="condition", token_kind="metric", token="dup_metric",
            resolver="backend.api.algo.nonexistent_module_xyz.fn",
        )
        with pytest.raises(HTTPException) as exc_info:
            await g.GrammarTokenController.create_token.fn(None, data=data)

        assert exc_info.value.status_code == 409
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_token_dispatches_identically_to_system_row_after_reload(
        self, monkeypatch,
    ):
        """End-to-end reload parity — the real GrammarToken row
        create_token() built and persisted must dispatch through
        GrammarRegistry.metric() EXACTLY like a system (is_system=True)
        row using the same resolver, with no special-casing of custom
        tokens anywhere in the dispatch path.

        Uses a fresh GrammarRegistry() instance (not the global REGISTRY)
        so this test cannot leak fake rows into the shared singleton for
        later tests in the same pytest process — same reload()/metric()
        code path, isolated state.
        """
        ctx, session = _make_session_mock(dup_row=None)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        monkeypatch.setattr(g, "_reload_registry", AsyncMock())

        data = GrammarTokenCreate(
            grammar_kind="condition", token_kind="metric", token="good_custom_metric",
            resolver=_REAL_RESOLVER,
        )
        await g.GrammarTokenController.create_token.fn(None, data=data)

        # The real GrammarToken ORM row create_token() constructed and
        # handed to session.add() — not a hand-built SimpleNamespace, so
        # this proves the exact row that gets persisted is accepted by
        # the loader, not just a lookalike fixture.
        created_row = session.add.call_args[0][0]
        assert created_row.resolver == _REAL_RESOLVER
        assert created_row.is_system is False

        system_row = _grammar_token_row(
            id=55, grammar_kind="condition", token_kind="metric",
            token="existing_system_metric", resolver=_REAL_RESOLVER, is_system=True,
        )

        from backend.api.algo.grammar_registry import GrammarRegistry
        from backend.api.algo.grammar_tokens.metric_cash import _metric_cash

        reg = GrammarRegistry()
        reload_session = _make_seed_session_mock([created_row, system_row])
        with patch("backend.api.database.async_session", return_value=reload_session):
            await reg.reload()

        assert reg.metric("good_custom_metric") is _metric_cash
        assert reg.metric("existing_system_metric") is _metric_cash
        assert "good_custom_metric" not in reg.metric_factories

    @pytest.mark.asyncio
    async def test_none_resolver_is_always_accepted(self, monkeypatch):
        """Plenty of real token kinds legitimately carry no resolver —
        creating one must not be blocked by this validation."""
        ctx, session = _make_session_mock(dup_row=None)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        monkeypatch.setattr(g, "_reload_registry", AsyncMock())

        data = GrammarTokenCreate(
            grammar_kind="notify", token_kind="template", token="no_resolver_token",
            resolver=None,
        )
        out = await g.GrammarTokenController.create_token.fn(None, data=data)
        assert out.resolver is None
        session.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
#  patch_token
# ═══════════════════════════════════════════════════════════════════════════

class TestPatchTokenResolverValidation:
    @pytest.mark.asyncio
    async def test_rejects_unimportable_resolver_row_unchanged(self, monkeypatch):
        row = _grammar_token_row(id=7, resolver=_REAL_RESOLVER, is_system=False)
        ctx, session = _make_get_session_mock(row)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        reload_called = AsyncMock()
        monkeypatch.setattr(g, "_reload_registry", reload_called)

        data = GrammarTokenPatch(resolver="backend.api.algo.nonexistent_module_xyz.fn")
        with pytest.raises(HTTPException) as exc_info:
            await g.GrammarTokenController.patch_token.fn(None, token_id=7, data=data)

        assert exc_info.value.status_code == 400
        # row never mutated — still the original real resolver
        assert row.resolver == _REAL_RESOLVER
        session.commit.assert_not_called()
        reload_called.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_real_resolver_change_on_custom_token(self, monkeypatch):
        row = _grammar_token_row(id=8, resolver=_REAL_RESOLVER, is_system=False)
        ctx, session = _make_get_session_mock(row)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        reload_called = AsyncMock()
        monkeypatch.setattr(g, "_reload_registry", reload_called)

        data = GrammarTokenPatch(resolver=_REAL_RESOLVER_2)
        out = await g.GrammarTokenController.patch_token.fn(None, token_id=8, data=data)

        assert row.resolver == _REAL_RESOLVER_2
        assert out.resolver == _REAL_RESOLVER_2
        session.commit.assert_awaited_once()
        reload_called.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_system_token_patch_bypasses_resolver_validation_path(self, monkeypatch):
        """System tokens are restricted to is_active toggles only — the
        resolver-validation branch must never even run for them, matching
        the pre-existing "any other field is a no-op" contract."""
        row = _grammar_token_row(id=9, resolver="some.existing.path", is_system=True)
        ctx, session = _make_get_session_mock(row)
        monkeypatch.setattr(g, "async_session", lambda: ctx)
        monkeypatch.setattr(g, "_reload_registry", AsyncMock())

        # A bad resolver on a system token must NOT 400 — it's silently
        # ignored by the is_system branch, same as before this change.
        data = GrammarTokenPatch(resolver="backend.api.algo.nonexistent_module_xyz.fn")
        out = await g.GrammarTokenController.patch_token.fn(None, token_id=9, data=data)
        assert row.resolver == "some.existing.path"
        assert out.resolver == "some.existing.path"


# ═══════════════════════════════════════════════════════════════════════════
#  seed_grammar_tokens() — custom tokens are structurally protected
# ═══════════════════════════════════════════════════════════════════════════

def _make_seed_session_mock(existing_rows):
    async def _execute(stmt, *a, **kw):
        result = MagicMock()
        result.scalars.return_value.all.return_value = existing_rows
        result.__stmt__ = stmt
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestSeedGrammarTokensProtectsCustomTokens:
    def test_existing_rows_query_filters_is_system_true(self):
        """seed_grammar_tokens()'s own `by_key` existing-rows SELECT only
        ever reads is_system=True rows — a custom token can never be
        returned by this query against a real DB, so it is never read,
        updated, or deleted by a bulk YAML reseed."""
        session = _make_seed_session_mock(existing_rows=[])
        captured = {}

        async def _execute(stmt, *a, **kw):
            captured["stmt"] = stmt
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)

        with patch("backend.api.database.async_session", return_value=session):
            asyncio.run(grammar.seed_grammar_tokens())

        where_sql = str(captured["stmt"].whereclause).lower()
        assert "is_system" in where_sql
        assert "true" in where_sql or "= 1" in where_sql or ":is_system" in where_sql

    def test_custom_keyed_row_never_mutated_even_if_present_in_result(self):
        """Behavioral companion — even in the (not-possible-against-a-
        real-DB) case where a custom row leaked into the mocked query
        result, seed_grammar_tokens() only iterates keys drawn from
        SYSTEM_TOKENS, so a row keyed by a token name that is NOT a real
        system token is never read from `by_key` and never mutated."""
        custom_row = _grammar_token_row(
            id=999, grammar_kind="condition", token_kind="metric",
            token="totally_custom_test_token_xyz", description="operator wrote this",
            resolver="some.operator.path", is_system=False,
        )
        # Sanity: this key must not collide with a real system token.
        system_keys = {
            (t["grammar_kind"], t["token_kind"], t["token"]) for t in grammar.SYSTEM_TOKENS
        }
        assert ("condition", "metric", "totally_custom_test_token_xyz") not in system_keys

        session = _make_seed_session_mock(existing_rows=[custom_row])
        with patch("backend.api.database.async_session", return_value=session):
            asyncio.run(grammar.seed_grammar_tokens())

        assert custom_row.description == "operator wrote this"
        assert custom_row.resolver == "some.operator.path"
