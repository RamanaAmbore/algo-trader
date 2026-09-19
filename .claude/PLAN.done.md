# Plan: Apply directional color scheme to all numeric columns in Capital/Equity NavBreakdown grids + fix Pulse Invested color

## Task
1. In `NavBreakdown.svelte` Capital slot (_cCols): apply `agDirCellText` to `collateral` and `totalCash` (currently plain `ag-right-aligned-cell`)
2. In `NavBreakdown.svelte` Holdings slot (_hCols): apply `agDirCellText` to `value` (currently plain `ag-right-aligned-cell`)
3. In `pulseColumns.js`: change `inv_val` (Invested) cellClass from `${RA} cell-muted` to `${RA}` so Invested matches Value's color treatment

Result: all numeric columns in Capital and Equity (Holdings) NavBreakdown grids use green/amber/slate directional coloring, consistent with NavStrip popup grids. Pulse Invested matches Pulse Value.

## Agents
- frontend: Make the following changes:

  **File 1: `frontend/src/lib/NavBreakdown.svelte`**

  In `_cCols` (Capital slot, around line 389-401):
  - `collateral` column: change `cellClass: 'ag-right-aligned-cell'` → `cellClass: agDirCellText`
  - `totalCash` column: change `cellClass: 'ag-right-aligned-cell'` → `cellClass: agDirCellText`

  In `_hCols` (Holdings slot, around line 403-415):
  - `value` column: change `cellClass: 'ag-right-aligned-cell'` → `cellClass: agDirCellText`

  **File 2: `frontend/src/lib/data/pulseColumns.js`**

  Around line 621-625, for the `inv_val` column:
  Change `cellClass: \`${RA} cell-muted\`` → `cellClass: RA`

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
fix(NavBreakdown, pulseColumns): apply agDirCellText to collateral/totalCash/value in Capital+Equity grids; match Invested color to Value in Pulse

## Done when
- Capital grid: liveCash, collateral, totalCash all color-coded green/amber/slate by sign
- Holdings (H) grid: todayMtm, value, lifetime all color-coded green/amber/slate
- Pulse holdings: Invested (inv_val) same color as Value (cur_val) — no dimming
