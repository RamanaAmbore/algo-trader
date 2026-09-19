# Plan: Legs symbol format sync + LTP tint removal + payoff labels

## Context
Three small fixes:
1. Legs symbol cell has `font-weight: 600` on the sym-main text (making it bold) and `0.3rem` of horizontal padding on the row container — positions ag-Grid uses normal weight and `4px` per-cell horizontal padding. User wants them in sync.
2. Legs LTP cell applies `ltp-vs-avg-up` / `ltp-vs-avg-down` background tints (green/red when LTP > avg or < avg). Positions ag-Grid LTP column has no background tint — only text color via `ltpDayClass`. Remove the tint.
3. Payoff overlay legend labels are currently "Day P&L" and "Exp Val". User wants "P&L" and "Exp P&L".

## Task
Three targeted changes across two files.

## Agents
- frontend: All changes below
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

### Change 1 — Symbol cell: reduce horizontal padding + sync font weight

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

a) In `.cand-row` (around line 419), change:
   - `padding: 0 0.3rem` → `padding: 0` (remove horizontal container padding; move to cells)

b) In `.cand-row > span` (the rule added recently with vertical padding), add horizontal padding matching ag-Grid:
   - Add `padding-left: 4px; padding-right: 4px;`

c) In `:global(.cand-sym .sym-main)` (around line 536), change:
   - `font-weight: 600` → `font-weight: 500`  
   (Lighter than the current bold; keeps readability above normal weight while aligning closer to positions.)

### Change 2 — Remove LTP background tint

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

In the LTP span template (around line 327), the class string contains:
```
{typeof ltp === 'number' && typeof cost === 'number' && cost > 0
  ? (ltp > cost ? 'ltp-vs-avg-up' : ltp < cost ? 'ltp-vs-avg-down' : 'ltp-vs-avg-flat')
  : ''}
```
Remove this entire ternary block from the class string. Keep the `ltpDayClass(...)` and `{flash.classOf(...)}` class bindings — only remove the `ltp-vs-avg-*` block.

The `.ltp-vs-avg-up` and `.ltp-vs-avg-down` CSS rules (around lines 606–607) can remain in the style block (dead but harmless) or be removed — remove them to keep the file clean.

### Change 3 — Payoff legend labels

**File: `frontend/src/lib/OptionsPayoff.svelte`**

- Line ~1269: `Day P&L` → `P&L`
- Line ~1284: `Exp Val` → `Exp P&L`

---

After edits, run `cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1` and fix any errors.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): legs symbol weight/padding sync with positions, remove LTP tint, fix payoff labels

## Done when
- Legs symbol text weight 500 (lighter, closer to positions)
- All legs cells have 4px left/right padding (consistent with ag-Grid cell model)
- LTP cell in legs has no background tint — text color only (matching positions)
- Payoff overlay legend shows "P&L" and "Exp P&L"
- svelte-check 0 errors
