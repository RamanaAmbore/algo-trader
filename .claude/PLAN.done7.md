# Plan: Text color consistency + legs loading state centering

## Context
Three surfaces show inconsistent text colors:
- ag-Grid (positions/holdings): `var(--algo-slate)` = `#c8d8f0` — canonical
- Snapshot rows: hardcoded `#c8d8f0` literal instead of using the variable
- Legs symbol `sym-main`: hardcoded `#e2e8f0` — visibly brighter/whiter than ag-Grid cells
- Legs data cells (`.cand-row > span`): no explicit color — inherits unpredictably

Rule to enforce: data cell text = `var(--algo-slate)`, headers = `var(--text-muted)`.

Separately: the legs loading state ("Loading candidates…") shares `.cand-empty` with
`align-self: start`, so it sticks to the top. It should be centered both horizontally
and vertically inside the card. The "no candidates" empty state should stay top-aligned.

## Agents
- backend: skip
- frontend: All changes below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

### Change 1 — Legs data cell text color

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

a) In `.cand-row > span` CSS rule, add `color: var(--algo-slate);`

b) In `:global(.cand-sym .sym-main)` (around line 536), change:
   - `color: #e2e8f0` → `color: var(--algo-slate)`
   (CE/PE overrides `var(--c-long)` / `var(--c-short)` stay untouched)

### Change 2 — Snapshot row text color uses variable

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

In `.byund-row > span` CSS rule (around line 5707), change:
- `color: #c8d8f0` → `color: var(--algo-slate)`

### Change 3 — Legs loading state: centered both ways

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

a) In the legs empty/loading template (around line 4568), add a conditional class to
   the `.cand-empty` wrapper so loading gets a different layout from no-candidates:

   Change:
   ```svelte
   <div class="cand-empty">
   ```
   To:
   ```svelte
   <div class="cand-empty" class:cand-loading={loading || !selectedUnderlying}>
   ```

   This way: loading + no-underlying → centered; no-candidates → top-aligned (align-self: start).

b) In the CSS (near `.cand-empty`), add a new rule:
   ```css
   .cand-empty.cand-loading {
     align-self: stretch;
     display: flex;
     align-items: center;
     justify-content: center;
     min-height: 8rem;
   }
   ```
   `align-self: stretch` overrides the `start` from `.cand-empty`.
   `display: flex` + `align-items: center` + `justify-content: center` centers the
   EmptyState component both ways within the available height.

---

After edits, run:
```
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1
```
Fix any errors. Report what changed.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): consistent data cell text color across legs/snapshot, center legs loading state

## Done when
- Legs data cells use `var(--algo-slate)` — visually matches ag-Grid positions rows
- Snapshot rows use `var(--algo-slate)` via variable (not hardcoded literal)
- Legs loading message is centered both horizontally and vertically in the card
- "No candidates" empty state remains top-aligned
- svelte-check 0 errors
