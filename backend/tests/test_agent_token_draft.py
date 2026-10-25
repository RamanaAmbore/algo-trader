"""
Wave 3 of the "Unify DSL token registration" plan — AI-drafted grammar
TOKEN pipeline (`backend/api/algo/agent_ai.py:draft_token_from_prompt`).

This is the token-drafting sibling of `draft_agent_from_prompt`
(covered by `test_agent_ai_coverage.py`): operator prompt in, a
candidate `(resolver .py source, test .py source, params_schema,
description)` bundle out, as plain JSON — never written to disk, never
imported/executed by this process. See `TokenDraft`'s own docstring in
`agent_ai.py` for the full safety boundary this suite is verifying.

Mocking strategy: `_td_call_gemini(user_msg) -> (raw_text, error)` is
patched directly, the same granularity the module already affords
rather than reaching into `google.genai` internals (which
`test_agent_ai_coverage.py` also avoids, for the sibling
`draft_agent_from_prompt` pipeline, because the import is local to the
function and not usefully mockable at the SDK-client level).

Five quality dimensions:
  1. SSOT        — one validation pipeline (`_td_build_validated_draft`)
                   covers both the CREATE-style flow (no hints) and the
                   hinted flow; tests exercise both without duplicating
                   the underlying checks.
  2. Correctness — covers the full matrix: empty prompt, disabled
                   GenAI, pre-LLM collision/unsupported-kind rejection
                   (LLM never called), post-LLM collision, malformed
                   JSON, non-dict draft, broken resolver/test source
                   (syntax error — never raises, draft still returned),
                   wrong-shape signature per kind, a dangerous-import
                   warning (not a hard error), hinted-identity override,
                   and a fully clean draft.
  3. Performance — the pre-LLM hint-collision test asserts
                   `_td_call_gemini` was NEVER CALLED — the cheap check
                   really does run first.
  4. Reuse       — module-level whitelist/regex constants
                   (`_TOKEN_KIND_PREFIX`, `_TOKEN_NAME_RE`) are asserted
                   against directly rather than re-derived in the test.
  5. UX          — every hard-failure path is checked for a non-empty,
                   human-readable `errors` entry; the "broken code"
                   case explicitly asserts the function returns rather
                   than raises, so a caller (the /admin/mcp UI) can
                   always render something.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.api.algo import agent_ai as m


_CLEAN_METRIC_RESOLVER = '''"""Metric resolver — `mean_drawdown_30d`."""

from __future__ import annotations


def _metric_mean_drawdown_30d(ctx, row):
    """Returns a float or None."""
    return row.get("drawdown_30d")
'''

_CLEAN_METRIC_TEST = '''from backend.api.algo.grammar_tokens.metric_mean_drawdown_30d import (
    _metric_mean_drawdown_30d,
)


def test_reads_drawdown_30d():
    assert _metric_mean_drawdown_30d(None, {"drawdown_30d": -500.0}) == -500.0


def test_none_when_missing():
    assert _metric_mean_drawdown_30d(None, {}) is None
'''

_CLEAN_LLM_RESPONSE = {
    "draft": {
        "grammar_kind": "condition",
        "token_kind": "metric",
        "token": "mean_drawdown_30d",
        "description": "30-day mean drawdown.",
        "params_schema": None,
        "resolver_source": _CLEAN_METRIC_RESOLVER,
        "test_source": _CLEAN_METRIC_TEST,
    },
    "why_summary": "Computes the 30-day mean drawdown for a position row.",
    "errors": [],
}


def _mock_llm(parsed: dict, err: "str | None" = None):
    """Patch `_td_call_gemini` to return a fixed (raw_json, err) pair."""
    if err is not None:
        return patch.object(m, "_td_call_gemini", return_value=("", err))
    import json
    return patch.object(m, "_td_call_gemini", return_value=(json.dumps(parsed), None))


# ═══════════════════════════════════════════════════════════════════════════
#  Early hard failures — never reach the LLM
# ═══════════════════════════════════════════════════════════════════════════

def test_empty_prompt():
    result = m.draft_token_from_prompt("")
    assert result.draft == {}
    assert result.errors == ["Empty prompt"]


def test_whitespace_only_prompt():
    result = m.draft_token_from_prompt("   \n\t  ")
    assert result.draft == {}
    assert result.errors != []


def test_genai_disabled():
    with patch.object(m, "is_enabled", return_value=False):
        result = m.draft_token_from_prompt("add a metric for 30-day drawdown")
    assert result.draft == {}
    assert any("disabled" in e.lower() for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════
#  Pre-LLM hint precheck — collision / unsupported kind rejected BEFORE
#  any LLM call
# ═══════════════════════════════════════════════════════════════════════════

def test_hinted_collision_rejected_before_llm_call():
    existing = {("condition", "metric", "cash")}
    with patch.object(m, "is_enabled", return_value=True), \
         patch.object(m, "_td_call_gemini") as mock_llm:
        result = m.draft_token_from_prompt(
            "a metric that reads live cash",
            grammar_kind="condition", token_kind="metric", token="cash",
            existing_names=existing,
        )
    mock_llm.assert_not_called()
    assert result.draft == {}
    assert any("already exists" in e for e in result.errors)


def test_hinted_unsupported_kind_rejected_before_llm_call():
    with patch.object(m, "is_enabled", return_value=True), \
         patch.object(m, "_td_call_gemini") as mock_llm:
        result = m.draft_token_from_prompt(
            "a new condition operator",
            grammar_kind="condition", token_kind="operator", token="between",
        )
    mock_llm.assert_not_called()
    assert result.draft == {}
    assert any("unsupported" in e.lower() for e in result.errors)


def test_hint_collision_is_case_insensitive():
    existing = {("condition", "metric", "cash")}
    with patch.object(m, "is_enabled", return_value=True), \
         patch.object(m, "_td_call_gemini") as mock_llm:
        result = m.draft_token_from_prompt(
            "a metric that reads live cash",
            grammar_kind="condition", token_kind="metric", token="CASH",
            existing_names=existing,
        )
    mock_llm.assert_not_called()
    assert any("already exists" in e for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════
#  LLM transport failures
# ═══════════════════════════════════════════════════════════════════════════

def test_llm_unavailable():
    with patch.object(m, "is_enabled", return_value=True), _mock_llm({}, err="LLM unavailable: boom"):
        result = m.draft_token_from_prompt("a new metric")
    assert result.draft == {}
    assert result.errors == ["LLM unavailable: boom"]


def test_llm_empty_response():
    with patch.object(m, "is_enabled", return_value=True), \
         patch.object(m, "_td_call_gemini", return_value=("", None)):
        result = m.draft_token_from_prompt("a new metric")
    assert result.draft == {}
    assert any("empty" in e.lower() for e in result.errors)


def test_llm_invalid_json():
    with patch.object(m, "is_enabled", return_value=True), \
         patch.object(m, "_td_call_gemini", return_value=("{not json", None)):
        result = m.draft_token_from_prompt("a new metric")
    assert result.draft == {}
    assert any("invalid json" in e.lower() for e in result.errors)


def test_llm_draft_field_not_a_dict():
    with patch.object(m, "is_enabled", return_value=True), \
         _mock_llm({"draft": "nope", "why_summary": "", "errors": []}):
        result = m.draft_token_from_prompt("a new metric")
    assert result.draft == {}
    assert any("not a dict" in e for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════
#  Static validation of a parsed draft — never raises, always returns
# ═══════════════════════════════════════════════════════════════════════════

def test_clean_draft_has_no_errors():
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(_CLEAN_LLM_RESPONSE):
        result = m.draft_token_from_prompt("add a metric for 30-day drawdown")
    assert result.errors == []
    assert result.draft["grammar_kind"] == "condition"
    assert result.draft["token_kind"] == "metric"
    assert result.draft["token"] == "mean_drawdown_30d"
    assert result.draft["resolver_function_name"] == "_metric_mean_drawdown_30d"
    assert result.draft["resolver_module_path"] == (
        "backend.api.algo.grammar_tokens.metric_mean_drawdown_30d"
    )
    assert result.draft["resolver_file_path"] == (
        "backend/api/algo/grammar_tokens/metric_mean_drawdown_30d.py"
    )
    assert result.draft["test_file_path"] == (
        "backend/tests/grammar_tokens/test_metric_mean_drawdown_30d.py"
    )
    assert result.why_summary


def test_broken_resolver_syntax_returns_error_never_raises():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = "def _metric_broken(ctx, row:\n    return None"  # syntax error
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric for 30-day drawdown")
    assert any("does not parse" in e for e in result.errors)
    # draft is still returned (not raised, not {}), so the UI has
    # something to render.
    assert result.draft.get("token") == "mean_drawdown_30d"


def test_resolver_with_two_top_level_functions_rejected():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = (
        "def _a(ctx, row):\n    return 1\n\n\ndef _b(ctx, row):\n    return 2\n"
    )
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("exactly one top-level function" in e for e in result.errors)


def test_metric_resolver_wrong_signature_rejected():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = "def _metric_oops(row):\n    return row\n"
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("must take (ctx, row)" in e for e in result.errors)


def test_action_resolver_must_be_async():
    bad = {
        "draft": {
            "grammar_kind": "action", "token_kind": "action_type", "token": "ping_slack",
            "description": "sends a slack ping",
            "params_schema": {"type": "object", "properties": {}},
            "resolver_source": "def ping_slack(ctx, params):\n    return {}\n",
            "test_source": "def test_x():\n    assert True\n",
        },
        "why_summary": "pings slack", "errors": [],
    }
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add an action that pings slack")
    assert any("async def" in e for e in result.errors)


def test_scope_resolver_signature_ok_with_single_arg():
    good = {
        "draft": {
            "grammar_kind": "condition", "token_kind": "scope", "token": "positions.mcx_only",
            "description": "MCX-only position rows",
            "params_schema": None,
            "resolver_source": "def _scope_positions_mcx_only(ctx):\n    return []\n",
            "test_source": "def test_x():\n    assert True\n",
        },
        "why_summary": "scope", "errors": [],
    }
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(good):
        result = m.draft_token_from_prompt("a scope for mcx-only positions")
    assert result.errors == []
    assert result.draft["resolver_file_path"] == (
        "backend/api/algo/grammar_tokens/scope_positions_mcx_only.py"
    )


def test_channel_resolver_signature_ok():
    good = {
        "draft": {
            "grammar_kind": "notify", "token_kind": "channel", "token": "pagerduty",
            "description": "pages pagerduty",
            "params_schema": None,
            "resolver_source": (
                "def _send_pagerduty(title, body, tg=None, priority=None):\n"
                "    pass\n"
            ),
            "test_source": "def test_x():\n    assert True\n",
        },
        "why_summary": "pager", "errors": [],
    }
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(good):
        result = m.draft_token_from_prompt("a notify channel for pagerduty")
    assert result.errors == []


def test_dangerous_import_is_a_warning_not_an_error():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = (
        "import backend.brokers.adapters.kite as kite\n\n\n"
        "def _metric_dangerous(ctx, row):\n"
        "    return 0\n"
    )
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert result.errors == []  # never a hard error
    assert any("backend.brokers" in w for w in result.warnings)


def test_dangerous_call_is_a_warning():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = (
        "def _metric_dangerous(ctx, row):\n"
        "    eval('1')\n"
        "    return 0\n"
    )
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("eval(" in w for w in result.warnings)


def test_params_schema_wrong_type_rejected():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["params_schema"] = "not a dict"
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("params_schema must be a JSON object" in e for e in result.errors)


def test_post_llm_collision_when_no_hint_given():
    existing = {("condition", "metric", "mean_drawdown_30d")}
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(_CLEAN_LLM_RESPONSE):
        result = m.draft_token_from_prompt(
            "add a metric for 30-day drawdown", existing_names=existing,
        )
    assert any("already exists" in e for e in result.errors)
    # draft is still populated so the operator can see what was proposed.
    assert result.draft.get("token") == "mean_drawdown_30d"


def test_unsupported_kind_proposed_by_llm_without_hint():
    bad = {
        "draft": {
            "grammar_kind": "condition", "token_kind": "operator", "token": "between",
            "description": "x", "params_schema": None,
            "resolver_source": None, "test_source": None,
        },
        "why_summary": "x", "errors": [],
    }
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("a new comparison operator")
    assert any("unsupported" in e.lower() for e in result.errors)


def test_invalid_token_name_rejected():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["token"] = "Not Valid!"
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("not a valid lowercase identifier" in e for e in result.errors)


def test_hinted_identity_overrides_llm_proposal():
    """The operator's explicit hints win over whatever name/kind the LLM
    proposed — closes the drift the LLM could otherwise introduce."""
    drifted = dict(_CLEAN_LLM_RESPONSE)
    drifted["draft"] = dict(drifted["draft"])
    drifted["draft"]["token"] = "some_other_name"
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(drifted):
        result = m.draft_token_from_prompt(
            "add a metric for 30-day drawdown",
            grammar_kind="condition", token_kind="metric", token="mean_drawdown_30d",
        )
    assert result.draft["token"] == "mean_drawdown_30d"
    assert result.errors == []


def test_resolver_path_never_trusted_from_llm():
    """Even if the LLM's JSON smuggled a `resolver` field pointing at an
    already-importable, already-reviewed function elsewhere in the
    codebase, the derived path must come from the whitelist + verified
    token name — never from that field (which this module never even
    reads)."""
    sneaky = dict(_CLEAN_LLM_RESPONSE)
    sneaky["draft"] = dict(sneaky["draft"])
    sneaky["draft"]["resolver"] = "backend.api.algo.actions_live._action_live_place_order"
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(sneaky):
        result = m.draft_token_from_prompt("add a metric for 30-day drawdown")
    assert "resolver" not in result.draft
    assert result.draft["resolver_module_path"] == (
        "backend.api.algo.grammar_tokens.metric_mean_drawdown_30d"
    )


def test_empty_resolver_source_is_an_error():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["resolver_source"] = ""
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("resolver_source is empty" in e for e in result.errors)


def test_empty_test_source_is_an_error():
    bad = dict(_CLEAN_LLM_RESPONSE)
    bad["draft"] = dict(bad["draft"])
    bad["draft"]["test_source"] = None
    with patch.object(m, "is_enabled", return_value=True), _mock_llm(bad):
        result = m.draft_token_from_prompt("add a metric")
    assert any("test_source is empty" in e for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════
#  Whitelist / regex sanity
# ═══════════════════════════════════════════════════════════════════════════

def test_whitelist_is_exactly_four_kinds():
    assert set(m._TOKEN_KIND_PREFIX.keys()) == {
        ("condition", "metric"), ("condition", "scope"),
        ("notify", "channel"), ("action", "action_type"),
    }


def test_token_name_regex_accepts_dotted_and_underscored():
    assert m._TOKEN_NAME_RE.match("mean_drawdown_30d")
    assert m._TOKEN_NAME_RE.match("funds.total")
    assert not m._TOKEN_NAME_RE.match("Not Valid!")
    assert not m._TOKEN_NAME_RE.match("_leading_underscore")
    assert not m._TOKEN_NAME_RE.match("3starts_with_digit")
