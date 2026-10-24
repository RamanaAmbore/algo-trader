"""
Byte-for-byte equivalence regression for the SYSTEM_TOKENS / LOG_TAG_TOKENS
externalization (Phase 1 of the order/agent grammar unification).

`backend/api/algo/grammar.py` used to hold SYSTEM_TOKENS / LOG_TAG_TOKENS as
Python list literals. They now load from
`backend/config/grammars/agent_grammar.yaml` at import time and get
reconstructed into the identical list-of-dicts shape. This is meant to be
PURE DATA RELOCATION — zero behavior change.

`backend/tests/golden/agent_grammar_baseline.json` is a frozen snapshot of
SYSTEM_TOKENS (post `.extend(LOG_TAG_TOKENS)`, i.e. the full list the app
actually runs with — 92 entries at the moment of the original YAML
externalization, 100 as of the Sprint 6 `ntfy` amendment below) and
LOG_TAG_TOKENS, captured from the Python literal exactly as it existed
immediately before the YAML externalization, then hand-amended in lockstep
with every later documented, intentional catalog change. It is a static
fixture — never regenerated from the live module — so this test cannot
silently degrade into a no-op comparing the module against itself after
the refactor is committed.

Amendment (2026-10-08): the 7 dead action-stub `resolver` fields
(place_order/modify_order/cancel_order/cancel_all_orders/
chase_close_positions/expiry_auto_close/close_position) were set to
`null` (not deleted — an absent key would be preserved as the STALE old
dotted-path by `seed_grammar_tokens()`'s upsert on an already-seeded DB,
since it only overwrites a key the spec still carries) in both
`agent_grammar.yaml` and this baseline fixture when those stub functions
were deleted from `actions.py` — see CLAUDE.md's "Agent action-dispatch
SSOT" note. The baseline is no longer byte-identical to the
pre-YAML-externalization Python literal for those 7 entries; it now
reflects the current, intentional post-dead-code-removal shape.

Amendment (Sprint 6, agent CLI grammar): a new `(notify, channel, ntfy)`
token was added to `agent_grammar.yaml` (and mirrored here) — `ntfy`
already dispatches in `events.py`/`event_agents.py` but was missing from
the authorable catalog. Inserted at the same list position as the YAML
(right after `email`, before `websocket`) so `test_system_tokens_byte_
for_byte_equal_to_baseline` keeps passing with a baseline that reflects
this intentional, documented addition rather than a real regression.

Amendment (Sprint 18, event-agent decompiler gaps): a channel-level
`gate` boolean was added to `telegram`/`email`/`ntfy`'s `params_schema`
in `agent_grammar.yaml` (and mirrored here) — the field already worked
functionally (`event_agents.py:_send_channel`, line ~345) but had no
catalog declaration, which is the one thing blocking 8 of 15 real
seeded event agents (see `event_agents.py:SEEDED_AGENTS`) from
round-tripping through the frontend's `event_stmt` CLI decompiler. See
`test_grammar_event_action_gate_field.py` for the dedicated coverage.

Amendment (Sprint 21, orders.* + quote.* scope families): two new
`(condition, scope, ...)` tokens — `orders.any_acct` and `quote.tracked`
— were added to `agent_grammar.yaml` right after `watchlist.markets`
(and mirrored here at the same position) — see CLAUDE.md's Sprint 21
notes / `backend/api/algo/grammar.py`'s `_scope_orders_any_acct` /
`_scope_quote_tracked`.

Amendment (Sprint G, symbols.* scope family): one new
`(condition, scope, symbols.all)` token was added to `agent_grammar.yaml`
right after `quote.tracked` (and mirrored here at the same position) —
unifies watchlist/pinned/gainers/losers behind one scope token + a
required `scope_filter.kind` qualifier. See
`backend/api/algo/grammar.py`'s `_scope_symbols_all` and
`backend/tests/test_symbols_scope_family.py`.

Amendment (2026-10-12, telegram_info channel): one new
`(notify, channel, telegram_info)` token was added to `agent_grammar.yaml`
right after `telegram` (and mirrored here at the same position) — closes
the real seeded `market-summary` agent's Edit/decompile failure ("not
declared in its catalog schema"); see
`test_grammar_event_action_gate_field.py`'s own dedicated coverage.

Amendment (2026-10, order/place_order unification Phase A): a new
`(action, action_type, order)` token was added to `agent_grammar.yaml`
right after `place_order` (and mirrored here at the same position,
index 85) — byte-identical in shape to `place_order` (same resolver/
params_schema/mutually_exclusive_groups), since both names dispatch to
the exact same `_action_place_order` handler. See CLAUDE.md's "order/
place_order unification" note and `backend/tests/test_order_place_order_
unification.py` for the dedicated dispatch-equivalence + backfill-
migration coverage.

Amendment (2026-10, action-dispatch drift validator): a new
`(action, action_type, send_summary)` token was added to
`agent_grammar.yaml` right after `emit_log` (and mirrored here at the
same position, index 96 at the time) — `send_summary` already had a fully-wired,
tested dispatch handler in `_NOOP_ACTION_HANDLERS`
(`actions_live._action_send_summary`) but no catalog row at all, so
`is_known_action_type()` rejected it and the Lab-chat agent-builder
could never offer it. Caught by the new
`validate_action_dispatch_catalog()` check (`grammar_registry.py`) added
the same round, which cross-references the catalog against both
dispatch dicts and would otherwise have let this exact "dead/orphaned
handler" drift class — the same one CLAUDE.md's "7 dead grammar-resolver
stub functions" incident describes in the opposite direction — go
uncaught indefinitely. See `backend/tests/test_grammar_registry.py`.

Amendment (Item 3, 2026-10-22, pre-open LTP pinning + daily draft sweep +
market-status-report plan): a new `(action, action_type,
send_market_status)` token was added to `agent_grammar.yaml` (and
mirrored here) right after `send_summary` — index 97, pushing the first
log tag (`orders`) to 98. Mirrors `send_summary`'s own wiring exactly: no
`ACTION_DISPATCH_CATALOG_ALLOWLIST` entry needed (a real catalog row +
dispatch handler exist from the start), registered in
`_NOOP_ACTION_HANDLERS` (`actions.py`) rather than `_LIVE_ACTION_HANDLERS`
— it only sends alerts, no broker call, same category as `send_summary`
itself. Backs the new `market-status-report` built-in agent
(`agent_engine.py`), which fires at 05:35 IST daily reporting NSE/MCX
open-or-closed status. See `backend/tests/test_market_status_alert.py`
for the handler's own dedicated coverage.

Amendment (2026-10-22, market-cycle event tokens): ten new
`(condition, metric, ...)` tokens were added to `agent_grammar.yaml` right
after `minutes_until_close` (and mirrored here at the same position) —
`is_mcx_open`, `is_nse_open`, `minutes_since_mcx_open`,
`minutes_since_nse_open`, `minutes_until_mcx_close`,
`minutes_until_nse_close`, `minutes_until_mcx_open`,
`minutes_until_nse_open`, `is_past_holiday_refresh`,
`is_past_window_open`. First real condition-tree consumers of
`agent_engine._ae_segment_flags()`'s per-segment computed dict, which
previously had zero grammar-token readers (see CLAUDE.md's "Segment
flags honour overrides" note). Resolvers live in `grammar.py`, reading
`ctx.segment_flags` — a new `Context` field (`agent_evaluator.py`)
threaded from `run_cycle`'s `context["segment_flags"]`. No existing
built-in agent (`market-open-nse`/`market-preclose-mcx`/
`market-status-report`) was touched — those stay fixed-clock
`fire_at_time` sentinels; these tokens are purely additive for a future
condition-based agent. See `backend/tests/test_market_cycle_tokens.py`.
Same-day correction: `is_past_window_open`'s description originally said
"0.0 during the closed overnight window," which read as if the value
drops at 23:31 IST — the code (and its own test,
`TestIsPastWindowOpen::test_late_evening_same_session_is_still_true`)
intentionally stays 1.0 through 23:59 IST, only reading 0.0 during
00:00-07:59 IST (it answers "has today's session begun," not "is the
window live right now"). Description corrected to say so explicitly.

Amendment (theoretical-vs-market loss divergence, per underlying): three
new `(condition, metric, ...)` tokens — `underlying_market_loss`,
`underlying_theoretical_loss`, `underlying_loss_divergence` — were added
to `agent_grammar.yaml` right after `is_past_window_open` (and mirrored
here at the same position), plus one new `(condition, scope,
positions.any_underlying)` token right after `positions.any_acct` (and
mirrored here at the same position). Gates a rate-of-change loss alert on
how far today's real market P&L has diverged from a frozen-vol Greeks/
IV-implied reprice, per underlying rather than per account. Backs the new
`loss-positions-underlying-divergence` built-in agent (`agent_engine.py`).
Resolvers live in `grammar.py`, reading `ctx.position_rows` /
`ctx.spot_prices` / `ctx.alert_state['underlying_theo_baseline']` (the
last populated once per tick by `agent_engine._update_underlying_theo_
baseline()`, a sibling of `_update_pnl_history`). See
`backend/tests/test_underlying_loss_divergence_tokens.py`.

Amendment (2026-10-22, underlying_market_loss reference-point fix): the
`underlying_market_loss` token's `description` (and the identical text
baked into `agent_grammar_baseline.json`) was corrected in place — no
new token, no position change. Original text said "Sum of day_change_val
(the broker's own day P&L)," i.e. raw day_change_val measured from
prev_close. `underlying_theoretical_loss` measures from a DIFFERENT
baseline (each leg's own LTP/spot/IV frozen the first tick seen this
session). A pure overnight gap move already reflected in day_change_val
by the time the theo baseline was captured read as a large false
"divergence" with zero real volatility surprise, holding the gate open
all session for the wrong reason — flagged before activating the new
`loss-positions-underlying-divergence` agent. Fixed: `_metric_underlying_
market_loss` (`grammar.py`) now sums (day_change_val now − day_change_val
at baseline capture) per leg, using a new `dcv_base` field
`agent_engine._capture_leg_theo_baseline`/`_capture_opt_leg_baseline` now
stamp onto each baseline entry at the SAME moment the rest of that leg's
baseline is captured. A leg with no baseline yet doesn't contribute
(skipped, not coerced to 0); the metric returns None only when NO leg of
the underlying has a baseline. `underlying_loss_divergence` needed no
change — it only subtracts whatever the two metric functions return.
`loss-positions-underlying-divergence` flips from `status="inactive"` to
active as part of this fix. See
`backend/tests/test_underlying_loss_divergence_tokens.py` and
`backend/tests/test_loss_agents.py` for the activation-state assertion
move.

Amendment (2026-10-23, `emit_log` -> `log` rename + `help` action): the
`(action, action_type, emit_log)` token was renamed to `(action,
action_type, log)` in place (same list position, same resolver dotted
path `backend.api.algo.actions.emit_log` — the Python function itself
keeps its original name; only the DSL-visible token string changed).
Confirmed via live DB check on both prod and dev: zero existing saved
`Agent` rows referenced `emit_log` in their compiled JSON, so this is a
clean rename with no migration/alias needed. Its `params_schema` grew a
new optional `priority` field (`low`/`default`/`high`/`urgent`, reusing
`alert_utils._TIER_TO_NTFY_PRIORITY`'s own vocabulary) and its `level`
enum grew `critical`. A new `(action, action_type, help)` token was
inserted immediately BEFORE it (right after `set_flag`, mirrored here
at the same position) — read-only catalog introspection, resolver
`backend.api.algo.actions.help_lookup` (named to avoid colliding with
the `help` builtin), logged at DEBUG only. Both changes are mirrored in
`agent_grammar_baseline.json` at the exact same list position so
`test_system_tokens_byte_for_byte_equal_to_baseline` stays a real,
order-sensitive equality check rather than silently degrading. See
`backend/tests/test_help_lookup_action.py` and CLAUDE.md's "Agent
action-dispatch SSOT" note.

Amendment (2026-10, alert-routing-tree plan): the `(action, action_type,
log)` token's `params_schema` grew two new optional fields, `name`
(string, `token_ref_ok: true`) and `tags` (array), inserted right after
the existing `priority` field — same position in both
`agent_grammar.yaml` and this baseline. Both thread into the log
record's `extra` dict exactly like `priority` already does (see
`actions.py:emit_log`'s own docstring); neither is consulted by any
current `alert_routing.py` rule — forward-looking plumbing for a future
per-record matcher, not a behavior change for any existing caller. See
`backend/tests/test_alert_routing.py` and `test_actions_coverage.py`'s
`test_emit_log_name_and_tags_threaded_into_extra_and_result`.

Amendment (2026-10, user-registration alert): one new `(log, tag, auth)`
token was added to `agent_grammar.yaml`'s `log_tags.tags` (and mirrored
here, appended at the end of both `system_tokens_full` and `log_tags`,
matching `LOG_TAG_TOKENS.extend()`'s own append order) — closes the gap
where `routes/auth.py`'s new-registration log line had no alert-tag
vocabulary member to match against, so no event agent could ever see
it. See `backend/tests/test_event_agents.py`'s `USER_REGISTERED_AGENT`
coverage and CLAUDE.md's routing-tree plan note.

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
