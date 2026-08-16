# Plan: Fix pos_state column visibility + holdings revert + Lots left separator

## Context

Three regressions from the previous commit:
1. `pos_state` column appeared on **holdings** grid (wrong — positions only)
2. `pos_state` column is invisible — `headerName: ''` empty string + narrow 38px width
3. LTP's left-side `inset box-shadow` separator still on LTP; should move to Lots since Lots is now the first data column

---

## Fix 1 — Remove pos_state from holdings grid

**Root cause**: `mkRightColDefs()` is called once and its result (`rightColDefs`) is shared between `gridPositions` and `gridHoldings` in `MarketPulse.svelte` (~lines 3616 and 3621).

**Fix in `frontend/src/lib/MarketPulse.svelte`**: after `mkRightColDefs()` call, derive a holdings-specific array that excludes the state column:
```js
const holdingsColDefs = rightColDefs.filter(c => c.colId !== 'pos_state');
```
Use `holdingsColDefs` when initialising `gridHoldings`, keep `rightColDefs` for `gridPositions`.

**Also check `frontend/src/lib/PerformancePage.svelte`**: if `holdingsAllGrid` or `holdingsSummaryGrid` share `positionsCols` (which now has `pos_state` prepended), filter it out for those grids too.

---

## Fix 2 — Make pos_state column visible

**Root cause**: `headerName: ''` (empty) and `width: 38` make the column invisible unless the user knows to look for it.

**Fix in `frontend/src/lib/data/pulseColumns.js`** — update the `pos_state` column definition:
- Change `headerName: ''` → `headerName: 'St'`
- Add `headerTooltip: 'Position state: Paired (P1/P2 cyan) · Orphan (○ amber) · GTT (green)'`
- Add `hide: false` explicitly to prevent ag-Grid column-state cache from hiding it

Same fix in `frontend/src/lib/PerformancePage.svelte` where the identical column object is inlined.

---

## Fix 3 — Move left separator from LTP to Lots

**Root cause**: `.ltp-vs-prev-up/down/flat` add `box-shadow: inset 1px 0 0 0 rgba(...)` to every LTP cell. This creates a left visual separator. Lots is now the first data column so the separator should be there.

**Fix in `frontend/src/lib/data/pulseColumns.js`** — Lots column currently has `cellClass: RA`. Change to also add a static separator class **for position rows only** (not holdings, watchlist, etc.):
```js
cellClass: (p) => {
  const d = p.data;
  // separator only on position rows (they have qty_pos); holdings/watch rows skip it
  return d?.qty_pos !== undefined ? [RA, 'lots-left-sep'] : [RA];
},
```

**Fix in `frontend/src/app.css`** — add after `.ltp-vs-prev-flat` rules:
```css
/* Lots column left separator — positions only (matches LTP inset-shadow pattern) */
.ag-theme-algo .ag-cell.lots-left-sep {
  box-shadow: inset 1px 0 0 0 rgba(126,151,184,0.40);
}
.ag-theme-ramboq .ag-cell.lots-left-sep {
  box-shadow: inset 1px 0 0 0 rgba(112,99,76,0.40);
}
```

---

## Files to change

| File | Change |
|---|---|
| `frontend/src/lib/MarketPulse.svelte` | Derive `holdingsColDefs` (filter pos_state); pass it to `gridHoldings` |
| `frontend/src/lib/PerformancePage.svelte` | Filter pos_state from holdings grids; not from positionsAllGrid |
| `frontend/src/lib/data/pulseColumns.js` | `pos_state`: `headerName: 'St'`, `hide: false`; Lots: add `lots-left-sep` cellClass for position rows |
| `frontend/src/app.css` | Add `.lots-left-sep` CSS rules (both themes) |

---

## Agents
- frontend: apply all four file changes above
- backend: skip
- backend-test: skip
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(positions): visible St column header + revert holdings pos_state + Lots left separator

## Done when
- "St" column header visible as first column in positions grid only (not holdings)
- Holdings grid has no St/pos_state column
- Lots column shows left inset-shadow separator on position rows
- svelte-check 0 errors
