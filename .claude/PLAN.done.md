# Plan: Apply agDirCellText to all numeric columns in NavBreakdown Margin (M) slot

## Task
In `NavBreakdown.svelte` `_mCols` (Margin slot, lines 372–387), change all four numeric columns from `cellClass: 'ag-right-aligned-cell'` to `cellClass: agDirCellText` so they follow the same green/amber/slate color scheme as Capital and Equity grids:
- `usedMargin` (Used)
- `availMargin` (Avail)
- `totalMargin` (Total)
- `utilPct` (Util %) — change formatter stays `agPctFmt`

## Agents
- frontend: In `frontend/src/lib/NavBreakdown.svelte` `_mCols` block, replace `cellClass: 'ag-right-aligned-cell'` with `cellClass: agDirCellText` for usedMargin, availMargin, totalMargin, and utilPct columns.
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
fix(NavBreakdown): apply agDirCellText to all Margin slot columns for consistent color scheme

## Done when
Margin tab in NavBreakdown (dashboard + NavStrip popup): all four numeric columns (Used, Avail, Total, Util%) show green/amber/slate coloring by value sign.
