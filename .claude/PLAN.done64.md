# Plan: Agent-editor multi-line formatting + P&L%/Capt% columns

## Context

Two unrelated, independently-shippable requests from the operator, both small/medium frontend-only
changes. The prior plan in this file (Bracket/Template/GTT removal, Track 1-3) is fully shipped and
unrelated — this overwrites it per the "different task, start fresh" rule.

**Task 1** — operator (asked twice): "when you edit an agent, show when, do, and individual statement
on new lines to make them more readable." The Console/Automation agent editor currently decompiles a
saved agent's conditional clause onto a single line: `WHEN <cond> DO <stmt1>, <stmt2>, ...`. Hard to
read once an agent has more than one DO-clause statement.

**Task 2** — operator: "for all surfaces for positions, show p&l % with p&l, also show p&l as a % of
exp p&l, give a proper label for it." Scoped via AskUserQuestion during this session:
- "P&L %" = the existing lifetime P&L% formula (pnl ÷ cost basis × 100), made consistent across both
  target surfaces (Pulse already has it; Legs grid doesn't).
- The new ratio = lifetime P&L ÷ Exp P&L × 100, labeled **"Capt %"** (short for "captured" — how much
  of the theoretical expiry payoff has already been captured), with a header tooltip spelling out the
  full formula.
- Surfaces in scope: **Pulse Positions grid** and **Derivatives Legs grid** only (operator explicitly
  excluded Holdings and summary/TOTAL rows from this round).

Both tasks were explored directly (file:line level) in this session, not via a fresh Explore pass —
no further exploration agents needed before implementation.

---

## Task 1 — Agent-editor multi-line WHEN/DO/statement formatting

**File**: `frontend/src/lib/command/grammars/agents.js`, function `decompileAgent()` (~line 8254-8262).

**Current**:
```js
whenBlock = `WHEN ${condResult.text} DO ${doText}`;
```
where `doText` is `doParts.join(', ')` and `doParts` is `[alert(...)?, actionsResult.text]` —
`actionsResult.text` itself can be a comma-joined string when there are multiple actions
(`decompileActions()` returns `parts.join(', ')`).

**Change**:
1. `decompileActions()` (~line 7906, return at ~line 8024) — additively extend its return to also
   expose the pre-join `parts` array: `return { text: parts.join(', '), errors: [], parts };`. Both
   existing callers (`decompileAgent`, `_decompilePreambleEntries`) and the test file
   (`agentsDecompiler.test.js`) only destructure `.text`/`.errors` today — confirmed via grep, adding a
   field is additive and safe.
2. `decompileAgent()` — build the DO block as one statement per line instead of comma-joined:
   ```js
   const doLines = [];
   if (eventsResult.text !== 'nop') doLines.push(`alert(${eventsResult.text})`);
   if (actionsResult.text !== 'nop') doLines.push(...(actionsResult.parts || [actionsResult.text]));
   const doText = doLines.length ? doLines.join('\n') : 'nop';
   whenBlock = `WHEN ${condResult.text}\nDO\n${doText}`;
   ```
   Apply this unconditionally (not just when there's more than one statement) — for consistency with
   how the preamble block already always renders one call per line (`_decompilePreambleEntries`
   joins with `\n` regardless of count, ~line 8203).

**Why this is safe — confirmed via source reading, not assumed**:
- `parseItemListClause` (~line 2423-2462) is the SAME shared item-list grammar used for both the
  preamble AND the DO clause (`parseAgentStmt`, ~line 2537-2605). It unconditionally accepts a bare
  newline as an item separator (line ~2452: `if (_peek(state).nlBefore && _looksLikeNewListItemStart(...))`)
  — no opt-in flag, always on for this grammar.
- `parseAgentStmt`'s WHEN/DO keyword detection (`_wordIs(_peek(state), 'when'/'do')`) doesn't care
  whether a newline precedes the token — newlines are tracked as a `nlBefore` metadata flag on tokens,
  not as separate skip-rules at the lexer level.
- `decompileAgent()` already re-compiles its own output via `compileAgentCliStatement(text, ...)`
  immediately before returning (~line 8263) and throws a loud internal error if the round-trip fails —
  this existing safety net covers the new multi-line format for free; no new round-trip test is needed
  beyond what already runs.

**Test fixture impact** (confirmed via grep, not estimated): ~55 existing literal-text assertions
across `frontend/src/lib/__tests__/agentsDecompiler.test.js` (42 hits) and
`frontend/src/lib/__tests__/consoleDispatch.test.js` (13 hits) assert on the old single-line
`WHEN ... DO ...` shape and need mechanical updates to the new multi-line shape — this is the
test-coverage "update" this change requires (not new tests; the existing ones need their expected
strings corrected to match the new, intentional format).

---

## Task 2 — P&L % and "Capt %" columns

### 2a. Pulse Positions grid — `frontend/src/lib/data/pulseColumns.js`

- Lifetime P&L% already exists and is correct: `_pnlPctValueGetter` (~line 529) = `pnl / cost_basis *
  100`, column def `pnl_pct` / header "P&L %" (~line 717-720). Verify at implementation time that this
  column is actually visible on the Positions grid instance (not just defined) — no code change
  expected here, just confirmation.
- **New**: add `mkCaptPctCol(getExpPnlForRow, { RA, numericHdr })`, modeled directly on the existing
  `mkExpPnlCol`/`mkExtrinsicCol` factories (~line 850-895) — same `getExpPnlForRow(row).exp_pnl`
  accessor shape, same gating (`...(getExpPnlForRow ? [...] : [])` at ~line 716) so it only appears
  on a grid instance that actually wires Exp P&L in.
  - Formula: `pnl / exp_pnl * 100`. Null-guard: return `null` when `exp_pnl` is null, not finite, or
    below a small epsilon (near-zero Exp P&L — e.g. an ATM position just opened) — same convention
    `_pnlPctValueGetter` already uses for its own cost-basis guard (`!(cost > 0)` → null). Pick the
    epsilon by checking the real scale of Exp P&L values (rupees) at implementation time.
  - Header: "Capt %", with a `headerTooltip` (or this grid's equivalent mechanism) explaining: "Lifetime
    P&L as a % of theoretical Exp P&L — how much of the eventual payoff has been captured so far."

### 2b. Derivatives Legs grid — `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

- `CAND_COLUMN_SPECS` (~line 2749) already has `pnl`, `expPnl`, `dayPnl` as bare-number accessors; no
  `pnlPct`/`captPct` exists yet. Add two new keys:
  - `pnlPct`: reuse the exact same pnl/cost-basis accessor chain the existing `pnl` key already builds
    (`legCost(c, lg)`, `legDisplayQty(c)`) — `(pnl / Math.abs(cost * qty)) * 100`, null when
    `cost * qty` is 0. This matches Pulse's own cost-basis formula (`_cost_basis = avg * qty`,
    confirmed in `pulseUnified.js:607`), so the two surfaces compute the SAME ratio, not two different
    ones that happen to share a name.
  - `captPct`: `(pnl / expPnl) * 100`, same null-guard convention as 2a's `mkCaptPctCol`.
- **Important — `CAND_COLUMN_SPECS` only supplies SORT accessors** (per CLAUDE.md's "Hand-rolled-grid
  column sort SSOT" entry — this is a hand-rolled CSS-subgrid table, `.cand-grid`, not ag-Grid). The
  implementing agent must ALSO locate and update, together, not just the accessor dict:
  1. The header row markup (wherever the column headers for `.cand-grid` are rendered — near the
     `.cand-grid`/`.cand-headrow` CSS classes, ~line 6078+/8187+).
  2. `CandidateLegRow.svelte`'s per-cell rendering (the actual data cells shown per leg row).
  3. The CSS `grid-template-columns` track list that `.cand-grid` and its subgrid children share.
  Adding the two new accessor keys alone would make the columns sortable-but-invisible — a real risk
  this plan flags explicitly so the implementing agent doesn't stop after step 1.

---

## Agents (for `/impl`)

- frontend (task 1): implement the `decompileAgent`/`decompileActions` change above, then mechanically
  update the ~55 affected assertions in `agentsDecompiler.test.js`/`consoleDispatch.test.js` to the new
  multi-line expected text. Self-contained, touches only `agents.js` + its 2 test files.
- frontend (task 2, bundled — both surfaces are one feature): implement 2a (`pulseColumns.js`) and 2b
  (`derivatives/+page.svelte` + `CandidateLegRow.svelte` + the `.cand-grid` CSS) together in one agent,
  per CLAUDE.md's "bundle related files into one agent" convention — these are the same two-column
  feature landing on two surfaces, not two independent changes.
- backend: skip — both tasks are frontend-only.
- doc: skip for task 1 (no doc describes decompile formatting at this granularity). For task 2, add one
  row each to CLAUDE.md's "Common Tasks" table (P&L% / Capt% column additions) — small, non-blocking.
- backend-test: skip.
- playwright: task 2 needs a spec (new or updated) asserting the two new columns render with correct
  values on at least one of the two grids — required by this project's hard test-coverage gate for any
  `.svelte` change.

## Tests

- pytest: no.
- svelte-check: yes (baseline).
- playwright: yes (task 2 — new/updated spec for the two new columns).
- Vitest: task 1's ~55 existing assertions get their expected text corrected (this IS the required test
  update for this change — the existing tests assert on exactly the thing being changed).

## Commit message

Two separate commits (unrelated changes, per "fix one issue at a time"):
- `feat(agents): render WHEN/DO/each statement on separate lines when decompiling an agent for editing`
- `feat(pulse,derivatives): add lifetime P&L% and Capt% (P&L as % of Exp P&L) columns`

## Done when

- Opening any saved agent in the Console/Automation editor shows its conditional clause as
  `WHEN <cond>` / `DO` / one statement per line below — and saving it still round-trips correctly
  (compiles back to the identical conditions/events/actions JSON).
- Pulse Positions grid shows both the existing lifetime P&L% and the new "Capt %" column, correctly
  computed and null-safe.
- Derivatives Legs grid shows a new lifetime P&L% column and a new "Capt %" column, both ACTUALLY
  RENDERING (not just sortable) in the hand-rolled grid.
- Full vitest suite green (including the ~55 corrected assertions), svelte-check 0 errors, a
  playwright spec covering the two new columns passes.
