# Plan: Fix Pulse total row Day P&L + NavBreakdown total row styling

## Context

Two related issues in the grids:

1. **Pulse positions total row Day P&L = 0**: The `day_pnl` valueGetter patched onto the
   positions grid column (MarketPulse.svelte ~line 3615) reads `positionsDerivedStore.get(sym).day_pnl`
   for per-symbol rows (correct). For the pinned total row, `sym = ""` (no tradingsymbol),
   so it falls back to `p.data?.day_pnl` = sum of stale broker `day_change_val` values = 0.
   Fix: detect `p.node?.rowPinned` and return `positionsDayPnlStore.total` instead.
   `positionsDayPnlStore` is already imported at line 85.

2. **NavBreakdown total rows look like regular rows**: The P/M/C/H grids in NavBreakdown
   are created with `mkBaseGridOpts()` which has no `getRowClass`. The pinned bottom rows
   have no CSS class applied. MarketPulse grids use `mp-total-row`; PerformancePage uses
   `totals-row` (from app.css). NavBreakdown grids need `getRowClass` returning `'totals-row'`
   for pinned rows (`account === 'TOTAL'`).

## Files to change

| File | Change |
|------|--------|
| `frontend/src/lib/MarketPulse.svelte` | ~line 3615: add `if (p.node?.rowPinned)` branch returning `positionsDayPnlStore.total` |
| `frontend/src/lib/NavBreakdown.svelte` | Add `getRowClass` to each grid's `createGrid()` call (P/M/C/H grids) |

## Detailed changes

### 1. `MarketPulse.svelte` — Pulse positions total row Day P&L

**Find (lines 3613–3619):**
```javascript
rightColDefs[_dayPnlColIdx] = {
  ..._origDayPnlCol,
  valueGetter: p => {
    const sym = String(p.data?.tradingsymbol || '').toUpperCase();
    return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl;
  },
};
```

**Replace with:**
```javascript
rightColDefs[_dayPnlColIdx] = {
  ..._origDayPnlCol,
  valueGetter: p => {
    if (p.node?.rowPinned) return positionsDayPnlStore.total ?? p.data?.day_pnl;
    const sym = String(p.data?.tradingsymbol || '').toUpperCase();
    return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl;
  },
};
```

`positionsDayPnlStore` already imported at line 85.

### 2. `NavBreakdown.svelte` — total row styling

The `createGrid` calls for _pGrid, _mGrid, _cGrid, _hGrid each need a `getRowClass` option.
Check the exact lines where each grid is initialized (around lines 419–431 from the explore).

For each grid creation, add `getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : ''`:

**Pattern (apply to all four P/M/C/H grids):**
```javascript
_pGrid = createGrid(_pEl, {
  ...mkBaseGridOpts(),
  columnDefs: _pCols,
  rowData: [],
  domLayout: 'autoHeight',
  getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : '',
});
```

The `.totals-row` CSS class already exists in `frontend/src/app.css` (lines ~602–618) with
amber background + border styling — the same look as PerformancePage total rows.

**Scope:** Only the four grids that have a TOTAL row (P/M/C/H). Do not touch other grids.

## Agents
- frontend: apply both changes above
- backend-test: skip
- frontend-test: add Vitest test — valueGetter returns `positionsDayPnlStore.total` when `p.node.rowPinned` is set

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes (new test for pinned-row branch)
- playwright: no

## Commit message
fix(MarketPulse,NavBreakdown): positions total row Day P&L from store; apply totals-row class to nav/capital/equity/holdings grids

## Done when
- Pulse positions total row Day P&L shows correct non-zero value
- Nav/capital/equity/holdings grids' total rows have amber background matching legs/pulse style
- svelte-check 0 errors; vitest 0 failures
