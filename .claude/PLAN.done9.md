# Plan: Sparkline column — symmetric left+right borders in pulse grids

## Context
The sparkline (5d chart) column in MarketPulse pulse grids has a `border-right` separator
restored as an exception to the global border-strip rule, but no `border-left`. The sym
column to its left also has no right border (stripped by the same global rule), so the
left edge of the sparkline column shows no visual separator. The right edge shows the
amber separator. This is asymmetric and appears inconsistent across grids when the
sym column's background tint ends and the bare sparkline starts.

**Root cause**: `MarketPulse.svelte` line 5034-5036:
```css
:global(.mp-bucket-wrap .ag-theme-algo .ag-cell.spark-cell) {
  border-right: 1px solid var(--algo-amber-border-soft) !important;
  /* border-left missing */
}
```

The global strip (lines 5027-5031) zeroes both `border-right` and `border-left` on ALL
cells, then only spark-cell's right side is restored. Left side is never restored.

## Implementation

**File**: `frontend/src/lib/MarketPulse.svelte` line 5034-5036

Change:
```css
:global(.mp-bucket-wrap .ag-theme-algo .ag-cell.spark-cell) {
  border-right: 1px solid var(--algo-amber-border-soft) !important;
}
```

To:
```css
:global(.mp-bucket-wrap .ag-theme-algo .ag-cell.spark-cell) {
  border-right: 1px solid var(--algo-amber-border-soft) !important;
  border-left:  1px solid var(--algo-amber-border-soft) !important;
}
```

Both sides use `var(--algo-amber-border-soft)` at `1px` — matching the existing right
border. Applies identically to all 6 grids (Pinned, Watchlist, Winners, Losers,
Positions, Holdings) since they all share `.mp-bucket-wrap .ag-theme-algo`.

## Also applies to header

The spark column header (`ag-header-cell-spark`) gets the same global strip. Add a
matching rule to restore left+right borders on the header cell too so the column
separator is visible in the header row:

```css
:global(.mp-bucket-wrap .ag-theme-algo .ag-header-cell.ag-header-cell-spark) {
  border-right: 1px solid var(--algo-amber-border-soft) !important;
  border-left:  1px solid var(--algo-amber-border-soft) !important;
}
```

## Tests
- svelte-check must exit 0 errors
- vitest run must pass (no logic change, CSS only)

## Agents
- frontend: apply the two CSS rule changes above to MarketPulse.svelte lines 5034-5036 and add the header rule after it
- backend: skip
- backend-test: skip
- playwright: skip

## Commit message
fix(pulse): symmetric left+right amber borders on sparkline column in all pulse grids

## Done when
- Sparkline column has matching amber separators on both left and right edges
- Consistent across Pinned, Watchlist, Winners, Losers, Positions, Holdings grids
- Header row also shows the left+right separator on the sparkline column
- svelte-check passes
