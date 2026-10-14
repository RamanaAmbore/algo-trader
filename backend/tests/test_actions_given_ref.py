"""
Tests for Part 2's backend wiring in `backend/api/algo/actions.py` — the
`{"$given": "<name>"}` marker (parallel to the existing `{"$row":
"<name>"}` marker) resolved against `context["given"][agent.id]`.

Also covers the `_action_target_exchanges("order", ...)` parity fix
found during Part 3 implementation (the exchange-open gate must treat
"order" identically to "place_order", or an "order" action would
silently bypass the market-hours gate).

Five quality dimensions:
  SSOT        — drives the REAL `execute()` dispatch path (mocking only
                `_al_dispatch_by_mode`, the actual broker/sim/paper hand-
                off point), not a hand-rolled stand-in.
  Correctness — fail-closed contract matches `resolve_row_ref_params`'s
                existing, already-shipped sibling exactly (no markers ->
                true no-op; unresolved name -> explicit error, never a
                silent None/blank).
  Performance — no DB/broker I/O; `_al_dispatch_by_mode` and `log_event`
                mocked at the module boundary.
  Reuse       — a single minimal `_Agent` stand-in shared across the file.
  UX          — every assert carries actual/expected context.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from backend.api.algo import actions as actions_mod
from backend.api.algo.actions import (
    _is_given_ref_marker,
    resolve_given_ref_params,
    _action_target_exchanges,
    execute,
)


class _Agent:
    """Minimal Agent-row stand-in — mirrors test_agent_action_fanout.py's
    `_Agent` but only carries what `execute()`/`resolve_given_ref_params`
    actually read."""
    def __init__(self, id=1, slug="test-agent", kind="cycle"):
        self.id = id
        self.slug = slug
        self.kind = kind


# ═══════════════════════════════════════════════════════════════════════
#  _is_given_ref_marker
# ═══════════════════════════════════════════════════════════════════════

class TestIsGivenRefMarker:
    def test_recognizes_exact_single_key_marker(self):
        assert _is_given_ref_marker({"$given": "msg_txt"}) is True

    def test_rejects_plain_string(self):
        assert _is_given_ref_marker("msg_txt") is False

    def test_rejects_multi_key_dict_even_if_given_present(self):
        assert _is_given_ref_marker({"$given": "msg_txt", "other": 1}) is False

    def test_rejects_none(self):
        assert _is_given_ref_marker(None) is False

    def test_rejects_row_ref_marker(self):
        assert _is_given_ref_marker({"$row": "symbol"}) is False


# ═══════════════════════════════════════════════════════════════════════
#  resolve_given_ref_params — pure function
# ═══════════════════════════════════════════════════════════════════════

class TestResolveGivenRefParams:
    def test_noop_when_no_markers_present(self):
        agent = _Agent(id=1)
        params = {"account": "ZG0790", "symbol": "NIFTY25JULFUT", "qty": 5}
        context = {"given": {1: {"p": -1000.0}}}
        resolved, error = resolve_given_ref_params(params, agent, context)
        assert error is None
        assert resolved == params
        # No markers present -> true no-op, returns the SAME object
        # (byte-identical to resolve_row_ref_params's own no-op contract).
        assert resolved is params

    def test_noop_for_empty_params(self):
        agent = _Agent(id=1)
        resolved, error = resolve_given_ref_params({}, agent, {})
        assert resolved == {}
        assert error is None

    def test_noop_for_none_params(self):
        agent = _Agent(id=1)
        resolved, error = resolve_given_ref_params(None, agent, {})
        assert resolved is None
        assert error is None

    def test_resolves_marker_to_real_value(self):
        agent = _Agent(id=1)
        params = {"account": "ZG0790", "tag": {"$given": "msg_txt"}}
        context = {"given": {1: {"msg_txt": "Loss: -60000"}}}
        resolved, error = resolve_given_ref_params(params, agent, context)
        assert error is None
        assert resolved["tag"] == "Loss: -60000"
        assert resolved["account"] == "ZG0790"

    def test_fails_closed_when_name_not_in_bindings(self):
        agent = _Agent(id=1)
        params = {"tag": {"$given": "never_defined"}}
        context = {"given": {1: {"other": 1.0}}}
        resolved, error = resolve_given_ref_params(params, agent, context)
        assert error is not None
        assert "never_defined" in error
        assert resolved == params  # unresolved — original params returned unchanged

    def test_fails_closed_when_bindings_resolved_to_none(self):
        """A binding that resolved to None (missing data this tick, or a
        skipped per-row-scope reference) must fail closed too — never
        silently thread a None into a broker-bound param."""
        agent = _Agent(id=1)
        params = {"tag": {"$given": "p"}}
        context = {"given": {1: {"p": None}}}
        resolved, error = resolve_given_ref_params(params, agent, context)
        assert error is not None
        assert resolved == params

    def test_fails_closed_when_no_bindings_dict_at_all_for_this_agent(self):
        agent = _Agent(id=99)   # no entry in context["given"]
        params = {"tag": {"$given": "p"}}
        context = {"given": {1: {"p": 5.0}}}
        resolved, error = resolve_given_ref_params(params, agent, context)
        assert error is not None

    def test_fails_closed_when_context_has_no_given_key_at_all(self):
        agent = _Agent(id=1)
        params = {"tag": {"$given": "p"}}
        resolved, error = resolve_given_ref_params(params, agent, {})
        assert error is not None

    def test_never_mutates_input_params(self):
        agent = _Agent(id=1)
        params = {"tag": {"$given": "p"}}
        context = {"given": {1: {"p": 5.0}}}
        original_id = id(params)
        resolve_given_ref_params(params, agent, context)
        assert id(params) == original_id
        assert params == {"tag": {"$given": "p"}}  # unchanged


# ═══════════════════════════════════════════════════════════════════════
#  execute() end-to-end — $given resolves before dispatch
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteEndToEndGivenResolution:
    @pytest.mark.asyncio
    async def test_given_marked_param_reaches_dispatch_resolved(self):
        """Real execute() (including the real resolve_given_ref_params +
        resolve_action_params + exchange-gate chain), with only
        `_al_dispatch_by_mode` mocked — the lowest level at which the
        FINAL resolved params dict can be inspected."""
        agent = _Agent(id=1, slug="given-e2e")
        actions = [{
            "type": "order",
            "params": {
                "account": "ZG0790", "symbol": "NIFTY25JULFUT",
                "exchange": "NFO", "side": "SELL", "qty": 5,
                "tag": {"$given": "msg_txt"},
            },
        }]
        context = {
            "sim_mode": True,
            "given": {1: {"msg_txt": "Loss: -60000 on NIFTY"}},
        }

        with (
            patch.object(actions_mod, "_al_dispatch_by_mode",
                         new=AsyncMock(return_value=True)) as dispatch_mock,
            patch("backend.api.algo.events.log_event", new=AsyncMock()),
        ):
            await execute(agent, actions, context)

        assert dispatch_mock.call_count == 1
        seen_params = dispatch_mock.call_args.args[3]
        assert seen_params["tag"] == "Loss: -60000 on NIFTY", (
            f"expected the $given marker resolved to the real bound value, "
            f"got {seen_params.get('tag')!r}"
        )

    @pytest.mark.asyncio
    async def test_unresolved_given_marker_skips_action_never_dispatches(self):
        agent = _Agent(id=1, slug="given-e2e-skip")
        actions = [{
            "type": "order",
            "params": {
                "account": "ZG0790", "symbol": "NIFTY25JULFUT",
                "exchange": "NFO", "side": "SELL", "qty": 5,
                "tag": {"$given": "never_defined"},
            },
        }]
        context = {"sim_mode": True, "given": {1: {}}}

        with (
            patch.object(actions_mod, "_al_dispatch_by_mode",
                         new=AsyncMock(return_value=True)) as dispatch_mock,
            patch("backend.api.algo.events.log_event", new=AsyncMock()) as log_event_mock,
        ):
            await execute(agent, actions, context)

        assert dispatch_mock.call_count == 0, "unresolved $given must never reach dispatch"
        skipped = [c for c in log_event_mock.call_args_list if c.args[1] == "action_skipped"]
        assert skipped, "expected an action_skipped audit event"

    @pytest.mark.asyncio
    async def test_place_order_action_type_also_resolves_given_marker(self):
        """The legacy 'place_order' action type must get the exact same
        $given resolution treatment as 'order' — the marker-resolution
        step runs for every action type uniformly, before mode dispatch."""
        agent = _Agent(id=1, slug="place-order-given")
        actions = [{
            "type": "place_order",
            "params": {
                "account": "ZG0790", "symbol": "NIFTY25JULFUT",
                "exchange": "NFO", "side": "SELL", "qty": 5,
                "tag": {"$given": "msg_txt"},
            },
        }]
        context = {"sim_mode": True, "given": {1: {"msg_txt": "hello"}}}

        with (
            patch.object(actions_mod, "_al_dispatch_by_mode",
                         new=AsyncMock(return_value=True)) as dispatch_mock,
            patch("backend.api.algo.events.log_event", new=AsyncMock()),
        ):
            await execute(agent, actions, context)

        seen_params = dispatch_mock.call_args.args[3]
        assert seen_params["tag"] == "hello"


# ═══════════════════════════════════════════════════════════════════════
#  _action_target_exchanges("order", ...) parity with "place_order"
# ═══════════════════════════════════════════════════════════════════════

class TestActionTargetExchangesOrderParity:
    def test_order_and_place_order_resolve_same_exchange(self):
        params = {"exchange": "MCX"}
        assert (_action_target_exchanges("order", params, {})
                == _action_target_exchanges("place_order", params, {})
                == ["MCX"])

    def test_order_defaults_to_nfo_like_place_order(self):
        assert (_action_target_exchanges("order", {}, {})
                == _action_target_exchanges("place_order", {}, {})
                == ["NFO"])

    def test_order_is_case_insensitive_like_place_order(self):
        assert _action_target_exchanges("ORDER", {"exchange": "nse"}, {}) == ["NSE"]

    def test_order_not_treated_as_scope_skip_action(self):
        """Before the fix, 'order' fell through to the chase_close-style
        'no gate' branch (empty list) — the single-symbol gate must
        actually apply."""
        result = _action_target_exchanges("order", {"exchange": "BSE"}, {})
        assert result == ["BSE"], (
            f"expected 'order' to be gated as a single-symbol action like "
            f"'place_order', got {result!r} (empty means the market-hours "
            f"gate was silently bypassed)"
        )
