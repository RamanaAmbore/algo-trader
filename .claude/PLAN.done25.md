# Plan: Agent CLI grammar — Sprint 10, fragments become Tokens (reference + creation)

## Task

Rename "Fragment"/"Agent Template" to "Token" in operator-facing text
only (backend table/model names unchanged). Wire bare Token references
(`{"$ref": "name"}`) as ordinary catalog tokens in the CLI grammar,
closing Sprint 3's documented "$ref is a non-goal" limitation. Add a
`token(...)` creation statement for notify-kind Tokens — **this part is
now REQUIRED, not deferred**: Sprint 9 already shipped and removed the
old Agent-templates page's creation form; Tokens currently have NO
creation path anywhere in the app. Condition-kind Token creation (a full
boolean-tree body, doesn't fit as a simple call argument) is explicitly
OUT of scope — still editable via the existing Structured-JSON path only.

**Backend facts already confirmed**: `backend/api/routes/agent_templates.py`
already has a working create endpoint (`POST /api/admin/fragments`,
request struct fields: `kind: str, name: str, body: list | dict`) — no new
backend route needed for creation. The resolution mechanism
(`template_registry.py`'s `resolve_events`/condition-$ref walk) already
exists and already has cycle detection — the frontend grammar compiler
only ever PASSES THROUGH a `$ref` reference, never expands/walks it
itself, so this existing backend guard remains the sole and sufficient
safeguard (state this explicitly in code comments, it's a deliberate
boundary, not an oversight).

## Agents

- backend: The frontend's `fetchAgentCatalog()` already fetches
  `fetchGrammarTokens('condition')`/`('notify')`/`('action')` — whatever
  backend endpoint(s) serve those calls today (grep `grammar_tokens`/
  `fetchGrammarTokens`'s backend counterpart to find the real route) need
  to ALSO include active `AgentTemplate` rows, merged into the SAME
  response shape (`grammar_kind: 'condition'|'notify', token_kind:
  'metric'|'channel', token: <name>, description: <row's own
  description>`) so the frontend's EXISTING catalog-building code
  (`buildCatalog` in agents.js, unchanged) picks them up automatically
  into `catalog.metrics`/`catalog.channels` with zero frontend
  catalog-fetch changes. Read the real current catalog-serving endpoint
  before assuming its shape — don't guess.

  Do NOT rename `AgentTemplate`, `template_registry.py`, the
  `agent_fragments` table, or the `/api/admin/fragments` route path — this
  is a display-layer rename only; the frontend compiles `token(...)` to
  this EXISTING route unchanged.

  **Test coverage (mandatory)**: a test confirming an active
  `AgentTemplate` row of each kind appears in the merged catalog response
  with the right `grammar_kind`/`token_kind`, and an inactive one does
  NOT appear (mirror whatever existing test covers catalog-row filtering
  for ordinary metrics/channels).

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short -k "grammar or catalog or fragment or template"`
  then full suite: `venv/bin/pytest backend/tests/ -q --tb=line`

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. **Reference wiring**: once the backend merges Token rows into the
     catalog (parallel task), a bare name resolving to a catalog entry
     whose underlying row IS a Token (you may need the catalog entry to
     carry a flag/marker distinguishing "this is a Token row" vs. "this
     is a regular metric/channel" — check what the merged backend
     response actually looks like once you can see it, or coordinate via
     each other's handback reports since you're dispatched in parallel)
     compiles to `{"$ref": "<name>"}` instead of a normal leaf/channel
     dict, in whichever position it's used (WHEN → condition-kind Token,
     ALERT → notify-kind Token). The decompiler's inverse: a `{"$ref":
     "<name>"}` entry decompiles to that bare Token name, same as any
     other bare token — remove the Sprint 3 "$ref fragment — explicit
     non-goal" special-case entirely (find it in the decompiler and in
     `agentsDecompiler.test.js`'s existing `$ref`-non-goal tests — update
     those tests to assert successful decompilation now, not the old
     "fails loud, falls back to Structured" behavior).
  2. **New `token(...)` creation statement** (standalone-only, mirrors
     `bracket_stmt`'s restriction and error-message style): `kind=notify`
     is the only supported value this sprint (compile error if
     `kind=condition` is given — "condition-kind Token creation isn't
     supported from the CLI yet — edit it via the Structured tab"). Body
     is a plain `call_list` of channels, same shape `ALERT`'s clause
     already parses — reuse that parsing logic, don't duplicate it.
     `#name=` (via Sprint 7's `parseMetaClause`/`compileMetaClause`,
     required) and `#desc=` are the only meta fields that apply (no
     `#group=`/`#tag=` — `AgentTemplate` doesn't have those columns, don't
     invent them). Compiles to `{kind: 'notify', name: <from meta>, body:
     [<channel objects, same shape ALERT's call_list already compiles
     channels to>]}` — matching `backend/api/routes/agent_templates.py`'s
     real request struct exactly.
     Example: `token(kind=notify) telegram, email, log #name="critical-trio" #desc="..."`.
  3. Rename every OPERATOR-FACING string this file produces or documents
     (error messages, EBNF header comment prose) from "fragment" to
     "Token" where it refers to this concept — do NOT rename internal
     variable/function names that mirror backend naming (e.g. if you have
     a local `_fragmentCatalog` variable, renaming it is fine and
     encouraged for clarity, but don't go rename backend-facing field
     names like `kind`/`body` that must match the real API).

  **Test coverage (mandatory)**: in `agentsGrammar.test.js` — a bare
  Token reference in WHEN compiles to `{"$ref": "name"}`; a bare Token
  reference in ALERT compiles to `{"$ref": "name"}`; `token(kind=notify)
  telegram, email #name="x"` compiles to the exact
  `{kind:'notify', name:'x', body:[...]}` shape; `token(kind=condition)`
  is a clear compile error; `token(...)` with no `#name=` is a compile
  error; `token(...)` used inside WHEN/DO (not standalone) is rejected.
  In `agentsDecompiler.test.js` — update the existing `$ref`-non-goal
  tests to assert successful round-trip decompilation now (a `{"$ref":
  "notify-critical-trio"}` entry decompiles to the bare token name and
  recompiles to the identical JSON) — this REPLACES the old "fails loud"
  assertions, don't just add new tests alongside stale failing ones.

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no (console already has the CLI wired from Sprint 9; no
  new UI surface — `token(...)` just types through the existing input)

## Commit message
feat(agents): Sprint 10 — fragments become Tokens (reference + notify-kind creation)

## Done when
- A bare reference to an existing notify/condition Token compiles to
  `{"$ref": "name"}` and decompiles back to the bare name — no more
  "$ref is a non-goal" special case anywhere in the compiler/decompiler.
- `token(kind=notify) telegram, email #name="x"` creates a real,
  resolvable Token via the existing `/api/admin/fragments` endpoint.
- `token(kind=condition)` is a clear, documented compile error, not a
  silent failure.
- Full backend + frontend test suites green, CC clean.
