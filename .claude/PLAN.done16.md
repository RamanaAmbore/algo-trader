# Plan: Agent CLI grammar — Sprint 1 (backend foundations)

## Task

First of five sprints building a CLI/text grammar for authoring agents (full design
in `~/.claude/plans/purrfect-marinating-pixel.md` — operator-negotiated EBNF,
operator table, type system, sprint breakdown; Sprints 2-5 are frontend-heavy and
come later). This sprint is backend-only, independent of the frontend work, and
closes a real, pre-existing gap discovered during design:

1. **Close a save-time validation gap**: `create_agent`/`update_agent` in
   `backend/api/routes/agents.py` already validate the `conditions` tree (via
   `_age_validate_threshold_conditions()`, shipped in the prior "Sprint 3" of the
   expression-operators effort) but never validate the `actions` array at all for
   `kind='cycle'` agents — a typo'd action type or a missing required param
   currently saves with 200/201 and silently never fires correctly. Add a sibling
   validator, `_age_validate_action_entries()`.
2. **Add `Agent.cli_source`**, a nullable text column to round-trip what an
   operator typed in the (future, Sprint 4) CLI, for display/audit only — never
   read by `run_cycle()`/`execute()`, which continue reading only the normalized
   `conditions`/`events`/`actions` JSON.
3. **Extend `expr_eval.py`** (the whitelisted AST expression evaluator used for
   `token_ref_ok` action params, e.g. `qty=base_lots*2`) to also support **string**
   results and `+` for string concatenation (e.g. a future
   `tag="order_" + account`), in addition to its current number/boolean-only
   results. This is real, scoped new capability for an upcoming CLI feature — not
   speculative — but must be done with the same rigor the file already applies
   (it sits directly upstream of real order placement): add an explicit
   max-string-length guard alongside the existing length/depth guards, don't just
   loosen the type check.

## Agents

- backend: |
    Implement all three items below in `backend/api/routes/agents.py`,
    `backend/api/models.py`, and `backend/api/algo/expr_eval.py`.

    **1. `_age_validate_action_entries()` in `agents.py`** — mirror the existing
    `_age_validate_threshold_conditions()` function (find it in the same file —
    it validates `conditions` via `agent_evaluator.validate()` and
    `agent_engine.is_grammar_tree()`, gated so an empty/non-grammar-tree value is
    left alone) exactly in spirit, but for the `actions` array instead:
    - For each entry in `actions` (a JSON list of `{"type": "<action_type>",
      "params": {...}}` dicts): look up `action_type` via
      `backend.api.algo.grammar.get_action_params_schema(action_type)`. If it
      returns empty/`None`, the action type is unknown — collect an error
      `f"unknown action type '{action_type}'"`.
    - If the schema resolves, check every key in the schema whose spec has
      `required: True` is present as a key in that entry's `params` dict (not
      just non-None — presence). Collect an error per missing required key, e.g.
      `f"{action_type}: missing required param '{key}'"`.
    - If any errors were collected across all entries, raise
      `HTTPException(status_code=422, detail="; ".join(errors), extra={"errors": errors})`
      — follow the EXACT same raise pattern
      `_age_validate_threshold_conditions()`/`_age_validate_event_spec()` already
      use in this file.
    - An empty `actions` list (`[]`) is valid — skip entirely, no errors.
    - Wire this into `create_agent` (call on `data.actions`, unconditionally,
      alongside the existing `_age_validate_threshold_conditions(data.conditions)`
      call) and into `update_agent` (call on the MERGED `agent.actions` — i.e.
      AFTER the per-field copy loop has applied any supplied changes — at the
      exact point the existing conditions-validation and event-spec-validation
      calls already run post-merge; read that existing code closely to match the
      pattern, since merged-state re-validation is already an established
      invariant in this function for exactly the same reason: a save that only
      touches an unrelated field must still re-check previously-saved content).

    **2. `Agent.cli_source` column** — in `backend/api/models.py`, add
    `cli_source: Mapped[str | None] = mapped_column(Text, nullable=True)` to the
    `Agent` model (match whatever import/column style the neighboring columns on
    that model already use — e.g. how `description`/`long_name` are declared).
    Generate the Alembic migration for this one additive, nullable column (check
    `backend/alembic/` or wherever this project's migrations live — look at the
    most recent migration file for the exact revision-chaining convention before
    writing a new one; this must be a pure additive column, no data migration, no
    changes to any other table).

    **3. `expr_eval.py` string + `+` support** — read the whole file first; it's
    a small, carefully-scoped whitelist AST evaluator (currently returns only
    `int | float | bool`, raises only `ExprError`, with explicit guards:
    `_MAX_EXPR_LEN`, `_check_paren_depth`, `_MAX_DEPTH`, Pow magnitude guards,
    etc. — read every guard, this file is deliberately narrow and sits directly
    upstream of real order placement). Add:
    - `ast.Constant` handling: currently only accepts int/float/bool constants —
      extend to also accept `str` constants, with a new length guard (e.g.
      `_MAX_STRING_LEN`, pick a sane bound like 200 chars, matching the spirit of
      `_MAX_EXPR_LEN`) raising `ExprError` if a string constant exceeds it.
    - `ast.BinOp` with `Add`: currently only numeric addition — extend so that
      if EITHER operand is a string, perform string concatenation instead
      (`str(left) + str(right)` is wrong — only allow `str + str`, reject
      `str + number` explicitly as a type error, i.e. both operands must
      already be strings for the Add-as-concat path; if both are numeric, keep
      existing numeric-add behavior unchanged). Every other `BinOp` operator
      (`Sub`/`Mult`/`Div`/`Mod`/`Pow`) stays numeric-only — reject a string
      operand there with `ExprError`, don't silently coerce.
    - Update the function's return-type annotation/docstring to
      `int | float | bool | str`.
    - Do NOT touch `grammar_registry.py`'s separate call-syntax resolver — that
      module's docstring explicitly states it is a distinct trust boundary from
      this file and the two must never be merged; this task only touches
      `expr_eval.py`.

    **Test requirement (mandatory)**: for every change above, write or update a
    pytest test covering it:
    - `backend/tests/test_agent_kind_tier_topic.py` (or create a new test file if
      you judge that cleaner — but this file already has the exact mock-session +
      handler-calling pattern for `create_agent`/`update_agent`, reuse it): tests
      for `_age_validate_action_entries()` — unknown action type rejected 422,
      missing required param rejected 422, valid actions list saves fine, empty
      `[]` actions list saves fine, update_agent re-validates merged actions when
      untouched by the current request (mirroring the existing conditions
      merged-revalidation tests in that same file).
    - `backend/tests/test_expr_eval.py`: tests for string constants (within and
      exceeding the new length guard), string `+` string concatenation, string +
      number rejected as a type error, existing numeric/boolean behavior
      unchanged (re-run existing tests to confirm, don't just trust it).
    - A migration test or at minimum confirm `alembic upgrade head` /
      `alembic check` (whatever this repo's convention is — check for an existing
      migration test file first) passes cleanly with the new column.

## Tests
- pytest: yes
- svelte-check: no (this sprint touches no frontend files)
- playwright: no

## Commit message
fix(agents): close actions-array save-time validation gap + Agent.cli_source column + expr_eval string/+ support

Sprint 1 of the agent CLI grammar effort (full design in
~/.claude/plans/purrfect-marinating-pixel.md). Adds
_age_validate_action_entries() mirroring the existing conditions validator —
actions arrays were completely unchecked at save time for kind='cycle' agents
before this. Adds Agent.cli_source (nullable, audit-only, never read by
run_cycle/execute) for the upcoming CLI's edit flow. Extends expr_eval.py's
whitelisted evaluator to support string results and + for concatenation,
scoped to token_ref_ok action params, with its own length guard.

## Done when
- `_age_validate_action_entries()` exists, is wired into both `create_agent` and
  `update_agent` (merged-state re-validation on update), and rejects an unknown
  action type or a missing required param with 422 — for every agent kind, not
  just `cycle`.
- `Agent.cli_source` column exists via a clean additive migration; nothing reads
  it except whatever will display it later (Sprint 4, not this sprint).
- `expr_eval.py` evaluates `"a" + "b"` to `"ab"`, rejects a string constant over
  the new length guard, rejects `str + number`, and all pre-existing
  numeric/boolean tests still pass unchanged.
- Full backend pytest suite green; broker/api coverage thresholds still hold.
- CC gate clean.
