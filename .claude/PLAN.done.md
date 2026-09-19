# Plan: Fix NavBreakdown cell background tints — text-color-only directional cells

## Context
`agDirCell` in algoGridUtils.js returns `pnl-gain`/`pnl-loss` CSS classes. In app.css,
`.ag-theme-algo .pnl-gain` and `.pnl-loss` both carry `background-color` (rgba green/red
8% opacity) in addition to text color. The old NavBreakdown HTML table used `.nav-up`/`.nav-down`
which were text-color-only. The ag-Grid conversion introduced cell-level background tints
on all directional P&L columns. Account column (`ag-col-fill ag-col-acct`) is correct — keep it.

## Task
Add a text-color-only cellStyle variant to algoGridUtils.js and use it on all directional
P&L columns in NavBreakdown. No changes to app.css or other grids (those legitimately use
the background tints in positions/legs/snapshot grids).

## Agents
- frontend: Two-file change:
  1. `frontend/src/lib/data/algoGridUtils.js` — add export `agDirCellText`:
     ```js
     export const agDirCellText = (p) => {
       const v = p.value ?? 0;
       return { color: v > 0 ? 'var(--algo-green)' : v < 0 ? 'var(--algo-red)' : 'var(--algo-dim)' };
     };
     ```
     Used as `cellStyle` (not `cellClass`). Also update the import comment/docstring line.

  2. `frontend/src/lib/NavBreakdown.svelte` — for every directional column, replace
     `cellClass: agDirCell` with `cellClass: 'ag-right-aligned-cell', cellStyle: agDirCellText`.
     Also add `agDirCellText` to the import line (line 27).
     Affected column defs:
       - `_pCols`: `day_pnl`, `lifetime`, `expiry`
       - `_cCols`: `liveCash`
       - `_hCols`: `todayMtm`, `lifetime`
     The account column (`ag-col-fill ag-col-acct`) is NOT touched.

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(NavBreakdown): remove cell background tints from directional P&L columns — use text-color-only cellStyle via agDirCellText

## Done when
- NavBreakdown ag-Grid rows show no green/red cell background tints on directional columns
- Text color (green/red/dim) still applies correctly
- Account column `ag-col-fill` background unchanged
- svelte-check 0 errors
- Same fix visible in NavStrip popup windows (share NavBreakdown component)
