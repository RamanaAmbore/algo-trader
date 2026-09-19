# Plan: SSOT NavBreakdown — ag-Grid + shared grid utils + Dashboard Capital/Equity replacement

## Context
The dashboard NAV/Capital/Equity card currently has three distinct implementations
of the same data: NavBreakdown HTML tables (NAV tab), and separate ag-Grid instances
for Capital (margin + funds) and Equity (positions + holdings summary). The NavStrip
popup also uses NavBreakdown HTML tables. The user wants:
1. Visual consistency across all algo-page grids (same style as legs/snapshot/positions)
2. Code reuse — NavBreakdown as the SSOT component for all four tabs
3. A shared grid utility module so dashboard, NavBreakdown, and PerformancePage all
   draw their column helpers and base options from one place

PerformancePage.svelte was incorrectly modified in bbfca58f — those changes must be
reverted. NavCard.svelte aggCompact changes stay.

---

## Task
1. Revert `PerformancePage.svelte` to its state at `841a0d0f`.
2. Create `frontend/src/lib/data/algoGridUtils.js` — shared ag-Grid helpers used
   by every algo-theme grid on the site.
3. Convert `NavBreakdown.svelte` HTML tables → ag-Grid using algoGridUtils, enrich
   M slot (usedMargin, utilPct) and C slot (collateral).
4. Replace the Capital and Equity ag-Grid instances in `dashboard/+page.svelte` with
   NavBreakdown slots. Update dashboard to import from algoGridUtils.
5. Remove now-dead column defs, $effect creators, derived data, bind:this vars for
   the four replaced dashboard grids.

---

## Agents
- backend: skip
- frontend: implement all changes across 3 files + new util module
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

---

## File 0: New shared module `frontend/src/lib/data/algoGridUtils.js`

```js
import { priceFmt, aggCompact, pctFmt } from '$lib/format';

/** Right-aligned header class — used by every numeric column. */
export const NUMERIC_HDR = 'ag-right-aligned-header';

/** Price formatter (exact, e.g. 1,23,456.78) */
export const agNumFmt = ({ value }) => value == null ? '—' : priceFmt(value);

/** Compact formatter (K / L / C, e.g. 1.23L) */
export const agAggFmt = ({ value }) => value == null ? '—' : aggCompact(value);

/** Percentage formatter (e.g. 4.56%) */
export const agPctFmt = ({ value }) => value == null ? '—' : `${pctFmt(value)}%`;

/**
 * Direction-coloured numeric cell class.
 * Returns `ag-right-aligned-cell pnl-gain|pnl-loss|pnl-zero`.
 */
export const agDirCell = (p) => {
  const v = p.value ?? 0;
  return `ag-right-aligned-cell ${v > 0 ? 'pnl-gain' : v < 0 ? 'pnl-loss' : 'pnl-zero'}`;
};

/**
 * Base ag-Grid options shared by every algo-theme grid.
 * Pass `getRowId` override when the default (account → symbol) isn't right.
 */
export function mkBaseGridOpts(overrides = {}) {
  return {
    theme: 'legacy',
    defaultColDef: {
      resizable: true, sortable: true,
      suppressMovable: true, suppressHeaderMenuButton: true,
    },
    rowHeight: 26,
    domLayout: 'autoHeight',
    getRowId: ({ data }) => String(data?.account ?? data?.symbol ?? ''),
    getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
    ...overrides,
  };
}
```

This replaces:
- `_numericHdr` / `numericHdr` (inline constants in dashboard + PerformancePage)
- `_agAggFmt` / `_agNumFmt` / `_agPctFmt` (inline arrow functions in dashboard)
- `_baseGridOpts` (inline object in dashboard)

---

## File 1: Revert PerformancePage.svelte

```bash
git checkout 841a0d0f -- frontend/src/lib/PerformancePage.svelte
```

No further edits to PerformancePage.

---

## File 2: `frontend/src/lib/NavBreakdown.svelte`

### Script additions
```js
import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
import { mkBaseGridOpts, NUMERIC_HDR, agAggFmt, agDirCell, agPctFmt } from '$lib/data/algoGridUtils.js';
ModuleRegistry.registerModules([AllCommunityModule]);
```

### Data enrichment — M slot
Extend `_mByAcct` to include `usedMargin` and `utilPct`:
```js
const usedMargin  = Number(f?.used_margin ?? 0);
const totalMargin = availMargin + usedMargin;
const utilPct     = totalMargin > 0 ? (usedMargin / totalMargin) * 100 : 0;
return { account: acct, availMargin, usedMargin, totalMargin, utilPct };
```
Update `_mTotal` to sum `usedMargin`.

### Data enrichment — C slot
Add `collateral` to `_cByAcct`:
```js
const collateral = Number(f?.collateral ?? 0);
return { account: acct, liveCash, collateral, totalCash };
```
Update `_cTotal` to sum `collateral`.

### Four bind:this containers + grid instances
```js
let _pEl, _mEl, _cEl, _hEl;
let _pGrid, _mGrid, _cGrid, _hGrid;
```

### Column defs per slot
**P:** Account(fill+acct) | Day P&L(dir,agAggFmt) | Lifetime(dir,agAggFmt) | Expiry(dir,agAggFmt — null→'—')

**M:** Account(fill+acct) | Used(right,agAggFmt) | Avail(right,agAggFmt) | Total(right,agAggFmt) | Util%(right, agPctFmt)

**C:** Account(fill+acct) | Live Cash(dir,agAggFmt) | Collateral(right,agAggFmt) | Total Cash(right,agAggFmt)

**H:** Account(fill+acct) | Today MTM(dir,agAggFmt) | Value(right,agAggFmt) | Lifetime(dir,agAggFmt)

### Grid creation $effects (one per slot, gate on `activeSlot`)
```js
$effect(() => {
  if (activeSlot !== 'P' || !_pEl || _pGrid) return;
  _pGrid = createGrid(_pEl, { ...mkBaseGridOpts(), columnDefs: _pCols, rowData: [] });
});
// repeat for M, C, H
```

### Row-data update $effects
Each tracks its derived rows + total and calls `setGridOption`:
```js
$effect(() => {
  if (!_pGrid) return;
  _pGrid.setGridOption('rowData', _pByAcct.map(r => ({
    account: r.account, day_pnl: r.dayPnl, lifetime: r.lifetimePnl, expiry: r.expiryPnl,
  })));
  _pGrid.setGridOption('pinnedBottomRowData', [{
    account: 'TOTAL', day_pnl: _pTotal.dayPnl,
    lifetime: _pTotal.lifetimePnl, expiry: _pTotal.expiryPnl,
  }]);
});
// repeat for M, C, H
```

### Template — replace 4 `<table>` blocks with
```svelte
{#if activeSlot === 'P'}<div bind:this={_pEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
{#if activeSlot === 'M'}<div bind:this={_mEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
{#if activeSlot === 'C'}<div bind:this={_cEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
{#if activeSlot === 'H'}<div bind:this={_hEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
```
Keep `nav-bd-caption`, loading/error/empty state divs unchanged.

### CSS — remove all HTML-table rules
Remove: `.nav-bd-table`, `.nav-bd-acct`, `.nav-num`, `.nav-bd-total`, mobile media query
table overrides. Keep: `.nav-bd-wrap`, `.nav-bd-caption`, `.nav-bd-empty`, error/warn/hint
states. Add:
```css
.nav-bd-ag { width: 100%; }
```
The ag-theme-algo CSS (app.css) already provides: dark header bg, amber border, muted
uppercase text, `totals-row` amber tint, `ag-col-fill` account bg, `pnl-gain`/`pnl-loss`
directional colours. No extra scoped CSS needed for grid cells.

---

## File 3: `frontend/src/routes/(algo)/dashboard/+page.svelte`

### Script — import shared utils
```js
import { mkBaseGridOpts, NUMERIC_HDR, agAggFmt, agNumFmt, agPctFmt, agDirCell } from '$lib/data/algoGridUtils.js';
```
Replace inline `_agNumFmt`, `_agAggFmt`, `_agPctFmt`, `_agDirCell`, `_numericHdr`,
`_baseGridOpts` with the imported versions.

### Script — remove dead grid infrastructure
Variables to delete: `_fundsEl`, `_marginEl`, `_eqPosEl`, `_eqHoldEl`,
`_fundsGrid`, `_marginGrid`, `_eqPosGrid`, `_eqHoldGrid`,
`_fundsReady`, `_marginReady`, `_eqPosReady`, `_eqHoldReady`

`$effect` blocks to delete:
- funds grid creator + row-data updater
- margin grid creator + row-data updater
- eqPos grid creator + row-data updater
- eqHold grid creator + row-data updater

Derived state to delete (after verifying not used elsewhere):
`_fundsBody`, `_fundsTotal`, `_marginRows`, `_marginTotal`,
`_positionsSummary`, `_positionsTotal`, `_holdingsSummary`, `_holdingsTotal`

Check `_positionsSummary`/`_holdingsSummary` — if they feed `_positionsCount`/
`_holdingsCount` (used in bucket-subheader labels), keep those count derivations
or re-derive from NavBreakdown slot data. (NavBreakdown exposes no public count.
Simplest fix: keep a lightweight `_posCount = $derived(positionsStore.value?.filter(...).length ?? 0)`.)

Keep: `_eqAccounts` (NAV tab still uses it). Check `mkUtilPctCol` import — remove
if only used by the now-deleted margin grid, keep if used elsewhere.

### Template — Capital tab
Replace:
```svelte
<!-- old: margin grid + funds grid -->
<div bucket-subheader>Margin Utilisation</div>
<div bind:this={_marginEl} class="ag-theme-quartz ag-theme-algo dash-mini-grid" ...></div>
<div bucket-subheader>Funds</div>
<div bind:this={_fundsEl} class="ag-theme-quartz ag-theme-algo dash-mini-grid" ...></div>
<EmptyState ... />
```
With:
```svelte
<NavBreakdown activeSlot="M" />
<NavBreakdown activeSlot="C" />
```

### Template — Equity tab
Replace:
```svelte
<!-- old: eqPos grid + eqHold grid -->
<div bucket-subheader>Positions <span>{_positionsCount}</span></div>
<div bind:this={_eqPosEl} ...></div>
<div bucket-subheader>Holdings <span>{_holdingsCount}</span></div>
<div bind:this={_eqHoldEl} ...></div>
<EmptyState ... />
```
With:
```svelte
<NavBreakdown activeSlot="P" />
<NavBreakdown activeSlot="H" />
```
(Remove bucket-subheader labels — NavBreakdown caption provides slot context.
Or keep them as section labels above each grid if the operator needs the count —
use lightweight `_posCount` and `_holdCount` derived from store lengths.)

---

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes (algoGridUtils.js is a plain JS module — add 3-4 unit tests)
- playwright: no

## Commit message
```
refactor(dashboard): shared algoGridUtils + NavBreakdown ag-Grid + Capital/Equity SSOT

- Add frontend/src/lib/data/algoGridUtils.js — NUMERIC_HDR, agAggFmt, agNumFmt,
  agPctFmt, agDirCell, mkBaseGridOpts — single source for all algo ag-Grid config
- NavBreakdown.svelte: HTML tables → ag-Grid (algo dark theme); enrich M slot
  (usedMargin + utilPct) and C slot (collateral); use algoGridUtils
- dashboard/+page.svelte: Capital→NavBreakdown M+C; Equity→NavBreakdown P+H;
  replace inline grid helpers with algoGridUtils imports; remove 4 dead grid defs
- Revert PerformancePage.svelte to 841a0d0f (visual-polish was on wrong page)
- NavStrip popups inherit new ag-Grid style automatically (shared component)
```

## Done when
- PerformancePage reverted (row tints + AccountMultiSelect restored)
- `algoGridUtils.js` exports NUMERIC_HDR, agNumFmt, agAggFmt, agPctFmt, agDirCell, mkBaseGridOpts
- NavBreakdown renders ag-Grid with dark header/amber border/neutral rows/TOTAL row matching legs visual style
- Dashboard Capital: M grid (Used/Avail/Total/Util%) + C grid (LiveCash/Collateral/Total)
- Dashboard Equity: P grid (DayPnL/Lifetime/Expiry) + H grid (TodayMTM/Value/Lifetime)
- NavStrip popup grid matches dashboard visual (shared component)
- svelte-check 0 errors, vitest all pass
