"""
Phase 2 of the order/agent grammar unification — shared order-field catalog.

Covers:
  1. order_fields.yaml structure (the 8 shared concepts + the CLI chase_levels
     mirror) loads correctly.
  2. backend/api/algo/grammar.py's `$ref` resolver merges catalog metadata
     into place_order's params_schema with the documented key order and
     local-override semantics.
  3. place_order's resolved params_schema is UNCHANGED from the pre-Phase-2
     shape for every field except the one new, explicitly-tested
     `chase_level` addition — this is the "except chase_level" parity test
     the Phase 2 plan calls for.
  4. `chase_level` threads into `_action_place_order`'s existing live-chase
     path (`_live_chase_config` via `chase_aggressiveness`), falling back
     correctly when absent.
  5. orders.yaml (the frontend CLI grammar — read-only here, via PyYAML;
     the real consumer is js-yaml in orders.js) still carries the exact
     literal `values:`/`chase_levels:` content `_wireTokens`/`_wireKwargs`
     depend on — a drift guard against order_fields.yaml, since the two
     files cannot be truly single-sourced without a frontend change
     (orders.yaml is loaded client-side via a Vite `?raw` import with no
     cross-file YAML include mechanism available).

Perf: no DB/network calls, pure in-memory YAML + dict comparisons.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from backend.api.algo import grammar

_ORDERS_YAML_PATH = (
    Path(__file__).resolve().parents[2] / "backend" / "config" / "grammars" / "orders.yaml"
)


def _load_orders_yaml() -> dict:
    with open(_ORDERS_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────
#  order_fields.yaml — catalog structure
# ─────────────────────────────────────────────────────────────────────────

class TestOrderFieldsCatalogStructure:
    def test_catalog_file_exists_and_loads(self):
        assert grammar._ORDER_FIELDS_YAML_PATH.exists()
        assert grammar._ORDER_FIELDS_YAML_PATH.name == "order_fields.yaml"
        assert isinstance(grammar._ORDER_FIELDS_CATALOG, dict)

    def test_catalog_has_all_eight_shared_fields(self):
        expected = {
            "qty", "order_type", "price", "trigger_price",
            "product", "variety", "tag", "chase_level",
        }
        assert expected <= set(grammar._ORDER_FIELDS.keys())

    @pytest.mark.parametrize("key,expected_type", [
        ("qty", "number"),
        ("order_type", "enum"),
        ("price", "number"),
        ("trigger_price", "number"),
        ("product", "enum"),
        ("variety", "enum"),
        ("tag", "string"),
        ("chase_level", "enum"),
    ])
    def test_field_types(self, key, expected_type):
        assert grammar._ORDER_FIELDS[key]["type"] == expected_type

    def test_order_type_enum_matches_place_order_pre_phase2_values(self):
        assert grammar._ORDER_FIELDS["order_type"]["enum"] == ["MARKET", "LIMIT", "SL", "SL-M"]

    def test_product_enum_matches_place_order_pre_phase2_values(self):
        assert grammar._ORDER_FIELDS["product"]["enum"] == ["MIS", "CNC", "NRML"]

    def test_variety_enum_matches_place_order_pre_phase2_values(self):
        assert grammar._ORDER_FIELDS["variety"]["enum"] == [
            "regular", "amo", "co", "iceberg", "auction",
        ]

    def test_chase_level_enum_matches_cli_values(self):
        assert grammar._ORDER_FIELDS["chase_level"]["enum"] == ["LOW", "MED", "HIGH"]

    def test_order_type_product_variety_carry_no_description(self):
        """These three fields had NO `description` key in place_order's
        params_schema before Phase 2. The catalog must not introduce one —
        doing so would silently add a new key to an already-shipped
        schema. (qty/price/trigger_price/tag DO carry descriptions,
        matching their pre-existing text exactly — see
        test_shared_fields_resolve_from_catalog below.)"""
        for key in ("order_type", "product", "variety"):
            assert "description" not in grammar._ORDER_FIELDS[key]

    def test_cli_chase_levels_mirror_present(self):
        mirror = grammar._ORDER_FIELDS_CATALOG.get("chase_levels_cli_reference")
        assert mirror == {
            "LOW": {"band_ticks": 8, "retry_seconds": 60},
            "MED": {"band_ticks": 4, "retry_seconds": 30},
            "HIGH": {"band_ticks": 1, "retry_seconds": 10},
        }


# ─────────────────────────────────────────────────────────────────────────
#  $ref resolver unit tests
# ─────────────────────────────────────────────────────────────────────────

class TestRefResolver:
    def test_non_ref_spec_passes_through_unchanged_identity(self):
        spec = {"type": "string", "required": True}
        assert grammar._resolve_param_spec(spec) is spec

    def test_non_dict_spec_passes_through(self):
        assert grammar._resolve_param_spec(None) is None

    def test_ref_spec_merges_catalog_and_local_overrides(self):
        resolved = grammar._resolve_param_spec({"$ref": "qty", "required": True, "token_ref_ok": True})
        assert resolved == {
            "type": "number",
            "required": True,
            "token_ref_ok": True,
            "description": "Number of lots × lot size. Must be positive.",
        }

    def test_ref_spec_key_order_matches_canonical_order(self):
        resolved = grammar._resolve_param_spec({"$ref": "qty", "required": True, "token_ref_ok": True})
        assert list(resolved.keys()) == ["type", "required", "token_ref_ok", "description"]

    def test_ref_spec_local_description_override_wins(self):
        resolved = grammar._resolve_param_spec({"$ref": "tag", "description": "overridden"})
        assert resolved["description"] == "overridden"

    def test_unknown_ref_raises_keyerror(self):
        with pytest.raises(KeyError):
            grammar._resolve_param_spec({"$ref": "does_not_exist"})

    def test_resolve_params_schema_none_passthrough(self):
        assert grammar._resolve_params_schema(None) is None
        assert grammar._resolve_params_schema({}) == {}


# ─────────────────────────────────────────────────────────────────────────
#  place_order's resolved params_schema — parity (all fields except
#  chase_level must be byte-for-byte identical to the pre-Phase-2 shape)
# ─────────────────────────────────────────────────────────────────────────

# Frozen inline copy of place_order's params_schema exactly as it existed
# immediately before Phase 2 (i.e. the Phase-1 baseline, before the
# chase_level addition and before any field moved to $ref). This is a
# STATIC fixture, not derived from the live module, so this test cannot
# silently degrade into comparing the module against itself.
_PRE_PHASE2_PLACE_ORDER_PARAMS_SCHEMA = {
    "account": {
        "type": "string", "required": True, "token_ref_ok": True,
        "description": "Masked account id to route the order to (e.g. ZG####).",
    },
    "symbol": {
        "type": "string", "required": True,
        "description": "Tradingsymbol, e.g. NIFTY26APR22500CE or RELIANCE.",
    },
    "exchange": {
        "type": "enum", "enum": ["NSE", "BSE", "NFO", "CDS", "MCX"],
        "required": False, "default": "NFO",
    },
    "side": {
        "type": "enum", "enum": ["BUY", "SELL"], "required": True,
        "description": "BUY opens long / covers short; SELL opens short / closes long.",
    },
    "qty": {
        "type": "number", "required": True, "token_ref_ok": True,
        "description": "Number of lots × lot size. Must be positive.",
    },
    "order_type": {
        "type": "enum", "enum": ["MARKET", "LIMIT", "SL", "SL-M"],
        "required": False, "default": "MARKET",
    },
    "price": {
        "type": "number", "required": False, "token_ref_ok": True,
        "description": "Required for LIMIT / SL.",
    },
    "trigger_price": {
        "type": "number", "required": False, "token_ref_ok": True,
        "description": "Required for SL / SL-M.",
    },
    "product": {
        "type": "enum", "enum": ["MIS", "CNC", "NRML"],
        "required": False, "default": "MIS",
    },
    "variety": {
        "type": "enum", "enum": ["regular", "amo", "co", "iceberg", "auction"],
        "required": False, "default": "regular",
    },
    "tag": {
        "type": "string", "required": False,
        "description": "Free-form tag propagated into the broker order id and AlgoOrder row.",
    },
    "template_id": {
        "type": "number", "required": False,
        "description": "OrderTemplate row id. Mutually exclusive with template_slug.",
    },
    "template_slug": {
        "type": "string", "required": False,
        "description": 'OrderTemplate stable slug (e.g. "default-bull", "default-short-vol", "none").',
    },
    "tp_pct_override": {
        "type": "number", "required": False,
        "description": "Per-action TP% override (e.g. 25.0 = +25%). Wins over template default.",
    },
    "sl_pct_override": {
        "type": "number", "required": False,
        "description": "Per-action SL% override (e.g. 15.0 = -15%). Wins over template default.",
    },
    "wing_premium_pct_override": {
        "type": "number", "required": False,
        "description": "Per-action wing premium % override (sell_option only).",
    },
    "wing_strike_offset_override": {
        "type": "number", "required": False,
        "description": "Per-action wing strike offset override (e.g. 500 → wing at +500 strike).",
    },
}


def _live_place_order_params_schema() -> dict:
    for tok in grammar.SYSTEM_TOKENS:
        if tok.get("grammar_kind") == "action" and tok.get("token") == "place_order":
            return tok["params_schema"]
    raise AssertionError("place_order token not found in SYSTEM_TOKENS")


class TestPlaceOrderParamsSchemaParity:
    def test_every_pre_phase2_field_unchanged_except_chase_level_is_new(self):
        live = _live_place_order_params_schema()
        live_minus_chase_level = {k: v for k, v in live.items() if k != "chase_level"}
        assert live_minus_chase_level == _PRE_PHASE2_PLACE_ORDER_PARAMS_SCHEMA

    def test_chase_level_is_the_only_new_field(self):
        live = _live_place_order_params_schema()
        added = set(live.keys()) - set(_PRE_PHASE2_PLACE_ORDER_PARAMS_SCHEMA.keys())
        assert added == {"chase_level"}

    def test_chase_level_schema_shape(self):
        live = _live_place_order_params_schema()
        assert live["chase_level"]["type"] == "enum"
        assert live["chase_level"]["enum"] == ["LOW", "MED", "HIGH"]
        assert live["chase_level"]["required"] is False
        assert "default" not in live["chase_level"]
        assert isinstance(live["chase_level"]["description"], str) and live["chase_level"]["description"]

    @pytest.mark.parametrize("field,ref_key", [
        ("qty", "qty"),
        ("order_type", "order_type"),
        ("price", "price"),
        ("trigger_price", "trigger_price"),
        ("product", "product"),
        ("variety", "variety"),
        ("tag", "tag"),
        ("chase_level", "chase_level"),
    ])
    def test_shared_fields_resolve_from_catalog(self, field, ref_key):
        """Each shared field's live type/enum traces back to
        order_fields.yaml — proving these are sourced from the catalog,
        not hand-duplicated."""
        live = _live_place_order_params_schema()[field]
        catalog = grammar._ORDER_FIELDS[ref_key]
        assert live["type"] == catalog["type"]
        if "enum" in catalog:
            assert live["enum"] == catalog["enum"]

    def test_agent_grammar_yaml_actually_uses_ref_for_shared_fields(self):
        """Guards against someone reverting agent_grammar.yaml back to
        hand-typed fields while leaving this test suite otherwise green —
        the raw (pre-resolution) catalog must still show `$ref` markers."""
        raw_schema = None
        for tok in grammar._GRAMMAR_CATALOG["system_tokens"]:
            if tok.get("grammar_kind") == "action" and tok.get("token") == "place_order":
                raw_schema = tok["params_schema"]
                break
        assert raw_schema is not None
        for field in ("qty", "order_type", "price", "trigger_price", "product", "variety", "tag", "chase_level"):
            assert raw_schema[field].get("$ref") == field


# ─────────────────────────────────────────────────────────────────────────
#  chase_level threading into the live chase path
# ─────────────────────────────────────────────────────────────────────────
#
# Real integration coverage of `_action_place_order`'s
# `params.get("chase_level") or params.get("chase_aggressiveness") or "med"`
# formula (chase_level=HIGH; chase_level winning when both keys are set)
# lives in backend/tests/test_actions.py, immediately after the
# pre-existing chase_aggressiveness integration tests — those tests call
# the real production function and assert on the real ChaseConfig it
# builds, reusing that file's `_make_broker_stub`/`_make_conns_stub`
# fixtures. A unit test here that re-typed the fallback formula as a
# standalone expression (rather than exercising `_action_place_order`
# itself) would pass even if the production code were reverted, so it is
# deliberately NOT duplicated in this file — see test_actions.py's
# `test_action_place_order_chase_level_high_threads_through` and
# `test_action_place_order_chase_level_wins_over_legacy_chase_aggressiveness`,
# plus the pre-existing `test_action_place_order_chase_aggressiveness_high_
# threads_through` and `test_action_place_order_no_aggressiveness_key_
# matches_prior_bare_chaseconfig` for the legacy-only and neither-key cases.


# ─────────────────────────────────────────────────────────────────────────
#  orders.yaml (frontend CLI grammar) — drift guard against the catalog
# ─────────────────────────────────────────────────────────────────────────
#
# orders.yaml is loaded CLIENT-SIDE ONLY via a Vite `?raw` import + js-yaml
# (frontend/src/lib/command/grammars/orders.js — not touched this phase).
# These tests parse the same file with PyYAML to prove its literal
# `values:`/`chase_levels:` content — the exact subset `_wireTokens`/
# `_wireKwargs` read — still matches both (a) order_fields.yaml's shared
# enums (set-equality; display ORDER is independently a CLI-side UX
# concern, not shared) and (b) its own pre-edit shape (field-for-field),
# so an operator/agent accidentally overwriting a `values:` list with
# something NOT present in the catalog gets caught here even though
# runtime enforcement of the catalog itself is impossible on the JS side
# without a frontend change.

class TestOrdersYamlDriftGuard:
    def test_orders_yaml_still_parses(self):
        doc = _load_orders_yaml()
        assert "verbs" in doc
        assert "buy" in doc["verbs"] and "sell" in doc["verbs"]

    @pytest.mark.parametrize("verb", ["buy", "sell"])
    def test_order_type_values_set_equal_to_catalog(self, verb):
        doc = _load_orders_yaml()
        tokens = {t["role"]: t for t in doc["verbs"][verb]["tokens"]}
        cli_values = set(tokens["orderType"]["values"])
        catalog_values = set(grammar._ORDER_FIELDS["order_type"]["enum"])
        assert cli_values == catalog_values, (
            "orders.yaml orderType values drifted from order_fields.yaml "
            "order_type enum — update one to match the other"
        )

    @pytest.mark.parametrize("verb", ["buy", "sell"])
    def test_chase_values_set_equal_to_catalog(self, verb):
        doc = _load_orders_yaml()
        tokens = {t["role"]: t for t in doc["verbs"][verb]["tokens"]}
        cli_values = set(tokens["chase"]["values"])
        catalog_values = set(grammar._ORDER_FIELDS["chase_level"]["enum"])
        assert cli_values == catalog_values

    def test_modify_kwarg_chase_values_set_equal_to_catalog(self):
        doc = _load_orders_yaml()
        cli_values = set(doc["verbs"]["modify"]["kwargs"]["chase"]["values"])
        catalog_values = set(grammar._ORDER_FIELDS["chase_level"]["enum"])
        assert cli_values == catalog_values

    @pytest.mark.parametrize("verb", ["buy", "sell"])
    def test_product_kwarg_values_set_equal_to_catalog(self, verb):
        doc = _load_orders_yaml()
        cli_values = set(doc["verbs"][verb]["kwargs"]["product"]["values"])
        catalog_values = set(grammar._ORDER_FIELDS["product"]["enum"])
        assert cli_values == catalog_values

    def test_chase_levels_block_byte_identical_to_catalog_mirror(self):
        """orders.yaml's own `chase_levels:` (band_ticks/retry_seconds) —
        a DIFFERENT config from the live chase engine's chase_level tiers
        — must stay byte-identical to order_fields.yaml's
        `chase_levels_cli_reference` mirror, since the JS loader can't
        actually read the shared file."""
        doc = _load_orders_yaml()
        assert doc["chase_levels"] == grammar._ORDER_FIELDS_CATALOG["chase_levels_cli_reference"]


# Frozen inline projection of orders.yaml's wired shape (the exact subset
# `_wireTokens`/`_wireKwargs` in frontend/src/lib/command/grammars/orders.js
# read: role, kind, values, required, parse, hint for tokens; values,
# kind, parse, hint for kwargs) captured from the file as it existed
# immediately before this phase's `# catalog:` comment annotations were
# added. Comments are not data — PyYAML and js-yaml both ignore them — so
# this is expected to still match exactly after the edit; this test is the
# regression guard that proves it, independent of running node/js-yaml.
_PRE_PHASE2_ORDERS_YAML_BUY_SELL_TOKENS = [
    {"role": "account", "kind": "account", "required": True},
    {"role": "instType", "values": ["CALL", "PUT", "FUT", "EQ"], "required": False,
     "parse": "upper", "hint": "CALL | PUT | FUT | EQ (default: EQ)"},
    {"role": "symbol", "kind": "symbol", "required": True, "hint": "underlying — type 3+ chars"},
    {"role": "strike", "kind": "strike", "required": "if:instType==CALL|PUT", "parse": "float",
     "hint": "strike price (* = wide spread)"},
    {"role": "expiry", "kind": "expiry", "required": "if:instType==CALL|PUT|FUT", "hint": "expiry date"},
    {"role": "qty", "kind": "qty", "required": True, "parse": "int",
     "hint": "quantity (lots × lot_size for F&O)"},
    {"role": "orderType", "values": ["LIMIT", "SL", "SL-M", "MARKET"], "required": True,
     "parse": "upper", "hint": "LIMIT | SL | SL-M | MARKET"},
    {"role": "price", "kind": "price", "required": "if:orderType!=MARKET", "parse": "float",
     "hint": "limit/trigger price"},
    {"role": "chase", "values": ["LOW", "MED", "HIGH"], "required": False, "parse": "upper",
     "hint": "chase aggressiveness (non-MARKET only)"},
]
_PRE_PHASE2_ORDERS_YAML_PRODUCT_KWARG = {"values": ["MIS", "NRML", "CNC"]}
_PRE_PHASE2_ORDERS_YAML_MODIFY_KWARGS = {
    "price": {"parse": "float"},
    "qty": {"parse": "int"},
    "chase": {"values": ["LOW", "MED", "HIGH"], "parse": "upper"},
}


def _project_token(spec: dict) -> dict:
    """Mirror `_wireTokens`'s field selection in orders.js — the exact
    keys that function reads off each token spec."""
    out = {"role": spec["role"], "required": spec["required"]}
    if "kind" in spec:
        out["kind"] = spec["kind"]
    if "values" in spec:
        out["values"] = spec["values"]
    if "parse" in spec:
        out["parse"] = spec["parse"]
    if "hint" in spec:
        out["hint"] = spec["hint"]
    return out


def _project_kwarg(spec: dict) -> dict:
    """Mirror `_wireKwargs`'s field selection in orders.js."""
    out = {}
    if "values" in spec:
        out["values"] = spec["values"]
    if "kind" in spec:
        out["kind"] = spec["kind"]
    if "parse" in spec:
        out["parse"] = spec["parse"]
    if "hint" in spec:
        out["hint"] = spec["hint"]
    return out


class TestOrdersYamlWiredShapeUnchanged:
    """Proves the `# catalog:` comment annotations added to orders.yaml
    this phase are purely cosmetic from the JS loader's point of view —
    `_wireTokens`'s field selection produces the exact same projection
    before and after."""

    @pytest.mark.parametrize("verb", ["buy", "sell"])
    def test_buy_sell_tokens_projection_unchanged(self, verb):
        doc = _load_orders_yaml()
        live_projection = [_project_token(t) for t in doc["verbs"][verb]["tokens"]]
        assert live_projection == _PRE_PHASE2_ORDERS_YAML_BUY_SELL_TOKENS

    @pytest.mark.parametrize("verb", ["buy", "sell"])
    def test_product_kwarg_projection_unchanged(self, verb):
        doc = _load_orders_yaml()
        kwarg = doc["verbs"][verb]["kwargs"]["product"]
        projected = {"values": kwarg["values"]}
        assert projected == _PRE_PHASE2_ORDERS_YAML_PRODUCT_KWARG

    def test_modify_kwargs_projection_unchanged(self):
        """The `modify` verb's kwargs (price/qty/chase) also got a
        `# catalog:` annotation on `price`/`qty`/`chase` this phase — the
        node/js-yaml spot-check proved it unchanged at edit time, but only
        buy/sell + product were covered by a committed fixture. This closes
        that gap."""
        doc = _load_orders_yaml()
        kwargs = doc["verbs"]["modify"]["kwargs"]
        projected = {name: _project_kwarg(spec) for name, spec in kwargs.items()}
        assert projected == _PRE_PHASE2_ORDERS_YAML_MODIFY_KWARGS
