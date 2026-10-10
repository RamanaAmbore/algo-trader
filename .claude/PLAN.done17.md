# Plan: Agent CLI grammar — Sprint 2 (core parser/compiler, no UI yet)

## Task

Second of five sprints building a CLI/text grammar for authoring agents (full
design in `~/.claude/plans/purrfect-marinating-pixel.md` — this file excerpts
everything you need, but that's the canonical source if anything here is
ambiguous). Sprint 1 (backend, already shipped on `workshop` at commit `9b7da82f`)
closed a save-time validation gap and extended `expr_eval.py` — this sprint is
**100% frontend, 100% client-side JS, fully testable via Vitest with no UI at
all** (the actual CLI input box on the automation page is Sprint 4, later).

**Why this exists**: today an agent's condition tree and action array are two
separate hand-typed JSON blobs. This sprint builds a parser + compiler that lets
an operator type ONE line of text (e.g.
`WHEN mean_pnl(30)@positions.total <= -50000 ALERT telegram DO order(account, symbol="NIFTY25JULFUT", side="SELL", lots=1)`)
and compiles it to the EXACT SAME JSON shape the existing JSON-textarea authoring
flow already produces — so the backend needs zero changes to accept it. A second,
much simpler form — a bare `order(...)` statement with no `WHEN`/`ALERT`/`DO` at
all — compiles to a single-ticket (or, if comma-separated, a basket) order-
placement request instead of an agent.

A critical existing precedent you MUST read first: `frontend/src/lib/command/
engine.js` (342 lines) already powers the order-entry terminal with a tokenizer
+ token-by-level dropdown-autocomplete engine, consumed by `frontend/src/lib/
command/grammars/orders.js` (684 lines, the `buy`/`sell`/`cancel`/`modify` verb
grammar) and rendered by `frontend/src/lib/CommandBar.svelte` /
`frontend/src/lib/order/CommandLineTab.svelte`. This sprint EXTENDS `engine.js`
with three small, targeted, backward-compatible modifications (below) and adds a
NEW file, `frontend/src/lib/command/grammars/agents.js`, which is NOT a
hand-written grammar like `orders.js` — it's a generic builder that constructs
the grammar dynamically from the live backend token catalog, plus the new
recursive-descent boolean-tree parser `orders.js` has no equivalent of, plus the
compiler, plus the semantic validator.

## The grammar (read carefully — this is the full, final, operator-negotiated
## spec, not a draft)

### Operator table

| Operator | Meaning | Where |
|---|---|---|
| `<` `<=` `>` `>=` `==` `!=` | comparison | condition leaf |
| `in` / `not in` | membership — RHS is a square-bracket list literal, e.g. `side in [BUY, SELL]` | condition leaf |
| `low <= metric@scope <= high` | "between" via native chained comparison. Both comparators must point the SAME direction (both `<`/`<=`, or both `>`/`>=`) — mixed direction is a parse error | condition leaf |
| `&` | AND | combining **parenthesized** condition leaves |
| `\|` | OR | combining **parenthesized** condition leaves |
| `~` | NOT | negating a parenthesized leaf/group |
| `( )` | dual role, disambiguated by ADJACENCY: a `(` immediately following a `NAME` with no space is that name's call-argument-list; a `(` anywhere else opens a grouped sub-expression | everywhere |
| `@` | binds a metric to its scope, within ONE leaf — a different grammatical role from `&`/`\|`/`~` (which combine whole leaves), kept as a distinct symbol deliberately | `mean_pnl(30)@positions.total` |
| `=` | keyword-argument assignment | inside any call |
| `,` | separates function arguments, AND separates multiple `ALERT`/`DO` calls | inside a call; `ALERT`/`DO` clauses |
| `nop` | literal "do nothing" — ALWAYS bare, `nop()` is a parse error, not tolerated | `ALERT nop` / `DO nop` |
| `always` | literal "always true" — sugar for the sentinel leaf `{"metric":"avail_margin","scope":"funds.total","op":">=","value":-999999999}` (the exact shape the existing `market-open-nse` builtin agent already uses — grep for it if you want to confirm the exact scope token name, since I'm not 100% certain it's `funds.total` vs `funds.any_acct` — verify against a real seeded builtin agent's conditions JSON rather than guessing). **Standalone only** — a parse error if combined with `&`/`\|`/`~`/anything else | `WHEN` clause, alone |
| `name` or `name(first, kw=val, ...)` | **every token (metric, scope, action, channel) is either a bare name or a function call — no third shape.** Parens appear ONLY when there's >=1 argument; a zero-arg token is ALWAYS bare, `name()` is a parse error for every token, not just `nop`. When parens ARE present: first argument positional OR keyword (operator's choice), every subsequent argument ALWAYS keyword. (NOT `WHEN`/`ALERT`/`DO` — fixed section keywords, never parenthesized, never tokens) | metrics, scopes, actions, channels, uniformly |
| `order` | alias for the `place_order` action (the real dispatch key stored in JSON stays `place_order` — `order` is purely a CLI-typing convenience, never written to the output) | `DO` clause / bare statement |
| `lots=` | order quantity in LOTS — convert to the real `qty` (contracts) field server-side-equivalent logic: `qty = lots * lot_size`. You need a lot_size lookup; check how the existing order ticket code (`frontend/src/lib/order/` or `orders.js`'s own `$ref: qty` resolution) already does lots→contracts conversion and reuse that exact mechanism — do not invent a second one | inside `order(...)` |

**Enum values are bare, free-text values are quoted.** `side=SELL` (bare — enum,
validated against that param's `enum` list from its schema), `account="ZG0790"`
(quoted — free text, preserved exactly as typed, never re-cased). A bare `NAME`
means "enum constant" in a `value` position and "token reference" in a `call`
position — context determines which.

**All unquoted identifiers are case-insensitive** (`WHEN`/`when`, `mean_pnl`/
`MEAN_PNL`, `SELL`/`sell`) — matched case-insensitively against the catalog,
normalized to the catalog's canonical stored casing when compiling to JSON.
**Quoted strings are the one exception** — preserved exactly as typed.

**Metric/scope call-syntax ALWAYS compiles to positional-only in the output
JSON, regardless of how it was typed.** The already-shipped backend mechanism
(`backend/api/algo/grammar_registry.py`, already in this repo, do not modify it)
that resolves a string like `"mean_pnl(30)"` strictly rejects keyword arguments
— `"mean_pnl(minutes=30)"` would be REJECTED by the live backend as an unknown
token. So even though this CLI grammar's general rule allows typing
`mean_pnl(minutes=30)`, **your compiler must emit `"mean_pnl(30)"` (positional)
in the generated JSON metric field, never the keyword form** — this applies
ONLY to metric/scope tokens. Actions and channels compile to a plain keyed dict
(`{"type": "order", "params": {"account": ...}}`), which was never
string-parsed by the backend, so keyword names are always safe there — no
normalization needed for those.

**Return types**: `metric_ref` is `number | boolean | null` (most metrics are
numeric; a few like `is_itm`/`is_ntm` are boolean; `null` = not computable this
tick, never matches, leaf is skipped). A **boolean-metric shorthand** is valid:
a bare `metric_ref` with NO comparator is a complete leaf by itself —
`is_itm@positions.total` alone means "is truthy," equivalent to
`is_itm@positions.total == true`. A NUMERIC metric written bare with no
comparator is a parse error ("expected a comparator after a numeric metric"),
not a silent success — you don't know which metrics are boolean vs numeric
until you resolve the token against the catalog (`value_type` field), so this
check happens after resolution, not during raw parsing.

### Shape

`WHEN`, `ALERT`, `DO` are bare section keywords, never parenthesized. All three
are optional TOGETHER (not independently) — they exist only for a persistent
agent; placing order(s) directly omits all three entirely:

```
WHEN <condition>
ALERT <channel-calls> | nop
DO <action-calls> | nop
```
or, standalone:
```
order(account, symbol="NIFTY25JULFUT", side="SELL", lots=1)
```

**`order`'s first positional argument is `account`** — this matches the real
backend schema's own declared field order for `place_order` (verify by reading
`backend/config/grammars/agent_grammar.yaml`'s `place_order` entry — `account`
is declared before `symbol`). The rule is general, not order-specific: "first
positional" for ANY token is simply whichever key its `params_schema` declares
first — no CLI-only override field needed anywhere.

### Formal grammar (implement this EXACTLY — this is not a sketch)

```ebnf
statement      := agent_stmt | order_stmt
               (* dispatch: if the first token is the reserved word WHEN,
                  parse as agent_stmt; otherwise order_stmt. Unambiguous
                  since WHEN is reserved and can never be a real token name. *)

agent_stmt     := "WHEN" condition "ALERT" alert_clause "DO" do_clause
alert_clause   := "nop" | call_list
do_clause      := "nop" | call_list
call_list      := call ("," call)*

order_stmt     := call ("," call)*
               (* restricted to order/place_order calls ONLY — any other
                  action name here is a parse error: "only order(...) can
                  be used standalone — wrap other actions in a WHEN ... DO
                  ... agent", since no standalone endpoint exists for a
                  bare non-order action *)

condition      := "always"                    (* standalone only *)
                 | or_expr
or_expr        := term (("&" | "|") term)*     (* EQUAL precedence, strictly
                                                   left-to-right; parens are
                                                   the only way to force a
                                                   different grouping — do
                                                   NOT implement AND-before-OR
                                                   precedence, that was
                                                   explicitly rejected *)
term           := "~" primary | primary
primary        := "(" or_expr ")" | leaf
leaf           := metric_ref comparator value
                 | value comparator metric_ref comparator value
               (* "between" form. Disambiguate by the FIRST token: a literal
                  (NUMBER/STRING/"[") means try the between-form; a NAME
                  means try the normal metric-first form. No backtracking
                  needed. Both comparators must be the SAME direction. *)
metric_ref     := call "@" scope_ref
scope_ref      := call ("." call)*             (* e.g. positions.total; each
                  segment MAY be a call per the uniform bare-or-call rule,
                  though no real parameterized scope exists in the catalog
                  yet — support the production anyway, don't special-case
                  bare-only *)
comparator     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in"
value          := NUMBER | STRING | NAME | list_literal
               (* NAME = bare enum constant or true/false literal, case-
                  insensitive, validated against that field's enum set.
                  STRING (quoted) = free text, exact. *)
list_literal   := "[" value ("," value)* "]"

call           := NAME                        (* bare — zero args, ALWAYS *)
                 | NAME "(" arg_list ")"       (* parens only with >=1 arg *)
arg_list       := first_arg ("," kw_arg)*
first_arg      := value | NAME "=" value       (* positional OR keyword *)
kw_arg         := NAME "=" value               (* every arg after the first
                                                   is ALWAYS keyword *)
```

**Lexer rules:**
- Reserved words (checked BEFORE generic identifier classification, never
  treated as a callable `NAME`): `WHEN`, `ALERT`, `DO`, `nop`, `always`, `in`,
  `not`, `true`, `false`.
- Depth/length guard mirroring `backend/api/algo/expr_eval.py`'s own
  `_MAX_EXPR_LEN`/`_MAX_DEPTH` constants (read that file for the exact values
  and philosophy) — a pathologically long/deeply-nested statement must not be
  able to hang the parser via runaway recursion.
- Trailing/leading commas (`order(account,)`, `ALERT , telegram`) are a clean
  parse error.
- A `(` immediately following a `NAME` with NO space is that name's
  call-open-paren; a `(` anywhere else opens a grouped `or_expr`.
- `-` is only ever lexed as part of a numeric literal's sign — no general
  unary-minus on arbitrary sub-expressions.
- Strings accept both `"..."` and `'...'`. Newlines are ordinary whitespace.
- Whitespace is insignificant around every punctuation operator EXCEPT the one
  adjacency rule above.
- Keyword-boundary scanning (finding where `ALERT`/`DO` begin) must be
  TOKEN-level, never a substring search — a quoted value like
  `tag="ALERT_ME"` must never be mistaken for the real keyword.

### Exhaustive semantic validation (do not skip — this is as important as parsing)

Every `call` referenced in a statement must be resolved against the live
catalog AND kind-checked for the clause it appears in: metric-kind only inside
`WHEN`'s `metric_ref` position, channel-kind only inside `ALERT`, action-kind
only inside `DO`/bare order statements. A token that doesn't resolve, or
resolves to the WRONG kind for its position (e.g. a metric written inside
`DO`), must be a clear compile-time error object (not a thrown exception that
crashes a UI later — Sprint 4 will consume whatever error-reporting shape you
design here, so make it structured: at minimum `{message: string, position?:
number}`).

Also validate: numeric comparators require both sides numeric (reject a
string/enum where a number's expected); required params present (not just
type-correct) for every resolved action/metric call; `always` never combined
with anything else.

## Agents

- frontend: |
    Implement everything below. This is ALL client-side JavaScript — no backend
    calls except fetching the token catalog (read-only). No UI component yet
    (that's Sprint 4) — build and test this as pure logic modules.

    **Step 1 — read before writing anything:**
    - `frontend/src/lib/command/engine.js` in full (342 lines) — understand
      `tokenize()`, `tokenAtCursor()`, `_alignPositionalSpecs()`, `suggestAt()`,
      `_buildCtx()`, `parse()`, `applySuggestion()`.
    - `frontend/src/lib/command/grammars/orders.js` in full (684 lines) — the
      existing hand-written grammar, to understand the `{verbs: {<verb>:
      {tokens: [...], kwargs: {...}}}}` shape `engine.js` expects, and how
      `$ref`-based param specs resolve from `order_fields.yaml`.
    - `backend/api/algo/expr_eval.py` — for the depth/length guard philosophy
      to mirror (just read it, don't modify it — that's Sprint 1, already
      shipped).
    - `backend/config/grammars/agent_grammar.yaml` — the real token catalog
      shape (`params_schema` as a flat `{param_name: {type, required, enum,
      default, token_ref_ok, description}}` dict — confirm `place_order`'s
      exact declared field order, and look at 2-3 metric tokens with
      `params_schema` like `mean_pnl` to confirm their shape too).
    - `frontend/src/lib/api.js:372-374` — `fetchGrammarTokens(grammar)`, GET
      `/admin/grammar/tokens?grammar=<kind>` (kinds: `'condition'`, `'notify'`,
      `'action'`), returns rows shaped like `backend/api/routes/grammar.py`'s
      `_to_out()`: `{id, grammar_kind, token_kind, token, value_type, units,
      description, resolver, params_schema, enum_values, template_body,
      is_system, is_active}`. Already reachable with the `view_agents_catalog`
      capability every automation-page operator already has — no new
      permission concern.

    **Step 2 — `engine.js` modifications (3, each backward-compatible):**
    1. First-positional-as-keyword-too: `parse()`'s required-check currently
       only checks `spec.role in args` (positional hits). Add
       `|| spec.role in kwargs` as an alternate satisfaction path, and when
       satisfied only via `kwargs`, also copy that value into `args[spec.role]`
       (applying `spec.parse` if present) so every downstream consumer
       (resolve hooks, suggestion context building) sees it consistently
       either way. Verify this causes ZERO behavior change for `orders.js` by
       running its existing tests before and after.
    2. A call-style tokenization mode: `tokenize()` today is purely
       whitespace-delimited with quote-awareness and `key=value` kwarg
       splitting. Add a new mode (new exported function, e.g.
       `tokenizeCallStyle()`, or a mode flag — your call on the exact API
       shape, but it must NOT change `tokenize()`'s existing behavior/
       signature, since `orders.js` depends on it unchanged) that also
       recognizes `(`, `)`, and `,` as structural delimiters, so
       `order(account, symbol="NIFTY25JULFUT")` tokenizes into a verb name
       plus a real argument list, not one opaque blob.
    3. Fuzzy-subsequence suggestion ranking: today's filtering in `suggestAt()`
       is plain `.startsWith()`. Add a fuzzy-subsequence matcher (typed
       characters must appear in order in the candidate, not necessarily
       contiguously — command-palette style, e.g. typing `mnpnl` matches
       `mean_pnl`) as an additional ranking function, exported separately so
       the new `agents.js` grammar can opt into it without forcing it onto
       `orders.js` (verify `orders.js`'s existing suggestion tests still pass
       with your changes — don't change its default behavior).

    Write Vitest coverage for all three BEFORE moving on, proving `orders.js`'s
    existing test suite is unaffected (run it, don't assume).

    **Step 3 — `frontend/src/lib/command/grammars/agents.js` (new file):**
    Build this in layers; each layer should have its own tests before moving
    to the next.

    a. **Catalog fetch + generic grammar builder** — fetch all three kinds via
       `fetchGrammarTokens`, build a lookup structure keyed by token name →
       `{grammar_kind, token_kind, value_type, params_schema, enum_values}`.
       For any token whose `params_schema` is non-empty, the FIRST key
       (object key order, matching the schema's own declared order) is the
       positional-or-keyword slot; every other key is keyword-only. Include a
       small, presentation-only shortened-keyword-name alias table (e.g.
       `{account: 'acct', symbol: 'sym', exchange: 'exch', order_type:
       'otype', price: 'px', product: 'prod', variety: 'var'}` — do NOT
       rename `chase_level`'s existing `chase` alias if you find one already
       established in `orders.yaml`/`order_fields.yaml`, reuse it verbatim)
       that the parser accepts as an alternate spelling for a kwarg name, and
       the compiler maps back to the real schema key — the real backend
       schema keys are never renamed, this is purely a CLI-typing layer.

    b. **Lexer** — reserved-word table, call-style tokenization (reusing
       engine.js's new mode from Step 2), the adjacency-based call-vs-grouping
       paren rule, case-insensitive matching for unquoted identifiers,
       case-exact for quoted strings, depth/length guards. Export this as its
       own testable unit.

    c. **Recursive-descent parser** — one function per EBNF production above:
       `parseStatement`, `parseAgentStmt`, `parseOrderStmt`, `parseCondition`,
       `parseOrExpr`, `parseTerm`, `parsePrimary`, `parseLeaf`,
       `parseMetricRef`, `parseScopeRef`, `parseValue`, `parseListLiteral`,
       `parseCall`, `parseArgList`. Produces an AST (your own internal shape —
       document it with a comment block, since Sprint 3's decompiler and
       Sprint 4's live-preview will need to understand it) — does NOT produce
       the final JSON yet, that's the compiler (step d).

    d. **Compiler** — AST → `{conditions, events, actions}` for an
       `agent_stmt` (matching `AgentCreateRequest`/`AgentUpdateRequest`'s
       `conditions`/`events`/`actions` fields exactly — check
       `backend/api/routes/agents.py`'s `AgentCreateRequest` struct for the
       precise shapes: `conditions` is the nested `{"all"/"any"/"not":...}`
       / leaf-dict tree, `events` is `[{"channel": "...", "enabled": true}]`
       (add any `priority` key here too, if you implement the `ntfy(priority=
       ...)` channel-param case — check whether `ntfy` actually has a
       `params_schema` with a `priority` key in the real catalog; if it
       doesn't exist yet, support the MECHANISM generically (channels CAN
       have params, compiled into extra keys on their events-row dict) without
       hardcoding `ntfy` specifically), `actions` is
       `[{"type": "<action_type>", "params": {...}}]`. For a bare
       `order_stmt`, compile to whatever request shape
       `frontend/src/lib/order/orderTicketSubmit.js` (or wherever the real
       ticket-submission payload gets built client-side today — find it) or
       the basket equivalent already expects — reuse that shape, don't invent
       a new one. Apply: positional-only normalization for metric/scope call
       strings, enum/boolean case normalization to canonical catalog casing,
       `lots→qty` conversion (find and reuse the existing lot_size lookup
       mechanism, don't write a second one), keyword-alias-to-real-key
       mapping from step (a), and the `order`→`place_order` action-type
       rewrite (the alias is CLI-only; `"place_order"` is what's written to
       `actions[].type`).

    e. **Semantic validator** — the exhaustive per-token kind-check,
       required-param-presence check, numeric-comparator type-check,
       `always`-standalone check, both-ALERT/DO-nop-together check (reject at
       this layer — the backend's own `_age_validate_action_entries`/
       `_age_validate_threshold_conditions` from Sprint 1 are a second,
       independent backstop, not a replacement for this one). Return a
       structured list of errors (not throw), consumed by both your own tests
       and, eventually, Sprint 4's UI.

    **Step 4 — exhaustive tests.** For EVERY example in the grammar spec above
    (there are 8 numbered examples in the design doc — ask me to paste them
    again if you don't have them, or they're visible in the `## Examples`
    section of `~/.claude/plans/purrfect-marinating-pixel.md`), write a Vitest
    test asserting: parses without error, compiles to the expected JSON shape
    (write out the expected JSON explicitly in the test, don't just assert
    "no error"). Then write adversarial tests for EVERY one of these (one test
    each, at minimum):
    - Unknown token name (metric, action, AND channel — three separate tests)
    - A metric token written inside `DO`, and an action token written inside
      `WHEN` (kind-mismatch rejection)
    - A keyword argument typo (e.g. `symbl=` instead of `symbol=`)
    - A missing required param
    - `ALERT nop DO nop` together (should be flagged — decide whether this is
      a PARSE-time rejection or left for the semantic validator; document
      your choice in a comment, either is acceptable here as long as it's
      caught somewhere before compilation completes)
    - Mixed-direction between-chain (`0 <= x >= 5`)
    - `always` combined with `&`/`|`
    - Trailing/leading comma
    - `nop()` / any zero-arg token written with parens
    - Case-insensitivity (`SELL` vs `sell` vs `Sell` all equivalent; a quoted
      string's case is NOT touched)
    - The `mean_pnl(minutes=30)` → compiles to `"mean_pnl(30)"` positional-only
      normalization specifically — this one is easy to get wrong silently, so
      assert the EXACT compiled string, not just "no error"
    - A deeply nested/very long statement hitting the depth/length guard
    - First-arg-as-keyword for `order` (e.g. `order(account="ZG0790",
      symbol="NIFTY25JULFUT", side="SELL", lots=1)` — all-keyword form must
      also work, not just the positional-first form)
    - Bare multiple `order(...)` statements compiling to a basket-shaped
      request, vs. a single bare `order(...)` compiling to a single-ticket
      shape

    When done, run `npx vitest run` and report the full pass/fail count,
    including confirming `orders.js`'s pre-existing tests still pass
    unchanged. Also run `npx svelte-check --output machine` even though you
    shouldn't have touched any `.svelte` file this sprint, just as a sanity
    check nothing broke.

## Tests
- pytest: no (no backend files touched this sprint)
- svelte-check: yes (sanity check only — no `.svelte` files expected to change)
- playwright: no

## Commit message
feat(automation): agent CLI grammar — Sprint 2, core parser/compiler (no UI)

Sprint 2 of 5 (full design in ~/.claude/plans/purrfect-marinating-pixel.md).
Client-side only, no UI yet (Sprint 4). Extends the existing order-entry
command engine (engine.js) with 3 backward-compatible modifications
(first-positional-as-keyword, call-style tokenization, fuzzy suggestion
ranking), and adds agents.js: a schema-driven grammar builder (not
hand-written per-token, unlike orders.js — pulls the live grammar_tokens
catalog), a recursive-descent parser for the full WHEN/ALERT/DO boolean-tree
grammar, a compiler to the exact JSON shape the existing agent/ticket/basket
endpoints already accept, and an exhaustive semantic validator.

## Done when
- Every example in the grammar's design doc parses and compiles to the exact
  expected JSON.
- Every adversarial case listed in the test requirements is rejected with a
  clear, structured error — not a thrown/uncaught exception.
- `orders.js`'s full pre-existing test suite passes unchanged.
- Metric/scope call-syntax always compiles positional-only in the output,
  regardless of how it was typed (keyword or positional) in the CLI text.
- Full Vitest suite green; svelte-check 0 new errors.
