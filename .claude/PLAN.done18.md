# Plan: Agent CLI grammar — Sprint 3 (decompiler + edit/delete verification)

## Task

Third of five sprints (Sprint 1: backend, Sprint 2: parser/compiler — both
already shipped on `workshop`, commits `9b7da82f` and `3b615b0b`). Full design
in `~/.claude/plans/purrfect-marinating-pixel.md`. This sprint is, like Sprint
2, **100% frontend JS, fully testable via Vitest, no UI yet** (the actual Edit
button/CLI text box wiring is Sprint 4) — you are building the DECOMPILER, the
reverse of Sprint 2's compiler: given an agent's *current*
`conditions`/`events`/`actions` JSON, regenerate the equivalent `WHEN ... ALERT
... DO ...` CLI text.

**Why this exists**: an operator editing an existing agent needs to see ACCURATE
CLI text reflecting what's actually stored right now — not a stale copy of
whatever text (if any) was originally typed. The agent's `cli_source` column
(Sprint 1) is audit/history only and must never be reloaded as the editable
buffer; Edit mode must decompile the live JSON fresh every time. This also
means the decompiler must handle agents that were NEVER authored via the CLI at
all (hand-typed JSON textarea, the LLM-backed AI-draft flow, direct admin
edits) — it only ever looks at `conditions`/`events`/`actions`, never at
`cli_source`.

**Read Sprint 2's `frontend/src/lib/command/grammars/agents.js` in full before
writing anything** — you are adding to this file, reusing its exported
constants/helpers, not building a parallel module. Specifically reuse:
- `ALWAYS_LEAF` (the frozen sentinel object `{metric:'avail_margin',
  scope:'funds.any_acct', op:'>=', value:-999999999}`) — a stored leaf deep-
  equal to this means render the literal `always`, not the raw leaf.
- `KWARG_ALIASES` — presentation-only short-name aliases (`account`→`acct`
  etc.). Decide (and document your choice in a comment) whether decompiled
  output uses the REAL schema key names or these short aliases — reusing real
  key names is simpler/safer and is the suggested default; it's your call,
  just be consistent and explicit about which you picked and why.
- The exact compile shapes `compileLeaf`/`compileBetween`/`compileCondNode`/
  `compileAlertClause`/`compileDoClause`/`compileAgentStmt` already produce
  (read all of these — lines ~853-1114) so you know EXACTLY what JSON shapes
  you need to reverse:
  - A leaf: `{metric: "<catalog-string, e.g. mean_pnl(30)>", scope:
    "<catalog-string>", op: "<=" etc., value: <literal>}`.
  - An inclusive between-chain compiles to the backend's NATIVE `between`
    operator: `{metric, scope, op: 'between', value: [low, high]}` — decompile
    this back to `low <= metric@scope <= high` (always render as `<=` on both
    sides, since `between` is always inclusive — no need to guess the
    original strictness).
  - A STRICT or mixed-strictness chain compiles differently — to
    `{all: [leaf1, leaf2]}` where both leaves share the identical
    metric+scope and their ops/values form a valid low/high pair (read
    `compileBetween`'s `_BETWEEN_FLIP` logic carefully to understand exactly
    how). Decompiling THIS shape back to pretty between-syntax is a NICE-TO-
    HAVE, not a hard requirement — if you can detect it reliably (same
    metric+scope on both children, complementary ops), do it; if detection
    would be fragile/ambiguous, it is PERFECTLY ACCEPTABLE to fall back to
    the generic `(metric@scope op1 value1) & (metric@scope op2 value2)`
    rendering instead — that is ALSO valid, round-trip-correct CLI text, just
    not using the prettier between-chain syntax. Round-trip correctness is
    the hard requirement; between-chain detection is cosmetic.
  - `{all: [...]}` / `{any: [...]}` / `{not: ...}` composites, arbitrarily
    nested — decompile to `(A) & (B)` / `(A) | (B)` / `~(A)`, with parens
    around every leaf/group per the grammar's own rule.
  - A metric call string like `"mean_pnl(30)"` (positional, as always
    stored) must decompile to KEYWORD form (`mean_pnl(minutes=30)`), since
    that's the ONLY valid CLI input syntax now (Sprint 2 removed positional
    args from the grammar entirely) — parse the stored string's positional
    value(s), look up that metric's `paramKeys` from the catalog (in the
    SAME order used to build the string — see `_compileMetricCallString`),
    and zip them into `key=value` pairs. A zero-arg metric string (e.g.
    `"pnl"`) decompiles to the bare name, unchanged.
  - `events`: `[{"channel": "telegram", "enabled": true}]` → bare `telegram`
    (no extra params beyond `channel`/`enabled`); any additional key (e.g. a
    future `priority`) → render as `channel_name(key="value", ...)`. An empty
    `[]` → `nop`.
  - `actions`: `[{"type": "place_order", "params": {...}}]` → `order(...)`
    using the CLI alias (never literally emit `place_order` — this maps back
    the OPPOSITE direction from how the compiler maps `order`→`place_order`).
    **`params.qty` is in CONTRACTS (the agent-action convention) — convert
    back to LOTS for display** (`lots=qty/lot_size`, using whatever lot-size
    lookup Sprint 2's compiler already uses for the forward conversion — find
    and reuse it, don't write a second one), since `lots=` is the ONLY valid
    CLI spelling for order quantity on `order(...)` — there is no bare `qty=`
    form in this grammar at all. Every other param renders as
    `key="value"` (quoted, free-text) or `key=ENUM` (bare) per each param's
    schema `type`, same quoting rule as the rest of the grammar. Any OTHER
    action type (not `place_order`) renders as `action_type_name(key=value,
    ...)` generically, reusing the alias/quoting rules uniformly. An empty
    `[]` → `nop`.
  - **Explicit non-goal**: a `conditions` tree containing a `{"$ref": "..."}`
    fragment reference cannot be decompiled (no CLI syntax represents a
    template-fragment reference). Detect this and return a clear, structured
    "cannot decompile — this agent uses a $ref fragment; edit it via the JSON
    textarea instead" result rather than crashing or silently producing wrong
    text. Same treatment for ANY other condition/action shape you don't
    recognize — fail loud and clear, never guess.

## Agents

- frontend: |
    Add a decompiler to `frontend/src/lib/command/grammars/agents.js` (same
    file Sprint 2 built — see the detailed shape-by-shape spec above, which
    is grounded in that file's actual current code, not a guess). Suggested
    shape (your call on exact naming, but export at least these two):
    - `decompileCondition(conditions, catalog) -> { text: string|null, errors: ErrorEntry[] }`
    - `decompileAgent({conditions, events, actions}, catalog) -> { text: string|null, errors: ErrorEntry[] }`
      (the full `WHEN ... ALERT ... DO ...` line, or a structured error if
      conditions has an undecompilable shape like `$ref`).

    Build and test in this order:
    1. Leaf decompilation (plain leaf, boolean-shorthand leaf, `in`/`not in`
       list leaf, the native `between` op, the `always` sentinel).
    2. Metric-string positional→keyword reversal (the trickiest piece —
       write focused tests for 1-arg metrics specifically, since that's all
       that exists in the real catalog today).
    3. Composite (`all`/`any`/`not`) decompilation, arbitrarily nested —
       reuse/adapt the strict-between-as-`all` detection heuristic here too,
       falling back to generic `&`/`|` rendering per the guidance above.
    4. `events` → `ALERT` clause (including the `nop` case).
    5. `actions` → `DO` clause (including `order`'s qty→lots reconversion,
       the `nop` case, and the generic-action-type case).
    6. Full `decompileAgent` combining all of the above into one line.

    **Testing — this is the most important part of this sprint:**
    - **Round-trip test for every canonical example in the design doc** (8+
      examples — see `~/.claude/plans/purrfect-marinating-pixel.md`'s
      `### Examples` section): `compile(originalText)` → take its `.agent`
      JSON → `decompile(that JSON)` → `compile(the decompiled text)` again →
      assert the SECOND compile's JSON is DEEPLY EQUAL to the first. The
      decompiled TEXT does not need to match the original text byte-for-byte
      (e.g. `mean_pnl(30)` vs `mean_pnl(minutes=30)` are different text,
      SAME meaning) — only the re-compiled JSON needs to match exactly.
    - Decompile a handful of hand-constructed JSON shapes that were NEVER
      produced by Sprint 2's compiler at all — simulating a real agent
      authored via the OLD JSON textarea or the AI-draft flow (e.g. a
      conditions tree using a fixed-window token like `mean_pnl_30m` instead
      of a call-syntax string, or an `actions` entry for an action type
      Sprint 2's tests never exercised) — confirm these decompile to
      SOMETHING sensible and round-trip correctly too, not just the shapes
      your own compiler happens to produce.
    - A `$ref`-containing conditions tree returns the structured
      cannot-decompile error, not a crash or wrong output.
    - An unrecognized/malformed shape (missing a required key, an
      unknown op) also fails loud with a structured error, never silently
      produces plausible-looking-but-wrong text.

    When done, run `npx vitest run` (full suite) and `npx svelte-check
    --output machine`, and report full results — confirm the full existing
    suite (should be 2138 tests before your additions) is unaffected.

## Tests
- pytest: no
- svelte-check: yes (sanity check only)
- playwright: no

## Commit message
feat(automation): agent CLI grammar — Sprint 3, decompiler (no UI)

Sprint 3 of 5. Adds the reverse of Sprint 2's compiler: given an agent's
current conditions/events/actions JSON, regenerate equivalent CLI text —
needed so Edit mode (Sprint 4) can show accurate text for ANY agent
(JSON-textarea-authored, AI-drafted, or CLI-authored) by decompiling the
live JSON fresh, never trusting the audit-only cli_source column. Handles
the native `between` operator, the `always` sentinel, metric-string
positional-to-keyword reversal, and order's qty(contracts)->lots
reconversion. Round-trip tested against every canonical example plus
hand-constructed non-CLI-originated agent shapes.

## Done when
- Every canonical example's compiled JSON survives a decompile→recompile
  round-trip unchanged.
- A hand-constructed agent JSON that Sprint 2's compiler never produced
  (simulating JSON-textarea/AI-draft authorship) decompiles to valid,
  round-trip-correct CLI text.
- A `$ref`-containing conditions tree fails with a clear, structured error
  instead of crashing or producing wrong text.
- Full Vitest suite green (2138 existing + new); svelte-check 0 new errors.
