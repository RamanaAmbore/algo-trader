# Plan: Fix MarketPulse grid symbol cell vertical alignment — uniform row height

## Context
MarketPulse grids show uneven visual row heights: rows with badge chips (P/H/W/U/M) in
the symbol cell look taller than rows without. Root cause: two compounding CSS issues.

1. `--ag-row-height: 24px` in `.ag-theme-algo` makes ag-Grid derive `line-height: 24px`
   for cells, but `rowHeight: 28` (JS) makes actual rows 28px. Content sits in a 24px
   inline box within a 28px physical row.
2. `.sym-badges` uses `display: inline-flex; vertical-align: middle` with badge chips
   (`line-height: 12px`). The inline-flex + vertical-align: middle interacts with the
   inline line-box differently when badges are present vs absent — causing the visual
   "alternating padding" the operator sees.

Derivatives legs grid (CandidateLegRow) has no issue because it is a hand-rolled CSS
Grid where every cell has `display: flex; align-items: center` explicitly.

## Fix (CSS-only, no JS changes)

Two rules in `frontend/src/app.css`:

### 1 — Match `--ag-row-height` to JS `rowHeight: 28`
In `.ag-theme-algo` CSS variables block (line ~1074), change:
```css
--ag-row-height: 24px;
```
to:
```css
--ag-row-height: 28px;
```
This aligns ag-Grid's internal `line-height` calculation with the actual rendered row
height, eliminating the 4px dead-space mismatch.

### 2 — Make `.ag-col-sym` a flex container
Add a new CSS rule in `app.css` (after the existing `.ag-theme-algo .ag-cell` rule,
around line 1169):
```css
.ag-theme-algo .ag-col-sym {
  display: flex !important;
  align-items: center !important;
  overflow: hidden;
}
```
This makes the symbol cell a proper flex container. Badge chips become flex items;
`vertical-align: middle` on `.sym-badges` has no effect in flex context; all content
(sym-main + badges + alias + action buttons) centers uniformly in the 28px cell
regardless of whether the row has badges. Matches the `display: flex; align-items:
center` pattern used in CandidateLegRow.

## Agents
- backend: skip
- frontend: Edit `frontend/src/app.css`:
  1. In the `.ag-theme-algo` CSS variables block, find `--ag-row-height: 24px` and
     change it to `--ag-row-height: 28px`.
  2. After the `.ag-theme-algo .ag-cell { ... }` rule (around line 1169), add:
     ```css
     .ag-theme-algo .ag-col-sym {
       display: flex !important;
       align-items: center !important;
       overflow: hidden;
     }
     ```
  These are the only two changes. Do not touch any JS files, any other CSS rules, or
  any Svelte files.
  Write/update tests: add a Vitest test in `frontend/src/lib/__tests__/` verifying that
  the ag-col-sym class is expected to be on the symbol column definition (check
  pulseColumns.js exports mkSymColLeft / mkSymColRight have cellClass containing
  'ag-col-sym').
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(pulse): uniform symbol cell height — flex align ag-col-sym; match --ag-row-height to rowHeight:28

## Done when
- MarketPulse grids: all rows visually same height regardless of badge presence
- Symbol cell content vertically centered in every row
- svelte-check 0 errors
