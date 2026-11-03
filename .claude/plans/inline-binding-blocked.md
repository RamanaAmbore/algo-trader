# Plan: inline `(NAME := rhs)` label binding — one generic operand mechanism, shared by WHEN, DO, and preamble

## ⚠️ STATUS — BLOCKED, do not /impl yet

Two open points from the operator, neither confirmed into a final design yet:

1. ("with that we don't need to result fields") — may widen this plan's scope to also let `:=`
   capture a `$row` (matched-row column) reference inline, not just a `$given` (metric-arithmetic
   scalar) reference — making the `result.<name>` alias (and the bare-name-matches-exposed-column
   `$row` convention) unnecessary for operators who use `:=` instead. NOT confirmed yet.
2. ("the expression evaluation if it is when, do, preamble should be generic") — confirmed and
   folded into this write-up below: `(NAME := rhs)` must be ONE shared grammar production and ONE
   shared compile function, invoked identically from WHEN-leaf (metric and value positions), `DO`
   action-param position, and recursively inside another `rhs` itself — never three separately
   hand-wired special cases per clause type. This mirrors an existing pattern already in this
   codebase: `_compileSharedCall` is one compiler used by both preamble and `DO` for exactly this
   reason (see CLAUDE.md's "Unified agent DSL" entry) — operand resolution for `:=` should follow
   the same discipline.

**Do not run `/impl` on this plan until point 1 is resolved** — the scope below (pure `$given`
desugar, metric arithmetic only) is what's confirmed; if the wider `$row`-capturing scope is
confirmed instead, the generic operand-resolver designed below needs to also route to `$row`
resolution (not a rewrite — the whole point of making it one shared function is that widening its
target set later is a local change, not a three-places change), and there's a real question about
whether `result.<name>`/bare-name `$row` resolution should be deprecated or just left as a
redundant-but-harmless alternate spelling once `:=` can do the same thing inline.

The already-completed, unrelated DSL work (where-filter, row-set aggregates, group-derived columns,
the `alert`-lookahead fix, and the where-leaf `metric`→`column` wire-shape bug fix) has been archived
to `.claude/PLAN.done.md` and committed separately — none of that is blocked by this.

---

Operator (2026-11-01): "I want outside preamble into inline within when enclosed in brackets" — a
preamble label assignment (`x = expr WHEN x > 1000 DO close_position(qty=x)`) should be writable
INLINE, inside the WHEN expression's own parens, with the SAME visibility as today's preamble form
(the binding must reach `DO`, not just the rest of the WHEN expression). Confirmed with the operator
this is the "escapes to DO" semantics, not a narrower scoped-to-the-parens form.

## Context

Today, binding a derived scalar for later reuse requires a separate top-level preamble item BEFORE
`WHEN`:
```
x = pnl@positions.total - sod_cash@funds.total
WHEN x > -30000
DO close_position(qty=x)
```
The operator wants the SAME capability without the separate line:
```
WHEN (x := pnl@positions.total - sod_cash@funds.total) > -30000 DO close_position(qty=x)
```

Decided design direction (discussed and agreed with the operator before this addendum): this ships
as a **pure compile-time desugar**, not a new runtime construct. The parser recognizes
`"(" NAME ":=" rhs ")"` anywhere a `metric_ref` (or a value-position `NAME`-reference) is already
legal inside a WHEN leaf, reusing the EXACT SAME `rhs` grammar the existing top-level preamble item
(`NAME "=" rhs`) already parses — no new arithmetic/combination grammar. At compile time, each
`(NAME := rhs)` found while walking the WHEN AST is hoisted into a synthetic preamble item (in
left-to-right encounter order within the statement, appended AFTER any real, explicitly-typed
preamble lines), and the spot where it appeared in the WHEN tree is replaced with the SAME
`{"$given": "NAME"}` marker an explicit preamble binding already produces. **Net effect: the
compiled JSON is byte-identical to writing the equivalent explicit preamble line** — zero engine
changes, zero new evaluation-order semantics, inherits every existing `$given` rule for free
(per-tick scalar resolution before the condition tree walk — never per-row; the restriction already
documented for a bare-NAME label reference in `metric_ref` position applies unchanged here, since
this is the SAME marker, not a new one).

Token choice: `:=`, not `=`. Reusing `=` inline (next to `>`/`==`/etc. comparators) would read
ambiguously as a mistyped `==`; `:=` is visually unambiguous, matching why Python introduced a
distinct token for exactly this embedded-assignment case rather than overloading `=`.

**Open question raised after this was written (see STATUS above)**: should `:=`'s RHS ALSO be
allowed to be a bare per-row exposed-column name (a `$row` target, e.g. `(acct := account_id)`), not
just a `$given`-style metric-arithmetic expression? If so:
- The hoist target is no longer always a preamble item — a `$row`-shaped RHS has no meaning outside a
  per-row-scoped WHEN leaf, so it cannot be hoisted to a preamble (preamble calls run unconditionally,
  outside any match, with no row available — see the existing preamble/no-row-available design note
  this grammar already documents for exactly this reason).
- `_resolveRowRefOrNull`'s existing four compile-time guards (aggregate scope, per-leaf-scope
  consistency, watchlist scope, aggregate metric) would need to apply to the INLINE binding's
  RHS too, not just to a bare NAME used directly in a DO-action param.
- Whether `result.<name>` and the bare-name `$row` convention become redundant, or stay as
  equally-valid alternate spellings, is an explicit decision to make once this is confirmed — not an
  automatic consequence of adding `:=`.

## Approach

**Guiding rule (operator, 2026-11-01): "the expression evaluation [whether it's] when, do,
preamble — should be generic."** Everything below is designed as ONE shared production and ONE
shared compile function, not three per-clause-type special cases. Concretely:

0. **One shared operand production and compile function.** Add a single new grammar alternative,
   `value_or_binding := value | "(" NAME ":=" rhs ")"`, and a single compile function
   (`_compileValueOrBinding` or similar — exact name decided during implementation) that every
   existing "resolve this operand" call site routes through: WHEN-leaf `metric_ref` position,
   WHEN-leaf `value` position, `DO`/preamble action-param values (`_compileGenericActionParams`/
   `_compileOrderActionParams`, which already special-case a bare-NAME `$row`/`$given` check — this
   new alternative slots into the SAME check, not a new one beside it), and recursively inside
   another `rhs` itself (so a preamble item's own RHS, or another inline binding's own RHS, can
   nest one). Do NOT hand-wire `(NAME := rhs)` recognition separately into each of these call
   sites — if a future 5th operand position is ever added to this grammar, it gets `:=` support for
   free by routing through the same function, the same way `_compileSharedCall` already gives
   preamble and `DO` call-compiling parity for free.
1. **Grammar** — the new alternative is `"(" NAME ":=" rhs ")"`, reusing the EXACT SAME `rhs`
   grammar the existing top-level preamble item (`NAME "=" rhs`) already parses — no new
   arithmetic/combination grammar. Do NOT allow it as a freestanding bare leaf with no comparator
   at all — an arithmetic given-expr scalar has no boolean leaf type in this grammar (no
   `type:'boolean'` leaf exists anywhere), so `(x := rhs)` alone, uncompared, in WHEN-leaf position,
   must be a specific compile-time error, not a silent no-op or an inferred truthiness check. (A
   `DO`-param or preamble-rhs position has no such restriction — `close_position(qty=(x := ...))`
   is a plain value substitution, not a boolean leaf, so no comparator is expected there at all.)
2. **Hoisting** — walk the FULL parsed statement AST left-to-right (WHEN tree, then `DO` calls, in
   source order — not just the WHEN tree, now that `DO` can also carry an inline binding), collect
   every `(NAME := rhs)` node in encounter order, and synthesize a preamble item for each (same AST
   shape the existing `NAME "=" rhs` preamble-item parse already produces) appended after any real
   preamble items already present in the statement. This ordering lets a LATER inline binding's
   `rhs` reference an EARLIER one by name (bare-NAME resolution against `state.bindings`, exactly
   like today's preamble-item sequencing) — confirm this is the ordering
   `compilePreambleClause`/`state.bindings` already expects before wiring it in, don't invent a new
   binding-visibility rule. A binding written inline inside `DO` is visible only to OTHER `DO`-param
   values after it in the same statement (never to WHEN, which is evaluated first and compiled
   first) — document this directionality explicitly, since it's the one place "generic" does NOT
   mean "identical in every direction."
3. **Collision rules** — a `:=`-bound NAME follows the SAME `label_reserved`/`reserved_word`
   rejection every top-level preamble `NAME = rhs` item already enforces (reuse the existing
   check, don't duplicate it). Also reject a NAME that collides with an already-bound label (either
   an explicit preamble item OR an earlier inline `:=` anywhere in the same statement, WHEN or `DO`)
   — a silent last-write-wins shadow would be a correctness hazard for an inline binding more than
   an explicit one, since it's easy to miss a collision when the binding is buried inside a
   condition or a call argument rather than sitting on its own line. Whatever precedence rule
   already governs "a bare NAME that happens to match BOTH a real catalog metric/column token AND a
   bound label" for the top-level preamble form must be preserved unchanged here — do not invent a
   different one for the inline form.
4. **Decompiler** — the compiled JSON for an inline-desugared binding is INDISTINGUISHABLE from one
   written as an explicit preamble line (same preamble-array + `$given`-marker shape), REGARDLESS
   of whether it was written inline in WHEN or inline in `DO` — one decompile rule, not two. The
   decompiler therefore ALWAYS renders it back as a separate preamble line, never reconstructs
   either inline spelling. This is a deliberate normalization, consistent with this grammar's
   existing policy elsewhere (e.g. a flattened 3+-element `all`/`any` chain normalizing to whatever
   shape the left-associative compiler naturally produces on recompile) — compile → decompile →
   recompile stays semantically identical, NOT textually identical. Document this explicitly; do
   not treat it as a bug to fix.
5. **Lookahead/suggestions** — `(` immediately after ANY operand position that now accepts
   `value_or_binding` (WHEN metric-ref, WHEN value, `DO`-param value) should offer `NAME :=` as a
   visible possibility, via the SAME suggestion branch for all three — not a WHEN-only fix that
   leaves `DO` silently unsupported, mirroring exactly the gap the `alert`-in-`DO`-lookahead
   addendum (`.claude/PLAN.done.md`) found and fixed for a different construct. At minimum, typing
   `(` in any of these three positions must not break suggestion lookahead.

## Files

- `frontend/src/lib/command/grammars/agents.js` — ONE new shared `value_or_binding` production +
  compile function for `(NAME := rhs)`, wired into WHEN-leaf metric_ref/value positions AND
  `DO`/preamble action-param value resolution (the existing bare-NAME `$row`/`$given` check inside
  `_compileGenericActionParams`/`_compileOrderActionParams` — the new alternative joins that check,
  not a separate one); the hoist-to-preamble compile step (walks WHEN then `DO`, source order);
  collision checks; lookahead wiring shared across all three positions.
- `frontend/src/lib/__tests__/agentsGrammar.test.js` — compile tests: basic inline binding compiles
  to the same JSON as the equivalent explicit-preamble form (byte-for-byte `toEqual` comparison, not
  just "compiles ok"), for BOTH a WHEN-leaf binding and a `DO`-param binding; visibility in later
  WHEN leaves; visibility in `DO`; a `DO`-inline binding is NOT visible back in WHEN (directionality
  test); multiple inline bindings (mixed WHEN + `DO`) referencing each other in encounter order;
  collision rejection (reserved word, label_reserved, duplicate name against both an explicit
  preamble item and another inline binding anywhere in the statement); the no-comparator
  freestanding-leaf rejection (WHEN only — a `DO`-param binding has no such restriction, test that
  distinction explicitly); decompile-normalizes-to-preamble-line round-trip test for both positions.
- `frontend/src/lib/__tests__/agentsSuggest.test.js` — lookahead test for the `(` position, in all
  three operand positions (WHEN metric, WHEN value, `DO` param).
- `docs/specs/AGENTS_GRAMMAR_EBNF.md` — new grammar alternative under `metric_ref`/`value` (mirror
  this file's existing dense cross-referencing style — file:line links, "Resolved"/"Known gap"
  framing); a short prose paragraph under "Prose — how the pieces combine" explaining the
  desugar-to-preamble mechanism and the decompile-normalization behavior; add the decompile
  normalization as an explicit, deliberate note (not a "Known gap" — it's intended behavior).
- `docs/guides/AGENTS_GUIDE.md` — check whether this operator-facing guide documents the
  preamble-label-assignment syntax; if so, add a short mention of the inline form as a shorthand for
  the same thing, cross-referencing the "escapes to DO exactly like preamble" rule so operators don't
  assume it's scoped narrower than it is.
- No backend changes. This is 100% a frontend compile-time transform — the compiled JSON reaching
  the backend is unchanged in shape, so `backend/api/algo/*` and `backend/config/grammars/*.yaml`
  are untouched.

## Agents

- frontend: grammar + compiler + hoist logic + collision checks + lookahead, with its own tests
  (per the standing test-gate rule).
- doc: EBNF + AGENTS_GUIDE.md sync, dispatched only after the frontend implementation lands (so the
  doc agent documents the REAL shipped syntax/precedence rules, not a guessed one — same sequencing
  the prior, now-archived DSL plan used).

## Tests

- pytest: no
- svelte-check: yes
- playwright: no (pure compile/decompile logic — Vitest coverage via agentsGrammar.test.js is the
  right layer; no live-UI behavior changes beyond lookahead suggestions, already covered by
  agentsSuggest.test.js)

## Commit message

feat(agents): inline `(NAME := rhs)` label binding — one generic operand mechanism shared by WHEN,
DO, and preamble; desugars to an equivalent preamble item at compile time, zero engine changes

## Done when

- `(x := rhs)` compiles, in the WHEN metric-position, WHEN value-position, AND `DO`-param-value
  position, to JSON byte-identical to the equivalent explicit `x = rhs` preamble form — via ONE
  shared compile function, not three.
- The bound name is visible to later WHEN leaves AND to `DO`, matching preamble visibility exactly.
  A binding written inline inside `DO` is visible only to later `DO`-param values, never back into
  WHEN (WHEN compiles first) — tested explicitly, not just assumed.
- Multiple inline bindings in one statement (mixed WHEN + `DO`) can reference each other in
  left-to-right encounter order.
- Collision with a reserved word, a `label_reserved` word, or an already-bound name (preamble or
  another inline binding anywhere in the statement) is a clear compile-time error, never a silent
  shadow.
- A bare, uncompared `(x := rhs)` with no surrounding comparator is a compile-time error in WHEN-leaf
  position; the same shape in a `DO`-param position is simply a value substitution, not an error.
- Decompiling an inline-bound agent always renders the binding as a separate preamble line,
  regardless of which of the three positions it was written in — this is asserted by a test, not
  left as an assumption.
- `svelte-check` clean; full Vitest suite green; no backend file touched.
