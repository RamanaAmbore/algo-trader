"""
Condition-kind Token save-time validation (fix for the confirmed bug where
`_validate_body_shape` only checked `isinstance(body, dict)` for a
condition Token, never running the body through the grammar validator).

A Token saved via the TokensTab "Edit" flow with an unknown metric, a
malformed `quote.*` leaf (missing the required `symbol` qualifier), or
any other structurally-invalid condition tree used to be accepted with no
error. Every agent that references that Token via `{"$ref": <name>}` then
silently evaluates to `[]` forever, with no indication anywhere of why.

`backend.api.routes.agent_templates._validate_condition_body` now runs
the EXISTING `agent_evaluator.validate()` function (the same one
`create_agent`/`update_agent` already use for an agent's own `conditions`
tree) on every condition-kind Token body, on both create and patch.
Notify-kind Tokens are unaffected — their body shape is a channel list,
never a condition tree.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.routes import agent_templates as at
from backend.api.algo.grammar_registry import REGISTRY as GRAMMAR_REGISTRY
from backend.api.algo import grammar as _grammar


# ── grammar registry seeding (mirrors test_condition_fragments.py) ─────
#
# The real DB-driven registry isn't loaded under pytest; without seeding,
# every leaf would fail validation for the WRONG reason (unknown scope /
# unknown operator) rather than the specific defect each test targets.
# Snapshot + restore so this file never leaks seeded tokens into other
# test modules that run in the same process.

@pytest.fixture(autouse=True)
def _seed_grammar_minimal():
    _orig_metrics = dict(GRAMMAR_REGISTRY.metrics)
    _orig_scopes = dict(GRAMMAR_REGISTRY.scopes)
    _orig_operators = dict(GRAMMAR_REGISTRY.operators)
    try:
        GRAMMAR_REGISTRY.metrics["pnl"] = _grammar._metric_pnl
        GRAMMAR_REGISTRY.scopes["positions.total"] = _grammar._scope_positions_total
        GRAMMAR_REGISTRY.scopes["quote.tracked"] = _grammar._scope_quote_tracked
        from backend.api.algo.grammar import OPERATORS
        for k, v in OPERATORS.items():
            GRAMMAR_REGISTRY.operators[k] = v
        yield
    finally:
        GRAMMAR_REGISTRY.metrics = _orig_metrics
        GRAMMAR_REGISTRY.scopes = _orig_scopes
        GRAMMAR_REGISTRY.operators = _orig_operators


@pytest.fixture(autouse=True)
def _no_reload(monkeypatch):
    """Short-circuit TemplateRegistry.reload() — not under test here."""
    monkeypatch.setattr(at, "_reload_registry", AsyncMock(side_effect=lambda: None))


def _handler_fn(handler):
    return handler.fn


def _mock_session(existing=None):
    """Same mocking shape as test_agent_templates_token_wording.py."""
    session = MagicMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=existing)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), session


class _FakeFragmentRow:
    def __init__(self, **kw):
        self.id = 7
        self.kind = "condition"
        self.name = "custom-cond"
        self.body = {"metric": "pnl", "scope": "positions.total",
                     "op": "<=", "value": -1000}
        self.description = ""
        self.is_system = False
        self.is_active = True
        self.created_at = None
        self.updated_at = None
        for k, v in kw.items():
            setattr(self, k, v)


# ── unit-level: _validate_condition_body / _validate_body_shape ────────

def test_validate_condition_body_rejects_unknown_metric():
    with pytest.raises(HTTPException) as exc:
        at._validate_body_shape("condition", {
            "metric": "p_n_l_typo", "scope": "positions.total",
            "op": "<=", "value": -1000,
        })
    assert exc.value.status_code == 422
    assert "p_n_l_typo" in exc.value.detail
    assert "errors" in exc.value.extra


def test_validate_condition_body_rejects_quote_leaf_missing_symbol():
    with pytest.raises(HTTPException) as exc:
        at._validate_body_shape("condition", {
            "metric": "ltp", "scope": "quote.tracked",
            "op": ">", "value": 100,
        })
    assert exc.value.status_code == 422
    assert "requires a non-wildcard scope_filter.symbol" in exc.value.detail
    # Must be the symbol-qualifier defect specifically, not a scope/metric
    # typo masking the real signal.
    assert "unknown scope token" not in exc.value.detail
    assert "unknown metric token" not in exc.value.detail


def test_validate_condition_body_accepts_valid_tree():
    # Must not raise.
    at._validate_body_shape("condition", {
        "metric": "pnl", "scope": "positions.total",
        "op": "<=", "value": -1000,
    })


def test_validate_body_shape_never_runs_grammar_validator_for_notify(monkeypatch):
    """Regression guard — notify bodies are a channel list, not a
    condition tree; the grammar validator must never be invoked."""
    def _boom(*a, **k):
        raise AssertionError("validate() must not run for kind=='notify'")
    monkeypatch.setattr(at, "_validate_condition_body", _boom)
    # Must not raise — proves _validate_condition_body was never reached.
    at._validate_body_shape("notify", [{"channel": "telegram", "enabled": True}])


# ── endpoint-level: create_fragment ─────────────────────────────────────

@pytest.mark.asyncio
async def test_create_condition_fragment_unknown_metric_returns_422():
    session_factory, _ = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentCreate(kind="condition", name="bad-metric", body={
        "metric": "p_n_l_typo", "scope": "positions.total",
        "op": "<=", "value": -1000,
    })

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.create_fragment)(controller, data)

    assert exc.value.status_code == 422
    assert "p_n_l_typo" in exc.value.detail


@pytest.mark.asyncio
async def test_create_condition_fragment_quote_leaf_missing_symbol_returns_422():
    session_factory, _ = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentCreate(kind="condition", name="bad-quote-leaf", body={
        "metric": "ltp", "scope": "quote.tracked",
        "op": ">", "value": 100,
    })

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.create_fragment)(controller, data)

    assert exc.value.status_code == 422
    assert "requires a non-wildcard scope_filter.symbol" in exc.value.detail


@pytest.mark.asyncio
async def test_create_condition_fragment_valid_body_saves_successfully():
    session_factory, session = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentCreate(kind="condition", name="good-cond", body={
        "metric": "pnl", "scope": "positions.total",
        "op": "<=", "value": -1000,
    })

    with patch.object(at, "async_session", session_factory):
        out = await _handler_fn(at.AgentTemplateController.create_fragment)(controller, data)

    assert out.kind == "condition"
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_notify_fragment_body_unaffected_by_condition_validation(monkeypatch):
    """Regression guard at the route level — notify create must not
    invoke the grammar validator at all."""
    def _boom(*a, **k):
        raise AssertionError("validate() must not run for kind=='notify'")
    monkeypatch.setattr(at, "_validate_condition_body", _boom)

    session_factory, session = _mock_session(existing=None)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentCreate(kind="notify", name="ops-alert-2",
                              body=[{"channel": "telegram", "enabled": True}])

    with patch.object(at, "async_session", session_factory):
        out = await _handler_fn(at.AgentTemplateController.create_fragment)(controller, data)

    assert out.kind == "notify"
    session.commit.assert_awaited_once()


# ── endpoint-level: update_fragment (patch) ─────────────────────────────

@pytest.mark.asyncio
async def test_patch_condition_fragment_unknown_metric_returns_422_and_leaves_body_unchanged():
    existing_row = _FakeFragmentRow()
    original_body = dict(existing_row.body)
    session_factory, _ = _mock_session(existing=existing_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentPatch(body={
        "metric": "p_n_l_typo", "scope": "positions.total",
        "op": "<=", "value": -1000,
    })

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 7, data)

    assert exc.value.status_code == 422
    assert "p_n_l_typo" in exc.value.detail
    assert existing_row.body == original_body


@pytest.mark.asyncio
async def test_patch_condition_fragment_quote_leaf_missing_symbol_returns_422():
    existing_row = _FakeFragmentRow()
    session_factory, _ = _mock_session(existing=existing_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    data = at.FragmentPatch(body={
        "metric": "ltp", "scope": "quote.tracked",
        "op": ">", "value": 100,
    })

    with patch.object(at, "async_session", session_factory):
        with pytest.raises(HTTPException) as exc:
            await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 7, data)

    assert exc.value.status_code == 422
    assert "requires a non-wildcard scope_filter.symbol" in exc.value.detail


@pytest.mark.asyncio
async def test_patch_condition_fragment_valid_body_saves_successfully():
    existing_row = _FakeFragmentRow()
    session_factory, session = _mock_session(existing=existing_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    new_body = {"metric": "pnl", "scope": "positions.total",
                "op": "<=", "value": -5000}
    data = at.FragmentPatch(body=new_body)

    with patch.object(at, "async_session", session_factory):
        out = await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 7, data)

    assert out.body == new_body
    assert existing_row.body == new_body
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_notify_fragment_body_unaffected_by_condition_validation(monkeypatch):
    """Regression guard — patching a notify Token's body must never
    invoke the grammar validator."""
    def _boom(*a, **k):
        raise AssertionError("validate() must not run for kind=='notify'")
    monkeypatch.setattr(at, "_validate_condition_body", _boom)

    existing_row = _FakeFragmentRow(
        kind="notify", name="ops-alert",
        body=[{"channel": "telegram", "enabled": True}],
    )
    session_factory, session = _mock_session(existing=existing_row)
    controller = at.AgentTemplateController.__new__(at.AgentTemplateController)
    new_body = [{"channel": "email", "enabled": True}]
    data = at.FragmentPatch(body=new_body)

    with patch.object(at, "async_session", session_factory):
        out = await _handler_fn(at.AgentTemplateController.update_fragment)(controller, 7, data)

    assert out.body == new_body
    session.commit.assert_awaited_once()
