"""
Sprint 18 (frontend half) — catalog projection of the render-action
params_schema (`backend.api.algo.grammar.get_render_action_params_schema`,
sourced from `event_actions.render.params_schema` in agent_grammar.yaml)
into `GET /api/admin/grammar/tokens` (`list_tokens`).

This is the missing route wiring the backend agent's own Sprint 18
commit deliberately left for a separate dispatch — `get_render_action_
params_schema()` existed on `backend.api.algo.grammar` with its own
pytest coverage, but `routes/grammar.py:list_tokens()` never surfaced it
anywhere, which is what blocked the frontend agent CLI decompiler
(`decompileEventAgent` in `agents.js`) from recognizing a real seeded
agent's action-level `gate` key via a real catalog lookup instead of a
hardcoded `{type, render}` set.

Covers (mirrors `test_grammar_catalog_exposed_columns.py`'s own
sibling-helper test pattern):
  - `_render_action_catalog_rows('action')` returns exactly one
    `GrammarTokenOut` row shaped `token_kind='render_action'`,
    `token='render'`, carrying the real `gate` params_schema.
  - Any `grammar` value other than exactly `'action'` (including
    `None`, the unfiltered admin Tokens CRUD page) short-circuits to [].
  - `token_kind='render_action'` is NEVER `'action_type'` — the one
    invariant that keeps this row out of `buildCatalog()`'s DO-action
    bucket on the frontend (and out of `is_known_action_type('render')`
    on the backend, already covered by
    `test_grammar_event_action_gate_field.py`).
  - `list_tokens(grammar='action')` end-to-end: the real action_type
    rows AND the one projected render_action row both appear.
  - `list_tokens(grammar=None)` / `grammar='condition'` / `grammar=
    'notify'` never include the render_action row (same
    unfiltered-never-merges rule every other synthetic-row source in
    this file follows).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.algo.grammar import get_render_action_params_schema
from backend.api.routes import grammar as g
from backend.api.schemas import GrammarTokenOut


def _grammar_token(**kw):
    defaults = dict(
        id=1, grammar_kind="action", token_kind="action_type", token="close_position",
        value_type=None, units=None, description="", resolver="x",
        params_schema=None, enum_values=None, template_body=None,
        is_system=True, is_active=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _session_ctx(rows):
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    async def _execute(stmt):
        return mock_result

    session = AsyncMock()
    session.execute = _execute

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ── _render_action_catalog_rows ─────────────────────────────────────────

def test_render_action_catalog_rows_shape():
    rows = g._render_action_catalog_rows("action")
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, GrammarTokenOut)
    assert row.grammar_kind == "action"
    assert row.token_kind == "render_action"
    assert row.token == "render"
    assert row.is_system is True
    assert row.is_active is True
    # negative id, clear of _fragment_catalog_rows' -row.id range and
    # _exposed_column_catalog_rows' -2_000_000-based range.
    assert row.id < -2_500_000


def test_render_action_catalog_rows_carries_real_gate_schema():
    row = g._render_action_catalog_rows("action")[0]
    live_schema = get_render_action_params_schema()
    assert row.params_schema == live_schema
    assert "gate" in row.params_schema
    assert row.params_schema["gate"]["type"] == "boolean"


def test_render_action_catalog_rows_token_kind_is_never_action_type():
    """The one invariant that keeps this row out of `buildCatalog()`'s
    DO-action bucket on the frontend — see that file's `buildCatalog`
    comment for the matching `token_kind === 'action_type'` filter."""
    row = g._render_action_catalog_rows("action")[0]
    assert row.token_kind != "action_type"


@pytest.mark.parametrize("grammar", [None, "condition", "notify", "action "])
def test_render_action_catalog_rows_short_circuits_for_non_action(grammar):
    assert g._render_action_catalog_rows(grammar) == []


# ── list_tokens end-to-end ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tokens_action_includes_render_action_row(monkeypatch):
    real_row = _grammar_token(id=1, grammar_kind="action", token_kind="action_type", token="close_position")

    async def _fragment_rows_stub(grammar):
        return []

    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([real_row]))
    monkeypatch.setattr(g, "_fragment_catalog_rows", _fragment_rows_stub)

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar="action")

    render_rows = [r for r in out if r.token_kind == "render_action"]
    assert len(render_rows) == 1
    assert render_rows[0].token == "render"
    assert "gate" in render_rows[0].params_schema

    action_type_rows = [r for r in out if r.token_kind == "action_type"]
    assert any(r.token == "close_position" for r in action_type_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("grammar", [None, "condition", "notify"])
async def test_list_tokens_never_includes_render_action_row_for_other_grammars(monkeypatch, grammar):
    real_row = _grammar_token(id=1, grammar_kind="action", token_kind="action_type", token="close_position")

    async def _fragment_rows_stub(g_):
        return []

    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([real_row]))
    monkeypatch.setattr(g, "_fragment_catalog_rows", _fragment_rows_stub)

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar=grammar)

    assert not any(r.token_kind == "render_action" for r in out)
