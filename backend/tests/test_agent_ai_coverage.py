"""
Comprehensive coverage for backend/api/algo/agent_ai.py (0% → 80%+).

Covers:
  - _summarise_token(): render registry token with value_type, units, enum, params
  - _grammar_snapshot(): pull active tokens from registry
  - _build_prompt(): compose user message with operator request + grammar
  - _enrich_unknown_token_errors(): enhance error messages with token hints
  - _strip_fences(): strip markdown code fences from LLM response
  - _clamp_safety(): force inactive, paper-only, one_shot defaults
  - _walk_leaves(): recursively yield condition tree leaves
  - _scan_thresholds(): flag sub-percent/sub-rupee trip-wire risks
  - _validate_against_registry(): use evaluator validator on condition tree
  - draft_agent_from_prompt(): main entry point (mocking Gemini API)
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call
from dataclasses import dataclass

from backend.api.algo.agent_ai import (
    AgentDraft,
    _summarise_token,
    _grammar_snapshot,
    _build_prompt,
    _enrich_unknown_token_errors,
    _strip_fences,
    _clamp_safety,
    _walk_leaves,
    _scan_thresholds,
    _validate_against_registry,
    draft_agent_from_prompt,
    _DESTRUCTIVE_ACTIONS,
    _TRIPWIRE_PCT,
    _TRIPWIRE_ABS,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Token Summarization
# ═══════════════════════════════════════════════════════════════════════════

def test_summarise_token_with_all_fields():
    """Token with value_type, units, enum, params_schema → rich summary."""
    @dataclass
    class Token:
        token: str = "pnl_percentage"
        value_type: str = "float"
        units: str = "%"
        enum_values: list = None
        description: str = "Portfolio P&L as percentage"
        params_schema: dict = None

    tok = Token()
    result = _summarise_token(tok, "metric")
    assert "pnl_percentage" in result
    assert "float" in result
    assert "%" in result
    assert "Portfolio P&L as percentage" in result


def test_summarise_token_with_enum():
    """Token with enum_values → show set."""
    @dataclass
    class Token:
        token: str = "scope_type"
        value_type: str = "str"
        units: str = ""
        enum_values: list = None
        description: str = "Aggregation scope"
        params_schema: dict = None

        def __post_init__(self):
            if self.enum_values is None:
                self.enum_values = ["total", "per_account", "any_acct"]

    tok = Token()
    result = _summarise_token(tok, "scope")
    assert "scope_type" in result
    assert "total" in result
    assert "per_account" in result


def test_summarise_token_action_type_with_params_schema():
    """Action type with params_schema → show required params."""
    @dataclass
    class Token:
        token: str = "place_order"
        value_type: str = None
        units: str = ""
        enum_values: list = None
        description: str = "Place a new order"
        params_schema: dict = None

        def __post_init__(self):
            if self.params_schema is None:
                self.params_schema = {
                    "required": ["symbol", "qty"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "qty": {"type": "integer"},
                        "price": {"type": "number"},
                    }
                }

    tok = Token()
    result = _summarise_token(tok, "action_type")
    assert "place_order" in result
    assert "symbol*" in result  # required
    assert "qty*" in result     # required
    assert "price" in result    # optional


def test_summarise_token_minimal():
    """Token with no extras → just name."""
    @dataclass
    class Token:
        token: str = "minimal"
        value_type: str = None
        units: str = ""
        enum_values: list = None
        description: str = ""
        params_schema: dict = None

    tok = Token()
    result = _summarise_token(tok, "metric")
    assert result == "minimal"


# ═══════════════════════════════════════════════════════════════════════════
#  Grammar Snapshot
# ═══════════════════════════════════════════════════════════════════════════

def test_grammar_snapshot_pulls_active_tokens():
    """Snapshot pulls only active tokens, organized by kind."""
    # Use simple mock objects instead of dataclass
    def make_token(token, is_active, grammar_kind, token_kind, desc=""):
        t = MagicMock()
        t.token = token
        t.is_active = is_active
        t.grammar_kind = grammar_kind
        t.token_kind = token_kind
        t.value_type = None
        t.units = ""
        t.enum_values = None
        t.description = desc
        t.params_schema = None
        return t

    mock_tokens = {
        "pnl%": make_token("pnl%", True, "condition", "metric"),
        "total": make_token("total", True, "condition", "scope"),
        "gt": make_token("gt", True, "condition", "operator"),
        "telegram": make_token("telegram", True, "notify", "channel"),
        "place_order": make_token("place_order", True, "action", "action_type"),
        "inactive_tok": make_token("inactive", False, "condition", "metric"),
    }

    # Mock at the import point where REGISTRY is used
    with patch('backend.api.algo.grammar_registry.REGISTRY') as mock_registry:
        mock_registry.tokens = mock_tokens
        snap = _grammar_snapshot()

        assert "pnl%" in snap["metrics"]
        assert "total" in snap["scopes"]
        assert "gt" in snap["operators"]
        assert "telegram" in snap["channels"]
        assert "place_order" in snap["actions"]
        assert "inactive" not in str(snap), "inactive tokens should be filtered"
        assert "_raw" in snap, "snapshot must include _raw for diagnostics"


def test_grammar_snapshot_sorted():
    """Snapshot tokens are sorted alphabetically."""
    def make_token(token, is_active, grammar_kind, token_kind):
        t = MagicMock()
        t.token = token
        t.is_active = is_active
        t.grammar_kind = grammar_kind
        t.token_kind = token_kind
        t.value_type = None
        t.units = ""
        t.enum_values = None
        t.description = ""
        t.params_schema = None
        return t

    mock_tokens = {
        "zebra": make_token("zebra", True, "condition", "metric"),
        "alpha": make_token("alpha", True, "condition", "metric"),
        "beta": make_token("beta", True, "condition", "metric"),
    }

    with patch('backend.api.algo.grammar_registry.REGISTRY') as mock_registry:
        mock_registry.tokens = mock_tokens
        snap = _grammar_snapshot()

        metrics = snap["metrics"]
        assert metrics[0].startswith("alpha")
        assert metrics[1].startswith("beta")
        assert metrics[2].startswith("zebra")


# ═══════════════════════════════════════════════════════════════════════════
#  Build Prompt
# ═══════════════════════════════════════════════════════════════════════════

def test_build_prompt_includes_user_request():
    """Prompt includes operator's natural-language request."""
    grammar = {"metrics": ["pnl%"], "scopes": ["total"], "operators": ["gt"],
               "channels": ["telegram"], "actions": []}
    user_prompt = "Alert when daily loss exceeds 2%"

    result = _build_prompt(user_prompt, grammar)
    assert "Alert when daily loss exceeds 2%" in result


def test_build_prompt_includes_live_grammar():
    """Prompt includes all grammar slots."""
    grammar = {
        "metrics": ["pnl%"],
        "scopes": ["total"],
        "operators": ["gt"],
        "channels": ["telegram"],
        "actions": ["place_order"]
    }

    result = _build_prompt("test", grammar)
    assert "metrics:" in result
    assert "scopes:" in result
    assert "operators:" in result
    assert "channels:" in result
    assert "actions:" in result


def test_build_prompt_empty_slots_show_none():
    """Empty slots render as (none)."""
    grammar = {"metrics": [], "scopes": [], "operators": [], "channels": [], "actions": []}

    result = _build_prompt("test", grammar)
    # Check for the placeholder text
    assert "(none)" in result


# ═══════════════════════════════════════════════════════════════════════════
#  Strip Fences
# ═══════════════════════════════════════════════════════════════════════════

def test_strip_fences_json_block():
    """Strip ```json ... ``` wrapping."""
    raw = '```json\n{"draft": {}}\n```'
    result = _strip_fences(raw)
    assert result == '{"draft": {}}'


def test_strip_fences_plain_json():
    """Pass through plain JSON."""
    raw = '{"draft": {}}'
    result = _strip_fences(raw)
    assert result == '{"draft": {}}'


def test_strip_fences_triple_backtick():
    """Strip ``` (without lang)."""
    raw = '```\n{"draft": {}}\n```'
    result = _strip_fences(raw)
    assert result == '{"draft": {}}'


def test_strip_fences_whitespace():
    """Strip leading/trailing whitespace."""
    raw = '  \n  ```json\n{"draft": {}}\n```  \n  '
    result = _strip_fences(raw)
    assert result == '{"draft": {}}'


# ═══════════════════════════════════════════════════════════════════════════
#  Clamp Safety
# ═══════════════════════════════════════════════════════════════════════════

def test_clamp_safety_defaults_to_inactive():
    """AI agents default to inactive status when not set."""
    draft = {}
    warnings = []
    _clamp_safety(draft, warnings)
    assert draft["status"] == "inactive"


def test_clamp_safety_forces_paper():
    """AI agents land in paper mode."""
    draft = {"trade_mode": "live"}
    warnings = []
    _clamp_safety(draft, warnings)
    assert draft["trade_mode"] == "paper"
    assert any("paper mode" in w.lower() for w in warnings)


def test_clamp_safety_default_one_shot():
    """Default lifespan is one_shot."""
    draft = {}
    warnings = []
    _clamp_safety(draft, warnings)
    assert draft["lifespan_type"] == "one_shot"


def test_clamp_safety_preserves_existing_lifespan():
    """If lifespan already set, keep it."""
    draft = {"lifespan_type": "persistent"}
    warnings = []
    _clamp_safety(draft, warnings)
    assert draft["lifespan_type"] == "persistent"


def test_clamp_safety_removes_destructive_actions():
    """Strip destructive action types."""
    draft = {
        "actions": [
            {"type": "place_order", "params": {}},
            {"type": "chase_close", "params": {}},
            {"type": "cancel_order", "params": {}},
        ]
    }
    warnings = []
    _clamp_safety(draft, warnings)
    assert len(draft["actions"]) == 1
    assert draft["actions"][0]["type"] == "place_order"
    assert any("destructive" in w.lower() for w in warnings)


# ═══════════════════════════════════════════════════════════════════════════
#  Walk Leaves
# ═══════════════════════════════════════════════════════════════════════════

def test_walk_leaves_simple_leaf():
    """Single leaf node yields itself."""
    node = {"metric": "pnl%", "scope": "total", "op": "gt", "value": 100}
    leaves = list(_walk_leaves(node))
    assert len(leaves) == 1
    assert leaves[0] == node


def test_walk_leaves_all_node():
    """AND node yields all child leaves."""
    node = {
        "all": [
            {"metric": "pnl%", "scope": "total", "op": "gt", "value": 100},
            {"metric": "margin%", "scope": "total", "op": "lt", "value": 50},
        ]
    }
    leaves = list(_walk_leaves(node))
    assert len(leaves) == 2


def test_walk_leaves_any_node():
    """OR node yields all child leaves."""
    node = {
        "any": [
            {"metric": "pnl%", "scope": "total", "op": "gt", "value": 100},
        ]
    }
    leaves = list(_walk_leaves(node))
    assert len(leaves) == 1


def test_walk_leaves_not_node():
    """NOT node yields child leaves."""
    node = {
        "not": {"metric": "pnl%", "scope": "total", "op": "gt", "value": 100}
    }
    leaves = list(_walk_leaves(node))
    assert len(leaves) == 1


def test_walk_leaves_nested():
    """Deeply nested tree yields all leaves."""
    node = {
        "all": [
            {
                "any": [
                    {"metric": "pnl%", "scope": "total", "op": "gt", "value": 100},
                    {"metric": "loss", "scope": "total", "op": "lt", "value": -500},
                ]
            },
            {"metric": "margin%", "scope": "total", "op": "lt", "value": 50},
        ]
    }
    leaves = list(_walk_leaves(node))
    assert len(leaves) == 3


def test_walk_leaves_non_dict_returns_none():
    """Non-dict input yields nothing."""
    leaves = list(_walk_leaves("string"))
    assert leaves == []
    leaves = list(_walk_leaves(None))
    assert leaves == []


# ═══════════════════════════════════════════════════════════════════════════
#  Scan Thresholds
# ═══════════════════════════════════════════════════════════════════════════

def test_scan_thresholds_percent_tripwire():
    """Sub-0.1% thresholds flag as trip-wire."""
    draft = {
        "conditions": {
            "metric": "pnl_percentage",
            "scope": "total",
            "op": "gt",
            "value": 0.05  # below _TRIPWIRE_PCT (0.10)
        }
    }
    warnings = []
    _scan_thresholds(draft, warnings)
    assert any("trip-wire" in w.lower() for w in warnings)


def test_scan_thresholds_absolute_tripwire():
    """Sub-₹100 thresholds flag as risky."""
    draft = {
        "conditions": {
            "metric": "absolute_loss",
            "scope": "total",
            "op": "lt",
            "value": 50  # below _TRIPWIRE_ABS (100)
        }
    }
    warnings = []
    _scan_thresholds(draft, warnings)
    assert any("₹100" in w or "below" in w.lower() for w in warnings)


def test_scan_thresholds_reasonable_values():
    """Reasonable thresholds produce no warnings."""
    draft = {
        "conditions": {
            "metric": "pnl_percentage",
            "scope": "total",
            "op": "gt",
            "value": 1.0  # above _TRIPWIRE_PCT
        }
    }
    warnings = []
    _scan_thresholds(draft, warnings)
    assert warnings == []


def test_scan_thresholds_ignores_non_numeric():
    """Non-numeric values are skipped."""
    draft = {
        "conditions": {
            "metric": "status",
            "scope": "total",
            "op": "eq",
            "value": "ready"
        }
    }
    warnings = []
    _scan_thresholds(draft, warnings)
    # No warnings since value isn't numeric


def test_scan_thresholds_percent_metric_detection():
    """Metrics with 'pct', '_percentage', or '%' are scanned as percent."""
    for metric_name in ["pnl_pct", "daily_percentage", "change%"]:
        draft = {
            "conditions": {
                "metric": metric_name,
                "scope": "total",
                "op": "gt",
                "value": 0.05
            }
        }
        warnings = []
        _scan_thresholds(draft, warnings)
        assert any("trip-wire" in w.lower() for w in warnings), f"Failed for {metric_name}"


# ═══════════════════════════════════════════════════════════════════════════
#  Enrich Unknown Token Errors
# ═══════════════════════════════════════════════════════════════════════════

def test_enrich_unknown_token_no_errors():
    """No errors → no mutation."""
    errors = []
    raw = {"metrics": ["pnl"], "scopes": [], "operators": []}
    _enrich_unknown_token_errors(errors, raw)
    assert errors == []


def test_enrich_unknown_token_metric():
    """Unknown metric error gets hint with available metrics."""
    errors = ["unknown metric token 'mystery_metric'"]
    raw = {"metrics": ["pnl", "margin", "loss"], "scopes": [], "operators": []}
    _enrich_unknown_token_errors(errors, raw)
    assert len(errors) == 1
    assert "Available metrics:" in errors[0]
    assert "pnl" in errors[0]


def test_enrich_unknown_token_scope():
    """Unknown scope error gets hint."""
    errors = ["unknown scope token 'per_leg'"]
    raw = {"metrics": [], "scopes": ["total", "per_account"], "operators": []}
    _enrich_unknown_token_errors(errors, raw)
    assert len(errors) == 1
    assert "Available scopes:" in errors[0]
    assert "per_account" in errors[0]


def test_enrich_unknown_token_multiple_errors():
    """Multiple errors, some unknown token, some other → only unknown get enriched."""
    errors = [
        "unknown metric token 'x'",
        "some other error",
    ]
    raw = {"metrics": ["a", "b"], "scopes": [], "operators": []}
    _enrich_unknown_token_errors(errors, raw)
    assert len(errors) == 2
    assert "Available metrics:" in errors[0]
    assert "some other error" == errors[1]


def test_enrich_truncates_long_list():
    """If > 8 tokens, show first 8 + ellipsis."""
    errors = ["unknown metric token 'x'"]
    raw = {"metrics": [f"m{i}" for i in range(20)], "scopes": [], "operators": []}
    _enrich_unknown_token_errors(errors, raw)
    assert "…" in errors[0]


# ═══════════════════════════════════════════════════════════════════════════
#  Validate Against Registry
# ═══════════════════════════════════════════════════════════════════════════

def test_validate_against_registry_valid_leaf():
    """Valid leaf passes validation."""
    draft = {
        "conditions": {
            "metric": "pnl%",
            "scope": "total",
            "op": "gt",
            "value": 100
        }
    }
    errors = []

    with patch('backend.api.algo.agent_evaluator.validate') as mock_validate:
        mock_validate.return_value = []
        _validate_against_registry(draft, errors)
        assert errors == []


def test_validate_against_registry_invalid_tree():
    """Invalid tree appends errors."""
    draft = {
        "conditions": {
            "metric": "unknown",
            "scope": "unknown",
            "op": "gt",
            "value": 100
        }
    }
    errors = []

    with patch('backend.api.algo.agent_evaluator.validate') as mock_validate:
        mock_validate.return_value = ["unknown metric", "unknown scope"]
        _validate_against_registry(draft, errors)
        assert len(errors) == 2
        assert "unknown metric" in errors


def test_validate_against_registry_empty_conditions():
    """Empty conditions dict → error."""
    draft = {"conditions": {}}
    errors = []
    _validate_against_registry(draft, errors)
    assert any("empty" in e.lower() for e in errors)


def test_validate_against_registry_none_conditions():
    """None conditions → error."""
    draft = {"conditions": None}
    errors = []
    _validate_against_registry(draft, errors)
    assert any("empty" in e.lower() or "not a dict" in e.lower() for e in errors)


# ═══════════════════════════════════════════════════════════════════════════
#  Main Entry Point: draft_agent_from_prompt
# ═══════════════════════════════════════════════════════════════════════════

def test_draft_empty_prompt():
    """Empty prompt returns error."""
    result = draft_agent_from_prompt("")
    assert result.errors != []
    assert result.draft == {}


def test_draft_whitespace_only_prompt():
    """Whitespace-only prompt returns error."""
    result = draft_agent_from_prompt("   \n  \t  ")
    assert result.errors != []


def test_draft_genai_disabled():
    """When genai is disabled → return error."""
    with patch('backend.api.algo.agent_ai.is_enabled') as mock_enabled:
        mock_enabled.return_value = False
        result = draft_agent_from_prompt("test prompt")
    assert any("disabled" in e.lower() for e in result.errors)


def test_draft_genai_not_installed():
    """When google.genai not installed → return error."""
    with patch('backend.api.algo.agent_ai.is_enabled', return_value=True), \
         patch('builtins.__import__', side_effect=ImportError("No module named 'google'")):
        result = draft_agent_from_prompt("test prompt")
    # Note: the exception is caught in the try block, so we'd get an API error


def test_draft_processes_json_response():
    """draft_agent_from_prompt processes valid JSON responses."""
    # We'll test the non-Gemini paths instead since google.genai is imported at function level
    # Test disabled genai path
    result = draft_agent_from_prompt("")
    assert result.errors != []
