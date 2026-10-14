# Plan: RamboQL grammar extensions — newline separators, value-holding GIVEN, order/place_order unification (Phase A), guide + sample updates

## Task

Four changes to the agent/order CLI grammar ("RamboQL"), fully designed and
council-reviewed (architect/risk/research) this session:
1. Accept a bare newline as an alternative to the existing `;`/`,` separators in
   `GIVEN`/`ALERT`/`DO` clause lists and both list-literal shapes.
2. Upgrade `GIVEN name = expr` so an expression involving operators (string
   concat, math) over a dynamic read is evaluated server-side, once per tick,
   into a real value reusable in `ALERT`'s message and `DO`'s string-typed
   params — not just a client-side text alias like today. Guarded: per-agent
   context isolation, compile-time rejection if a GIVEN-bound value is used for
   a numeric/enum param (qty/price/etc never affected), per-agent exception
   isolation so a bad expression never aborts the whole cycle.
3. Unify `order`/`place_order` — Phase A only (risk council BLOCKED a
   single-shot rename): widen backend dispatch tables + YAML catalog to accept
   BOTH `"order"` and `"place_order"` identically; flip the frontend compiler to
   EMIT `"order"` going forward; backfill existing stored `Agent.actions` JSON;
   verify via a live DB scan on both `ramboq`/`ramboq_dev`. Dropping legacy
   `"place_order"` acceptance (Phase B) is explicitly NOT part of this delivery.
4. Refresh `/automation/guide` (or its current real location) and the CLI
   editor's sample/placeholder statements to show `and`/`or`/`not` (already
   working as aliases for `&`/`|`/`~` — confirmed via grep, no grammar change
   needed there), `order`, and a GIVEN-computed-message example.

Full design rationale, the real-code investigation findings, and all council
feedback are in `~/.claude/plans/purrfect-marinating-pixel.md` (same content,
already approved) — read it for complete context before starting.

## Agents

- backend: Implement Part 2's backend half and Part 3's backend half.
  (1) New per-tick binding-evaluation step in `backend/api/algo/agent_engine.py`'s
  `_cycle_evaluate_agent`, inserted where `V2Context` is built, before
  `v2_evaluate` runs: if `agent.given` (a list of `{"name","expr"}` dicts, in
  declaration order) is non-empty, evaluate each binding — a bare `metric@scope`
  resolves via the existing scope/metric-resolver machinery (aggregate scopes
  ONLY in this phase, e.g. `positions.total`/`funds.total`; a per-row scope like
  `positions.any_acct` is a compile-time error for now, deferred); an operator
  expression calls `backend/api/algo/expr_eval.py`'s existing `eval_expr(expr,
  namespace)` with a namespace built from this agent's own already-resolved
  earlier bindings. Store the result under a PER-AGENT key —
  `context.setdefault("given", {})[agent.id] = bindings` — never a bare name on
  the shared `context` dict (it's reused across every agent in a cycle; a bare
  name would leak agent A's bindings into agent B). Wrap the whole per-agent
  step in its own try/except — a raised `ExprError` logs and skips only this
  agent for this tick, never aborts `run_cycle()` for others. (2) Wire
  `backend/api/algo/events.py` (alert message rendering) and
  `backend/api/algo/actions.py` (`resolve_action_params`) to read
  `context["given"][agent.id]` by name when a clause references a GIVEN label.
  (3) Part 3: widen `actions.py`'s `BROKER_ACTIONS` set (line ~36) and
  `_LIVE_ACTION_HANDLERS` dict (line ~199), `actions_paper.py` (~317),
  `actions_sim.py` (~397) to accept `"order"` as well as `"place_order"`,
  both resolving to the same `_action_place_order` handler — do not remove
  `"place_order"`. Add a new `token: order` row to
  `backend/config/grammars/agent_grammar.yaml` (same shape as the existing
  `place_order` row, same resolver) — do not edit/remove the existing row.
  (4) Write a new, idempotent backfill migration (adapt the migrate→verify
  pattern from `backend/api/algo/template_registry.py`'s
  `migrate_and_retire_agent_fragments()` — don't reuse it as-is, this rewrites
  values inside a JSON array, not a whole table): iterate every `Agent.actions`
  row, rewrite any `"type": "place_order"` to `"type": "order"`, verify by
  re-reading, log a count. Wire into `init_db()` alongside the existing
  migrations in `backend/api/database.py`. Do NOT touch `agent_events.detail`
  JSON (frozen historical audit log). After writing the migration, report back
  the exact `SELECT count(*) FROM agents WHERE actions::text LIKE
  '%place_order%'` result you'd expect on `ramboq_dev` so the operator can run
  it via `ssh ramboq` before/after — do not attempt to SSH or run this query
  yourself.
- frontend: Implement Part 1, Part 2's frontend half, Part 3's frontend half,
  and Part 4's CLI placeholders — all in
  `frontend/src/lib/command/grammars/agents.js` plus two small placeholder
  edits. (1) Newline-as-alternative-separator: in `parseItemListClause`
  (~agents.js:2208, the shared `;`-separated clause body for GIVEN/ALERT/DO),
  `parseCallList` (~2011, the `,`-separated call list within one clause item),
  and `parseListLiteral` (~1722, both the plain-value and list-of-object-
  literal shapes) — after consuming one item, if the next significant token
  isn't the expected separator/closing-token/reserved-keyword but a newline
  occurred since the last token, accept it as the separator. Keep the existing
  trailing-comma/trailing-semicolon error rules unchanged — a literal
  separator token still always demands a following item; only its ABSENCE,
  replaced by a bare newline, is newly accepted. Do NOT extend this to a
  single call's own argument list inside `(...)` or the bare multi-order
  top-level statement's comma. (2) GIVEN classification: at compile time,
  classify each binding's RHS as a pure alias (bare metric@scope/field
  reference, no operator — compiles exactly as today, client-side AST
  substitution) or an operator expression (anything with `+`/`-`/`*`/`/` over a
  dynamic read, or referencing another binding — compiles to a new
  `agent.given: [{"name":..., "expr":...}]` JSON key, in declaration order,
  with the expr string stored verbatim). Add a compile-time guard: when a
  GIVEN-bound name backed by an operator expression is used as an action-param
  value, check the target param's schema `type` (reuse the existing
  `_coerceValueForSpec` type system) — only `string`-typed params may reference
  it; a numeric/enum param referencing one is a compile error ("GIVEN label
  '<name>' is a computed value; it cannot be used for a numeric/enum field").
  Update the decompiler to read a stored `agent.given[].expr` string back
  VERBATIM when reconstructing the GIVEN line — never re-derive/reformat it
  independently. (2b) `debug(...)` interaction (already implemented, see
  `_compileDebugCall` ~agents.js:4572): today `debug(x)` on ANY GIVEN-bound
  label formats to a STATIC compile-time description (via
  `_debugFormatValueNode`/`JSON.stringify(compileCondition(...))`), by design,
  since GIVEN had no live value before this plan. Once an operator-based
  binding is server-evaluated (2 above), `debug(x)` referencing THAT KIND of
  binding must switch to a LIVE marker instead — compile it to
  `{"$given": "<name>"}` (parallel to the existing, already-live `{"$row":
  "<name>"}` marker), resolved backend-side from `context["given"][agent.id]`
  at dispatch time. `debug(x)` on a PURE-ALIAS GIVEN binding (no operator) is
  UNCHANGED — it keeps today's static behavior, since nothing about that case
  changed. Backend: `resolve_row_ref_params` (or a sibling resolver) needs a
  matching `{"$given": name}` branch alongside its existing `{"$row": name}`
  one, reading the same per-agent `context["given"][agent.id]` dict. (3) `order`/`place_order`: flip every one of the ~35 sites
  that currently emit/compare `'place_order'` (the generic DO-action compiler,
  bare-order-statement compiler, bracket-template compiler, the parser-level
  name guard, both decompiler functions, autocomplete/candidate-token logic —
  grep the file for the literal string to find them all) to emit `"order"`
  going forward. Invert/retire `_ACTION_NAME_ALIAS` (~agents.js:6971) — CLI
  text typing `place_order` should still PARSE (backward compatibility for
  typing), but must always COMPILE to `"type": "order"`, never
  `"type": "place_order"`. (4) Update
  `frontend/src/lib/command/AgentCliEditor.svelte`'s default placeholder
  (~line 93) to a GIVEN-using example demonstrating a computed message (e.g.
  `GIVEN p = pnl@positions.total\n      msg_txt = "Loss: " + p\nWHEN p <= -50000\nALERT msg = msg_txt, telegram\nDO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)`
  or similar, using `and`/`or`/`not`-style conventions where applicable);
  leave `frontend/src/lib/order/OrderCliTab.svelte`'s order-scoped placeholder
  (~line 125) as a plain `order(...)` example, unaffected.
- doc: Update `/automation/guide`'s real current content (find the actual file
  — it may be `docs/guides/AGENTS_GUIDE.md`, a Svelte route file, or similar;
  confirm the real location before editing) to add: a worked example of GIVEN
  computing a message from concatenated live values; a note that a newline is
  now a valid alternative to `;`/`,` in GIVEN/ALERT/DO's vertical lists; switch
  every boolean-logic example in the guide to `and`/`or`/`not` (keep one note
  that `&`/`|`/`~` remain valid compact aliases — do not remove them from the
  grammar); describe `order` as the one name to type, mentioning
  `place_order` only as "the historical internal name, not something you need
  to type." Do not claim Phase B (dropping legacy `place_order` acceptance) has
  happened — it explicitly has not.
- broker: skip
- backend-test: For every backend file changed above, add/update tests:
  the new per-tick binding step (aggregate-scope resolution, operator-expression
  evaluation via `expr_eval`, a two-agent-same-tick test proving per-agent key
  isolation, a forced-`ExprError` test proving one agent's bad expression never
  aborts the cycle for a sibling agent); the backfill migration (idempotency on
  a second run, correct count, confirm `agent_events.detail` is untouched);
  dispatch-table dual-acceptance (both `"order"` and `"place_order"` resolve to
  `_action_place_order`). Update the 7 existing backend test files + 1 golden
  fixture (`backend/tests/golden/agent_grammar_baseline.json`) that currently
  assert the literal `"place_order"` as the sole/canonical value, to expect
  `"order"` as canonical going forward, while adding ONE new regression test
  that explicitly confirms the dispatch tables still accept a raw
  `"place_order"` action dict (the legacy-acceptance safety net itself needs
  coverage, distinct from the new canonical path). Also do one grep pass for
  any residual `"place_order"` string-comparison outside the already-
  inventoried ~35 frontend + 5 backend sites (architect council's watch-point)
  and report anything found.
- playwright: skip (no new UI surface — CLI-grammar-only changes plus a guide
  doc update)

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

feat(agents): GIVEN becomes server-evaluated, newline separators, order/place_order unification (Phase A)

## Done when

- A bare newline works as an alternative to `;`/`,` in GIVEN/ALERT/DO's clause
  lists and both list-literal shapes; every existing comma/semicolon-based
  statement is unchanged (regression-tested).
- `GIVEN name = "text " + p + " " + q`-style bindings compute a real value once
  per tick, server-side, reusable in ALERT's message and DO's string-typed
  params; rejected at compile time if referenced from a numeric/enum param.
- Agent-to-agent binding leakage is impossible (test-confirmed) and a bad
  expression in one agent never aborts the cycle for others (test-confirmed).
- `order` is the sole name the compiler ever emits in compiled JSON;
  `place_order` is still accepted (typed in CLI text, or already stored in the
  DB) but never produced going forward. Phase B is explicitly not done here.
- The backfill migration is idempotent and leaves `agent_events.detail`
  untouched; the operator has the exact verification query to run manually via
  `ssh ramboq` on both databases.
- The guide and CLI sample statements show `and`/`or`/`not`, `order`, and a
  GIVEN-computed-message example as the recommended forms.
- Full backend + frontend test suites green, svelte-check clean.
