"""
Byte-for-byte equivalence regression for the SYSTEM_TOKENS / LOG_TAG_TOKENS
externalization (Phase 1 of the order/agent grammar unification).

`backend/api/algo/grammar.py` used to hold SYSTEM_TOKENS / LOG_TAG_TOKENS as
Python list literals. They now load from
`backend/config/grammars/agent_grammar.yaml` at import time and get
reconstructed into the identical list-of-dicts shape. This is meant to be
PURE DATA RELOCATION — zero behavior change.

`backend/tests/golden/agent_grammar_baseline.json` is a frozen snapshot of
SYSTEM_TOKENS (post `.extend(LOG_TAG_TOKENS)`, i.e. the full 92-entry list
the app actually runs with) and LOG_TAG_TOKENS, captured from the Python
literal exactly as it existed immediately before the YAML externalization.
It is a static fixture — never regenerated from the live module — so this
test cannot silently degrade into a no-op comparing the module against
itself after the refactor is committed.

Perf: no DB / network calls, pure in-memory comparison.
Reuse: shares the same SYSTEM_TOKENS / LOG_TAG_TOKENS module attributes
every other grammar test and `seed_grammar_tokens()` read.
UX/SSOT: guards the ONE catalog every resolver-dispatch consumer
(`seed_grammar_tokens()`, `GrammarRegistry.reload()` via the `grammar_tokens`
DB table, `event_agents.py`'s LOG_TAG_TOKENS read) ultimately derives from.
"""

import json
from pathlib import Path

from backend.api.algo import grammar

_BASELINE_PATH = Path(__file__).resolve().parent / "golden" / "agent_grammar_baseline.json"


def _load_baseline() -> dict:
    with open(_BASELINE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TestAgentGrammarYamlCatalogEquivalence:
    """SYSTEM_TOKENS / LOG_TAG_TOKENS loaded from YAML must exactly match
    the pre-refactor Python-literal baseline — same tokens, same resolver
    dotted-paths, same params_schema, same descriptions, same order."""

    def test_system_tokens_byte_for_byte_equal_to_baseline(self):
        baseline = _load_baseline()
        assert grammar.SYSTEM_TOKENS == baseline["system_tokens_full"], (
            "SYSTEM_TOKENS loaded from agent_grammar.yaml no longer matches "
            "the frozen pre-refactor baseline — this must be pure data "
            "relocation with zero behavior change. Diff the two structures "
            "to find the discrepancy."
        )

    def test_log_tag_tokens_byte_for_byte_equal_to_baseline(self):
        baseline = _load_baseline()
        assert grammar.LOG_TAG_TOKENS == baseline["log_tags"], (
            "LOG_TAG_TOKENS loaded from agent_grammar.yaml no longer matches "
            "the frozen pre-refactor baseline."
        )

    def test_system_tokens_json_serialization_literally_byte_identical(self):
        """`==` treats `10` and `10.0` as equal, `True` and `1` as equal, and
        ignores dict key order — none of those would show up as a
        regression under plain `==` but WOULD be a real, observable change
        to anything that serializes this catalog (e.g. an admin API JSON
        response). Comparing the `json.dumps` text closes that gap and
        makes "byte-for-byte" literally true, including key order and
        value types."""
        baseline = _load_baseline()
        live_json = json.dumps(grammar.SYSTEM_TOKENS, ensure_ascii=False)
        baseline_json = json.dumps(baseline["system_tokens_full"], ensure_ascii=False)
        assert live_json == baseline_json

    def test_log_tag_tokens_json_serialization_literally_byte_identical(self):
        baseline = _load_baseline()
        live_json = json.dumps(grammar.LOG_TAG_TOKENS, ensure_ascii=False)
        baseline_json = json.dumps(baseline["log_tags"], ensure_ascii=False)
        assert live_json == baseline_json

    def test_log_tag_tokens_are_a_suffix_of_system_tokens(self):
        """Mirrors the original module's `SYSTEM_TOKENS.extend(LOG_TAG_TOKENS)`
        — LOG_TAG_TOKENS entries must also be present, in the same order, at
        the tail of SYSTEM_TOKENS."""
        n = len(grammar.LOG_TAG_TOKENS)
        assert grammar.SYSTEM_TOKENS[-n:] == grammar.LOG_TAG_TOKENS

    def test_token_identity_set_matches_baseline(self):
        """Same set of (grammar_kind, token_kind, token) triples — the key
        seed_grammar_tokens() dedupes system rows on."""
        baseline = _load_baseline()
        baseline_keys = {
            (t["grammar_kind"], t["token_kind"], t["token"])
            for t in baseline["system_tokens_full"]
        }
        live_keys = {
            (t["grammar_kind"], t["token_kind"], t["token"])
            for t in grammar.SYSTEM_TOKENS
        }
        assert live_keys == baseline_keys

    def test_resolver_dotted_paths_match_baseline(self):
        """Resolver dotted-path strings (unchanged function homes in
        grammar.py / actions.py) must match the baseline exactly."""
        baseline = _load_baseline()
        baseline_resolvers = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("resolver")
            for t in baseline["system_tokens_full"]
        }
        live_resolvers = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("resolver")
            for t in grammar.SYSTEM_TOKENS
        }
        assert live_resolvers == baseline_resolvers

    def test_params_schema_matches_baseline(self):
        baseline = _load_baseline()
        baseline_schemas = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("params_schema")
            for t in baseline["system_tokens_full"]
        }
        live_schemas = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("params_schema")
            for t in grammar.SYSTEM_TOKENS
        }
        assert live_schemas == baseline_schemas

    def test_descriptions_match_baseline(self):
        baseline = _load_baseline()
        baseline_desc = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("description")
            for t in baseline["system_tokens_full"]
        }
        live_desc = {
            (t["grammar_kind"], t["token_kind"], t["token"]): t.get("description")
            for t in grammar.SYSTEM_TOKENS
        }
        assert live_desc == baseline_desc

    def test_resolver_functions_still_importable(self):
        """The `resolver` column only stores a dotted-path STRING — this
        confirms every resolver still resolves to a real, callable Python
        function living exactly where it did before (grammar.py / actions.py),
        proving the YAML move did not touch resolver function bodies."""
        import importlib

        checked = 0
        for tok in grammar.SYSTEM_TOKENS:
            resolver = tok.get("resolver")
            if not resolver:
                continue
            module_path, _, attr = resolver.rpartition(".")
            mod = importlib.import_module(module_path)
            fn = getattr(mod, attr)
            assert callable(fn), f"resolver {resolver} is not callable"
            checked += 1
        assert checked > 0

    def test_no_non_yaml_serializable_values_in_catalog(self):
        """Every value in SYSTEM_TOKENS must be a plain YAML-safe type
        (str, int, float, bool, None, list, dict) — i.e. nothing like a
        lambda or a non-primitive object leaked into the catalog metadata.
        Resolver function bodies are referenced by dotted-path STRING only,
        never embedded directly."""

        def _check(value):
            if isinstance(value, dict):
                for v in value.values():
                    _check(v)
            elif isinstance(value, list):
                for v in value:
                    _check(v)
            elif value is None or isinstance(value, (str, int, float, bool)):
                pass
            else:
                raise AssertionError(f"non-YAML-serializable value found: {type(value)} = {value!r}")

        for tok in grammar.SYSTEM_TOKENS:
            _check(tok)


class TestAgentGrammarYamlFileLoading:
    """The YAML file itself is the single source of truth — verify the
    loader path and that the catalog round-trips through it correctly."""

    def test_yaml_path_exists_and_is_used(self):
        assert grammar._GRAMMAR_YAML_PATH.exists()
        assert grammar._GRAMMAR_YAML_PATH.name == "agent_grammar.yaml"

    def test_system_tokens_is_a_list_copy_not_the_same_object_as_catalog(self):
        """SYSTEM_TOKENS must be a distinct list object from
        _GRAMMAR_CATALOG['system_tokens'] so `.extend(LOG_TAG_TOKENS)`
        doesn't mutate the raw loaded YAML structure."""
        assert grammar.SYSTEM_TOKENS is not grammar._GRAMMAR_CATALOG["system_tokens"]

    def test_reloading_yaml_produces_identical_catalog(self):
        """Calling the loader function again (simulating a fresh import)
        produces byte-identical data to what the module already loaded."""
        reloaded = grammar._load_grammar_catalog()
        assert reloaded["system_tokens"] == grammar._GRAMMAR_CATALOG["system_tokens"]
        assert reloaded["log_tags"] == grammar._GRAMMAR_CATALOG["log_tags"]
