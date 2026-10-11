"""
Sprint 15 — catalog projection of the per-scope exposed-column registry
(`backend.api.algo.grammar.EXPOSED_COLUMNS`) into
`GET /api/admin/grammar/tokens` (`list_tokens`).

Covers:
  - `_exposed_column_catalog_rows('condition')` returns one
    `GrammarTokenOut` per real registry entry, each with `token_kind
    == 'column'`, `token == '<scope>.<exposed_name>'`, correct
    `value_type`, and a `description` mentioning the real `from_column`.
  - Any `grammar` value other than exactly `'condition'` (including
    `None`, the unfiltered admin Tokens CRUD page) short-circuits to [].
  - `_merge_exposed_column_rows`: non-colliding rows append; a name
    collision with an existing token in the same (token_kind, token)
    bucket is skipped, not appended.
  - `list_tokens(grammar='condition')` end-to-end: real GrammarToken rows
    AND the projected column rows both appear in one response.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.algo.grammar import EXPOSED_COLUMNS
from backend.api.routes import grammar as g
from backend.api.schemas import GrammarTokenOut


def _grammar_token(**kw):
    defaults = dict(
        id=1, grammar_kind="condition", token_kind="metric", token="pnl",
        value_type="number", units="₹", description="", resolver="x",
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


# ── _exposed_column_catalog_rows ────────────────────────────────────────

def test_exposed_column_catalog_rows_shape_and_count():
    rows = g._exposed_column_catalog_rows("condition")
    expected_count = sum(len(cols) for cols in EXPOSED_COLUMNS.values())
    assert len(rows) == expected_count
    assert all(isinstance(r, GrammarTokenOut) for r in rows)
    assert all(r.token_kind == "column" for r in rows)
    assert all(r.grammar_kind == "condition" for r in rows)
    assert all(r.is_system is True and r.is_active is True for r in rows)
    # ids are unique and all negative (never collides with a real,
    # positive GrammarToken id or a fragment row's own negative id range).
    ids = [r.id for r in rows]
    assert len(set(ids)) == len(ids)
    assert all(i < -1_000_000 for i in ids)


def test_exposed_column_catalog_rows_token_shape_is_scope_dot_exposed_name():
    rows = g._exposed_column_catalog_rows("condition")
    tokens = {r.token for r in rows}
    assert "positions.total.pnl" in tokens
    assert "funds.total.cash" in tokens
    assert "watchlist.all.watchlist_name" in tokens
    # every token round-trips: splitting on the LAST dot recovers an
    # exposed_name that is a real key in EXPOSED_COLUMNS[scope].
    for r in rows:
        scope_token, _, exposed_name = r.token.rpartition(".")
        # scope_token itself may contain dots (e.g.
        # 'positions.expiring_today.nfo') — rpartition on the token's
        # OWN last dot still isolates the correct exposed_name since
        # exposed names are dot-free by construction.
        assert exposed_name in EXPOSED_COLUMNS[scope_token], r.token


def test_exposed_column_catalog_rows_value_type_and_description_provenance():
    rows = {r.token: r for r in g._exposed_column_catalog_rows("condition")}
    row = rows["funds.total.cash"]
    assert row.value_type == "number"
    assert row.label == "Live Cash"
    assert "'avail cash'" in row.description
    assert "ctx.df_margins" in row.description
    assert "_scope_funds_total" in row.description

    row2 = rows["watchlist.all.watchlist_name"]
    assert row2.value_type == "string"
    assert "'account'" in row2.description
    assert "ctx.watchlist_rows" in row2.description


@pytest.mark.parametrize("grammar", [None, "notify", "action", "condition "])
def test_exposed_column_catalog_rows_short_circuits_for_non_condition(grammar):
    assert g._exposed_column_catalog_rows(grammar) == []


# ── _merge_exposed_column_rows ──────────────────────────────────────────

def test_merge_exposed_column_rows_appends_non_colliding_rows():
    base = [GrammarTokenOut(id=1, grammar_kind="condition", token_kind="metric", token="pnl")]
    col = [GrammarTokenOut(
        id=-2_000_000, grammar_kind="condition", token_kind="column",
        token="positions.total.pnl",
    )]
    out = g._merge_exposed_column_rows(base, col)
    assert len(out) == 2
    assert out[-1].token == "positions.total.pnl"
    assert out[-1].token_kind == "column"


def test_merge_exposed_column_rows_skips_name_collision():
    base = [GrammarTokenOut(
        id=1, grammar_kind="condition", token_kind="column",
        token="positions.total.pnl",
    )]
    col = [GrammarTokenOut(
        id=-2_000_000, grammar_kind="condition", token_kind="column",
        token="POSITIONS.TOTAL.PNL",   # case-insensitive collision
    )]
    out = g._merge_exposed_column_rows(base, col)
    assert len(out) == 1


def test_merge_exposed_column_rows_noop_when_empty():
    base = [GrammarTokenOut(id=1, grammar_kind="condition", token_kind="metric", token="pnl")]
    assert g._merge_exposed_column_rows(base, []) is base


# ── list_tokens end-to-end ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tokens_condition_includes_exposed_column_rows(monkeypatch):
    real_row = _grammar_token(id=1, grammar_kind="condition", token_kind="metric", token="pnl")

    async def _fragment_rows_stub(grammar):
        return []

    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([real_row]))
    monkeypatch.setattr(g, "_fragment_catalog_rows", _fragment_rows_stub)

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar="condition")

    column_rows = [r for r in out if r.token_kind == "column"]
    expected_count = sum(len(cols) for cols in EXPOSED_COLUMNS.values())
    assert len(column_rows) == expected_count

    by_token = {r.token: r for r in column_rows}
    row = by_token["positions.total.pnl"]
    assert row.token_kind == "column"
    assert row.value_type == "number"
    assert "'pnl'" in row.description

    metric_rows = [r for r in out if r.token_kind == "metric"]
    assert any(r.token == "pnl" for r in metric_rows)


@pytest.mark.asyncio
async def test_list_tokens_unfiltered_never_merges_exposed_columns(monkeypatch):
    real_row = _grammar_token(id=1, grammar_kind="condition", token_kind="metric", token="pnl")

    async def _fragment_rows_stub(grammar):
        return []

    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([real_row]))
    monkeypatch.setattr(g, "_fragment_catalog_rows", _fragment_rows_stub)

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar=None)

    assert len(out) == 1
    assert out[0].token == "pnl"
    assert not any(r.token_kind == "column" for r in out)
