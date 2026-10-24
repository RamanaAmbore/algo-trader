# Plan: Replace the hand-written parse/lex layer with a Chevrotain-generated parser — single source of truth for the grammar

## Context

A documentation-only fix (extracting the agent DSL's EBNF comment into
`docs/specs/AGENTS_GRAMMAR_EBNF.md`) doesn't solve the real problem: it just
relocates the SSOT-drift risk from "comment vs. implementation" to "doc file
vs. implementation" — which is exactly the staleness already found (the
extracted EBNF still describes the pre-unification grammar). The operator's
real ask: the **compiler should be driven from the grammar file**, so there
is only ever one source of truth.

Two investigations grounded this before any design:

**What's actually in `agents.js` today (9,438 lines) — a clean 2-phase split
already exists, just hand-written on both sides:**
- **Parse/lex layer** (~2,252 lines, 24%): every `parse*`/`lex*` function
  takes only `(state)`/`(text)` — confirmed via exhaustive grep, **zero**
  live references to `catalog` anywhere in the parser. Every documented
  "ambiguous" case (bare `call` with no `@`, `col_ref` vs `scope_ref`,
  boolean-metric shorthand) is handled by emitting a generic, untyped AST
  node (`{type:'ref', call}`, `{type:'colref', segments}`) — **not** by
  grammar-level backtracking or lookahead. The ambiguity is resolved by
  catalog *data* in a later pass, not by more tokens. This means a formal
  grammar/parser-generator can represent 100% of this layer with zero
  special accommodation for "deferred resolution" — it's already shaped
  exactly like a parser-generator's output would be.
- **Compile layer** (~34%, 3,243 lines): every `compile*`/`_compile*`/
  `_resolve*` function takes `catalog` explicitly and does the real
  `catalog.metrics.get(...)`/`catalog.scopes.get(...)`/etc. resolution —
  this is genuinely semantic, backend-catalog-dependent logic that **cannot**
  live in a context-free grammar and must stay hand-written regardless of
  tooling.
- **Decompile** (~17%) and **suggest/describe** (~17%, autocomplete/hover)
  are both built on top of the same AST the parser produces today — they'd
  need to keep consuming an AST, not be rewritten, as long as the new
  parser's output AST is shape-compatible with what they already expect.
- **One real structural wrinkle**: `tp_scales_json=[...]`'s value needs a
  *second*, separate mini-lexer (`_lexObjScalesTok`) because call-argument
  values are first split as raw, bracket-unaware substrings by
  `tokenizeCallStyle`, not run through the main token stream. A
  parser-generator assuming one unified lexer feeding one parser doesn't
  accommodate this for free — needs an explicit decision (below).
- Regression-risk baseline: **725 tests across 5 files, 7,765 lines**
  (`agentsGrammar.test.js` 419, `agentsDecompiler.test.js` 128,
  `agentsSuggest.test.js` 86, `describeTokenAt.test.js` 19,
  `consoleDispatch.test.js` 73).

**Tool choice — Chevrotain, not Peggy or Nearley:**
- Peggy's ordered-choice model works for this grammar (the deferred-resolution
  cases aren't real grammatical ambiguity, so a generic-node convention in
  actions covers them fine) — but it's a convention you impose, not a
  first-class feature, and Peggy has no built-in parse/semantics separation.
- Nearley solves genuine structural ambiguity (multiple parses of the same
  tokens) — this grammar doesn't have that; using it would pay real
  performance/complexity cost for a capability this grammar doesn't need.
- **Chevrotain is the architectural match**: it *enforces* a separate
  parse → CST → visitor pipeline (`validateVisitor()` fails loudly at
  startup if the visitor drifts from the grammar) — the exact two-phase
  split this file already has by hand, now enforced by the tool instead of
  by discipline. Its track record backs the autocomplete/hover half too:
  Langium (Eclipse's DSL/LSP framework) is built on Chevrotain specifically
  because this CST/visitor shape supports completion/hover providers
  cleanly — directly relevant since this grammar also drives
  `agentsSuggest.test.js`/`describeTokenAt.test.js`.
- **Honest counter-signal, not ignored**: Metabase went the *opposite*
  direction (Chevrotain → hand-written) once their need for the generator's
  error-recovery/autocomplete machinery went away. The lesson: a
  parser-generator only pays off if you're genuinely using its strengths —
  here, the autocomplete/hover requirement and the SSOT-drift problem both
  argue it will.
- **Realistic estimate: 3–6 weeks of focused work**, not days. Grammar
  authoring itself is the easy part (3–5 days); the real cost is re-wiring
  decompile/autocomplete onto the new tree and differential-testing against
  the hand-written parser across the full suite before trusting a cutover
  on a real-money-adjacent DSL.

## Design — strangler-fig, not a big-bang rewrite

Industry-standard pattern for this exact shape of migration (Microsoft's
Strangler Fig architecture pattern): carve out a clean seam, build the
replacement behind it, run both in parallel on real inputs with zero
user-visible change, cut over only once proven, delete the old code last.
Applied here:

### Phase 1 — build the new parser in isolation, zero production risk

1. **New file**, e.g. `frontend/src/lib/command/grammars/agentsLexer.js` +
   `agentsParser.js` (Chevrotain grammars are plain JS — no separate `.g`/
   `.peg` file format; the JS file(s) *are* the grammar, which is itself
   the point: one artifact, not a grammar spec plus a hand-written parser
   claiming to implement it). This becomes the new single source of truth.
2. **Resolve the `tp_scales_json` wrinkle first, as its own small
   sub-step**: unify call-argument lexing with the main token stream (let
   the one lexer handle `[`/`]`/`{`/`}` inside call arguments too) rather
   than keeping a second embedded mini-lexer — simplifies the end state and
   removes a wart, at the cost of needing to verify nothing currently
   depends on the "raw substring" call-arg behavior. Confirm scope via the
   existing tests for this argument type before changing it.
3. **CST → AST visitor** producing the *exact* existing AST node shapes
   (`{type:'leaf', metric, scope, op, value}`, `{type:'ref', call}`,
   `{type:'colref', segments}`, etc. — the shapes catalogued by the audit
   above) — so `compile*`/`decompile*`/`suggest*`/`describe*` need **zero**
   changes, they just start consuming AST from a different producer.
4. **Differential test harness**: run the FULL existing 725-test corpus
   through both the old hand-written parser and the new Chevrotain-based
   one, diff every AST output, triage every mismatch. This is the real
   proof, not eyeballing the new grammar against the old EBNF.

### Phase 2 — cutover, only once Phase 1's diff is clean

5. Switch `agents.js`'s parse entry points to call the new parser instead of
   the hand-written one. Keep the old hand-written parser functions in the
   codebase but unreferenced for a burn-in period (easy revert if something
   surfaces in production use the differential tests didn't catch).
6. Update `agents.js`'s header comment (currently a pointer to
   `docs/specs/AGENTS_GRAMMAR_EBNF.md`) to instead point at the new
   grammar file(s) as the live source of truth. Mark
   `AGENTS_GRAMMAR_EBNF.md` explicitly as a historical/pre-migration
   snapshot (or delete it) — don't leave a THIRD stale artifact behind.

### Phase 3 — retire the old code, only after a confirmed clean bake

7. Delete the superseded hand-written `parse*`/`lex*` functions once the
   burn-in period confirms no regressions.

## Explicitly out of scope for this plan

- Rewriting `compile*`/`decompile*`/`suggest*`/`describe*` — they stay
  hand-written, consuming whichever parser produces AST; this plan never
  touches them beyond confirming the new parser's output shape matches
  what they already expect.
- Any change to the grammar's actual *syntax* (no new keywords, no changed
  semantics) — this is a pure implementation-swap, not a language change.

## Files

- New: `frontend/src/lib/command/grammars/agentsLexer.js` +
  `agentsParser.js` (or combined, implementer's call) — the Chevrotain
  grammar + CST-to-AST visitor.
- `frontend/src/lib/command/grammars/agents.js` — parse entry points
  re-point to the new parser (Phase 2); old `parse*`/`lex*` functions
  deleted (Phase 3); header comment updated.
- `docs/specs/AGENTS_GRAMMAR_EBNF.md` — marked historical or deleted once
  the new grammar file is the live source.
- New differential-test script/harness (Phase 1, not a permanent test file
  necessarily — implementer's call whether to keep it as ongoing CI
  coverage or a one-time migration-verification tool).

## Agents

Given the 3–6 week scope, this should be executed as its own dedicated,
carefully-sequenced effort — not parallelized against other in-flight work
the way smaller plans this session have been. Recommended sequencing:
- **frontend**: Phase 1 steps 1–4 (the new parser + visitor + differential
  harness) — this is the bulk of the work and should run to a *fully
  green differential diff* before anything else proceeds.
- **frontend** (same or second pass): Phase 2 cutover, only after Phase 1's
  diff is clean — do not parallelize the cutover with Phase 1's own
  development.
- **doc**: Phase 2 step 6 (header comment + `AGENTS_GRAMMAR_EBNF.md`
  disposition).

## Tests

- vitest: yes — the full existing 725-test corpus must pass unchanged after
  cutover (Phase 2), plus the new differential-harness results from Phase 1
  as supporting evidence.
- svelte-check: yes.
- playwright: the existing agent/order/bracket/alert CLI e2e specs must stay
  green through cutover.

## Commit message (Phase 1, first commit)
`feat(grammar): add a Chevrotain-generated parser for the agent DSL, differentially verified against the existing hand-written parser — not yet wired in`

## Done when

**Phase 1:**
- The new Chevrotain grammar + CST-to-AST visitor exists, producing AST
  shape-identical to the current hand-written parser for every one of the
  725 existing tests.
- A differential test run shows zero AST diffs between old and new parsers
  across the full existing test corpus.

**Phase 2:**
- `agents.js` calls the new parser; the full 725-test suite, svelte-check,
  and the relevant Playwright specs are all green against the new parser
  with no test changes beyond what's mechanically required.
- `agents.js`'s header comment and `AGENTS_GRAMMAR_EBNF.md` both correctly
  point to the new grammar file as the one live source of truth — no
  third stale artifact left behind.

**Phase 3:**
- Old hand-written `parse*`/`lex*` functions removed after a confirmed
  clean burn-in period (length — operator's call, suggest at least one
  full week of normal Console/CLI usage in dev before deleting).

---

## Operator decision needed before `/impl`

This is a genuinely large (3–6 week), real-money-adjacent undertaking, not
a quick follow-up. Given the alerts routing-tree work already in flight
this session, I'd recommend treating this as its own dedicated effort
rather than something to kick off immediately alongside everything else —
but that's a scheduling call, not a technical one. Confirm before
proceeding: start Phase 1 now (isolated, zero production risk either way,
since nothing is wired in until Phase 2), or queue it for later.
