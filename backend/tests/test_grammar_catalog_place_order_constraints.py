"""
Sprint 17 — conditional parameter requirements (`required_if`) and
declared mutual-exclusion (`mutually_exclusive_groups`) for place_order's
grammar catalog entry.

Covers the exact "did the new YAML key actually survive to the wire"
question the Sprint 17 plan calls out:
  1. agent_grammar.yaml's `price`/`trigger_price` fields carry the
     expected `required_if` spec after `$ref` resolution
     (backend/api/algo/grammar.py:_resolve_param_spec).
  2. place_order's token-level `mutually_exclusive_groups` key survives
     into SYSTEM_TOKENS and is exposed via
     `grammar.get_action_mutex_groups("place_order")`.
  3. End-to-end: `GET /api/admin/grammar/tokens?grammar=action` (the real
     route `fetchGrammarTokens('action')` hits — see
     frontend/src/lib/api.js:fetchGrammarTokens and
     backend/api/routes/grammar.py:GrammarTokenController.list_tokens) —
     exercised here via `GrammarTokenController.list_tokens` directly
     (same pattern as test_grammar_catalog_fragment_merge.py) with a fake
     DB row built from the REAL, live-resolved place_order params_schema
     — returns `required_if` on price/trigger_price and
     `mutually_exclusive_groups` at the token level in the serialized
     GrammarTokenOut.

Perf: no DB/network calls (mocked session), pure in-memory assertions.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.algo import grammar
from backend.api.routes import grammar as g
from backend.api.schemas import GrammarTokenOut


def _live_place_order_token() -> dict:
    for tok in grammar.SYSTEM_TOKENS:
        if tok.get("grammar_kind") == "action" and tok.get("token") == "place_order":
            return tok
    raise AssertionError("place_order token not found in SYSTEM_TOKENS")


# ── required_if resolution (in-memory catalog) ──────────────────────────

class TestRequiredIfResolution:
    def test_price_required_if_limit_or_sl(self):
        schema = _live_place_order_token()["params_schema"]
        assert schema["price"]["required_if"] == {
            "field": "order_type", "in": ["LIMIT", "SL"],
        }

    def test_trigger_price_required_if_sl_or_sl_m(self):
        schema = _live_place_order_token()["params_schema"]
        assert schema["trigger_price"]["required_if"] == {
            "field": "order_type", "in": ["SL", "SL-M"],
        }

    def test_required_if_is_local_not_in_shared_catalog(self):
        """order_fields.yaml's own header comment says required-ness is
        LOCAL per consuming schema — required_if must not leak into the
        shared catalog entries themselves."""
        assert "required_if" not in grammar._ORDER_FIELDS["price"]
        assert "required_if" not in grammar._ORDER_FIELDS["trigger_price"]

    def test_other_fields_have_no_required_if(self):
        """Scope guard — PLAN confirmed no other field has this pattern."""
        schema = _live_place_order_token()["params_schema"]
        for name, spec in schema.items():
            if name in ("price", "trigger_price"):
                continue
            assert "required_if" not in spec, f"unexpected required_if on {name}"


# ── mutually_exclusive_groups (in-memory catalog) ───────────────────────

class TestMutexGroupsResolution:
    def test_place_order_token_level_mutex_groups(self):
        tok = _live_place_order_token()
        assert tok["mutually_exclusive_groups"] == [
            ["wing_premium_pct_override", "wing_strike_offset_override"],
        ]

    def test_mutex_groups_not_nested_inside_params_schema(self):
        tok = _live_place_order_token()
        assert "mutually_exclusive_groups" not in tok["params_schema"]

    def test_get_action_mutex_groups_place_order(self):
        assert grammar.get_action_mutex_groups("place_order") == [
            ["wing_premium_pct_override", "wing_strike_offset_override"],
        ]

    def test_get_action_mutex_groups_unknown_action_is_none(self):
        assert grammar.get_action_mutex_groups("not_a_real_action") is None

    def test_get_action_mutex_groups_action_without_groups_is_none(self):
        """modify_order declares no mutex groups — must not spuriously
        inherit place_order's."""
        assert grammar.get_action_mutex_groups("modify_order") is None


# ── end-to-end: survives _to_out / the real catalog-serving route ──────

def _grammar_token_row(**kw):
    defaults = dict(
        id=42, grammar_kind="action", token_kind="action_type", token="place_order",
        value_type="void", units=None,
        description="Place a new broker order with the supplied parameters.",
        resolver=None,
        params_schema=_live_place_order_token()["params_schema"],
        enum_values=None, template_body=None,
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


class TestToOutSurvival:
    def test_to_out_populates_mutex_groups_for_place_order(self):
        row = _grammar_token_row()
        out = g._to_out(row)
        assert isinstance(out, GrammarTokenOut)
        assert out.mutually_exclusive_groups == [
            ["wing_premium_pct_override", "wing_strike_offset_override"],
        ]

    def test_to_out_params_schema_price_carries_required_if(self):
        row = _grammar_token_row()
        out = g._to_out(row)
        assert out.params_schema["price"]["required_if"] == {
            "field": "order_type", "in": ["LIMIT", "SL"],
        }
        assert out.params_schema["trigger_price"]["required_if"] == {
            "field": "order_type", "in": ["SL", "SL-M"],
        }

    def test_to_out_no_mutex_groups_for_non_action_token(self):
        row = _grammar_token_row(
            grammar_kind="condition", token_kind="metric", token="pnl",
            params_schema=None,
        )
        out = g._to_out(row)
        assert out.mutually_exclusive_groups is None

    def test_to_out_no_mutex_groups_for_custom_non_system_action(self):
        """A custom, operator-authored action token can never carry a
        mutex-groups value — it can only ever come from the code-reviewed
        YAML catalog (grammar._ACTION_MUTEX_GROUPS), never a DB row."""
        row = _grammar_token_row(token="place_order", is_system=False)
        out = g._to_out(row)
        assert out.mutually_exclusive_groups is None


@pytest.mark.asyncio
class TestListTokensRouteSurvival:
    """End-to-end through `GrammarTokenController.list_tokens` — the exact
    handler behind `GET /api/admin/grammar/tokens?grammar=action`, which
    is the real route `frontend/src/lib/api.js:fetchGrammarTokens('action')`
    calls (see frontend/src/lib/command/grammars/agents.js:602-603)."""

    async def test_list_tokens_action_grammar_includes_required_if_and_mutex_groups(
        self, monkeypatch,
    ):
        row = _grammar_token_row()
        monkeypatch.setattr(g, "async_session", lambda: _session_ctx([row]))

        out = await g.GrammarTokenController.list_tokens.fn(None, grammar="action")

        assert len(out) == 1
        place_order_out = out[0]
        assert place_order_out.token == "place_order"
        assert place_order_out.mutually_exclusive_groups == [
            ["wing_premium_pct_override", "wing_strike_offset_override"],
        ]
        assert place_order_out.params_schema["price"]["required_if"] == {
            "field": "order_type", "in": ["LIMIT", "SL"],
        }
        assert place_order_out.params_schema["trigger_price"]["required_if"] == {
            "field": "order_type", "in": ["SL", "SL-M"],
        }
