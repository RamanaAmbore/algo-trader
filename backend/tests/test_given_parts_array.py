"""
Tests for the `parts`-array GIVEN binding shape (2026-10) —
`backend/api/algo/agent_engine.py`'s `_given_resolve_parts` /
`_given_format_part_value`, wired into `_cycle_evaluate_given_bindings`.

Background: `expr_eval.eval_expr`'s string-concat path is deliberately
strict (no implicit str/number coercion — confirmed: `"Loss: " + p` over
a numeric `p` raises `ExprError`). The frontend compiler classifies an
operator-expression GIVEN binding by a static property — any string
literal anywhere in its arith-AST — and compiles THOSE to
`{"name": ..., "parts": [...]}` instead of `{"name": ..., "expr": ...}`.
A "part" is either a plain string literal segment, or `{"$given":
"<name>"}` referencing an earlier-bound name in the SAME agent's own
bindings dict (already resolved, declaration order enforced).
Pure-number-math bindings keep using `expr`/`eval_expr` unchanged — see
test_agent_given_bindings.py for that path's own coverage.

Covers:
  - Plain concatenation of string + referenced-name parts.
  - Float formatting rule: a whole-number float renders without a
    trailing `.0`; a genuinely fractional float uses normal `str()`.
  - A missing/unresolved referenced name treats the WHOLE parts binding
    as unresolved (graceful skip, never a crash) — same contract as a
    failed `expr` binding.
  - An unrecognized part shape (not a string, not a `{"$given": name}`
    marker) is also treated as unresolved, never a crash.
  - Masking: an account code reaching the final string via a
    `{"$given": name}` part comes out masked.
  - Full `_cycle_evaluate_given_bindings` integration: a bare
    metric@scope binding followed by a `parts` binding referencing it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.api.algo.agent_engine import (
    _cycle_evaluate_given_bindings,
    _given_resolve_parts,
    _given_format_part_value,
    _GIVEN_UNRESOLVED,
)
from backend.api.algo.agent_evaluator import Context as V2Context
from backend.api.algo.grammar_registry import REGISTRY


class _Agent:
    def __init__(self, id, slug, given=None):
        self.id = id
        self.slug = slug
        self.given = given or []


@pytest.fixture(autouse=True)
def _seed_grammar_minimal():
    """Same minimal grammar seed as test_agent_given_bindings.py — needed
    for the full-integration test's bare metric@scope binding, plus the
    real comparison operators (needed for TestFullChainThroughRichAlert's
    full `_cycle_evaluate_agent` condition-tree evaluation)."""
    from backend.api.algo import grammar as _grammar

    prior_metrics = dict(REGISTRY.metrics)
    prior_scopes = dict(REGISTRY.scopes)
    prior_operators = dict(REGISTRY.operators)
    REGISTRY.metrics["pnl"] = _grammar._metric_pnl
    REGISTRY.scopes["positions.total"] = _grammar._scope_positions_total
    for k, v in _grammar.OPERATORS.items():
        REGISTRY.operators[k] = v
    yield
    REGISTRY.metrics.clear(); REGISTRY.metrics.update(prior_metrics)
    REGISTRY.scopes.clear(); REGISTRY.scopes.update(prior_scopes)
    REGISTRY.operators.clear(); REGISTRY.operators.update(prior_operators)


def _ctx(rows: list[dict]) -> V2Context:
    return V2Context(sum_positions=pd.DataFrame(rows), now=datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════════════════════
#  _given_format_part_value
# ═══════════════════════════════════════════════════════════════════════

class TestFormatPartValue:
    def test_whole_number_float_renders_without_trailing_zero(self):
        assert _given_format_part_value(-60000.0) == "-60000"

    def test_fractional_float_uses_normal_str(self):
        assert _given_format_part_value(-60000.5) == "-60000.5"

    def test_int_value_renders_as_is(self):
        assert _given_format_part_value(42) == "42"

    def test_string_value_passes_through(self):
        assert _given_format_part_value("NIFTY") == "NIFTY"

    def test_bool_value_uses_normal_str(self):
        # bool is an int subclass but not a float — must not hit the
        # float-formatting branch at all; plain str() is correct here.
        assert _given_format_part_value(True) == "True"


# ═══════════════════════════════════════════════════════════════════════
#  _given_resolve_parts
# ═══════════════════════════════════════════════════════════════════════

class TestResolveParts:
    def test_plain_string_parts_concatenate(self):
        result = _given_resolve_parts(["Loss", ": ", "big"], {}, "agent")
        assert result == "Loss: big"

    def test_string_and_given_ref_parts_concatenate(self):
        bindings = {"p": -60000.0}
        result = _given_resolve_parts(["Loss: ", {"$given": "p"}], bindings, "agent")
        assert result == "Loss: -60000"

    def test_multiple_given_refs_and_literals(self):
        bindings = {"p": -60000.0, "symbol": "NIFTY25JULFUT"}
        result = _given_resolve_parts(
            ["Loss: ", {"$given": "p"}, " on ", {"$given": "symbol"}],
            bindings, "agent",
        )
        assert result == "Loss: -60000 on NIFTY25JULFUT"

    def test_missing_referenced_name_returns_unresolved(self):
        result = _given_resolve_parts(["Loss: ", {"$given": "never_defined"}], {}, "agent")
        assert result is _GIVEN_UNRESOLVED

    def test_reference_resolved_to_none_is_also_unresolved(self):
        """A referenced name present in `bindings` but holding `None`
        (missing data this tick — e.g. a zero-row aggregate scope on a
        partial-outage tick) must be treated as unresolved too, not
        silently rendered as the literal text 'None'. Matches the same
        missing-vs-zero convention `resolve_given_ref_params`/
        `_resolve_given_message` already use."""
        result = _given_resolve_parts(["Loss: ", {"$given": "p"}], {"p": None}, "agent")
        assert result is _GIVEN_UNRESOLVED

    def test_unrecognized_part_shape_returns_unresolved(self):
        result = _given_resolve_parts([42], {}, "agent")
        assert result is _GIVEN_UNRESOLVED

    def test_row_ref_marker_is_not_mistaken_for_given_ref(self):
        """A {"$row": name} marker is a DIFFERENT feature (actions.py's
        matched-row fan-out) — must not be silently treated as a GIVEN
        reference."""
        result = _given_resolve_parts(["x: ", {"$row": "symbol"}], {"symbol": "NIFTY"}, "agent")
        assert result is _GIVEN_UNRESOLVED

    def test_empty_parts_list_resolves_to_empty_string(self):
        assert _given_resolve_parts([], {}, "agent") == ""

    def test_account_code_via_given_ref_is_not_masked_at_storage_time(self):
        """Masking is deliberately NOT applied here — a parts-built
        value is also readable via `{"$given": name}` in a DO-action
        param (`actions.py:resolve_given_ref_params`, which must stay
        raw, same contract as the existing `{"$row": name}` marker).
        Masking is applied at the MESSAGE boundary instead
        (`events._resolve_given_message`) — see
        test_events_given_message.py / test_rich_alert_custom_message.py
        for that coverage, and TestExecuteDoParamReceivesRawValue below
        for the DO-action-param proof."""
        bindings = {"acct": "ZG0790"}
        result = _given_resolve_parts(["Loss on ", {"$given": "acct"}, ": -60000"], bindings, "agent")
        assert result == "Loss on ZG0790: -60000"

    def test_pnl_number_and_symbol_text_unaffected_by_masking(self):
        bindings = {"p": -60000.0}
        result = _given_resolve_parts(["Loss: ", {"$given": "p"}, " on NIFTY"], bindings, "agent")
        assert result == "Loss: -60000 on NIFTY"


# ═══════════════════════════════════════════════════════════════════════
#  Full _cycle_evaluate_given_bindings integration
# ═══════════════════════════════════════════════════════════════════════

class TestFullPartsBindingIntegration:
    def test_bare_metric_scope_then_parts_binding_resolves_end_to_end(self):
        """Mirrors the plan's flagship example exactly: GIVEN p =
        pnl@positions.total; msg = "Loss: " + p — now compiled to a
        `parts` binding by the frontend instead of `expr`."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=1, slug="parts-e2e", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "msg", "parts": ["Loss: ", {"$given": "p"}]},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][1] == {"p": -60000.0, "msg": "Loss: -60000"}

    def test_parts_binding_with_missing_ref_is_skipped_gracefully(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=2, slug="parts-missing-ref", given=[
            {"name": "msg", "parts": ["Loss: ", {"$given": "never_defined"}]},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][2] == {}

    def test_parts_binding_does_not_leak_between_agents_same_tick(self):
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        context: dict = {}
        agent1 = _Agent(id=10, slug="a1", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "msg", "parts": ["Loss: ", {"$given": "p"}]},
        ])
        agent2 = _Agent(id=11, slug="a2", given=[
            {"name": "p", "expr": "pnl@positions.total"},
        ])
        _cycle_evaluate_given_bindings(agent1, v2_ctx, context)
        _cycle_evaluate_given_bindings(agent2, v2_ctx, context)
        assert context["given"][10] == {"p": -60000.0, "msg": "Loss: -60000"}
        assert context["given"][11] == {"p": -60000.0}
        assert "msg" not in context["given"][11]

    def test_pure_number_math_binding_still_uses_expr_unaffected(self):
        """Regression guard: a pure-number-math expression (no string
        literal anywhere) must keep going through eval_expr exactly as
        before — zero change to that path."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=3, slug="pure-math", given=[
            {"name": "p", "expr": "pnl@positions.total"},
            {"name": "double_p", "expr": "p * 2"},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][3] == {"p": -60000.0, "double_p": -120000.0}

    def test_account_code_in_parts_binding_stored_raw_not_masked_in_context(self):
        """A `parts` binding's joined output is stored RAW in
        `context["given"][agent.id]` — masking is deliberately deferred
        to the MESSAGE boundary (`events._resolve_given_message`), not
        applied at binding-storage time, because this same dict is also
        what a `{"$given": name}` DO-action param reads
        (`actions.py:resolve_given_ref_params`) and that path must
        receive the real, unmasked value (same contract as the existing
        `{"$row": name}` marker) — see
        TestExecuteDoParamReceivesRawAccountValue below for the proof,
        and test_events_given_message.py /
        test_rich_alert_custom_message.py for where masking actually
        happens when this value is used as an alert message."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=4, slug="parts-mask-e2e", given=[
            {"name": "acct_literal", "expr": '"ZG0790"'},
            {"name": "msg", "parts": ["Loss on ", {"$given": "acct_literal"}, ": -60000"]},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][4]["acct_literal"] == "ZG0790"
        assert context["given"][4]["msg"] == "Loss on ZG0790: -60000"


# ═══════════════════════════════════════════════════════════════════════
#  DO-action param reads the RAW (unmasked) value — proves masking is
#  correctly scoped to the message boundary, not binding storage
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteDoParamReceivesRawAccountValue:
    @pytest.mark.asyncio
    async def test_given_parts_value_reaches_dispatch_unmasked(self):
        """A `parts`-built GIVEN binding referenced via `{"$given": name}`
        in a DO-action param (e.g. `account=`) must reach the broker
        dispatch call with the REAL, unmasked value — masking only
        applies when that same binding is used as an ALERT message, per
        the design documented in `_given_resolve_parts`'s own docstring."""
        from unittest.mock import AsyncMock, patch
        from backend.api.algo import actions as actions_mod
        from backend.api.algo.actions import execute

        class _Agent:
            def __init__(self, id=1, slug="parts-do-param"):
                self.id = id
                self.slug = slug
                self.kind = "cycle"

        agent = _Agent(id=7)
        actions = [{
            "type": "order",
            "params": {
                "account": {"$given": "acct"}, "symbol": "NIFTY25JULFUT",
                "exchange": "NFO", "side": "SELL", "qty": 5,
            },
        }]
        context = {"sim_mode": True, "given": {7: {"acct": "ZG0790"}}}

        with (
            patch.object(actions_mod, "_al_dispatch_by_mode",
                         new=AsyncMock(return_value=True)) as dispatch_mock,
            patch("backend.api.algo.events.log_event", new=AsyncMock()),
        ):
            await execute(agent, actions, context)

        seen_params = dispatch_mock.call_args.args[3]
        assert seen_params["account"] == "ZG0790", (
            f"expected the raw, unmasked account value to reach dispatch, "
            f"got {seen_params.get('account')!r}"
        )


# ═══════════════════════════════════════════════════════════════════════
#  Full closing chain — GIVEN eval -> condition eval -> rich alert render
# ═══════════════════════════════════════════════════════════════════════

class TestFullChainThroughRichAlert:
    @pytest.mark.asyncio
    async def test_parts_binding_message_renders_in_rich_alert_table_masked(self):
        """The complete chain the plan's flagship example depends on:
        GIVEN p = pnl@positions.total; msg = "Loss: " + p (-> parts),
        run through the REAL `_cycle_evaluate_agent`, then the REAL
        `_v2_send_rich_alert` with an ALERT message of
        `{"$given": "msg"}` — the rendered `tg_table` must contain the
        computed text, AND an account code threaded through the same
        parts binding must come out masked at this rendered level."""
        from unittest.mock import patch
        from backend.api.algo.agent_engine import (
            _cycle_evaluate_agent, _v2_send_rich_alert, _v2_cfg,
        )
        from backend.api.algo import agent_engine as ae_mod
        from backend.api.algo.events import EvalResult

        now = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
        condition = {"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -1}

        class _Agent:
            def __init__(self):
                self.id = 20
                self.slug = "full-chain-agent"
                self.name = "Full Chain Agent"
                self.tier = "medium"
                self.fire_at_time = None
                self.actions = []
                self.conditions = condition
                self.given = [
                    {"name": "p", "expr": "pnl@positions.total"},
                    {"name": "acct", "expr": '"ZG0790"'},
                    {"name": "msg", "parts": [
                        "Loss: ", {"$given": "p"}, " acct ", {"$given": "acct"},
                    ]},
                ]
                self.events = [
                    {"channel": "telegram", "enabled": True, "message": {"$given": "msg"}},
                ]

        agent = _Agent()
        context = {
            "sum_positions": pd.DataFrame([{"account": "TOTAL", "pnl": -60000.0}]),
            "sum_holdings": pd.DataFrame(),
            "df_margins": pd.DataFrame(),
        }
        cfg = _v2_cfg()

        matches, _obs = _cycle_evaluate_agent(
            agent, context, cfg, now, alert_state={}, bypass_schedule=True,
        )
        assert matches, "condition must fire for this chain to mean anything"

        from backend.shared.helpers.utils import mask_account
        eval_result = EvalResult(triggered=True, condition_text="pnl <= -1", detail={"matches": []})
        calls = []
        with patch.object(ae_mod.logger, "info",
                           side_effect=lambda msg, **kw: calls.append(kw.get("extra"))):
            sent = await _v2_send_rich_alert(
                agent, matches, now, sim_mode=False, context=context, eval_result=eval_result,
            )
        assert sent is True
        tg_table = calls[0]["tg_table"]
        assert "Loss: -60000" in tg_table
        assert "ZG0790" not in tg_table
        assert mask_account("ZG0790") in tg_table


# ═══════════════════════════════════════════════════════════════════════
#  Case-insensitive binding names
# ═══════════════════════════════════════════════════════════════════════
#
# The frontend compiler emits a binding's OWN `name` field preserving
# the operator's original typed case (e.g. `GIVEN P = ...` -> {"name":
# "P", ...}), but LOWERCASES every REFERENCE to it: `{"$given": name}`
# markers (parts refs, DO-action params, ALERT message markers) are all
# `.toLowerCase()`'d at compile time, and an `expr` string's own `Name`
# node referencing an earlier binding keeps the operator's original
# typed case (rendered verbatim, per the decompiler's own rule) — e.g.
# `D = P * 2` keeps `P` uppercase in its `expr` text even though every
# OTHER reference to the same binding is lowercased. Without
# normalising storage/lookup to a single case, a mixed-case operator-
# chosen name would silently fail to resolve on every tick.

class TestCaseInsensitiveBindingNames:
    def test_pure_math_expr_references_original_case_resolves(self):
        """D = P * 2 — `expr` text keeps the operator's original 'P'
        case; must still resolve against the lowercased-stored binding."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=30, slug="case-math", given=[
            {"name": "P", "expr": "pnl@positions.total"},
            {"name": "D", "expr": "P * 2"},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        assert context["given"][30] == {"p": -60000.0, "d": -120000.0}

    def test_parts_binding_references_lowercased_name(self):
        """msgTxt = "Loss: " + P — the parts ref is `{"$given": "p"}`
        (lowercased by the frontend), referencing a binding whose own
        `name` field is the uppercase 'P'."""
        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        agent = _Agent(id=31, slug="case-parts", given=[
            {"name": "P", "expr": "pnl@positions.total"},
            {"name": "msgTxt", "parts": ["Loss: ", {"$given": "p"}]},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(agent, v2_ctx, context)
        # Stored under the lowercased key regardless of the binding's
        # own original-case `name` field.
        assert context["given"][31]["msgtxt"] == "Loss: -60000"

    @pytest.mark.asyncio
    async def test_do_action_param_resolves_mixed_case_binding(self):
        """`context["given"]` here is built by the REAL
        `_cycle_evaluate_given_bindings` storage path (not hand-built
        with already-lowercased keys) — a hand-built context would pass
        whether or not the storage-time lowercasing fix actually works,
        proving nothing about the fix itself."""
        from unittest.mock import AsyncMock, patch
        from backend.api.algo import actions as actions_mod
        from backend.api.algo.actions import execute

        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        bind_agent = _Agent(id=32, slug="case-do", given=[
            {"name": "AcctId", "expr": '"ZG0790"'},
            {"name": "msgTxt", "parts": ["Loss on ", {"$given": "acctid"}]},
        ])
        context: dict = {"sim_mode": True}
        _cycle_evaluate_given_bindings(bind_agent, v2_ctx, context)
        assert context["given"][32]["acctid"] == "ZG0790", (
            "sanity check: the real storage path must have lowercased 'AcctId' -> 'acctid'"
        )

        class _ExecAgent:
            def __init__(self, id=32, slug="case-do"):
                self.id = id
                self.slug = slug
                self.kind = "cycle"

        exec_agent = _ExecAgent()
        actions = [{
            "type": "order",
            "params": {
                "account": {"$given": "acctid"}, "symbol": "NIFTY25JULFUT",
                "exchange": "NFO", "side": "SELL", "qty": 5,
            },
        }]

        with (
            patch.object(actions_mod, "_al_dispatch_by_mode",
                         new=AsyncMock(return_value=True)) as dispatch_mock,
            patch("backend.api.algo.events.log_event", new=AsyncMock()),
        ):
            await execute(exec_agent, actions, context)

        seen_params = dispatch_mock.call_args.args[3]
        assert seen_params["account"] == "ZG0790", (
            "the real unmasked account value, resolved through the real "
            "case-normalising storage path, must reach dispatch"
        )

    def test_alert_message_resolves_mixed_case_binding(self):
        """Same real-storage-path requirement, PLUS a mixed-case marker
        (`{"$given": "MsgTxt"}`) on the lookup side — exercises
        `_resolve_given_message`'s own defense-in-depth `.lower()` too,
        not just the storage-time normalisation."""
        from backend.api.algo.events import _resolve_given_message
        from unittest.mock import MagicMock
        from backend.shared.helpers.utils import mask_account

        v2_ctx = _ctx([{"account": "TOTAL", "pnl": -60000.0}])
        bind_agent = _Agent(id=1, slug="case-msg", given=[
            {"name": "AcctId", "expr": '"ZG0790"'},
            {"name": "msgTxt", "parts": ["Loss on ", {"$given": "acctid"}]},
        ])
        context: dict = {}
        _cycle_evaluate_given_bindings(bind_agent, v2_ctx, context)
        assert context["given"][1]["msgtxt"] == "Loss on ZG0790"

        agent = MagicMock(id=1, slug="case-msg")
        resolved = _resolve_given_message({"$given": "MsgTxt"}, agent, context)
        assert "ZG0790" not in resolved
        assert mask_account("ZG0790") in resolved
