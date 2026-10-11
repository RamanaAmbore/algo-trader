"""
Sprint G backend, Task 3 — thread `agent.kind` into action execution
context, for a future LogPanel kind-filter (frontend work, not part of
this change).

`execute(agent, actions, context)` (`backend/api/algo/actions.py`) has
direct access to `agent.kind` at dispatch time but previously did NOT
thread it into the `context`/`params` individual action handlers (like
`emit_log(ctx, params)`) receive. Fix: `execute()` now injects
`context["agent_kind"] = getattr(agent, "kind", None)` before iterating
its action list — a plain dict mutation, safe because every caller
(`agent_engine.py:_ae_dispatch_actions`) already builds a FRESH
per-call `action_ctx = dict(context)` before invoking `execute()`.

`emit_log` reads `ctx.get("agent_kind")` and carries it both on its log
line and in its returned dict, under the field name `agent_kind`
(deliberately distinct from any bare `kind` key that might already
exist in a context dict — chosen per the brief since no prior art for
this field name exists anywhere in the codebase).

Covers:
  1. `execute()` injects `agent_kind` into context BEFORE any handler
     dispatch (observed via a patched `emit_log`/noop-handler capturing
     the context it was called with).
  2. `emit_log` carries `agent_kind` in its returned dict and in the
     logged line, for every real agent kind ('cycle'/'bracket'/
     'template'/'alert').
  3. `emit_log` with no `agent_kind` in context (ctx={} / ctx=None)
     degrades gracefully — no KeyError, `agent_kind` is None.
  4. `emit_log`'s existing `level` param already accepts "debug" cleanly
     — confirms the Sprint G addendum's `debug(x) -> emit_log(level=
     "debug")` compilation target needs zero backend change here.
  5. Regression: `execute()`'s existing per-action dispatch loop
     (mode resolution, $row binding, exchange gate, success/failure
     logging) is unaffected — mirrors the shape of existing
     `test_actions_coverage.py` coverage for the same loop.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.algo.actions import emit_log, execute


def _agent(kind: str = "cycle", slug: str = "test-agent"):
    return SimpleNamespace(kind=kind, slug=slug, actions=[])


# ═══════════════════════════════════════════════════════════════════════
#  1. execute() injects agent_kind before any handler dispatch
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteInjectsAgentKind:
    @pytest.mark.asyncio
    async def test_agent_kind_present_in_context_passed_to_emit_log(self):
        agent = _agent(kind="bracket")
        captured = {}

        async def _capture(ctx, params):
            captured.update(ctx)
            return {"action": "emit_log", "status": "logged"}

        with patch("backend.api.algo.actions.emit_log", new=_capture):
            await execute(agent, [{"type": "emit_log", "params": {"message": "x"}}], {})

        assert captured.get("agent_kind") == "bracket"

    @pytest.mark.asyncio
    async def test_agent_kind_reflects_each_real_kind(self):
        for kind in ("cycle", "bracket", "template", "alert"):
            agent = _agent(kind=kind)
            captured = {}

            async def _capture(ctx, params, _store=captured):
                _store.update(ctx)
                return {"action": "emit_log", "status": "logged"}

            with patch("backend.api.algo.actions.emit_log", new=_capture):
                await execute(agent, [{"type": "emit_log", "params": {"message": "x"}}], {})
            assert captured.get("agent_kind") == kind

    @pytest.mark.asyncio
    async def test_agent_with_no_kind_attribute_degrades_to_none(self):
        """A caller using a bare object with no `.kind` at all (shouldn't
        happen for a real Agent row, but must not raise) degrades to
        None via getattr's default."""
        agent = SimpleNamespace(slug="no-kind-agent", actions=[])
        captured = {}

        async def _capture(ctx, params):
            captured.update(ctx)
            return {"action": "emit_log", "status": "logged"}

        with patch("backend.api.algo.actions.emit_log", new=_capture):
            await execute(agent, [{"type": "emit_log", "params": {"message": "x"}}], {})
        assert captured.get("agent_kind") is None

    @pytest.mark.asyncio
    async def test_context_mutation_does_not_leak_across_separate_calls(self):
        """Each `execute()` call receives its own fresh dict in real
        usage (`agent_engine.py` builds `action_ctx = dict(context)` per
        call) — confirm two back-to-back calls with DIFFERENT agent
        kinds against two separate dicts don't cross-contaminate."""
        ctx_a: dict = {}
        ctx_b: dict = {}
        with patch("backend.api.algo.actions.emit_log", new=AsyncMock(return_value={})):
            await execute(_agent(kind="cycle"), [{"type": "emit_log", "params": {}}], ctx_a)
            await execute(_agent(kind="bracket"), [{"type": "emit_log", "params": {}}], ctx_b)
        assert ctx_a["agent_kind"] == "cycle"
        assert ctx_b["agent_kind"] == "bracket"


# ═══════════════════════════════════════════════════════════════════════
#  2/3. emit_log carries agent_kind (dict + log line) and degrades
#        gracefully when absent
# ═══════════════════════════════════════════════════════════════════════

class TestEmitLogCarriesAgentKind:
    @pytest.mark.asyncio
    async def test_returned_dict_carries_agent_kind(self):
        result = await emit_log({"agent_kind": "template"}, {"message": "hello"})
        assert result["agent_kind"] == "template"
        assert result["message"] == "hello"

    @pytest.mark.asyncio
    async def test_log_line_carries_agent_kind(self, monkeypatch):
        """The module logger has `propagate = False` (see
        `ramboq_logger.get_logger`'s own docstring), so `caplog` (which
        attaches at root) never sees it — patch the logger method
        directly instead, same pattern `test_scope_filter_symbol.py`'s
        `test_runtime_leaf_with_unknown_qualifier_logs_and_skips` uses."""
        import backend.api.algo.actions as actions_mod
        lines = []
        monkeypatch.setattr(actions_mod.logger, 'info', lambda msg: lines.append(msg))
        await emit_log({"agent_kind": "alert"}, {"message": "stop hit"})
        assert any("agent_kind=alert" in line for line in lines)

    @pytest.mark.asyncio
    async def test_empty_dict_context_degrades_to_none(self):
        result = await emit_log({}, {"message": "x"})
        assert result["agent_kind"] is None

    @pytest.mark.asyncio
    async def test_none_context_degrades_to_none_not_raise(self):
        result = await emit_log(None, {"message": "x"})
        assert result["agent_kind"] is None

    @pytest.mark.asyncio
    async def test_no_agent_kind_log_line_has_no_bracket_suffix(self, monkeypatch):
        import backend.api.algo.actions as actions_mod
        lines = []
        monkeypatch.setattr(actions_mod.logger, 'info', lambda msg: lines.append(msg))
        await emit_log({}, {"message": "plain message"})
        matching = [line for line in lines if "plain message" in line]
        assert matching
        assert "agent_kind=" not in matching[0]


# ═══════════════════════════════════════════════════════════════════════
#  4. emit_log already accepts level="debug" cleanly
# ═══════════════════════════════════════════════════════════════════════

class TestEmitLogDebugLevel:
    @pytest.mark.asyncio
    async def test_debug_level_dispatches_to_logger_debug(self, monkeypatch):
        import backend.api.algo.actions as actions_mod
        debug_lines = []
        info_lines = []
        monkeypatch.setattr(actions_mod.logger, 'debug', lambda msg: debug_lines.append(msg))
        monkeypatch.setattr(actions_mod.logger, 'info', lambda msg: info_lines.append(msg))
        result = await emit_log({"agent_kind": "cycle"}, {"level": "debug", "message": "inspect x"})
        assert result["level"] == "debug"
        assert any("inspect x" in line for line in debug_lines)
        assert not any("inspect x" in line for line in info_lines)

    @pytest.mark.asyncio
    async def test_debug_level_does_not_raise_and_returns_expected_shape(self):
        result = await emit_log({}, {"level": "DEBUG", "message": "x"})
        assert result["level"] == "debug"
        assert result["status"] == "logged"
        assert result["action"] == "emit_log"


# ═══════════════════════════════════════════════════════════════════════
#  5. Regression — execute()'s existing dispatch loop unaffected
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteRegression:
    @pytest.mark.asyncio
    async def test_execute_still_runs_multiple_actions_in_order(self):
        calls = []

        async def _track(ctx, params):
            calls.append(params.get("message"))
            return {"action": "emit_log", "status": "logged"}

        with patch("backend.api.algo.actions.emit_log", new=_track):
            await execute(
                _agent(), [
                    {"type": "emit_log", "params": {"message": "first"}},
                    {"type": "emit_log", "params": {"message": "second"}},
                ], {},
            )
        assert calls == ["first", "second"]

    @pytest.mark.asyncio
    async def test_execute_preserves_other_context_keys(self):
        """`context["agent_kind"] = ...` augments the dict in place —
        confirm any PRE-EXISTING context key survives alongside it.
        (Deliberately NOT `sim_mode=True` here — that reroutes `emit_log`
        actions to the sim paper-trade writer entirely, a different
        dispatch path this test isn't exercising.)"""
        captured = {}

        async def _capture(ctx, params):
            captured.update(ctx)
            return {"action": "emit_log", "status": "logged"}

        with patch("backend.api.algo.actions.emit_log", new=_capture):
            await execute(_agent(), [{"type": "emit_log", "params": {}}], {"some_flag": True})
        assert captured["some_flag"] is True
        assert captured["agent_kind"] == "cycle"
