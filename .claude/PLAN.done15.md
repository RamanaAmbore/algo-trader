# Plan: Sprint 3 — save-time condition validation + AI-builder/discoverability hardening

## Context

Sprint 2 (shipped, commit `1b80b7d8`) added parameterized call-syntax metric tokens
(`mean_pnl(30)` etc.) to the grammar registry. The originally-approved plan's Sprint 3
= Phase 4 was scoped as "save-time validation in `agents.py`/`agent_ai.py` + a small
frontend discoverability touch." Two Explore agents (backend save-path, frontend UI)
grounded this scope with four concrete, confirmed gaps — not speculative work:

1. **`create_agent`/`update_agent` never validate a threshold/cycle agent's condition
   tree at all.** The existing `validate()` function (`agent_evaluator.py:602`) is
   real and correct, but it's only wired into an *optional* `/validate-condition`
   pre-check endpoint the frontend may or may not call before saving, and into one
   narrow CLI path (`/interpret ai create`). The actual save routes only validate
   event-kind agents' log tags/renderers/channels (`_age_validate_event_spec`) — a
   typo'd metric/scope/op token (or a malformed call-syntax token) on an ordinary
   threshold agent saves with 200/201 and silently never fires.
2. **A call-syntax token's numeric window argument has no sanity bound.**
   `mean_pnl(0)` and `mean_pnl(0.001)` parse and resolve to a real callable (not
   `None`), so they pass today's only check (`REGISTRY.metric(token) is None`) —
   then always evaluate to `None` at runtime (window cutoff ≈ now ⇒ <2 samples ⇒
   every window reducer returns `None`). A silently-dead agent with no save-time
   signal.
3. **The AI agent-builder's token-summary generator doesn't generically surface
   `params_schema` for metric tokens.** `_summarise_token()` (`agent_ai.py:60`) only
   renders a structured "params: {...}" hint when `kind == "action_type"`. The 7 new
   metric call-syntax tokens are visible to the LLM today only because each one's
   YAML `description` happens to have "Call syntax, e.g. X(30)." hand-typed into it —
   a future token author who forgets that sentence gets an invisible-to-the-LLM
   call-syntax token.
4. **The agent-edit UI's "Conditions (JSON)" textarea is the one field on the whole
   form with no `InfoHint`/placeholder.** Every sibling field (Tags, Blackout windows,
   Long name, etc.) has one; an operator typing a condition by hand has zero in-UI
   indication the call syntax exists.

All four share one root cause (save-time enforcement never existed for this surface)
and one fix mechanism (reuse existing `validate()`/`is_grammar_tree()`/`params_schema`
— no new validation logic, just wiring + one bound + one generalization).

## Approach

**1. `backend/api/routes/agents.py`** — new helper, mirroring the existing
`_age_validate_event_spec` pattern exactly:
```python
def _age_validate_threshold_conditions(conditions: dict) -> None:
    from backend.api.algo.agent_evaluator import validate as v2_validate
    from backend.api.algo.agent_engine import is_grammar_tree
    if not is_grammar_tree(conditions):
        return   # empty/placeholder {} — same leniency /validate-condition already has
    errors = v2_validate(conditions)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors), extra={"errors": errors})
```
- `create_agent` (~line 736): call unconditionally on `data.conditions`, right
  alongside the existing `if kind == "event": _age_validate_event_spec(...)` line
  (both run; kind-agnostic token check + kind-specific event check are independent).
- `update_agent` (~line 832): call unconditionally on `agent.conditions` — the
  **merged** value, not `data.conditions` — at the exact same point the existing
  `if agent.kind == "event":` re-validation already runs post-merge, for the same
  reason given in that block's own comment (a save that only touches `actions` must
  still re-check a previously-saved `conditions`).
- Reuses `validate()` and `is_grammar_tree()` verbatim — zero new validation logic.
  The `is_grammar_tree()` gate means an already-tolerated empty/placeholder `{}`
  keeps saving exactly as before; only a tree that looks like a real condition
  attempt gets its tokens checked.

**2. `backend/api/algo/grammar_registry.py`** — `_parse_call_token`'s numeric-literal
guard (~line 230) gets one more condition: reject `arg_node.value <= 0`. This closes
the `mean_pnl(0)` footgun at the single place call-syntax tokens are already
validated, so it flows through `validate()`'s existing `REGISTRY.metric(...) is None`
check for free — no agents.py-side change needed for this part. (Negative literals
already fail today by accident — `ast` parses `-5` as `UnaryOp(USub, Constant(5))`,
not `Constant(-5)` — this makes that rejection intentional and documented instead of
incidental.)

**3. `backend/api/algo/agent_ai.py`** — `_summarise_token()`'s
`if kind == "action_type" and token.params_schema:` branch gains an `else` for every
other params_schema-bearing kind (the metric call-syntax tokens use a flat
`{param_name: spec}` shape, not the action tokens' JSON-Schema `{required,
properties}` shape — so it needs its own rendering, not shared logic):
```python
else:
    keys = list((token.params_schema or {}).keys())
    if keys:
        bits.append(f"call syntax: {token.token}({', '.join(keys)})")
```
Produces e.g. `"call syntax: mean_pnl(minutes)"`, generated structurally from the
schema rather than depending solely on hand-written description text.

**4. `frontend/src/routes/(algo)/automation/+page.svelte`** — add an `InfoHint` next
to the "Conditions (JSON)" field label (~line 1296), mirroring the existing
Tags/Blackout-windows `InfoHint` usage exactly (same component, same markup shape as
~line 1278/1287). Hint text: a short example covering both the fixed tokens
(`mean_pnl_30m`) and the new call syntax (`mean_pnl(45)` for any window), plus the
existing leaf shape. Scoped to this one primary editor only — the sibling textarea on
`agent-templates/+page.svelte` is a separate surface, intentionally not touched here
(stays a possible follow-up, not bundled into this "small" touch).

## Files

- `backend/api/routes/agents.py` — new `_age_validate_threshold_conditions`, wired into `create_agent` + `update_agent`.
- `backend/api/algo/grammar_registry.py` — one-line guard addition in `_parse_call_token`.
- `backend/api/algo/agent_ai.py` — `_summarise_token`'s new `else` branch.
- `frontend/src/routes/(algo)/automation/+page.svelte` — one `InfoHint` addition.
- Tests (same commits, not a separate pass):
  - `backend/tests/test_agents_routes.py` — real POST/PUT integration tests: unknown metric token → 422; well-formed `mean_pnl(30)` → saves fine (no false positive); `{}` placeholder conditions → still saves (no leniency regression).
  - `backend/tests/test_grammar_registry.py` — extend `TestParameterizedCallTokens` with `mean_pnl(0)` / `mean_pnl(0.0)` → `None`.
  - `backend/tests/test_agent_evaluator.py` — a `validate()` leaf test: `metric: "mean_pnl(0)"` → "unknown metric token" error (end-to-end closure of gap #1+#2 together).
  - `backend/tests/test_agent_ai_coverage.py` — new `test_summarise_token_metric_with_params_schema_shows_call_syntax`, mirroring the existing `test_summarise_token_action_type_with_params_schema` test but with a flat `{"minutes": {"type": "number"}}` schema and `kind="metric"`.
  - `frontend/src/lib/__tests__/automationConditionsInfoHint.test.js` (new, Vitest source-audit, mirrors `strategyDetailMetricsInfoHint.test.js`) — asserts the Conditions field now has an `InfoHint` with the call-syntax example text.

## Verification

- `venv/bin/pytest backend/tests/ -q --tb=line` full suite green, including the new tests above.
- `cd frontend && npx svelte-check --output machine` — 0 errors (existing 5 warnings unchanged).
- `cd frontend && npx vitest run` — new test passes, no regressions.
- CC gate (`radon cc backend/ -s -n D`) clean — these are small, low-branching additions.
- Manual sanity check (via the new integration test, not live UI): POST an agent with
  `conditions: {"metric": "nope_xyz", "scope": "positions.total", "op": "<=", "value": 0}`
  and confirm 422 with the error naming the bad token; POST the same shape with
  `metric: "mean_pnl(30)"` and confirm success.

## Done when

- `POST /api/agents/` and `PUT /api/agents/{slug}` reject an unknown/malformed
  metric/scope/op token (including a non-positive call-syntax window) with 422,
  instead of saving silently.
- `mean_pnl(0)` (and any non-positive window) resolves to `None` from
  `REGISTRY.metric()`, identical treatment to an unknown token.
- `_grammar_snapshot()`'s LLM-facing summary shows a structurally-generated
  call-syntax hint for every params_schema-bearing metric token, not just
  hand-written description text.
- The automation page's "Conditions (JSON)" field has an `InfoHint` matching the
  form's existing per-field pattern.
- Full backend suite + svelte-check green; CC gate clean.

## Commit message (draft)

Two commits, consistent with "fix one issue at a time" (these three backend items
are one logical gap discovered together — bundled; the frontend touch is unrelated
code, separate commit):
1. `fix(agents): close save-time condition-validation gap + call-syntax window bound + AI-builder hint generalization`
2. `fix(frontend): add InfoHint for the agent Conditions field call-syntax discoverability`
