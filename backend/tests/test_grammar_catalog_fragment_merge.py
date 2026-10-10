"""
Sprint 10 — merge active AgentTemplate ("Token") rows into the grammar
catalog response served by `GET /api/admin/grammar/tokens`, the exact
endpoint the agent CLI's `fetchGrammarTokens('condition')` /
`('notify')` calls hit (see `backend/api/routes/grammar.py`).

Covers:
  - `_fragment_catalog_rows`: an active condition-kind Template surfaces
    as grammar_kind='condition'/token_kind='metric', an active
    notify-kind Template surfaces as grammar_kind='notify'/
    token_kind='channel', both carrying `is_fragment_ref=True`.
  - The underlying DB query filters on `is_active == True` at the SQL
    level — the enforcement mechanism behind "an inactive row never
    appears".
  - grammar='action' (and None) short-circuits to [] with no DB call —
    AgentTemplate has no 'action' kind, and the unfiltered admin Tokens
    CRUD page must never see merged rows (id-space collision guard).
  - `_merge_fragment_rows`: a Template name colliding with an existing
    real token's name in the same bucket is skipped, not appended
    (prevents catalog shadowing); non-colliding rows are appended as-is.
  - `list_tokens` end-to-end: real GrammarToken rows + merged Template
    rows both appear in one response, each with the right
    `is_fragment_ref` value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.routes import grammar as g
from backend.api.schemas import GrammarTokenOut


# ── fixtures / helpers ───────────────────────────────────────────────────

def _agent_template(**kw):
    defaults = dict(
        id=5, kind="condition", name="loss-positions-acct-default",
        body={"any": []}, description="a saved condition subtree",
        is_system=True, is_active=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _grammar_token(**kw):
    defaults = dict(
        id=1, grammar_kind="condition", token_kind="metric", token="pnl",
        value_type="number", units="₹", description="", resolver="x",
        params_schema=None, enum_values=None, template_body=None,
        is_system=True, is_active=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _session_ctx(rows, capture: dict | None = None):
    """Mirrors the `_make_session_ctx` pattern used across the test suite
    (see test_agent_suppression.py) — a mock async_session() context
    manager whose one SELECT returns `rows`. When `capture` is given, the
    executed statement is stashed at capture['stmt'] for SQL-shape
    assertions."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    async def _execute(stmt):
        if capture is not None:
            capture["stmt"] = stmt
        return mock_result

    session = AsyncMock()
    session.execute = _execute

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ── _fragment_catalog_rows ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_condition_template_surfaces_as_metric_fragment_ref(monkeypatch):
    row = _agent_template(
        kind="condition", name="loss-positions-acct-default", id=5,
        description="the acct-default threshold set", is_system=True, is_active=True,
    )
    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([row]))

    out = await g._fragment_catalog_rows("condition")

    assert len(out) == 1
    o = out[0]
    assert isinstance(o, GrammarTokenOut)
    assert o.grammar_kind == "condition"
    assert o.token_kind == "metric"
    assert o.token == "loss-positions-acct-default"
    assert o.description == "the acct-default threshold set"
    assert o.is_fragment_ref is True
    assert o.is_active is True
    assert o.is_system is True
    assert o.id == -5   # negative namespace — never collides with a real GrammarToken id


@pytest.mark.asyncio
async def test_active_notify_template_surfaces_as_channel_fragment_ref(monkeypatch):
    row = _agent_template(
        kind="notify", name="notify-critical-trio", id=9,
        description="telegram + email + log + inapp", is_system=True, is_active=True,
    )
    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([row]))

    out = await g._fragment_catalog_rows("notify")

    assert len(out) == 1
    o = out[0]
    assert o.grammar_kind == "notify"
    assert o.token_kind == "channel"
    assert o.token == "notify-critical-trio"
    assert o.is_fragment_ref is True
    assert o.id == -9


@pytest.mark.asyncio
async def test_fragment_catalog_query_filters_inactive_at_sql_level(monkeypatch):
    """The enforcement mechanism behind 'an inactive Template never
    appears' — the SELECT itself carries `is_active = true`. A fake
    session can't apply a real WHERE clause, so this asserts the
    statement actually built carries the filter, proving the SQL layer
    (not just the mock) excludes inactive rows in production."""
    capture: dict = {}
    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([], capture))

    await g._fragment_catalog_rows("condition")

    compiled = str(capture["stmt"])
    assert "agent_fragments.is_active = true" in compiled
    assert "agent_fragments.kind = " in compiled


@pytest.mark.asyncio
async def test_fragment_catalog_rows_action_grammar_short_circuits_no_db_call(monkeypatch):
    called = False

    def _boom():
        nonlocal called
        called = True
        raise AssertionError("async_session must not be called for grammar='action'")

    monkeypatch.setattr(g, "async_session", _boom)

    out = await g._fragment_catalog_rows("action")

    assert out == []
    assert called is False


@pytest.mark.asyncio
async def test_fragment_catalog_rows_unfiltered_grammar_short_circuits(monkeypatch):
    """grammar=None (what the unfiltered /admin/tokens CRUD page always
    sends) must never merge Template rows in — AgentTemplate ids live in
    a separate table and would otherwise collide with real GrammarToken
    ids on that page's own id-keyed PATCH/DELETE actions."""
    monkeypatch.setattr(
        g, "async_session",
        lambda: (_ for _ in ()).throw(AssertionError("must not query DB")),
    )
    out = await g._fragment_catalog_rows(None)
    assert out == []


# ── _merge_fragment_rows ─────────────────────────────────────────────────

def test_merge_fragment_rows_appends_non_colliding_rows():
    base = [GrammarTokenOut(id=1, grammar_kind="condition", token_kind="metric", token="pnl")]
    frag = [GrammarTokenOut(
        id=-5, grammar_kind="condition", token_kind="metric",
        token="loss-positions-acct-default", is_fragment_ref=True,
    )]
    out = g._merge_fragment_rows(base, frag)
    assert out[-1].token == "loss-positions-acct-default"
    assert out[-1].is_fragment_ref is True
    assert len(out) == 2


def test_merge_fragment_rows_skips_name_collision_with_real_token():
    base = [GrammarTokenOut(id=1, grammar_kind="condition", token_kind="metric", token="pnl")]
    frag = [GrammarTokenOut(
        id=-5, grammar_kind="condition", token_kind="metric",
        token="PNL", is_fragment_ref=True,   # case-insensitive collision
    )]
    out = g._merge_fragment_rows(base, frag)
    assert len(out) == 1   # the colliding fragment row was skipped
    assert out[0].is_fragment_ref is False


def test_merge_fragment_rows_noop_when_no_fragments():
    base = [GrammarTokenOut(id=1, grammar_kind="notify", token_kind="channel", token="telegram")]
    assert g._merge_fragment_rows(base, []) is base


# ── list_tokens end-to-end ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tokens_merges_active_template_alongside_real_tokens(monkeypatch):
    real_row = _grammar_token(
        id=1, grammar_kind="condition", token_kind="metric", token="pnl",
    )
    frag_row = _agent_template(
        kind="condition", name="loss-positions-acct-default", id=5,
        description="desc", is_active=True,
    )

    calls = {"n": 0}

    def _session_factory():
        calls["n"] += 1
        # First async_session() call = the GrammarToken query inside
        # list_tokens itself; second = the AgentTemplate query inside
        # _fragment_catalog_rows.
        return _session_ctx([real_row]) if calls["n"] == 1 else _session_ctx([frag_row])

    monkeypatch.setattr(g, "async_session", _session_factory)

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar="condition")

    tokens = {o.token: o for o in out}
    assert tokens["pnl"].is_fragment_ref is False
    assert tokens["loss-positions-acct-default"].is_fragment_ref is True
    assert tokens["loss-positions-acct-default"].grammar_kind == "condition"
    assert tokens["loss-positions-acct-default"].token_kind == "metric"
    assert tokens["loss-positions-acct-default"].description == "desc"


@pytest.mark.asyncio
async def test_list_tokens_unfiltered_call_never_merges_templates(monkeypatch):
    real_row = _grammar_token(id=1, grammar_kind="condition", token_kind="metric", token="pnl")
    monkeypatch.setattr(g, "async_session", lambda: _session_ctx([real_row]))

    out = await g.GrammarTokenController.list_tokens.fn(None, grammar=None)

    assert len(out) == 1
    assert out[0].token == "pnl"
    assert out[0].is_fragment_ref is False
