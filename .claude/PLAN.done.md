# Plan: Positions 2-level grid grouping + PerformancePage header cleanup

## Context
Two separate UI improvements to PerformancePage and the positions grid:

1. **Positions 2-level grouping** — currently the positions Breakdown grid is flat (no grouping). User wants: Level 1 = underlying symbol, Level 2 within each underlying = FUT/EQ rows first, then all CE rows (sorted expiry↑ strike↑), then all PE rows (sorted expiry↑ strike↑). Calendar spreads are not a separate group — they land in their CE or PE bucket based on option type. Sort respects user column-click (early-return guard, same as current `postSortGroups`).

2. **Header cleanup** — two PerformancePage header changes:
   - Remove `GridDownloadButton` from the Nav/Funds tab row (the row at lines 1311–1327 that has AlgoTabs + GridDownloadButton for nav.csv / funds.csv)
   - Revert the Summary × 2 and Breakdown × 2 section headers from `CardHeader` back to compact `.perf-grid-headrow` divs (introduced in commit `0901199b`), restoring the pre-existing compact height. Keep search (GridSearchButton) and download (GridDownloadButton) in headrow style for algo side; suppress via `{#if showGridControls}` for public page (the `showGridControls` prop already exists).

## Files to change

### `frontend/src/lib/data/pulseGridSetup.js`
Add a new export `postSortGroups2Level({ nodes, api })` alongside the existing `postSortGroups`:

```javascript
export function postSortGroups2Level({ nodes, api }) {
  // Respect user column sort — skip grouping when a sort is active.
  if (api?.getColumnState().some(col => col.sort != null)) return;
  if (!nodes || nodes.length === 0) return;

  // Level 1: group by underlying
  const byUnderlying = new Map();
  const underlyingOrder = [];
  const standalone = [];

  for (const n of nodes) {
    const d = n.data || {};
    const u = String(d.underlying || '').toUpperCase();
    if (!u) { standalone.push(n); continue; }
    if (!byUnderlying.has(u)) { byUnderlying.set(u, []); underlyingOrder.push(u); }
    byUnderlying.get(u).push(n);
  }

  // Level 2: within each underlying split into FUT/EQ, CE, PE buckets
  // then sort CE+PE by expiry asc, strike asc
  function bucketOrder(n) {
    const d = n.data || {};
    const t = String(d.opt_type || '').toUpperCase();
    if (t === 'CE') return 1;
    if (t === 'PE') return 2;
    return 0; // FUT/EQ first
  }
  function sortKey(n) {
    const d = n.data || {};
    return `${d.expiry || ''}|${String(d.strike || 0).padStart(10, '0')}`;
  }

  // Interleave groups by first-appearance order (same as postSortGroups)
  const firstIdx = new Map();
  for (const u of underlyingOrder) firstIdx.set(u, nodes.indexOf(byUnderlying.get(u)[0]));

  const seq = [];
  for (const u of underlyingOrder) seq.push({ first: firstIdx.get(u), kind: 'g', key: u });
  for (const n of standalone)      seq.push({ first: nodes.indexOf(n), kind: 's', node: n });
  seq.sort((a, b) => a.first - b.first);

  const out = [];
  for (const entry of seq) {
    if (entry.kind === 'g') {
      const rows = byUnderlying.get(entry.key).slice().sort((a, b) => {
        const bo = bucketOrder(a) - bucketOrder(b);
        if (bo !== 0) return bo;
        return sortKey(a) < sortKey(b) ? -1 : sortKey(a) > sortKey(b) ? 1 : 0;
      });
      out.push(...rows);
    } else {
      out.push(entry.node);
    }
  }
  nodes.length = 0;
  for (const n of out) nodes.push(n);
}
```

### `frontend/src/lib/PerformancePage.svelte`

**A. Add row-enrichment helper** (near the existing `_refreshPerf` function):
```javascript
import { decomposeSymbol } from '$lib/data/decomposeSymbol';

function _enrichPositionRows(rows) {
  for (const r of rows) {
    if (!r.tradingsymbol) continue;
    if (r.underlying != null) continue; // already enriched
    const d = decomposeSymbol(r.tradingsymbol);
    r.underlying = d.root || null;
    r.opt_type   = d.optType || null;   // 'CE' | 'PE' | null
    r.expiry     = d.month   || null;
    r.strike     = d.strike  || null;
  }
  return rows;
}
```
Call `_enrichPositionRows(rows)` before every `positionsAllGrid?.setGridOption('rowData', rows)` call. (Search for all rowData assignments for positionsAllEl/positionsAllGrid — there are 2–3 call sites in `_refreshPerf` + the WebSocket handler.)

**B. Wire `postSortGroups2Level` into the positions Breakdown grid** — in `makeGrid(positionsAllEl, ...)` call, pass `postSortRows: postSortGroups2Level` in the grid options (same pattern as MarketPulse's `postSortRows: postSortGroups`). Import `postSortGroups2Level` from `$lib/data/pulseGridSetup.js`.

**C. Remove `GridDownloadButton` from Nav/Funds tab row** — delete lines 1321–1326 (the `<GridDownloadButton ... />` block); leave the `<AlgoTabs>` intact. The `.funds-nav-tabs` div just becomes a tab strip with no download button.

**D. Revert Summary/Breakdown section headers from CardHeader → compact headrow** — replace the 4 `<CardHeader>` instances with `.perf-grid-headrow` divs:

```svelte
<!-- Summary (positions) -->
<div class="perf-grid-headrow">
  <h2 class="section-heading">Summary</h2>
  <span class="perf-grid-headrow-spacer"></span>
  {#if showGridControls}
    <GridDownloadButton onClick={() => positionsSummaryGrid?.exportDataAsCsv({ fileName: 'positions-summary.csv' })} label="Positions Summary" />
  {/if}
</div>

<!-- Summary (holdings) -->
<div class="perf-grid-headrow">
  <h2 class="section-heading">Summary</h2>
  <span class="perf-grid-headrow-spacer"></span>
  {#if showGridControls}
    <GridDownloadButton onClick={() => holdingsSummaryGrid?.exportDataAsCsv({ fileName: 'holdings-summary.csv' })} label="Holdings Summary" />
  {/if}
</div>

<!-- Breakdown (positions) -->
<div class="perf-grid-headrow">
  <h2 class="section-heading">Breakdown</h2>
  <span class="perf-grid-headrow-spacer"></span>
  {#if showGridControls}
    <GridSearchButton bind:filter={_filterPositions} label="Positions" />
    <GridDownloadButton onClick={() => positionsAllGrid?.exportDataAsCsv({ fileName: 'positions.csv' })} label="Positions" />
  {/if}
</div>

<!-- Breakdown (holdings) -->
<div class="perf-grid-headrow">
  <h2 class="section-heading">Breakdown</h2>
  <span class="perf-grid-headrow-spacer"></span>
  {#if showGridControls}
    <GridSearchButton bind:filter={_filterHoldings} label="Holdings" />
    <GridDownloadButton onClick={() => holdingsAllGrid?.exportDataAsCsv({ fileName: 'holdings.csv' })} label="Holdings" />
  {/if}
</div>
```

Restore the CSS (removed in 0901199b):
```css
.perf-grid-headrow {
  display: flex;
  align-items: center;
  margin-bottom: 0.25rem;
}
.perf-grid-headrow .section-heading { margin-bottom: 0; }
.perf-grid-headrow-spacer { flex: 1; }
```

Re-add `import GridSearchButton from '$lib/GridSearchButton.svelte';` (removed in 0901199b).
Remove `import CardHeader from '$lib/CardHeader.svelte';` (no longer used after this change).

Note: the `_perfRefreshing` / `refreshLoading` binds that were in CardHeader are dropped — the section headers had these for their individual refresh buttons, but the page-level refresh at the top handles this. The filter variables (`_filterPositions`, `_filterHoldings`) still work via GridSearchButton's `bind:filter`.

## Agents
- frontend: Implement all four changes in `frontend/src/lib/PerformancePage.svelte` and `frontend/src/lib/data/pulseGridSetup.js` as described above. Read both files first before editing. Be precise about removing only the CardHeader and GridDownloadButton from funds-nav-tabs; leave all other structure intact.
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
fix(ui): positions 2-level grouping (underlying→CE/PE) + revert section header height + drop nav/funds download btn

## Done when
- Positions Breakdown grid groups rows: underlying → FUT/EQ first, CE bucket (expiry↑ strike↑), PE bucket (expiry↑ strike↑)
- User column-click sort overrides grouping (early-return guard)
- Nav/Funds tab row has no download button
- Summary + Breakdown section headers are compact headrow divs (not CardHeader) — same height as before 0901199b
- svelte-check: 0 errors
