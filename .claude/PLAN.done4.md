# Plan: Legs/snapshot font sync, empty-state fix, P&L tints, payoff labels

## Context
Five UI fixes:
1. Legs grid cell text (`--fs-sm` ≈ 0.6rem) and snapshot grid (`--fs-sm` ≈ 0.625rem) are smaller than positions pulse ag-Grid cells (`--ag-font-size: 0.72rem`). Sync both to 0.72rem.
2. The "No candidates" empty state in the legs card is vertically centered — it should be top-aligned (horizontally centered is fine).
3. P&L, Exp P&L, and Extrinsic columns in legs have green/red/grey background tints (`.cand-pnl.cell-pos/neg/flat`). Positions pulse shows only text color — remove the tints.
4. Payoff overlay stat overlay shows "TODAY" and "EXP" (lines ~789, ~818) and tooltip shows same (lines ~1246, ~1253). Previous plan only fixed legend items. User wants "TODAY" → "P&L" and "EXP" → "Exp P&L" everywhere.

## Agents
- backend: skip
- frontend: All changes below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

### Change 1 — Sync font sizes to 0.72rem

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

In `.cand-row` (around line 419 in `+page.svelte` or in `CandidateLegRow.svelte`), change:
- `font-size: var(--fs-sm)` → `font-size: 0.72rem`

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

In `.byund-grid` (around line 5681), change:
- `font-size: var(--fs-sm)` → `font-size: 0.72rem`

Also check if `.cand-headrow` (the legs header row) has a `font-size: var(--fs-xs)` — if so, change it to `0.65rem` to match ag-Grid header font size (`--ag-header-font-size: 0.65rem`).

### Change 2 — No candidates empty state: remove vertical centering

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

In `.cand-empty` (around line 5645), add `align-self: start`:
```css
.cand-empty {
  ...existing rules...
  align-self: start;
}
```

### Change 3 — Remove P&L / Exp P&L / Extrinsic background tints

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

Remove these three rules (around lines 591-600):
```css
:global(.cand-pnl.cell-pos)  { background-color: rgba(74,222,128,0.08); }
:global(.cand-pnl.cell-neg)  { background-color: rgba(248,113,113,0.08); }
:global(.cand-pnl.cell-flat) { background-color: rgba(148,163,184,0.06); }
```
Keep the `.cand-pnl` base rule (border-radius, padding, font-weight).

### Change 4 — Payoff overlay: TODAY → P&L, EXP → Exp P&L

**File: `frontend/src/lib/OptionsPayoff.svelte`**

Four label changes:
- Line ~789: `<span class="ps-k">TODAY</span>` → `<span class="ps-k">P&L</span>`
- Line ~818: `<span class="ps-k">EXP</span>` → `<span class="ps-k">Exp P&L</span>`
- Line ~1246: `<span class="chart-tooltip-label">TODAY</span>` → `<span class="chart-tooltip-label">P&L</span>`
- Line ~1253: `<span class="chart-tooltip-label">EXP</span>` → `<span class="chart-tooltip-label">Exp P&L</span>`

---

After edits, run `cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1` and fix any errors.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): sync legs/snapshot font size to 0.72rem, remove P&L tints, fix payoff labels

## Done when
- Legs grid cell text renders at 0.72rem (matching positions pulse)
- Snapshot (byund) grid cell text renders at 0.72rem
- Legs header row at 0.65rem (matching ag-Grid headers)
- "No candidates" message top-aligned in legs card
- P&L, Exp P&L, Extrinsic columns in legs show text color only (no background tint)
- Payoff overlay stat overlay and tooltip show "P&L" and "Exp P&L" (not "TODAY"/"EXP")
- svelte-check 0 errors
