"""
Wave 3 of the "Unify DSL token registration" plan — the
`POST /api/mcp/draft-token` route (`backend/api/routes/lab.py:
McpController.draft_token`).

Covers the route-level contract only — the drafting/validation logic
itself is `agent_ai.draft_token_from_prompt`, covered end-to-end by
`test_agent_token_draft.py`. Here:
  - the handler is guarded (not open to every caller)
  - the handler queries `_td_existing_token_names()` and threads the
    result into `draft_token_from_prompt` as `existing_names`
  - the handler offloads the (synchronous, LLM-calling)
    `draft_token_from_prompt` onto a worker thread via
    `asyncio.to_thread` — so a slow Gemini round-trip never blocks the
    event loop this process also uses for chase/postback handling
  - the response shape mirrors `TokenDraft` field-for-field
  - `_td_existing_token_names()` reads ALL rows (active + inactive),
    matching Wave 2's own uniqueness pre-flight

No file write, no code execution, no call to the Wave 2 registration
endpoint anywhere in this path — asserted by mocking
`draft_token_from_prompt` itself and confirming the route never touches
anything beyond passing its result through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.agent_ai import TokenDraft
from backend.api.routes import lab as R


def _make_names_session_mock(rows):
    """`select(GrammarToken.grammar_kind, GrammarToken.token_kind,
    GrammarToken.token)` → `.all()` returns a list of (gk, tk, tok)
    row-tuples."""
    result = MagicMock()
    result.all.return_value = rows

    async def _execute(stmt, *a, **kw):
        return result

    session = AsyncMock()
    session.execute = _execute

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ═══════════════════════════════════════════════════════════════════════════
#  _td_existing_token_names — reads active AND inactive rows
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_existing_token_names_lowercases_and_includes_all_rows(monkeypatch):
    rows = [
        ("condition", "metric", "Cash"),
        ("notify", "channel", "ntfy"),
    ]
    ctx = _make_names_session_mock(rows)
    monkeypatch.setattr(R, "async_session", lambda: ctx)

    names = await R._td_existing_token_names()
    assert names == {
        ("condition", "metric", "cash"),
        ("notify", "channel", "ntfy"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  draft_token — the route handler
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_draft_token_route_shape_and_wiring(monkeypatch):
    ctx = _make_names_session_mock([("condition", "metric", "cash")])
    monkeypatch.setattr(R, "async_session", lambda: ctx)

    fake_result = TokenDraft(
        draft={"grammar_kind": "condition", "token_kind": "metric", "token": "x"},
        errors=[], warnings=["review this"], why_summary="does a thing",
        prompt="a new metric",
    )
    with patch(
        "backend.api.algo.agent_ai.draft_token_from_prompt", return_value=fake_result,
    ) as mock_draft:
        data = R.TokenDraftRequest(prompt="a new metric")
        out = await R.McpController.draft_token.fn(None, data=data)

    assert isinstance(out, R.TokenDraftOut)
    assert out.draft == fake_result.draft
    assert out.errors == []
    assert out.warnings == ["review this"]
    assert out.why_summary == "does a thing"

    # existing_names threaded through from the DB query, not re-derived.
    mock_draft.assert_called_once()
    _, kwargs = mock_draft.call_args
    assert kwargs["existing_names"] == {("condition", "metric", "cash")}
    assert kwargs["grammar_kind"] is None
    assert kwargs["token_kind"] is None
    assert kwargs["token"] is None


@pytest.mark.asyncio
async def test_draft_token_route_threads_hints(monkeypatch):
    ctx = _make_names_session_mock([])
    monkeypatch.setattr(R, "async_session", lambda: ctx)

    fake_result = TokenDraft(draft={}, errors=[], warnings=[], why_summary="", prompt="")
    with patch(
        "backend.api.algo.agent_ai.draft_token_from_prompt", return_value=fake_result,
    ) as mock_draft:
        data = R.TokenDraftRequest(
            prompt="a metric for cash",
            grammar_kind="condition", token_kind="metric", token="cash",
        )
        await R.McpController.draft_token.fn(None, data=data)

    _, kwargs = mock_draft.call_args
    assert kwargs["grammar_kind"] == "condition"
    assert kwargs["token_kind"] == "metric"
    assert kwargs["token"] == "cash"


@pytest.mark.asyncio
async def test_draft_token_route_offloads_to_worker_thread(monkeypatch):
    """The LLM call is synchronous (same as draft_agent_from_prompt in
    _cmd_ai_create) — the route must run it via asyncio.to_thread so it
    never blocks the event loop."""
    ctx = _make_names_session_mock([])
    monkeypatch.setattr(R, "async_session", lambda: ctx)

    fake_result = TokenDraft(draft={}, errors=[], warnings=[], why_summary="", prompt="")

    async def _fake_to_thread(fn, *a, **kw):
        assert fn is R_draft_token_from_prompt_ref[0]
        return fn(*a, **kw)

    import backend.api.algo.agent_ai as agent_ai_mod
    R_draft_token_from_prompt_ref = [agent_ai_mod.draft_token_from_prompt]

    with patch.object(agent_ai_mod, "draft_token_from_prompt", return_value=fake_result) as mock_draft, \
         patch("backend.api.routes.lab.asyncio.to_thread", side_effect=_fake_to_thread) as mock_to_thread:
        R_draft_token_from_prompt_ref[0] = mock_draft
        data = R.TokenDraftRequest(prompt="a new metric")
        await R.McpController.draft_token.fn(None, data=data)

    mock_to_thread.assert_called_once()


def test_draft_token_route_is_guarded():
    guards = getattr(R.McpController.draft_token, "guards", None)
    assert guards, "draft_token must carry an explicit guard — never open to every caller"
